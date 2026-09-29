"""Guard Stripe transfers with the Stripe recipe: sealed for a worker, which runs them once approved.

Needs OUTIS_API_KEY, OUTIS_INTENT_KEY and STRIPE_API_KEY. The worker side is
``python -m outis init worker`` with ``clients={"stripe": client.v1}``.
"""

from __future__ import annotations

import os

from stripe import StripeClient

from outis import Outis
from outis.recipes import stripe as stripe_recipes

client = StripeClient(os.environ["STRIPE_API_KEY"])
outis = Outis(requester="payouts-api")

create_transfer = outis.guard_method(
    client.v1.transfers,
    "create",
    **stripe_recipes.transfers.create(),
    defer_to={"worker": "stripe"},
)

deferred = create_transfer({"amount": 2_500_000, "currency": "usd", "destination": "acct_9f2"})
print(f"waiting on approval: {deferred.id}")
