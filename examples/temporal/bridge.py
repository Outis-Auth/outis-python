"""Verifies Outis webhooks and signals the workflow named in the request's params.

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
