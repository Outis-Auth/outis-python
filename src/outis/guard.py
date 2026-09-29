"""``guard``: ask the approvers, then wait in the background or hand the call to a worker."""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, TypedDict, Union

from . import _intent
from ._duration import Duration
from ._hash import operation_hash
from .errors import NotAuthorizedError, WaitCancelledError, WaitTimeoutError
from .intents import DEFAULT_EXECUTE_WITHIN
from .models import Intent, OutisRequest

if TYPE_CHECKING:
    from .client import Outis


class _DeferToRequired(TypedDict):
    worker: str
    call: str


class DeferTo(_DeferToRequired, total=False):
    """Where an approved call runs: the ``worker`` client name, the dotted ``call``, and plain JSON arguments."""

    args: Sequence[Any]
    kwargs: Mapping[str, Any]
    execute_within: Duration


@dataclass(frozen=True)
class Deferred:
    """A call sealed for a worker. Nothing has run; the worker runs it once the approvers say yes."""

    id: str
    request: OutisRequest
    intent_digest: str
    intent: Intent


class _MethodDeferToRequired(TypedDict):
    worker: str


class MethodDeferTo(_MethodDeferToRequired, total=False):
    """``guard_method``'s ``defer_to``: the call's own arguments are sealed, and ``call`` can come from a recipe."""

    call: str
    execute_within: Duration


ShowApprovers = Mapping[str, str]
PerCall = Union[str, Callable[..., str], None]
PerCallApprovers = Union[ShowApprovers, Callable[..., ShowApprovers], None]


@dataclass(frozen=True)
class _Plan:
    action: str
    requester: str
    params: dict[str, str]
    summary: Optional[str]
    idempotency_key: Optional[str]
    wait: Optional[float]
    defer_to: Optional[DeferTo]


_DEFER_KEYS = {"worker", "call", "args", "kwargs", "execute_within"}


def _check_paths(wait: Optional[Duration], defer_to: Optional[Mapping[str, Any]]) -> None:
    if (wait is None) == (defer_to is None):
        raise ValueError("pass exactly one of wait= (wait here) or defer_to= (a worker runs it later)")


def _check_defer_to(outis: "Outis", defer_to: Mapping[str, Any]) -> None:
    from .client import _window_seconds

    unknown = set(defer_to) - _DEFER_KEYS
    if unknown:
        raise ValueError(f"defer_to doesn't take {', '.join(sorted(unknown))}")
    for key in ("worker", "call"):
        if not isinstance(defer_to.get(key), str) or not defer_to[key]:
            raise ValueError(f"defer_to needs {key!r}, a non-empty string")
    if "execute_within" in defer_to:
        _window_seconds(defer_to["execute_within"])
    if not outis.intent_keys:
        raise ValueError("defer_to seals the call, so it needs an intent key: set OUTIS_INTENT_KEY or pass intent_key")


def plan(
    outis: "Outis",
    *,
    action: str,
    requester: Optional[str],
    show_approvers: Optional[ShowApprovers],
    summary: Optional[str],
    idempotency_key: Optional[str],
    wait: Optional[Duration],
    defer_to: Optional[DeferTo],
) -> _Plan:
    """Check every option before anything is sent."""
    from .client import _check_idempotency_key, _wait_seconds

    _check_paths(wait, defer_to)
    if not isinstance(action, str) or not action:
        raise ValueError("action is required")
    who = requester or outis.requester
    if not who:
        raise ValueError("requester is required: pass requester= or set it once with Outis(requester=...)")
    params = dict(show_approvers or {})
    operation_hash(action, params)
    if idempotency_key is not None:
        _check_idempotency_key(idempotency_key)
    seconds = None
    if wait is not None:
        seconds = _wait_seconds(wait, "wait")
    if defer_to is not None:
        _check_defer_to(outis, defer_to)
        if _intent.DIGEST_PARAM in params:
            raise ValueError(f"show_approvers can't use the reserved key {_intent.DIGEST_PARAM!r} with defer_to")
    return _Plan(action, who, params, summary, idempotency_key, seconds, defer_to)


def _create(outis: "Outis", p: _Plan) -> OutisRequest:
    return outis.requests.create(
        action=p.action,
        requester=p.requester,
        params=p.params,
        summary=p.summary,
        idempotency_key=p.idempotency_key,
    )


def defer(outis: "Outis", p: _Plan) -> Deferred:
    """Create the request with the sealed call and return without running anything."""
    assert p.defer_to is not None
    d = p.defer_to
    pending, _ = outis.intents._propose(
        action=p.action,
        requester=p.requester,
        client=d["worker"],
        method=d["call"],
        args=list(d.get("args", ())),
        kwargs=d.get("kwargs"),
        params=p.params,
        summary=p.summary,
        callback_url=None,
        quorum=None,
        idempotency_key=p.idempotency_key,
        execute_within=d.get("execute_within", DEFAULT_EXECUTE_WITHIN),
    )
    return Deferred(id=pending.request_id, request=pending.request, intent_digest=pending.intent_digest,
                    intent=pending.intent)


def start(outis: "Outis", p: _Plan) -> "Future[OutisRequest]":
    """Return a future at once and create and poll the request on a daemon thread."""
    future: Future[OutisRequest] = Future()
    stop = threading.Event()
    future.add_done_callback(lambda _: stop.set())
    threading.Thread(target=_poll, args=(outis, p, future, stop), name="outis-guard", daemon=True).start()
    return future


def _poll(outis: "Outis", p: _Plan, future: "Future[OutisRequest]", stop: threading.Event) -> None:
    assert p.wait is not None
    try:
        created = _create(outis, p)
        decided = outis.requests.wait_for(created, timeout=p.wait, cancel=stop)
        if not decided.is_authorized:
            raise NotAuthorizedError(decided)
    except WaitCancelledError:
        return
    except BaseException as exc:
        _settle(future.set_exception, exc)
        return
    _settle(future.set_result, decided)


def _settle(setter: Callable[[Any], None], value: Any) -> None:
    try:
        setter(value)
    except InvalidStateError:
        pass  # The caller cancelled the future while the last poll was in flight.


async def run_async(outis: "Outis", p: _Plan) -> OutisRequest:
    """Create and poll on the running loop. HTTP calls go through ``asyncio.to_thread``."""
    from .client import _Backoff

    assert p.wait is not None
    created = await asyncio.to_thread(_create, outis, p)
    backoff = _Backoff(outis, p.wait)
    while True:
        last = await asyncio.to_thread(outis.requests.retrieve, created.id)
        if not last.live or last.outcome is not None:
            if not last.is_authorized:
                raise NotAuthorizedError(last)
            return last
        pause = backoff.pause()
        if pause is None:
            raise WaitTimeoutError(created.id, p.wait, last)
        if outis._sleep is time.sleep:
            await asyncio.sleep(pause)
        else:
            outis._sleep(pause)
            await asyncio.sleep(0)


def _per_call(value: Any, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    return value(*args, **kwargs) if callable(value) else value


class _MethodOptions:
    def __init__(
        self,
        outis: "Outis",
        obj: Any,
        name: str,
        *,
        action: str,
        requester: PerCall,
        show_approvers: PerCallApprovers,
        summary: PerCall,
        idempotency_key: PerCall,
        wait: Optional[Duration],
        defer_to: Optional[MethodDeferTo],
        call: Optional[str] = None,
    ) -> None:
        from .client import _wait_seconds

        if not callable(getattr(obj, name)):
            raise TypeError(f"{name!r} isn't callable")
        _check_paths(wait, defer_to)
        if wait is not None:
            _wait_seconds(wait, "wait")
        if defer_to is not None:
            if "args" in defer_to or "kwargs" in defer_to:
                raise ValueError("guard_method's defer_to takes the call's own arguments; drop args and kwargs")
            if call and "call" not in defer_to:
                defer_to = {**defer_to, "call": call}
            _check_defer_to(outis, defer_to)
        self.outis, self.obj, self.name = outis, obj, name
        self.action, self.requester, self.show_approvers = action, requester, show_approvers
        self.summary, self.idempotency_key = summary, idempotency_key
        self.wait, self.defer_to = wait, defer_to

    def plan(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> _Plan:
        defer_to: Optional[DeferTo] = None
        if self.defer_to is not None:
            d = self.defer_to
            defer_to = {"worker": d["worker"], "call": d.get("call", ""), "args": list(args), "kwargs": dict(kwargs)}
            if "execute_within" in d:
                defer_to["execute_within"] = d["execute_within"]
        return plan(
            self.outis,
            action=self.action,
            requester=_per_call(self.requester, args, kwargs),
            show_approvers=_per_call(self.show_approvers, args, kwargs),
            summary=_per_call(self.summary, args, kwargs),
            idempotency_key=_per_call(self.idempotency_key, args, kwargs),
            wait=self.wait,
            defer_to=defer_to,
        )


def guard_method(opts: _MethodOptions) -> Callable[..., Any]:
    """Wrap ``obj.name`` so each call is approved first (``wait``) or sealed for a worker (``defer_to``)."""
    original = getattr(opts.obj, opts.name)

    @functools.wraps(original)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        p = opts.plan(args, kwargs)
        if p.defer_to is not None:
            return defer(opts.outis, p)
        start(opts.outis, p).result()
        return getattr(opts.obj, opts.name)(*args, **kwargs)

    return guarded


def guard_method_async(opts: _MethodOptions) -> Callable[..., Any]:
    """The asyncio form of :func:`guard_method`. An awaitable result from the method is awaited."""
    original = getattr(opts.obj, opts.name)

    @functools.wraps(original)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        p = opts.plan(args, kwargs)
        if p.defer_to is not None:
            return await asyncio.to_thread(defer, opts.outis, p)
        await run_async(opts.outis, p)
        result = getattr(opts.obj, opts.name)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return guarded
