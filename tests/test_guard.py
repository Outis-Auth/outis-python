import asyncio
import threading
import time
from concurrent.futures import Future

import pytest

from outis import (
    Deferred,
    NotAuthorizedError,
    Outis,
    TimeoutTooLongError,
    WaitTimeoutError,
    generate_intent_key,
    operation_hash,
)

KEY = generate_intent_key()
SHOWN = {"db": "payments", "snapshot": "2026-09-26"}


def fast(fake, **kwargs):
    """A client on the real clock whose polls sleep 10ms, for tests that need real threads."""
    return Outis(api_key="ok_test_123", base_url=fake.base_url, sleep=lambda s: time.sleep(0.01), **kwargs)


def guard_threads():
    return [t for t in threading.enumerate() if t.name == "outis-guard"]


def settle_threads():
    deadline = time.monotonic() + 5
    while guard_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert guard_threads() == []


def test_exactly_one_path(client, fake):
    with pytest.raises(ValueError, match="exactly one"):
        client.guard(action="db.restore", requester="ops")
    with pytest.raises(ValueError, match="exactly one"):
        client.guard(action="db.restore", requester="ops", wait="5m",  # type: ignore[call-overload]
                     defer_to={"worker": "db", "call": "restore"})
    assert fake.log == []


def test_options_are_checked_before_anything_is_sent(client, fake):
    with pytest.raises(ValueError, match="requester"):
        client.guard(action="db.restore", wait="5m")
    with pytest.raises(TypeError):
        client.guard(action="db.restore", requester="ops", show_approvers={"size": 3}, wait="5m")  # type: ignore[dict-item]
    with pytest.raises(TimeoutTooLongError):
        client.guard(action="db.restore", requester="ops", wait="31m")
    with pytest.raises(ValueError, match="OUTIS_INTENT_KEY"):
        client.guard(action="db.restore", requester="ops", defer_to={"worker": "db", "call": "restore"})
    assert fake.log == []


def test_approved(client, fake):
    fake.next_outcome = (2, "authorized")
    future = client.guard(action="db.restore", requester="ops", show_approvers=SHOWN, summary="Restore", wait="5m")
    assert isinstance(future, Future)
    req = future.result(timeout=5)
    assert req.is_authorized and req.operation_hash == operation_hash("db.restore", SHOWN)
    assert fake.log[0]["body"] == {"action": "db.restore", "requester": "ops", "params": SHOWN, "summary": "Restore"}


def test_default_requester(fake, clock):
    c = Outis(api_key="k", base_url=fake.base_url, requester="payouts-api", clock=clock, sleep=clock.sleep)
    fake.next_outcome = (1, "authorized")
    c.guard(action="a", wait="1m").result(timeout=5)
    assert fake.log[0]["body"]["requester"] == "payouts-api"


@pytest.mark.parametrize("outcome", ["denied", "expired", "aborted"])
def test_each_rejection(client, fake, outcome):
    fake.next_outcome = (2, outcome)
    future = client.guard(action="db.restore", requester="ops", wait="5m")
    with pytest.raises(NotAuthorizedError) as err:
        future.result(timeout=5)
    assert err.value.outcome == outcome and err.value.request.id == "req-0001"


def test_timeout_names_the_request(client):
    future = client.guard(action="db.restore", requester="ops", wait="10s")
    err = future.exception(timeout=5)
    assert isinstance(err, WaitTimeoutError) and err.request_id == "req-0001"


def test_returns_at_once_and_two_guards_progress_concurrently(fake):
    c = fast(fake)
    fake.next_outcome = (2, "authorized")
    fake.holds["first"] = threading.Event()
    began = time.monotonic()
    first = c.guard(action="first", requester="ops", wait="5m")
    assert time.monotonic() - began < 0.5 and not first.done()
    second = c.guard(action="second", requester="ops", wait="5m")
    assert second.result(timeout=5).action == "second"
    assert not first.done()
    fake.holds["first"].set()
    assert first.result(timeout=5).action == "first"


def test_add_done_callback_runs_when_decided(fake):
    c = fast(fake)
    fake.next_outcome = (1, "authorized")
    seen = threading.Event()
    c.guard(action="a", requester="ops", wait="1m").add_done_callback(lambda f: seen.set())
    assert seen.wait(5)


def test_cancel_stops_polling_and_leaves_the_request_live(fake):
    c = fast(fake)
    fake.holds["a"] = threading.Event()
    future = c.guard(action="a", requester="ops", wait="5m")
    assert future.cancel()
    fake.holds["a"].set()
    settle_threads()
    assert fake.polls == {} and fake.requests["req-0001"]["live"]
    assert [e["method"] for e in fake.log] == ["POST"]


def test_guard_async(fake, clock):
    c = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY, clock=clock, sleep=clock.sleep)
    fake.next_outcome = (2, "authorized")

    async def main():
        ok = await c.guard_async(action="a", requester="ops", wait="1m")
        with pytest.raises(NotAuthorizedError):
            fake.next_outcome = (1, "denied")
            await c.guard_async(action="b", requester="ops", wait="1m")
        deferred = await c.guard_async(action="c", requester="ops", defer_to={"worker": "db", "call": "restore"})
        return ok, deferred

    ok, deferred = asyncio.run(main())
    assert ok.is_authorized and isinstance(deferred, Deferred) and guard_threads() == []


def test_guard_async_times_out(client):
    with pytest.raises(WaitTimeoutError):
        asyncio.run(client.guard_async(action="a", requester="ops", wait="10s"))


class Payouts:
    def __init__(self):
        self.calls = []

    def send(self, destination, amount, *, note=None):
        self.calls.append((destination, amount, note))
        return {"id": f"po_{len(self.calls)}"}


def test_defer_to_seals_the_call(fake):
    c = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    deferred = c.guard(
        action="payouts.send",
        requester="ops",
        show_approvers={"to": "acct_9f2", "amount": "2500"},
        idempotency_key="payout-1",
        defer_to={"worker": "payouts", "call": "send", "args": ["acct_9f2", 2500], "kwargs": {"note": "sept"},
                  "execute_within": "2d"},
    )
    assert isinstance(deferred, Deferred) and deferred.id == "req-0001" and deferred.request.is_pending
    sent = fake.log[0]
    params = {"to": "acct_9f2", "amount": "2500", "intent": deferred.intent_digest}
    assert sent["body"]["params"] == params and sent["body"]["intent"]["alg"] == "A256GCM"
    assert sent["body"]["execute_within"] == 2 * 86400 and sent["headers"]["idempotency-key"] == "payout-1"
    assert deferred.intent.operation_hash == operation_hash("payouts.send", params)
    fake.authorize(deferred.id)
    payouts = Payouts()
    assert c.worker(clients={"payouts": payouts}).execute(deferred.id).status == "succeeded"
    assert payouts.calls == [("acct_9f2", 2500, "sept")]


def test_defer_to_checks_its_fields(fake):
    c = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    with pytest.raises(ValueError, match="call"):
        c.guard(action="a", requester="ops", defer_to={"worker": "payouts"})  # type: ignore[typeddict-item]
    with pytest.raises(ValueError, match="reserved"):
        c.guard(action="a", requester="ops", show_approvers={"intent": "x"},
                defer_to={"worker": "payouts", "call": "send"})
    assert fake.log == []


class Account:
    def __init__(self):
        self.name = "acme"
        self.transfers = []

    def transfer(self, amount, destination):
        self.transfers.append((amount, destination))
        return {"id": "tr_1", "from": self.name}

    async def transfer_async(self, amount, destination):
        return self.transfer(amount, destination)


def rules():
    return {
        "action": "bank.transfer",
        "requester": lambda amount, destination: f"payouts-{destination}",
        "show_approvers": lambda amount, destination: {"amount": str(amount), "to": destination},
        "summary": lambda amount, destination: f"Pay {destination}",
    }


def test_guard_method_wait_runs_the_method_on_its_object(client, fake):
    fake.next_outcome = (1, "authorized")
    account = Account()
    transfer = client.guard_method(account, "transfer", wait="5m", **rules())
    assert transfer(2500, "acct_9f2") == {"id": "tr_1", "from": "acme"}
    assert account.transfers == [(2500, "acct_9f2")]
    body = fake.log[0]["body"]
    assert body["requester"] == "payouts-acct_9f2" and body["params"] == {"amount": "2500", "to": "acct_9f2"}
    assert body["summary"] == "Pay acct_9f2"


def test_guard_method_never_runs_a_denied_call(client, fake):
    fake.next_outcome = (1, "denied")
    account = Account()
    with pytest.raises(NotAuthorizedError):
        client.guard_method(account, "transfer", wait="5m", **rules())(2500, "acct_9f2")
    assert account.transfers == []


def test_guard_method_defer_to_returns_deferred(fake):
    c = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    account = Account()
    transfer = c.guard_method(account, "transfer", defer_to={"worker": "bank", "call": "transfer"}, **rules())
    deferred = transfer(2500, "acct_9f2")
    assert isinstance(deferred, Deferred) and account.transfers == []
    fake.authorize(deferred.id)
    real = Account()
    assert c.worker(clients={"bank": real}).execute(deferred.id).reference == "tr_1"
    assert real.transfers == [(2500, "acct_9f2")]


def test_guard_method_checks_options_up_front(client, fake):
    keyed = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    with pytest.raises(ValueError, match="exactly one"):
        client.guard_method(Account(), "transfer", **rules())
    with pytest.raises(ValueError, match="own arguments"):
        keyed.guard_method(Account(), "transfer", defer_to={"worker": "bank", "call": "transfer", "args": [1]},
                           **rules())
    with pytest.raises(TypeError):
        client.guard_method(Account(), "name", wait="1m", **rules())
    assert fake.log == []


def test_guard_method_async(client, fake):
    fake.next_outcome = (1, "authorized")
    account = Account()
    transfer = client.guard_method_async(account, "transfer_async", wait="5m", **rules())
    assert asyncio.run(transfer(10, "acct_1")) == {"id": "tr_1", "from": "acme"}
    assert account.transfers == [(10, "acct_1")]
