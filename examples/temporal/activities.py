"""Activities: propose the payout as a sealed intent, and execute it once authorized.

Needs OUTIS_API_KEY (propose, read and execute scopes) and OUTIS_INTENT_KEYS.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from outis import Outis, Worker, WorkerClient


class Payouts:
    """Stands in for your real client (the stripe module, a database, an internal API)."""

    def send(self, destination: str, amount_cents: int, *, idempotency_key: str) -> dict[str, str]:
        logging.info("paying %s cents to %s (idempotency key %s)", amount_cents, destination, idempotency_key)
        return {"id": f"po_{idempotency_key}"}


def build_worker(outis: Outis) -> Worker:
    # idempotency="stripe" passes idempotency_key=<request id>, the way Stripe takes it.
    return outis.worker(
        clients={"payouts": WorkerClient(Payouts(), idempotency="stripe")},
        allow=["payouts.*"],
        on_result=lambda r: logging.info("outis %s: %s %s", r.request_id, r.status, r.reason or r.reference or ""),
    )


@dataclass
class Payout:
    destination: str
    amount_cents: int


@dataclass
class ProposeInput:
    workflow_id: str
    payout: Payout


@functools.cache
def outis() -> Outis:
    return Outis()


@functools.cache
def worker() -> Worker:
    return build_worker(outis())


@activity.defn
async def propose_payout(inp: ProposeInput) -> str:
    """Create the request. The workflow id keys it, so a retried activity finds the same one."""
    deferred = await outis().guard_async(
        action="payouts.send",
        requester="payouts-workflow",
        show_approvers={
            "destination": inp.payout.destination,
            "amount": f"{inp.payout.amount_cents / 100:.2f} USD",
            "workflow_id": inp.workflow_id,
        },
        idempotency_key=f"temporal-{inp.workflow_id}",
        defer_to={"worker": "payouts", "call": "send", "args": [inp.payout.destination, inp.payout.amount_cents]},
    )
    return deferred.id


@activity.defn
async def execute_payout(request_id: str) -> str:
    """Claim, verify and run the intent. Returns the downstream reference."""
    result = await asyncio.to_thread(worker().execute, request_id)
    if result.status == "succeeded":
        return result.reference or ""
    if result.reason == "already_claimed":
        raise ApplicationError("another worker holds the claim; retry after its lease")
    raise ApplicationError(f"{result.reason}: {result.error}", non_retryable=True)
