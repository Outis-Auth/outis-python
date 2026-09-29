"""Ask two people to approve a database restore, then run it.

Needs OUTIS_API_KEY with the propose and read scopes. Run: python quickstart.py
"""

from __future__ import annotations

from outis import NotAuthorizedError, Outis, WaitTimeoutError


def restore(db: str, snapshot: str) -> None:
    print(f"restoring {db} from {snapshot}")


outis = Outis(requester="ops-cli")

approval = outis.guard(
    action="db.restore",
    show_approvers={"db": "payments", "snapshot": "2026-09-26"},
    wait="10m",
)

try:
    approval.result()
except NotAuthorizedError as exc:
    print(f"not approved: {exc.outcome}")
except WaitTimeoutError as exc:
    print(f"no decision yet, check {exc.request_id} later")
else:
    restore("payments", "2026-09-26")
