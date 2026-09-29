from __future__ import annotations

import asyncio
import os
import random
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Mapping, Sequence
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Callable, Optional, Union, overload

from ._duration import Duration, parse_duration
from ._hash import operation_hash
from ._http import Transport
from .errors import (
    NotAuthorizedError,
    OperationMismatchError,
    TimeoutTooLongError,
    WaitCancelledError,
    WaitTimeoutError,
)
from ._intent import KeyInput, load_keys
from .intents import Intents
from .models import Claim, OutisRequest
from .webhooks import Webhooks

if TYPE_CHECKING:
    from .guard import DeferTo, Deferred, MethodDeferTo, PerCall, PerCallApprovers
    from .worker import ResultHook, Worker
    from .wrap import Rule

DEFAULT_BASE_URL = "https://api.outis.tech"
MAX_WAIT_SECONDS = 30 * 60
_FIRST_POLL = 1.0
_MAX_POLL = 10.0

RequestRef = Union[str, OutisRequest]


def _request_id(ref: RequestRef) -> str:
    rid = ref.id if isinstance(ref, OutisRequest) else ref
    if not isinstance(rid, str) or not rid:
        raise ValueError("a request id is required")
    return rid


def _check_idempotency_key(key: str) -> None:
    if not 1 <= len(key) <= 255 or any(not 0x20 <= ord(c) < 0x7F for c in key):
        raise ValueError("an idempotency key is 1 to 255 printable ASCII characters")


MAX_EXECUTE_WITHIN = 30 * 86400


def _window_seconds(value: Duration) -> int:
    seconds = int(parse_duration(value))
    if not 1 <= seconds <= MAX_EXECUTE_WITHIN:
        raise ValueError("execute_within is 1 second to 30 days")
    return seconds


def _wait_seconds(timeout: Optional[Duration], name: str = "timeout") -> float:
    if timeout is None:
        raise ValueError(f"{name} is required: approval can take days, so a wait has to say how long")
    seconds = parse_duration(timeout)
    if seconds <= 0:
        raise ValueError(f"{name} must be positive")
    if seconds > MAX_WAIT_SECONDS:
        raise TimeoutTooLongError(
            f"{name} {seconds:g}s is over the 30 minute cap for an in-process wait. For longer, "
            "use defer_to= with a worker, or create the request and act on a webhook."
        )
    return seconds


class _Backoff:
    """Poll pacing: one second, doubling to ten, with jitter, until the deadline."""

    def __init__(self, client: "Outis", seconds: float) -> None:
        self._client = client
        self._deadline = client._clock() + seconds
        self._delay = _FIRST_POLL

    def pause(self) -> Optional[float]:
        """Seconds to sleep before the next poll, or None once the deadline has passed."""
        remaining = self._deadline - self._client._clock()
        if remaining <= 0:
            return None
        pause = min(remaining, self._delay * (0.8 + 0.4 * self._client._rng()))
        self._delay = min(_MAX_POLL, self._delay * 2)
        return pause


class Requests:
    """``outis.requests``: create a request, read it, and wait on it."""

    def __init__(self, client: "Outis") -> None:
        self._client = client

    def create(
        self,
        *,
        action: str,
        requester: str,
        params: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        callback_url: Optional[str] = None,
        quorum: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        intent: Optional[Mapping[str, Any]] = None,
        execute_within: Optional[Duration] = None,
    ) -> OutisRequest:
        """Propose an operation and return at once, before anyone decides.

        With ``idempotency_key``, a repeat of the same operation returns the
        original request (``replayed`` is True) and the call is safe to retry.
        ``intent`` and ``execute_within`` are for sealed intents; most code
        reaches them through ``outis.intents.propose``.
        """
        params = dict(params or {})
        operation_hash(action, params)  # validates types before anything leaves
        body: dict[str, Any] = {"action": action, "requester": requester, "params": params}
        if summary is not None:
            body["summary"] = summary
        if callback_url is not None:
            body["callback_url"] = callback_url
        if quorum is not None:
            body["quorum"] = quorum
        if intent is not None:
            body["intent"] = dict(intent)
        if execute_within is not None:
            body["execute_within"] = _window_seconds(execute_within)
        headers = {}
        if idempotency_key is not None:
            _check_idempotency_key(idempotency_key)
            headers["Idempotency-Key"] = idempotency_key
        resp = self._client._transport.request(
            "POST", "/v1/requests", body=body, headers=headers, retry=idempotency_key is not None
        )
        replayed = str(_get(resp.headers, "Idempotent-Replayed")).lower() == "true"
        return OutisRequest.from_dict(resp.body["request"], replayed=replayed)

    def retrieve(self, request: RequestRef) -> OutisRequest:
        """Read one request's current state."""
        rid = urllib.parse.quote(_request_id(request), safe="")
        resp = self._client._transport.request("GET", f"/v1/requests/{rid}", retry=True)
        return OutisRequest.from_dict(resp.body["request"])

    def list_executable(self, *, limit: int = 100) -> list[OutisRequest]:
        """Authorized requests with an intent nobody has claimed or reported, oldest first."""
        if not 1 <= limit <= 100:
            raise ValueError("limit is 1 to 100")
        resp = self._client._transport.request("GET", f"/v1/requests?executable=true&limit={limit}", retry=True)
        return [OutisRequest.from_dict(r) for r in (resp.body or {}).get("requests") or []]

    def claim(self, request: RequestRef, *, lease: Duration = 600) -> Claim:
        """Lease a request's execution. Only one live claim exists at a time.

        Raises :class:`APIError` with ``code`` ``already_claimed``,
        ``already_reported``, ``not_authorized`` or ``execution_window_closed``.
        """
        seconds = int(parse_duration(lease))
        if not 1 <= seconds <= 3600:
            raise ValueError("a lease is 1 second to 1 hour")
        rid = urllib.parse.quote(_request_id(request), safe="")
        resp = self._client._transport.request(
            "POST", f"/v1/requests/{rid}/claim", body={"lease_seconds": seconds}, retry=False
        )
        return Claim(
            claim_id=str(resp.body["claim_id"]),
            lease_expires_at=resp.body.get("lease_expires_at"),
            request=OutisRequest.from_dict(resp.body["request"]),
        )

    def report(
        self,
        request: RequestRef,
        *,
        claim_id: str,
        status: str,
        reference: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record how the claimed execution went: ``succeeded`` or ``failed``. Safe to repeat."""
        if status not in ("succeeded", "failed"):
            raise ValueError("status is 'succeeded' or 'failed'")
        body: dict[str, Any] = {"claim_id": claim_id, "status": status}
        if reference is not None:
            body["reference"] = reference[:512]
        if error is not None:
            body["error"] = error[:512]
        rid = urllib.parse.quote(_request_id(request), safe="")
        self._client._transport.request("POST", f"/v1/requests/{rid}/execution", body=body, retry=True)

    def wait_for(
        self,
        request: RequestRef,
        *,
        timeout: Duration,
        cancel: Optional[threading.Event] = None,
    ) -> OutisRequest:
        """Poll until the request is decided, whatever the outcome.

        ``timeout`` is required and capped at 30 minutes. Past it this raises
        :class:`WaitTimeoutError` and the request stays live, so resume later
        by id. Setting ``cancel`` stops the wait with :class:`WaitCancelledError`.
        """
        seconds = _wait_seconds(timeout)
        rid = _request_id(request)
        c = self._client
        backoff = _Backoff(c, seconds)
        while True:
            if cancel is not None and cancel.is_set():
                raise WaitCancelledError(rid)
            last = self.retrieve(rid)
            if not last.live or last.outcome is not None:
                return last
            pause = backoff.pause()
            if pause is None:
                raise WaitTimeoutError(rid, seconds, last)
            if cancel is not None and c._sleep is time.sleep:
                cancel.wait(pause)
            else:
                c._sleep(pause)

    def assert_authorized(
        self,
        request: RequestRef,
        *,
        action: str,
        params: Optional[Mapping[str, str]] = None,
    ) -> OutisRequest:
        """Check, right before executing, that this request authorized exactly this operation.

        Raises :class:`NotAuthorizedError` unless the outcome is authorized, and
        :class:`OperationMismatchError` unless its operation hash matches
        ``operation_hash(action, params)``.
        """
        expected = operation_hash(action, params)
        req = self.retrieve(request)
        if not req.is_authorized:
            raise NotAuthorizedError(req)
        actual = req.operation_hash or operation_hash(req.action, req.params)
        if actual != expected:
            raise OperationMismatchError(req, expected, actual)
        return req


def _get(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class Outis:
    """The Outis client.

    ``api_key`` and ``base_url`` fall back to ``OUTIS_API_KEY`` and
    ``OUTIS_BASE_URL``. ``requester`` is the default for :meth:`guard`. ``intent_key`` (one key or a list, newest first) falls
    back to ``OUTIS_INTENT_KEYS`` or ``OUTIS_INTENT_KEY``. ``clock``, ``sleep``,
    ``wall_clock`` and ``rng`` are there so tests don't have to wait in real time.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        requester: Optional[str] = None,
        intent_key: Union[KeyInput, Sequence[KeyInput], None] = None,
        http_timeout: float = 30.0,
        max_retries: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        api_key = api_key or os.environ.get("OUTIS_API_KEY")
        if not api_key:
            raise ValueError("an API key is required: pass api_key or set OUTIS_API_KEY")
        self.base_url = base_url or os.environ.get("OUTIS_BASE_URL") or DEFAULT_BASE_URL
        self.requester = requester
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._transport = Transport(
            api_key, self.base_url, http_timeout=http_timeout, max_retries=max_retries, sleep=sleep, rng=rng
        )
        self.intent_keys = load_keys(intent_key)
        self.requests = Requests(self)
        self.intents = Intents(self)
        self.webhooks = Webhooks(wall_clock)

    @overload
    def guard(
        self,
        *,
        action: str,
        wait: Duration,
        requester: Optional[str] = None,
        show_approvers: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> "Future[OutisRequest]": ...

    @overload
    def guard(
        self,
        *,
        action: str,
        defer_to: "DeferTo",
        requester: Optional[str] = None,
        show_approvers: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> "Deferred": ...

    def guard(
        self,
        *,
        action: str,
        requester: Optional[str] = None,
        show_approvers: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        wait: Optional[Duration] = None,
        defer_to: Optional["DeferTo"] = None,
    ) -> "Union[Future[OutisRequest], Deferred]":
        """Ask the approvers for ``action``, showing them exactly ``show_approvers``.

        With ``wait``, returns a :class:`concurrent.futures.Future` at once and
        polls on a daemon thread. It resolves with the authorized request or
        fails with :class:`NotAuthorizedError` or :class:`WaitTimeoutError`.
        With ``defer_to``, seals the call for a worker and returns
        :class:`Deferred` once the request exists.
        """
        from . import guard as g

        p = g.plan(self, action=action, requester=requester, show_approvers=show_approvers, summary=summary,
                   idempotency_key=idempotency_key, wait=wait, defer_to=defer_to)
        return g.defer(self, p) if p.defer_to is not None else g.start(self, p)

    @overload
    async def guard_async(
        self,
        *,
        action: str,
        wait: Duration,
        requester: Optional[str] = None,
        show_approvers: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> OutisRequest: ...

    @overload
    async def guard_async(
        self,
        *,
        action: str,
        defer_to: "DeferTo",
        requester: Optional[str] = None,
        show_approvers: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> "Deferred": ...

    async def guard_async(
        self,
        *,
        action: str,
        requester: Optional[str] = None,
        show_approvers: Optional[Mapping[str, str]] = None,
        summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        wait: Optional[Duration] = None,
        defer_to: Optional["DeferTo"] = None,
    ) -> "Union[OutisRequest, Deferred]":
        """The asyncio form of :meth:`guard`. Waits on the running loop instead of a thread."""
        from . import guard as g

        p = g.plan(self, action=action, requester=requester, show_approvers=show_approvers, summary=summary,
                   idempotency_key=idempotency_key, wait=wait, defer_to=defer_to)
        if p.defer_to is not None:
            return await asyncio.to_thread(g.defer, self, p)
        return await g.run_async(self, p)

    @overload
    def guard_method(
        self,
        obj: Any,
        name: str,
        *,
        action: str,
        wait: Duration,
        requester: "PerCall" = None,
        show_approvers: "PerCallApprovers" = None,
        summary: "PerCall" = None,
        idempotency_key: "PerCall" = None,
        call: Optional[str] = None,
    ) -> Callable[..., Any]: ...

    @overload
    def guard_method(
        self,
        obj: Any,
        name: str,
        *,
        action: str,
        defer_to: "MethodDeferTo",
        requester: "PerCall" = None,
        show_approvers: "PerCallApprovers" = None,
        summary: "PerCall" = None,
        idempotency_key: "PerCall" = None,
        call: Optional[str] = None,
    ) -> "Callable[..., Deferred]": ...

    def guard_method(
        self,
        obj: Any,
        name: str,
        *,
        action: str,
        requester: "PerCall" = None,
        show_approvers: "PerCallApprovers" = None,
        summary: "PerCall" = None,
        idempotency_key: "PerCall" = None,
        wait: Optional[Duration] = None,
        defer_to: Optional["MethodDeferTo"] = None,
        call: Optional[str] = None,
    ) -> Callable[..., Any]:
        """Return ``obj.name`` guarded. Options may be functions of the call's arguments.

        With ``wait``, a call blocks until approved, then runs the method and
        returns its result. With ``defer_to``, a call returns :class:`Deferred`
        and the method doesn't run here. ``call`` is the worker's dotted method
        when ``defer_to`` leaves it out; the recipes supply it.
        """
        from . import guard as g

        return g.guard_method(g._MethodOptions(
            self, obj, name, action=action, requester=requester, show_approvers=show_approvers,
            summary=summary, idempotency_key=idempotency_key, wait=wait, defer_to=defer_to, call=call,
        ))

    def guard_method_async(
        self,
        obj: Any,
        name: str,
        *,
        action: str,
        requester: "PerCall" = None,
        show_approvers: "PerCallApprovers" = None,
        summary: "PerCall" = None,
        idempotency_key: "PerCall" = None,
        wait: Optional[Duration] = None,
        defer_to: Optional["MethodDeferTo"] = None,
        call: Optional[str] = None,
    ) -> Callable[..., Awaitable[Any]]:
        """The asyncio form of :meth:`guard_method`. Works with sync or async methods."""
        from . import guard as g

        return g.guard_method_async(g._MethodOptions(
            self, obj, name, action=action, requester=requester, show_approvers=show_approvers,
            summary=summary, idempotency_key=idempotency_key, wait=wait, defer_to=defer_to, call=call,
        ))

    def wrap(
        self,
        target: Any,
        rules: Mapping[str, "Rule"],
        *,
        mode: str = "wait",
        timeout: Optional[Duration] = None,
        client: Optional[str] = None,
        execute_within: Optional[Duration] = None,
        wait: Optional[Duration] = None,
    ) -> Any:
        """Return a proxy of ``target`` whose listed methods need Outis first.

        Keys are method names, dotted for nested ones (``"transfers.create"``).
        ``mode="wait"`` guards each call with ``timeout`` and then runs it.
        ``mode="durable"`` (alias ``"propose"``) seals the call as an intent for
        the worker registered as ``client`` and returns :class:`Pending`.
        ``mode="hybrid"`` waits up to ``wait`` and runs it here if authorized in
        time, returning :class:`Done`, else returns :class:`Pending`.
        """
        from .wrap import wrap

        return wrap(
            self, target, rules, mode=mode, timeout=timeout, client=client, execute_within=execute_within, wait=wait
        )

    def worker(
        self,
        *,
        clients: Optional[Mapping[str, Any]] = None,
        handlers: Optional[Mapping[str, Callable[..., Any]]] = None,
        allow: Optional[Sequence[str]] = None,
        concurrency: int = 4,
        on_result: Optional["ResultHook"] = None,
        lease: Duration = 600,
    ) -> "Worker":
        """Build a worker that claims authorized intents and replays them on your clients.

        ``clients`` maps the names proposers use to objects (wrap one in
        :class:`WorkerClient` for Stripe style idempotency). ``handlers`` maps
        ``"client.method"`` to a function called as ``handler(ctx, *args, **kwargs)``.
        ``allow`` is a list of glob patterns like ``"stripe.transfers.*"``.
        """
        from .worker import Worker

        return Worker(
            self,
            clients=clients or {},
            handlers=handlers or {},
            allow=allow,
            concurrency=concurrency,
            on_result=on_result,
            lease=lease,
        )
