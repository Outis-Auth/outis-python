"""A workflow that waits days for people, then pays out."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import Payout, ProposeInput, execute_payout, propose_payout


@workflow.defn
class PayoutWorkflow:
    def __init__(self) -> None:
        self.outcome: str | None = None

    @workflow.signal(name="outisDecision")
    def outis_decision(self, outcome: str) -> None:
        """Sent by the bridge: authorized, denied, expired or aborted."""
        self.outcome = outcome

    @workflow.run
    async def run(self, payout: Payout) -> str:
        request_id = await workflow.execute_activity(
            propose_payout,
            ProposeInput(workflow.info().workflow_id, payout),
            start_to_close_timeout=timedelta(seconds=30),
        )
        try:
            await workflow.wait_condition(lambda: self.outcome is not None, timeout=timedelta(days=7))
        except asyncio.TimeoutError:
            return "undecided"
        if self.outcome != "authorized":
            return self.outcome or "undecided"
        return await workflow.execute_activity(
            execute_payout, request_id, start_to_close_timeout=timedelta(minutes=5)
        )
