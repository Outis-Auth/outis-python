"""An Outis worker behind a FastAPI webhook, with a slow poll as a safety net.

Outis posts request.authorized to /outis/webhook and the worker runs the intent
right there. The poll catches anything a missed delivery left behind. Needs
OUTIS_API_KEY (read and execute scopes), OUTIS_INTENT_KEYS and
OUTIS_WEBHOOK_SECRET. Run: uvicorn fastapi_webhook:app
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
