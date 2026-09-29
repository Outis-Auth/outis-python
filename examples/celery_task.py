"""Outis intents executed by Celery.

A beat task sweeps Outis for authorized intents and queues one execute task per
request; your webhook view can queue the same task on request.authorized. The
claim makes a duplicate harmless. Needs OUTIS_API_KEY (read and execute
scopes), OUTIS_INTENT_KEYS and CELERY_BROKER_URL.
Run: celery -A celery_task worker -B
"""

from __future__ import annotations

import functools
import logging
import os

from celery import Celery

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


app = Celery("celery_task", broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
app.conf.beat_schedule = {"outis-sweep": {"task": "celery_task.sweep", "schedule": 30.0}}


@functools.cache
def outis() -> Outis:
    return Outis()


@functools.cache
def worker() -> Worker:
    return build_worker(outis())


@app.task(name="celery_task.execute", acks_late=True)
def execute(request_id: str) -> str:
    return worker().execute(request_id).status


@app.task(name="celery_task.sweep")
def sweep() -> int:
    ready = outis().requests.list_executable(limit=100)
    for req in ready:
        execute.delay(req.id)
    return len(ready)
