"""Python client for Outis: ask the required humans, then act on what they decided."""

from ._duration import Duration, parse_duration
from ._hash import operation_hash
from ._intent import generate_key as generate_intent_key
from .client import DEFAULT_BASE_URL, MAX_WAIT_SECONDS, Outis, Requests
from .errors import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    IdempotencyConflictError,
    IntentError,
    NotAuthorizedError,
    NotFoundError,
    OperationMismatchError,
    OutisError,
    PermissionDeniedError,
    RateLimitError,
    TimeoutTooLongError,
    WaitCancelledError,
    WaitTimeoutError,
    WebhookVerificationError,
)
from .guard import DeferTo, Deferred, MethodDeferTo
from .intents import Intents
from .models import Claim, Done, Execution, Intent, OutisEvent, OutisRequest, Pending
from .webhooks import Webhooks, callback_secret, verify_webhook
from .worker import ExecutionContext, ExecutionResult, Worker, WorkerClient
from .wrap import Rule
from . import recipes

__version__ = "0.1.0"

__all__ = [
    "APIConnectionError",
    "APIError",
    "AuthenticationError",
    "Claim",
    "DEFAULT_BASE_URL",
    "DeferTo",
    "Deferred",
    "Done",
    "Duration",
    "Execution",
    "ExecutionContext",
    "ExecutionResult",
    "IdempotencyConflictError",
    "Intent",
    "IntentError",
    "Intents",
    "MAX_WAIT_SECONDS",
    "MethodDeferTo",
    "NotAuthorizedError",
    "NotFoundError",
    "OperationMismatchError",
    "Outis",
    "OutisError",
    "OutisEvent",
    "OutisRequest",
    "Pending",
    "PermissionDeniedError",
    "RateLimitError",
    "Requests",
    "Rule",
    "TimeoutTooLongError",
    "WaitCancelledError",
    "WaitTimeoutError",
    "Webhooks",
    "WebhookVerificationError",
    "Worker",
    "WorkerClient",
    "callback_secret",
    "generate_intent_key",
    "operation_hash",
    "parse_duration",
    "recipes",
    "verify_webhook",
]
