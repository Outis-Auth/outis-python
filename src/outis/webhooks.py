from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from typing import Any, Callable, Optional, Union

from .errors import WebhookVerificationError
from .models import OutisEvent, OutisRequest

SIGNATURE_HEADER = "Outis-Signature"
DEFAULT_TOLERANCE_SECONDS = 300
CALLBACK_KEY_INFO = b"outis/callback-key/v1"

Secret = Union[str, bytes]
"""A webhook endpoint's ``whsec_...`` string (its UTF-8 bytes are the key) or raw key bytes."""


def callback_secret(api_key: str) -> bytes:
    """The key that signs events sent to a request's ``callback_url``.

    It's HMAC-SHA256 of ``"outis/callback-key/v1"`` under the API key that
    created the request, 32 raw bytes. Pass it to :func:`verify_webhook`.
    """
    if not api_key:
        raise ValueError("an API key is required")
    return hmac.new(api_key.encode("utf-8"), CALLBACK_KEY_INFO, hashlib.sha256).digest()


def _key(secret: Secret) -> bytes:
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


def _header(headers: Any, name: str) -> Optional[str]:
    items = headers.items() if hasattr(headers, "items") else headers
    wanted = name.lower()
    for key, value in items:
        if str(key).lower() == wanted:
            return value.decode() if isinstance(value, bytes) else str(value)
    return None


def verify_webhook(
    raw_body: Union[bytes, bytearray, memoryview, str],
    headers: Any,
    secret: Union[Secret, Sequence[Secret]],
    *,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> OutisEvent:
    """Check an Outis webhook's signature and freshness, then parse it.

    ``raw_body`` must be the bytes as received, before any JSON parsing.
    ``headers`` is any mapping or list of pairs; names match case-insensitively.
    ``secret`` is a ``whsec_...`` string or key bytes (see :func:`callback_secret`),
    or a list while you rotate. ``now`` is unix seconds.
    Dedupe on the returned event's ``id``: a delivery can arrive more than once.
    """
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
    secrets = [secret] if isinstance(secret, (str, bytes, bytearray)) else list(secret)
    if not secrets or any(not s for s in secrets):
        raise ValueError("a webhook secret is required")

    header = _header(headers, SIGNATURE_HEADER)
    if not header:
        raise WebhookVerificationError(f"missing {SIGNATURE_HEADER} header")

    timestamp: Optional[int] = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                raise WebhookVerificationError("malformed signature timestamp") from None
        elif key == "v1" and value:
            signatures.append(value.lower())
    if timestamp is None or not signatures:
        raise WebhookVerificationError(f"malformed {SIGNATURE_HEADER} header")

    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise WebhookVerificationError("signature timestamp is outside the tolerance")

    signed = str(timestamp).encode("ascii") + b"." + body
    expected = [hmac.new(_key(s), signed, hashlib.sha256).hexdigest() for s in secrets]
    # Compare every pair so timing doesn't reveal which secret or signature matched.
    matched = False
    for want in expected:
        for got in signatures:
            matched |= hmac.compare_digest(want, got)
    if not matched:
        raise WebhookVerificationError("signature doesn't match")

    try:
        envelope = json.loads(body)
        data = envelope["data"]
        return OutisEvent(
            id=envelope["id"],
            type=envelope["type"],
            created_at=envelope["created_at"],
            org=envelope["org"],
            request=OutisRequest.from_dict(data["request"]),
            data=data,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise WebhookVerificationError(f"signed body isn't an Outis event: {exc}") from None


class Webhooks:
    """``outis.webhooks``: verification bound to the client's clock."""

    def __init__(self, wall_clock: Callable[[], float] = time.time) -> None:
        self._wall_clock = wall_clock

    def verify(
        self,
        raw_body: Union[bytes, bytearray, memoryview, str],
        headers: Any,
        secret: Union[Secret, Sequence[Secret]],
        *,
        tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
        now: Optional[float] = None,
    ) -> OutisEvent:
        """See :func:`verify_webhook`."""
        return verify_webhook(
            raw_body,
            headers,
            secret,
            tolerance_seconds=tolerance_seconds,
            now=self._wall_clock() if now is None else now,
        )
