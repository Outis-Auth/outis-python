"""Starter files for ``python -m outis init worker``. ``__MODULE__`` becomes the file's module name."""

from __future__ import annotations

PAYOUTS = '''

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
'''

PLAIN = '''"""An Outis worker on its own process.

It polls Outis for authorized intents, claims each one, checks it against what
the operators approved, and replays it with your clients. SIGTERM or Ctrl-C
stops it after the calls in flight finish. Needs OUTIS_API_KEY (read and
execute scopes) and OUTIS_INTENT_KEYS. Run: python __MODULE__.py
"""

from __future__ import annotations

import logging

from outis import Outis, Worker, WorkerClient
''' + PAYOUTS + '''

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_worker(Outis()).start(every="15s")
    logging.info("outis worker stopped")


if __name__ == "__main__":
    main()
'''

FASTAPI = '''"""An Outis worker behind a FastAPI webhook, with a slow poll as a safety net.

Outis posts request.authorized to /outis/webhook and the worker runs the intent
right there. The poll catches anything a missed delivery left behind. Needs
OUTIS_API_KEY (read and execute scopes), OUTIS_INTENT_KEYS and
OUTIS_WEBHOOK_SECRET. Run: uvicorn __MODULE__:app
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from outis import Outis, Worker, WorkerClient
''' + PAYOUTS + '''

logging.basicConfig(level=logging.INFO)
worker = build_worker(Outis())
secret = os.environ["OUTIS_WEBHOOK_SECRET"]
stop = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    poller = threading.Thread(target=worker.run, kwargs={"every": "1m", "stop": stop}, daemon=True)
    poller.start()
    yield
    stop.set()
    await asyncio.to_thread(poller.join, 30)


app = FastAPI(lifespan=lifespan)


@app.post("/outis/webhook")
async def outis_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    status, payload = await asyncio.to_thread(worker.handle_webhook, body, request.headers, secret)
    return JSONResponse(payload, status_code=status)
'''

CELERY = '''"""Outis intents executed by Celery.

A beat task sweeps Outis for authorized intents and queues one execute task per
request; your webhook view can queue the same task on request.authorized. The
claim makes a duplicate harmless. Needs OUTIS_API_KEY (read and execute
scopes), OUTIS_INTENT_KEYS and CELERY_BROKER_URL.
Run: celery -A __MODULE__ worker -B
"""

from __future__ import annotations

import functools
import logging
import os

from celery import Celery

from outis import Outis, Worker, WorkerClient
''' + PAYOUTS + '''

app = Celery("__MODULE__", broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
app.conf.beat_schedule = {"outis-sweep": {"task": "__MODULE__.sweep", "schedule": 30.0}}


@functools.cache
def outis() -> Outis:
    return Outis()


@functools.cache
def worker() -> Worker:
    return build_worker(outis())


@app.task(name="__MODULE__.execute", acks_late=True)
def execute(request_id: str) -> str:
    return worker().execute(request_id).status


@app.task(name="__MODULE__.sweep")
def sweep() -> int:
    ready = outis().requests.list_executable(limit=100)
    for req in ready:
        execute.delay(req.id)
    return len(ready)
'''

TEMPORAL_ACTIVITIES = '''"""Activities: propose the payout as a sealed intent, and execute it once authorized.

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
''' + PAYOUTS + '''

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
'''

TEMPORAL_WORKFLOW = '''"""A workflow that waits days for people, then pays out."""

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
'''

TEMPORAL_BRIDGE = '''"""Verifies Outis webhooks and signals the workflow named in the request's params.

Needs OUTIS_WEBHOOK_SECRET and TEMPORAL_ADDRESS. Run: uvicorn bridge:app
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response
from temporalio.client import Client
from temporalio.service import RPCError

from outis import WebhookVerificationError, verify_webhook
from workflow import PayoutWorkflow

DECISIONS = {"request.authorized", "request.denied", "request.expired", "request.aborted"}

app = FastAPI()
_client: Client | None = None


async def temporal() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    return _client


@app.post("/outis/webhook")
async def outis_webhook(request: Request) -> Response:
    try:
        event = verify_webhook(await request.body(), request.headers, os.environ["OUTIS_WEBHOOK_SECRET"])
    except WebhookVerificationError:
        return Response(status_code=400)
    workflow_id = event.request.params.get("workflow_id")
    if event.type not in DECISIONS or not workflow_id or event.request.outcome is None:
        return Response(status_code=200)
    handle = (await temporal()).get_workflow_handle(workflow_id)
    try:
        await handle.signal(PayoutWorkflow.outis_decision, event.request.outcome)
    except RPCError:
        pass  # The workflow already finished; a redelivered event has nothing to do.
    return Response(status_code=200)
'''

TEMPORAL_WORKER = '''"""Runs the Temporal worker for PayoutWorkflow. Run: python run_worker.py

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
'''

RUNTIMES: dict[str, dict[str, str]] = {
    "plain": {"outis_worker.py": PLAIN},
    "fastapi": {"outis_worker.py": FASTAPI},
    "celery": {"outis_worker.py": CELERY},
    "temporal": {
        "outis_temporal/activities.py": TEMPORAL_ACTIVITIES,
        "outis_temporal/workflow.py": TEMPORAL_WORKFLOW,
        "outis_temporal/bridge.py": TEMPORAL_BRIDGE,
        "outis_temporal/run_worker.py": TEMPORAL_WORKER,
    },
}


def render(content: str, module: str) -> str:
    return content.replace("__MODULE__", module)
