from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Optional, TypedDict, Union

from ._duration import Duration
from .errors import APIError, NotAuthorizedError, OperationMismatchError, OutisError, WaitTimeoutError
from .intents import DEFAULT_EXECUTE_WITHIN
from .models import Done, Pending
from .worker import _reference

if TYPE_CHECKING:
    from .client import Outis

log = logging.getLogger("outis")

MODES = ("wait", "durable", "hybrid")


class _RuleRequired(TypedDict):
    action: str
    params: Callable[..., Mapping[str, str]]
    requester: Union[str, Callable[..., str]]


class Rule(_RuleRequired, total=False):
    """How one wrapped method maps onto an Outis request. Callables get the call's arguments."""

    when: Callable[..., bool]
    summary: Callable[..., str]
    idempotency_key: Callable[..., Optional[str]]


def wrap(
    outis: "Outis",
    target: Any,
    rules: Mapping[str, Rule],
    *,
    mode: str,
    timeout: Optional[Duration],
    client: Optional[str],
    execute_within: Optional[Duration],
    wait: Optional[Duration],
) -> Any:
    from .client import _wait_seconds, _window_seconds

    if mode == "propose":
        mode = "durable"
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "wait":
        _wait_seconds(timeout)
        if client is not None or execute_within is not None or wait is not None:
            raise ValueError("wait mode takes a timeout, not client, execute_within or wait")
    else:
        if timeout is not None:
            raise ValueError(f"{mode} mode takes wait=, not timeout=")
        if not client:
            raise ValueError(f"{mode} mode needs client=, the name your worker registers this object under")
        if not outis.intent_keys:
            raise ValueError(f"{mode} mode seals calls: pass intent_key or set OUTIS_INTENT_KEY")
        if mode == "hybrid":
            _wait_seconds(wait)
        elif wait is not None:
            raise ValueError("durable mode doesn't wait; use hybrid for that")
        if execute_within is not None:
            _window_seconds(execute_within)

    tree: dict[str, Any] = {}
    for path, rule in rules.items():
        for key in ("action", "params", "requester"):
            if key not in rule:
                raise ValueError(f"rule for {path!r} is missing {key!r}")
        node, parts = tree, path.split(".")
        obj = target
        for part in parts[:-1]:
            obj = getattr(obj, part)
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"{path!r} nests under a wrapped method")
            node = child
        if not callable(getattr(obj, parts[-1])):
            raise TypeError(f"{path!r} isn't callable")
        if parts[-1] in node:
            raise ValueError(f"{path!r} is listed twice or has wrapped methods under it")
        node[parts[-1]] = (path, rule)
    caller = _Caller(outis, mode, timeout, client, execute_within or DEFAULT_EXECUTE_WITHIN, wait)
    return _Proxy(target, tree, caller)


class _Caller:
    def __init__(
        self,
        outis: "Outis",
        mode: str,
        timeout: Optional[Duration],
        client: Optional[str],
        execute_within: Duration,
        wait: Optional[Duration],
    ) -> None:
        self.outis, self.mode, self.timeout = outis, mode, timeout
        self.client, self.execute_within, self.wait = client, execute_within, wait

    def bind(self, fn: Callable[..., Any], path: str, rule: Rule) -> Callable[..., Any]:
        @functools.wraps(fn)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            when = rule.get("when")
            if when is not None and not when(*args, **kwargs):
                return fn(*args, **kwargs)
            params = dict(rule["params"](*args, **kwargs))
            requester = rule["requester"]
            summary = rule.get("summary")
            key = rule.get("idempotency_key")
            fields: dict[str, Any] = {
                "action": rule["action"],
                "requester": requester if isinstance(requester, str) else requester(*args, **kwargs),
                "params": params,
                "summary": summary(*args, **kwargs) if summary else None,
                "idempotency_key": key(*args, **kwargs) if key else None,
            }
            if self.mode == "wait":
                assert self.timeout is not None
                requests = self.outis.requests
                decided = requests.wait_for(requests.create(**fields), timeout=self.timeout)
                if not decided.is_authorized:
                    raise NotAuthorizedError(decided)
                return fn(*args, **kwargs)
            assert self.client is not None
            pending, _ = self.outis.intents._propose(
                client=self.client,
                method=path,
                args=args,
                kwargs=kwargs,
                callback_url=None,
                quorum=None,
                execute_within=self.execute_within,
                **fields,
            )
            if self.mode == "durable":
                return pending
            return self._hybrid(pending, fn, args, kwargs)

        return guarded

    def _hybrid(self, pending: Pending, fn: Callable[..., Any], args: Any, kwargs: Any) -> Union[Pending, Done]:
        requests = self.outis.requests
        assert self.wait is not None
        try:
            decided = requests.wait_for(pending.request_id, timeout=self.wait)
        except WaitTimeoutError:
            return pending
        if not decided.is_authorized:
            raise NotAuthorizedError(decided)
        try:
            claim = requests.claim(pending.request_id)
        except APIError as exc:
            if exc.status in (409, 410):
                return pending
            raise
        req = claim.request
        if req.operation_hash != pending.intent.operation_hash:
            _report(self.outis, req.id, claim.claim_id, "failed", error="operation_mismatch: the request covers a different operation")
            raise OperationMismatchError(req, pending.intent.operation_hash, req.operation_hash)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            _report(self.outis, req.id, claim.claim_id, "failed", error=f"execution_error: {str(exc) or type(exc).__name__}")
            raise
        _report(self.outis, req.id, claim.claim_id, "succeeded", reference=_reference(result))
        return Done(result=result, request=req)


def _report(outis: "Outis", rid: str, claim_id: str, status: str, **fields: Optional[str]) -> None:
    try:
        outis.requests.report(rid, claim_id=claim_id, status=status, **fields)
    except OutisError:
        log.exception("outis: couldn't report %s for %s", status, rid)


class _Proxy:
    """Stands in for the wrapped object. Attributes not listed pass straight through."""

    __slots__ = ("_outis_target", "_outis_tree", "_outis_caller")

    def __init__(self, target: Any, tree: dict[str, Any], caller: _Caller) -> None:
        object.__setattr__(self, "_outis_target", target)
        object.__setattr__(self, "_outis_tree", tree)
        object.__setattr__(self, "_outis_caller", caller)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._outis_target, name)
        node = self._outis_tree.get(name)
        if node is None:
            return value
        if isinstance(node, dict):
            return _Proxy(value, node, self._outis_caller)
        path, rule = node
        return self._outis_caller.bind(value, path, rule)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._outis_target, name, value)

    def __repr__(self) -> str:
        return f"<outis wrapped {self._outis_target!r}>"
