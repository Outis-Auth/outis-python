import pytest

from outis import (
    Done,
    Intent,
    NotAuthorizedError,
    Outis,
    Pending,
    TimeoutTooLongError,
    generate_intent_key,
    operation_hash,
)


class Transfers:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def create(self, amount, destination, *, currency="usd"):
        self.calls.append((amount, destination, currency))
        return {"id": "tr_1", "owner": self.owner.name}


class Stripe:
    name = "acme"

    def __init__(self):
        self.transfers = Transfers(self)

    def balance(self):
        return 42


RULES = {
    "transfers.create": {
        "action": "stripe.transfer",
        "params": lambda amount, destination, **kw: {"amount": str(amount), "destination": destination},
        "requester": "payouts-worker",
        "summary": lambda amount, destination, **kw: f"Pay {destination} {amount}",
        "when": lambda amount, destination, **kw: amount >= 10_000,
    }
}


def test_wait_mode_runs_after_authorization(client, fake):
    fake.next_outcome = (1, "authorized")
    stripe = Stripe()
    wrapped = client.wrap(stripe, RULES, mode="wait", timeout="5m")
    assert wrapped.transfers.create(50_000, "acct_9f2", currency="eur") == {"id": "tr_1", "owner": "acme"}
    assert stripe.transfers.calls == [(50_000, "acct_9f2", "eur")]
    body = fake.log[0]["body"]
    assert body["action"] == "stripe.transfer" and body["requester"] == "payouts-worker"
    assert body["params"] == {"amount": "50000", "destination": "acct_9f2"}
    assert body["summary"] == "Pay acct_9f2 50000"


def test_wait_mode_never_runs_a_denied_call(client, fake):
    fake.next_outcome = (1, "denied")
    stripe = Stripe()
    wrapped = client.wrap(stripe, RULES, mode="wait", timeout="5m")
    with pytest.raises(NotAuthorizedError):
        wrapped.transfers.create(50_000, "acct_9f2")
    assert stripe.transfers.calls == []


def test_when_false_skips_outis_and_unlisted_methods_pass_through(client, fake):
    stripe = Stripe()
    wrapped = client.wrap(stripe, RULES, mode="wait", timeout="5m")
    wrapped.transfers.create(10, "acct_small")
    assert wrapped.balance() == 42 and wrapped.name == "acme"
    assert stripe.transfers.calls == [(10, "acct_small", "usd")] and fake.log == []


def test_wait_mode_requires_a_capped_timeout(client):
    with pytest.raises(ValueError):
        client.wrap(Stripe(), RULES, mode="wait")
    with pytest.raises(TimeoutTooLongError):
        client.wrap(Stripe(), RULES, mode="wait", timeout="1h")


KEY = generate_intent_key()


def test_durable_mode_returns_pending_and_never_calls(fake, clock):
    client = Outis(api_key="ok_test_123", base_url=fake.base_url, intent_key=KEY, clock=clock, sleep=clock.sleep)
    stripe = Stripe()
    rules = {"transfers.create": {**RULES["transfers.create"],
                                  "idempotency_key": lambda amount, destination, **kw: f"payout-{destination}"}}
    wrapped = client.wrap(stripe, rules, mode="durable", client="stripe", execute_within="3d")
    pending = wrapped.transfers.create(50_000, "acct_9f2", currency="eur")
    assert isinstance(pending, Pending) and pending.status == "pending"
    assert stripe.transfers.calls == []
    assert pending.request_id == "req-0001" and pending.request.is_pending
    sent = fake.log[0]
    assert sent["headers"]["idempotency-key"] == "payout-acct_9f2"
    assert sent["body"]["execute_within"] == 3 * 86400
    assert sent["body"]["intent"]["alg"] == "A256GCM"
    params = {"amount": "50000", "destination": "acct_9f2", "intent": pending.intent_digest}
    assert sent["body"]["params"] == params
    assert pending.intent == Intent("req-0001", "stripe.transfer", params, operation_hash("stripe.transfer", params),
                                    "stripe.transfers.create")
    assert Intent.from_dict(pending.intent.to_dict()) == pending.intent


def test_propose_is_an_alias_for_durable(fake):
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    assert isinstance(client.wrap(Stripe(), RULES, mode="propose", client="stripe").transfers.create(50_000, "a"),
                      Pending)


def test_durable_then_a_worker_runs_it(fake):
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    pending = client.wrap(Stripe(), RULES, mode="durable", client="stripe").transfers.create(50_000, "acct_9f2",
                                                                                             currency="eur")
    fake.authorize(pending.request_id)
    real = Stripe()
    result = client.worker(clients={"stripe": real}).execute(pending.request_id)
    assert result.status == "succeeded" and result.reference == "tr_1"
    assert real.transfers.calls == [(50_000, "acct_9f2", "eur")]


def test_hybrid_runs_here_when_authorized_in_time(fake, clock):
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY, clock=clock, sleep=clock.sleep,
                   rng=lambda: 0.5)
    fake.next_outcome = (2, "authorized")
    stripe = Stripe()
    done = client.wrap(stripe, RULES, mode="hybrid", client="stripe", wait="45s").transfers.create(50_000, "a")
    assert isinstance(done, Done) and done.status == "done" and done.result["id"] == "tr_1"
    assert stripe.transfers.calls == [(50_000, "a", "usd")]
    assert fake.reports == [("req-0001", {"claim_id": "clm_1", "status": "succeeded", "reference": "tr_1"})]


def test_hybrid_falls_back_to_pending(fake, clock):
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY, clock=clock, sleep=clock.sleep,
                   rng=lambda: 0.5)
    stripe = Stripe()
    out = client.wrap(stripe, RULES, mode="hybrid", client="stripe", wait="45s").transfers.create(50_000, "a")
    assert isinstance(out, Pending) and stripe.transfers.calls == [] and fake.claims == {}


def test_hybrid_raises_on_denial(fake, clock):
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY, clock=clock, sleep=clock.sleep)
    fake.next_outcome = (1, "denied")
    with pytest.raises(NotAuthorizedError):
        client.wrap(Stripe(), RULES, mode="hybrid", client="stripe", wait="45s").transfers.create(50_000, "a")


def test_hybrid_leaves_it_to_a_worker_that_claimed_first(fake, clock):
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY, clock=clock, sleep=clock.sleep)
    fake.next_outcome = (1, "authorized")
    fake.claims["req-0001"] = "clm_worker"
    stripe = Stripe()
    out = client.wrap(stripe, RULES, mode="hybrid", client="stripe", wait="45s").transfers.create(50_000, "a")
    assert isinstance(out, Pending) and stripe.transfers.calls == []


def test_durable_modes_validate_options(fake):
    keyed = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    with pytest.raises(ValueError, match="client"):
        keyed.wrap(Stripe(), RULES, mode="durable")
    with pytest.raises(ValueError, match="timeout"):
        keyed.wrap(Stripe(), RULES, mode="durable", client="stripe", timeout="5m")
    with pytest.raises(ValueError):
        keyed.wrap(Stripe(), RULES, mode="hybrid", client="stripe")
    with pytest.raises(TimeoutTooLongError):
        keyed.wrap(Stripe(), RULES, mode="hybrid", client="stripe", wait="2h")
    with pytest.raises(ValueError, match="30 days"):
        keyed.wrap(Stripe(), RULES, mode="durable", client="stripe", execute_within="31d")
    with pytest.raises(ValueError, match="intent_key"):
        Outis(api_key="k", intent_key=[]).wrap(Stripe(), RULES, mode="durable", client="stripe")


def test_wrap_validates_rules(client):
    with pytest.raises(ValueError):
        client.wrap(Stripe(), {"transfers.create": {"action": "a", "requester": "k"}}, mode="wait", timeout="1m")
    with pytest.raises(AttributeError):
        client.wrap(Stripe(), {"payouts.create": RULES["transfers.create"]}, mode="wait", timeout="1m")
    with pytest.raises(ValueError):
        client.wrap(Stripe(), RULES, mode="later")
