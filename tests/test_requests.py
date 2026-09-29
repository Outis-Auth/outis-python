import threading

import pytest

from outis import (
    APIConnectionError,
    APIError,
    IdempotencyConflictError,
    NotAuthorizedError,
    NotFoundError,
    OperationMismatchError,
    Outis,
    TimeoutTooLongError,
    WaitCancelledError,
    WaitTimeoutError,
    operation_hash,
)

PARAMS = {"repo": "acme/payments-api", "env": "production", "sha": "8d93f71"}


def test_create_sends_the_proposal(client, fake):
    req = client.requests.create(action="deploy.production", requester="keith", params=PARAMS, summary="Ship it")
    call = fake.log[-1]
    assert call["method"] == "POST" and call["path"] == "/v1/requests"
    assert call["headers"]["authorization"] == "Bearer ok_test_123"
    assert "idempotency-key" not in call["headers"]
    assert call["body"] == {"action": "deploy.production", "requester": "keith", "params": PARAMS, "summary": "Ship it"}
    assert req.is_pending and not req.is_authorized and not req.replayed
    assert req.operation_hash == operation_hash("deploy.production", PARAMS)


def test_create_with_idempotency_key_replays(client, fake):
    first = client.requests.create(action="deploy.production", requester="keith", params=PARAMS, idempotency_key="wf-1")
    again = client.requests.create(action="deploy.production", requester="keith", params=PARAMS, idempotency_key="wf-1")
    assert fake.log[0]["headers"]["idempotency-key"] == "wf-1"
    assert again.id == first.id
    assert again.replayed and not first.replayed


def test_create_same_key_different_operation_conflicts(client):
    client.requests.create(action="deploy.production", requester="keith", params=PARAMS, idempotency_key="wf-1")
    with pytest.raises(IdempotencyConflictError) as err:
        client.requests.create(action="deploy.production", requester="keith", params={"env": "staging"},
                               idempotency_key="wf-1")
    assert err.value.status == 409 and err.value.code == "idempotency_conflict"


def test_create_rejects_bad_idempotency_key(client, fake):
    with pytest.raises(ValueError):
        client.requests.create(action="a", requester="k", idempotency_key="café")
    assert fake.log == []


def test_create_is_retried_only_with_an_idempotency_key(client, fake, clock):
    fake.script = [(503, {"error": "busy"}, {})]
    with pytest.raises(APIError) as err:
        client.requests.create(action="a", requester="k")
    assert err.value.status == 503 and len(fake.log) == 1

    fake.script = [(503, {"error": "busy"}, {})]
    req = client.requests.create(action="a", requester="k", idempotency_key="k-1")
    assert req.id and len(fake.log) == 3


def test_retrieve(client, fake):
    fake.add("req-1", "deploy.production", PARAMS, outcome="authorized")
    req = client.requests.retrieve("req-1")
    assert req.is_authorized and req.approvers == ("maya", "sam") and req.params == PARAMS
    assert req.decided_at == 1788350400000


def test_retrieve_retries_429_and_5xx(client, fake, clock):
    fake.add("req-1", "a", {})
    fake.script = [(429, {"error": "slow down"}, {"Retry-After": "3"}), (502, {"error": "bad gateway"}, {})]
    assert client.requests.retrieve("req-1").id == "req-1"
    assert clock.sleeps == [3.0, 0.75]


def test_retrieve_not_found(client):
    with pytest.raises(NotFoundError) as err:
        client.requests.retrieve("req-missing")
    assert err.value.code == "not_found"


def test_connection_error():
    c = Outis(api_key="k", base_url="http://127.0.0.1:1", max_retries=0)
    with pytest.raises(APIConnectionError):
        c.requests.retrieve("req-1")


def test_wait_for_resolves_with_backoff(client, fake, clock):
    fake.add("req-1", "a", {})
    fake.decide["req-1"] = (7, "denied")
    req = client.requests.wait_for("req-1", timeout="5m")
    assert req.outcome == "denied" and not req.live
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_wait_for_times_out_with_the_request_id(client, fake, clock):
    fake.add("req-1", "a", {})
    with pytest.raises(WaitTimeoutError) as err:
        client.requests.wait_for("req-1", timeout="30s")
    assert err.value.request_id == "req-1" and err.value.request.is_pending
    assert sum(clock.sleeps) == pytest.approx(30.0)


def test_wait_for_needs_a_bounded_timeout(client, fake):
    with pytest.raises(TypeError):
        client.requests.wait_for("req-1")  # type: ignore[call-arg]
    with pytest.raises(TimeoutTooLongError, match="durable|webhook"):
        client.requests.wait_for("req-1", timeout="31m")
    with pytest.raises(ValueError):
        client.requests.wait_for("req-1", timeout=0)
    assert fake.log == []


def test_wait_for_cancel(client, fake):
    fake.add("req-1", "a", {})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WaitCancelledError):
        client.requests.wait_for("req-1", timeout="1m", cancel=cancel)


def test_assert_authorized(client, fake):
    fake.add("req-1", "deploy.production", PARAMS, outcome="authorized")
    assert client.requests.assert_authorized("req-1", action="deploy.production", params=PARAMS).id == "req-1"


def test_assert_authorized_hash_mismatch(client, fake):
    fake.add("req-1", "deploy.production", PARAMS, outcome="authorized")
    with pytest.raises(OperationMismatchError) as err:
        client.requests.assert_authorized("req-1", action="deploy.production", params={**PARAMS, "sha": "ffffff"})
    assert err.value.actual_hash == operation_hash("deploy.production", PARAMS)


def test_assert_authorized_not_authorized(client, fake):
    fake.add("req-1", "deploy.production", PARAMS, outcome="denied")
    with pytest.raises(NotAuthorizedError):
        client.requests.assert_authorized("req-1", action="deploy.production", params=PARAMS)
    fake.add("req-2", "deploy.production", PARAMS)
    with pytest.raises(NotAuthorizedError):
        client.requests.assert_authorized("req-2", action="deploy.production", params=PARAMS)


def test_api_key_from_environment(monkeypatch):
    monkeypatch.setenv("OUTIS_API_KEY", "ok_env")
    monkeypatch.delenv("OUTIS_BASE_URL", raising=False)
    assert Outis().base_url == "https://api.outis.tech"
    monkeypatch.delenv("OUTIS_API_KEY")
    with pytest.raises(ValueError):
        Outis()


def test_error_code_reads_kind_then_code(client, fake):
    fake.script = [(409, {"error": "x", "kind": "idempotency_conflict", "code": "other"}, {})]
    with pytest.raises(IdempotencyConflictError) as err:
        client.requests.create(action="a", requester="k")
    assert err.value.code == "idempotency_conflict"
    fake.script = [(400, {"error": "x", "code": "legacy"}, {})]
    with pytest.raises(APIError) as err:
        client.requests.create(action="a", requester="k")
    assert err.value.code == "legacy"


def test_read_body_carries_intent_and_execution(client, fake):
    req = fake.add("req-9", "a", {})
    req["execution"] = {"state": "claimed", "claimed_at": 1, "lease_expires_at": 2, "execute_by": 3}
    got = client.requests.retrieve("req-9")
    assert got.execution.state == "claimed" and got.execution.execute_by == 3 and got.intent is None
