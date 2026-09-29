"""Runs the Temporal worker for PayoutWorkflow. Run: python run_worker.py

Start a payout from anywhere with a Temporal client:
    await client.start_workflow(PayoutWorkflow.run, Payout("acct_9f2", 2_500_000),
                                id="payout-123", task_queue="payouts")
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from activities import execute_payout, propose_payout
from workflow import PayoutWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(
        client,
        task_queue="payouts",
        workflows=[PayoutWorkflow],
        activities=[propose_payout, execute_payout],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
