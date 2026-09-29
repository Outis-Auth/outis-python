from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .models import OutisRequest


class OutisError(Exception):
    """Base class for everything this library raises."""


class APIConnectionError(OutisError):
    """The API couldn't be reached."""


class APIError(OutisError):
    """The API answered with a refusal. ``code`` is the body's machine readable ``kind``, if any."""

    def __init__(self, message: str, *, status: int, code: Optional[str] = None, body: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body

    def __str__(self) -> str:
        suffix = f" ({self.code})" if self.code else ""
        return f"{self.status}{suffix}: {self.message}"


class AuthenticationError(APIError):
    """401: no live API key."""


class PermissionDeniedError(APIError):
    """403: the key doesn't hold the scope this call needs."""


class NotFoundError(APIError):
    """404: no such request."""


class IdempotencyConflictError(APIError):
    """409: the idempotency key was already used for a different operation."""


class RateLimitError(APIError):
    """429: too many attempts. ``retry_after`` is in seconds when the API said."""

    def __init__(self, message: str, *, status: int, code: Optional[str] = None, body: Any = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message, status=status, code=code, body=body)
        self.retry_after = retry_after


class TimeoutTooLongError(OutisError, ValueError):
    """A blocking wait asked for more than the 30 minute cap."""


class WaitTimeoutError(OutisError):
    """The wait ran out before the request was decided. The request is still live; resume with its id."""

    def __init__(self, request_id: str, timeout: float, request: Optional["OutisRequest"] = None) -> None:
        super().__init__(f"request {request_id} wasn't decided within {timeout:g}s; it's still live")
        self.request_id = request_id
        self.timeout = timeout
        self.request = request


class WaitCancelledError(OutisError):
    """The caller's cancel event was set during a wait."""

    def __init__(self, request_id: str) -> None:
        super().__init__(f"wait for request {request_id} was cancelled")
        self.request_id = request_id


class NotAuthorizedError(OutisError):
    """The request ended without authorization. ``outcome`` is denied, expired or aborted."""

    def __init__(self, request: "OutisRequest") -> None:
        super().__init__(f"request {request.id} was not authorized (outcome: {request.outcome or 'pending'})")
        self.request = request
        self.outcome = request.outcome


class OperationMismatchError(OutisError):
    """The authorized request covers a different operation than the one about to run."""

    def __init__(self, request: "OutisRequest", expected: str, actual: Optional[str]) -> None:
        super().__init__(f"request {request.id} authorized {actual}, not {expected}")
        self.request = request
        self.expected_hash = expected
        self.actual_hash = actual


class WebhookVerificationError(OutisError):
    """A webhook's signature, timestamp or body didn't check out. Answer it with a 400."""


class IntentError(OutisError):
    """A sealed intent failed a check. ``reason`` is the code a worker reports, like ``digest_mismatch``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
