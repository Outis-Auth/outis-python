from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Optional

from . import _intent
from ._duration import Duration
from ._hash import operation_hash
from .models import Intent, Pending

if TYPE_CHECKING:
    from .client import Outis

DEFAULT_EXECUTE_WITHIN = 7 * 86400


class Intents:
    """``outis.intents``: propose a call to run later, sealed so Outis can't read or change it."""

    def __init__(self, client: "Outis") -> None:
        self._client = client

    def propose(
        self,
        *,
        action: str,
        requester: str,
        client: str,
        method: str,
        args: Sequence[Any] = (),
        kwargs: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        callback_url: Optional[str] = None,
        quorum: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        execute_within: Duration = DEFAULT_EXECUTE_WITHIN,
    ) -> Pending:
        """Create a request carrying a sealed intent and return without running anything.

        ``client`` and ``method`` name what the worker replays (``"stripe"``,
        ``"transfers.create"``). The intent's digest goes into ``params`` under
        ``intent``, so the operators' approval binds this exact call.
        """
        return self._propose(
            action=action,
            requester=requester,
            client=client,
            method=method,
            args=args,
            kwargs=kwargs,
            params=params,
            summary=summary,
            callback_url=callback_url,
            quorum=quorum,
            idempotency_key=idempotency_key,
            execute_within=execute_within,
        )[0]

    def _propose(
        self,
        *,
        action: str,
        requester: str,
        client: str,
        method: str,
        args: Sequence[Any],
        kwargs: Optional[Mapping[str, Any]],
        params: Optional[Mapping[str, str]],
        summary: Optional[str],
        callback_url: Optional[str],
        quorum: Optional[int],
        idempotency_key: Optional[str],
        execute_within: Duration,
    ) -> tuple[Pending, bytes]:
        keys = self._client.intent_keys
        if not keys:
            raise ValueError("an intent key is required: pass intent_key or set OUTIS_INTENT_KEY")
        p = dict(params or {})
        if _intent.DIGEST_PARAM in p:
            raise ValueError(f"params can't use the reserved key {_intent.DIGEST_PARAM!r}")
        plaintext = _intent.encode(client, method, args, kwargs)
        digest = _intent.digest(plaintext)
        p[_intent.DIGEST_PARAM] = digest
        envelope = _intent.seal(keys[0], action, plaintext)
        request = self._client.requests.create(
            action=action,
            requester=requester,
            params=p,
            summary=summary,
            callback_url=callback_url,
            quorum=quorum,
            idempotency_key=idempotency_key,
            intent=envelope,
            execute_within=execute_within,
        )
        intent = Intent(
            request_id=request.id,
            action=action,
            params=p,
            operation_hash=operation_hash(action, p),
            method=f"{client}.{method}",
        )
        return Pending(request_id=request.id, request=request, intent_digest=digest, intent=intent), plaintext
