import asyncio
import hashlib
import hmac
import io
import json
import threading
import time

import pytest

from outis import APIError, Outis, WorkerClient, generate_intent_key

KEY = generate_intent_key()
SECRET = "whsec_test"


class Payouts:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send(self, destination, amount, *, idempotency_key=None, note=None):
        self.calls.append((destination, amount, idempotency_key, note))
        if self.fail:
            raise RuntimeError("card declined")
        return {"id": f"po_{len(self.calls)}"}


@pytest.fixture
def outis(fake, monkeypatch):
    monkeypatch.delenv("OUTIS_INTENT_KEYS", raising=False)
    return Outis(api_key="ok_test_123", base_url=fake.base_url, intent_key=KEY)


def propose(outis, fake, *, authorize=True, method="send", args=("acct_9f2", 2500), kwargs=None, client="payouts"):
    pending = outis.intents.propose(action="payouts.send", requester="keith", client=client, method=method,
                                    args=args, kwargs=kwargs, params={"to": "acct_9f2"})
    if authorize:
        fake.authorize(pending.request_id)
    return pending.request_id


def test_execute_claims_runs_and_reports(outis, fake):
    rid = propose(outis, fake, kwargs={"note": "sept"})
    payouts, seen = Payouts(), []
    worker = outis.worker(clients={"payouts": WorkerClient(payouts, idempotency="stripe")}, on_result=seen.append)
    result = worker.execute(rid)
    assert result.status == "succeeded" and result.reference == "po_1" and result.reported
    assert payouts.calls == [("acct_9f2", 2500, rid, "sept")]
    assert fake.reports == [(rid, {"claim_id": "clm_1", "status": "succeeded", "reference": "po_1"})]
    assert seen == [result] and result.context.idempotency_key == rid


def test_execute_never_runs_a_claim_twice(outis, fake):
    rid = propose(outis, fake)
    payouts = Payouts()
    worker = outis.worker(clients={"payouts": payouts})
    worker.execute(rid)
    again = worker.execute(rid)
    assert again.status == "skipped" and again.reason == "already_reported"
    assert len(payouts.calls) == 1


def test_execute_skips_what_isnt_authorized(outis, fake):
    rid = propose(outis, fake, authorize=False)
    result = outis.worker(clients={"payouts": Payouts()}).execute(rid)
    assert result.status == "skipped" and result.reason == "not_authorized" and fake.reports == []


def test_a_failing_call_is_reported_failed(outis, fake):
    rid = propose(outis, fake)
    result = outis.worker(clients={"payouts": Payouts(fail=True)}).execute(rid)
    assert result.status == "failed" and result.reason == "execution_error"
    assert fake.reports[0][1]["error"] == "execution_error: card declined"


@pytest.mark.parametrize(
    "tamper, reason",
    [
        (lambda req: req["params"].update(intent="sha256:" + "0" * 64), "digest_mismatch"),
        (lambda req: req.update(operation_hash="sha256:" + "1" * 64), "operation_mismatch"),
        (lambda req: req.update(action="payouts.other"), "decrypt_failed"),
        (lambda req: req["intent"].update(kid="0000000000000000"), "unknown_key"),
        (lambda req: req.update(intent=None), "no_intent"),
    ],
)
def test_verification_failures_report_and_never_run(outis, fake, tamper, reason):
    rid = propose(outis, fake)
    tamper(fake.requests[rid])
    payouts = Payouts()
    result = outis.worker(clients={"payouts": payouts}).execute(rid)
    assert result.status == "failed" and result.reason == reason and payouts.calls == []
    assert fake.reports[0][1]["status"] == "failed" and fake.reports[0][1]["error"].startswith(reason)


def test_unregistered_clients_disallowed_and_private_methods_are_refused(outis, fake):
    cases = [
        (propose(outis, fake, client="stripe"), "client_not_registered", None),
        (propose(outis, fake, method="refund"), "not_allowed", ["payouts.send"]),
        (propose(outis, fake, method="_secret"), "client_not_registered", None),
        (propose(outis, fake, method="nope"), "client_not_registered", None),
        (propose(outis, fake, args=["acct_9f2"]), "bad_args", None),
        (propose(outis, fake, kwargs={"memo": "x"}), "bad_args", None),
    ]
    for rid, reason, allow in cases:
        payouts = Payouts()
        result = outis.worker(clients={"payouts": payouts}, allow=allow).execute(rid)
        assert (result.status, result.reason) == ("failed", reason) and payouts.calls == []
    assert all(body["error"].startswith(body["error"].split(":")[0] + ": ") for _, body in fake.reports)


def test_handlers_get_the_context_then_the_args(outis, fake):
    rid = propose(outis, fake, client="db", method="restore", args=["payments"], kwargs={"snapshot": "s1"})
    seen = []

    def restore(ctx, db, *, snapshot):
        seen.append((ctx, db, snapshot))
        return {"id": "rst_1"}

    worker = outis.worker(handlers={"db.restore": restore})
    assert worker.execute(rid).reference == "rst_1"
    ctx, db, snapshot = seen[0]
    assert (db, snapshot) == ("payments", "s1")
    assert ctx.idempotency_key == rid and ctx.request.is_authorized and ctx.method == "restore"


def test_a_handler_that_cant_take_the_args_fails_as_bad_args(outis, fake):
    rid = propose(outis, fake, client="db", method="restore", args=["payments", "extra"])
    result = outis.worker(handlers={"db.restore": lambda ctx, db: None}).execute(rid)
    assert (result.status, result.reason) == ("failed", "bad_args")


def test_claim_errors_other_than_conflicts_raise(outis, fake):
    rid = propose(outis, fake)
    fake.script = [(500, {"error": "boom", "kind": "internal"}, {})]
    with pytest.raises(APIError):
        outis.worker(clients={"payouts": Payouts()}).execute(rid)


def test_run_polls_until_stopped(outis, fake):
    rids = [propose(outis, fake) for _ in range(5)]
    payouts, stop = Payouts(), threading.Event()
    done = []

    def on_result(r):
        done.append(r.request_id)
        if len(done) == len(rids):
            stop.set()

    worker = outis.worker(clients={"payouts": payouts}, concurrency=2, on_result=on_result)
    thread = threading.Thread(target=worker.run, kwargs={"every": "10ms", "stop": stop})
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert sorted(done) == sorted(rids) and len(payouts.calls) == 5


def test_poll_runs_one_pass_and_returns_the_results(outis, fake):
    rids = [propose(outis, fake) for _ in range(3)]
    payouts = Payouts()
    results = outis.worker(clients={"payouts": payouts}, concurrency=2).poll()
    assert sorted(r.request_id for r in results) == sorted(rids)
    assert all(r.status == "succeeded" for r in results) and len(payouts.calls) == 3
    assert outis.worker(clients={"payouts": payouts}).poll() == []


def test_worker_needs_keys_and_something_to_run(fake, monkeypatch):
    monkeypatch.delenv("OUTIS_INTENT_KEYS", raising=False)
    monkeypatch.delenv("OUTIS_INTENT_KEY", raising=False)
    with pytest.raises(ValueError, match="intent keys"):
        Outis(api_key="k").worker(clients={"p": Payouts()})
    with pytest.raises(ValueError, match="register"):
        Outis(api_key="k", intent_key=KEY).worker()


def event_for(fake, rid, event_type="request.authorized"):
    body = json.dumps({"id": "evt_1", "type": event_type, "created_at": "2026-09-27T12:00:00Z", "org": "org_1",
                       "data": {"request": fake.requests[rid]}}).encode()
    t = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Outis-Signature": f"t={t},v1={sig}"}


def test_handle_webhook(outis, fake):
    rid = propose(outis, fake)
    payouts = Payouts()
    worker = outis.worker(clients={"payouts": payouts})
    body, headers = event_for(fake, rid)
    assert worker.handle_webhook(body, headers, SECRET) == (200, {"status": "succeeded", "reason": None})
    assert worker.handle_webhook(body + b" ", headers, SECRET)[0] == 400
    body, headers = event_for(fake, rid, "request.denied")
    assert worker.handle_webhook(body, headers, SECRET) == (200, {"status": "ignored"})
    assert len(payouts.calls) == 1


def test_wsgi_app(outis, fake):
    rid = propose(outis, fake)
    worker = outis.worker(clients={"payouts": Payouts()})
    body, headers = event_for(fake, rid)
    environ = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
               "HTTP_OUTIS_SIGNATURE": headers["Outis-Signature"]}
    started = []
    out = b"".join(worker.wsgi_app(SECRET)(environ, lambda status, hdrs: started.append(status)))
    assert started == ["200 OK"] and json.loads(out)["status"] == "succeeded"
    started.clear()
    worker.wsgi_app(SECRET)({"REQUEST_METHOD": "GET"}, lambda status, hdrs: started.append(status))
    assert started == ["405 Method Not Allowed"]


def test_asgi_app(outis, fake):
    rid = propose(outis, fake)
    worker = outis.worker(clients={"payouts": Payouts()})
    body, headers = event_for(fake, rid)
    scope = {"type": "http", "method": "POST",
             "headers": [(b"outis-signature", headers["Outis-Signature"].encode())]}
    chunks = [{"type": "http.request", "body": body[:10], "more_body": True},
              {"type": "http.request", "body": body[10:]}]
    sent = []

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(worker.asgi_app(SECRET)(scope, receive, send))
    assert sent[0]["status"] == 200 and json.loads(sent[1]["body"])["status"] == "succeeded"


def test_start_drains_on_sigterm(outis, fake):
    import os
    import signal

    rid = propose(outis, fake)
    payouts = Payouts()
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    outis.worker(clients={"payouts": payouts}).start(every=0.05)
    timer.join()
    assert [c[0] for c in payouts.calls] == ["acct_9f2"] and fake.reports[0][0] == rid
    assert signal.getsignal(signal.SIGTERM) is before


class StripeServices:
    """Shaped like StripeClient.v1: services whose methods take params and options."""

    class Transfers:
        def __init__(self):
            self.calls = []

        def create(self, params, options=None):
            self.calls.append((params, options))
            return {"id": "tr_1"}

    def __init__(self):
        self.transfers = self.Transfers()


def test_stripe_idempotency_goes_in_options_for_stripe_client_services(outis, fake):
    deferred = outis.guard(action="stripe.transfers.create", requester="keith", show_approvers={"to": "acct_9f2"},
                           defer_to={"worker": "stripe", "call": "transfers.create", "args": [{"amount": 1}]})
    fake.authorize(deferred.id)
    v1 = StripeServices()
    assert outis.worker(clients={"stripe": WorkerClient(v1, idempotency="stripe")}).execute(deferred.id).reference == "tr_1"
    assert v1.transfers.calls == [({"amount": 1}, {"idempotency_key": deferred.id})]
