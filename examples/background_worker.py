"""An Outis worker on its own process.

It polls Outis for authorized intents, claims each one, checks it against what
the operators approved, and replays it with your clients. SIGTERM or Ctrl-C
stops it after the calls in flight finish. Needs OUTIS_API_KEY (read and
execute scopes) and OUTIS_INTENT_KEYS. Run: python background_worker.py
"""

from __future__ import annotations

import logging

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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_worker(Outis()).start(every="15s")
    logging.info("outis worker stopped")


if __name__ == "__main__":
    main()
