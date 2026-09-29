import json
from pathlib import Path

import pytest

from outis import Outis, WorkerClient, generate_intent_key
from outis.recipes import stripe

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "stripe-recipes.json").read_text())
VECTORS = json.loads((Path(__file__).parent.parent / "recipe-vectors.json").read_text(encoding="utf-8"))


def recipe(name):
    group, method = name.split(".")
    return getattr(getattr(stripe, group), method)()


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["recipe"])
def test_recipes_show_only_the_vetted_fields(case):
    r = recipe(case["recipe"])
    assert r["action"] == case["action"]
    assert r["show_approvers"](*case["args"], **case["kwargs"]) == case["shown"]


@pytest.mark.parametrize("case", FIXTURES["errors"], ids=lambda c: c["recipe"])
def test_recipes_refuse_what_they_cant_show_exactly(case):
    with pytest.raises({"TypeError": TypeError, "ValueError": ValueError}[case["error"]]):
        recipe(case["recipe"])["show_approvers"](*case["args"])


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_recipe_vectors_match_every_sdk(case):
    r = recipe(case["action"].removeprefix("stripe."))
    assert r["action"] == case["action"]
    if case["action"] == "stripe.customers.delete":
        calls = [((case["customer"], None, case["options"]), {}), ((case["customer"],), case["options"])]
    else:
        calls = [
            ((case["params"], case["options"]), {}),
            ((), {"params": case["params"], "options": case["options"]}),
            ((), {**case["params"], **case["options"]}),
        ]
    for args, kwargs in calls:
        if "error" in case:
            method = case["action"].removeprefix("stripe.")
            with pytest.raises(ValueError, match=f"^stripe {method} needs {case['error']}$"):
                r["show_approvers"](*args, **kwargs)
        else:
            assert r["show_approvers"](*args, **kwargs) == case["shown"]
    assert VECTORS["description_max"] == stripe.DESCRIPTION_CAP


class TransferService:
    """Shaped like stripe-python's StripeClient transfers service."""

    def __init__(self):
        self.calls = []

    def create(self, params, options=None):
        self.calls.append((params, options))
        return {"id": "tr_1"}


def test_a_recipe_drives_guard_method(client, fake):
    fake.next_outcome = (1, "authorized")
    service = TransferService()
    create = client.guard_method(service, "create", **stripe.transfers.create(), requester="payouts-api", wait="5m")
    params = {"amount": 2500000, "currency": "usd", "destination": "acct_9f2"}
    assert create(params) == {"id": "tr_1"} and service.calls == [(params, None)]
    body = fake.log[0]["body"]
    assert body["action"] == "stripe.transfers.create"
    assert body["params"] == {"amount": "2500000", "currency": "usd", "destination": "acct_9f2"}


def test_recipes_name_the_worker_call():
    calls = [stripe.transfers.create(), stripe.payouts.create(), stripe.refunds.create(), stripe.customers.delete()]
    assert [r["call"] for r in calls] == ["transfers.create", "payouts.create", "refunds.create", "customers.delete"]


def test_a_recipe_fills_in_defer_to_call(fake):
    c = Outis(api_key="k", base_url=fake.base_url, intent_key=generate_intent_key())
    create = c.guard_method(TransferService(), "create", **stripe.transfers.create(), requester="payouts-api",
                            defer_to={"worker": "stripe"})
    params = {"amount": 2500000, "currency": "usd", "destination": "acct_9f2"}
    deferred = create(params)
    fake.authorize(deferred.id)

    class V1:
        transfers = TransferService()

    c.worker(clients={"stripe": WorkerClient(V1, idempotency="stripe")}).execute(deferred.id)
    assert V1.transfers.calls == [(params, {"idempotency_key": deferred.id})]
