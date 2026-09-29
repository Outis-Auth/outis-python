from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

TERMINAL_OUTCOMES = ("authorized", "denied", "expired", "aborted")


@dataclass(frozen=True)
class OutisRequest:
    """One request as the API reports it. Times are UTC epoch milliseconds."""

    id: str
    action: str
    requester: str
    state: str
    live: bool
    outcome: Optional[str]
    approvers: tuple[str, ...]
    params: Mapping[str, str]
    operation_hash: Optional[str]
    created_at: Optional[int]
    decided_at: Optional[int]
    intent: Optional[Mapping[str, Any]] = None
    """The sealed intent envelope, verbatim, or None."""
    execution: Optional["Execution"] = None
    """The customer's run of the intent, apart from the authorization."""
    replayed: bool = False
    """True when a create with an idempotency key returned an existing request."""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_pending(self) -> bool:
        return self.live

    @property
    def is_authorized(self) -> bool:
        return self.outcome == "authorized"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, replayed: bool = False) -> "OutisRequest":
        return cls(
            id=str(data.get("id", "")),
            action=str(data.get("action", "")),
            requester=str(data.get("requester") or ""),
            state=str(data.get("state", "")),
            live=bool(data.get("live", False)),
            outcome=data.get("outcome"),
            approvers=tuple(data.get("approvers") or ()),
            params=dict(data.get("params") or {}),
            operation_hash=data.get("operation_hash"),
            created_at=data.get("created_at"),
            decided_at=data.get("decided_at"),
            intent=data.get("intent"),
            execution=Execution.from_dict(data["execution"]) if isinstance(data.get("execution"), Mapping) else None,
            replayed=replayed,
            raw=dict(data),
        )


@dataclass(frozen=True)
class Execution:
    """The execution record: ``state`` is none, pending, claimed, succeeded or failed. Times are epoch ms."""

    state: str
    claimed_at: Optional[int] = None
    lease_expires_at: Optional[int] = None
    reported_at: Optional[int] = None
    reference: Optional[str] = None
    error: Optional[str] = None
    execute_by: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Execution":
        return cls(
            state=str(data.get("state") or "none"),
            claimed_at=data.get("claimed_at"),
            lease_expires_at=data.get("lease_expires_at"),
            reported_at=data.get("reported_at"),
            reference=data.get("reference"),
            error=data.get("error"),
            execute_by=data.get("execute_by"),
        )


@dataclass(frozen=True)
class Claim:
    """A lease on one request's execution. Only its holder may report."""

    claim_id: str
    lease_expires_at: Optional[int]
    request: OutisRequest


@dataclass(frozen=True)
class OutisEvent:
    """A verified webhook event. ``data`` is the raw payload; ``request`` is parsed from it."""

    id: str
    type: str
    created_at: str
    org: str
    request: OutisRequest
    data: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class Intent:
    """What a durable call asked for: the request it's waiting on and the operation it binds."""

    request_id: str
    action: str
    params: Mapping[str, str]
    operation_hash: str
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "params": dict(self.params),
            "operation_hash": self.operation_hash,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Intent":
        return cls(
            request_id=data["request_id"],
            action=data["action"],
            params=dict(data["params"]),
            operation_hash=data["operation_hash"],
            method=data["method"],
        )


@dataclass(frozen=True)
class Pending:
    """A durable call that's waiting on people. The real method hasn't run; a worker runs it later."""

    request_id: str
    request: OutisRequest
    intent_digest: str
    intent: Intent
    status: Literal["pending"] = "pending"


@dataclass(frozen=True)
class Done:
    """A hybrid call that was authorized in time and ran here. ``result`` is what the method returned."""

    result: Any
    request: OutisRequest
    status: Literal["done"] = "done"
