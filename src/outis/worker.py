from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
import signal
import threading
from collections.abc import Awaitable, Iterable, Mapping, MutableMapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Union

from . import _intent
from ._duration import Duration, parse_duration
from ._hash import operation_hash
from .errors import APIError, IntentError, OutisError, WebhookVerificationError
from .models import OutisRequest
from .webhooks import Secret

if TYPE_CHECKING:
    from .client import Outis

log = logging.getLogger("outis")

_SKIP_CODES = ("already_claimed", "already_reported", "not_authorized", "execution_window_closed")


@dataclass(frozen=True)
class WorkerClient:
    """A registered client with options.

    ``idempotency="stripe"`` passes the request id as Stripe's idempotency key:
    ``options={"idempotency_key": ...}`` to a ``StripeClient`` service, else ``idempotency_key=``.
    """

    target: Any
    idempotency: Optional[Literal["stripe"]] = None


@dataclass(frozen=True)
class ExecutionContext:
    """What a handler gets. Pass ``idempotency_key`` downstream so a retried run can't act twice."""

    request_id: str
    request: OutisRequest
    claim_id: str
    client: str
    method: str

    @property
    def idempotency_key(self) -> str:
        return self.request_id


@dataclass(frozen=True)
class ExecutionResult:
    """How one ``execute`` went.

    ``status`` is ``succeeded`` or ``failed`` once a claim was held and
    reported, or ``skipped`` when there was nothing to claim. ``reason`` is the
    verification or API code, ``error`` the message.
    """

    request_id: str
    status: Literal["succeeded", "failed", "skipped"]
    reason: Optional[str] = None
    reference: Optional[str] = None
    error: Optional[str] = None
    result: Any = field(default=None, repr=False)
    context: Optional[ExecutionContext] = field(default=None, repr=False)
    reported: bool = False


ResultHook = Callable[[ExecutionResult], None]
Handler = Callable[..., Any]
"""Runs one ``client.method`` itself, called as ``handler(ctx, *args, **kwargs)``."""


def _reference(value: Any) -> Optional[str]:
    ref = value.get("id") if isinstance(value, Mapping) else getattr(value, "id", None)
    return ref if isinstance(ref, str) else None


def _stripe_idempotency(fn: Callable[..., Any], kwargs: dict[str, Any], key: str) -> None:
    try:
        takes_options = "options" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_options = False
    if takes_options:
        kwargs["options"] = {**(kwargs.get("options") or {}), "idempotency_key": key}
    else:
        kwargs.setdefault("idempotency_key", key)


def _resolve(target: Any, dotted: str) -> Callable[..., Any]:
    obj = target
    for part in dotted.split("."):
        if not part or part.startswith("_"):
            raise IntentError("client_not_registered", f"{dotted!r} isn't a public method path")
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise IntentError("client_not_registered", f"the client has no {dotted!r}") from None
    if not callable(obj):
        raise IntentError("client_not_registered", f"{dotted!r} isn't callable")
    return obj  # type: ignore[no-any-return]


class Worker:
    """Claims authorized intents, checks them, and replays them on the clients you registered.

    Build one with ``outis.worker(...)``. It never runs anything the operators
    didn't approve byte for byte, and never runs a claim twice.
    """

    def __init__(
        self,
        outis: "Outis",
        *,
        clients: Mapping[str, Any],
        handlers: Mapping[str, Handler],
        allow: Optional[Sequence[str]],
        concurrency: int,
        on_result: Optional[ResultHook],
        lease: Duration,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency is at least 1")
        if not outis.intent_keys:
            raise ValueError("a worker needs intent keys: pass intent_key or set OUTIS_INTENT_KEYS")
        if not clients and not handlers:
            raise ValueError("register at least one client or handler")
        self._outis = outis
        self._clients = {name: c if isinstance(c, WorkerClient) else WorkerClient(c) for name, c in clients.items()}
        self._handlers = dict(handlers)
        self._allow = list(allow) if allow is not None else None
        self._concurrency = concurrency
        self._on_result = on_result
        self._lease = lease
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def _verify(self, req: OutisRequest) -> _intent.Call:
        if not req.intent:
            raise IntentError("no_intent", "the request carries no intent")
        plaintext = _intent.open_envelope(self._outis.intent_keys, req.action, req.intent)
        if req.params.get(_intent.DIGEST_PARAM) != _intent.digest(plaintext):
            raise IntentError("digest_mismatch", "the intent isn't the one the operators approved")
        if req.operation_hash != operation_hash(req.action, req.params):
            raise IntentError("operation_mismatch", "the operation hash doesn't cover these params")
        if not req.is_authorized:
            raise IntentError("not_authorized", f"the outcome is {req.outcome or 'pending'}")
        call = _intent.parse(plaintext)
        if call.target not in self._handlers and call.client not in self._clients:
            raise IntentError("client_not_registered", f"no client is registered as {call.client!r}")
        if self._allow is not None and not any(fnmatch.fnmatchcase(call.target, p) for p in self._allow):
            raise IntentError("not_allowed", f"{call.target} isn't in allow")
        return call

    def _invoke(self, call: _intent.Call, ctx: ExecutionContext) -> Any:
        handler = self._handlers.get(call.target)
        if handler is not None:
            fn: Callable[..., Any] = handler
            args: tuple[Any, ...] = (ctx, *call.args)
            kwargs = dict(call.kwargs)
        else:
            registered = self._clients[call.client]
            fn = _resolve(registered.target, call.method)
            args = tuple(call.args)
            kwargs = dict(call.kwargs)
            if registered.idempotency == "stripe":
                _stripe_idempotency(fn, kwargs, ctx.idempotency_key)
        try:
            inspect.signature(fn).bind(*args, **kwargs)
        except TypeError as exc:
            raise IntentError("bad_args", f"{call.target}: {exc}") from None
        except ValueError:
            pass  # No introspectable signature; let the call itself decide.
        return fn(*args, **kwargs)

    def execute(self, request_id: str) -> ExecutionResult:
        """Claim, verify and run one request, then report how it went.

        For engines (a Temporal activity, a Celery task). A request somebody
        else holds, one already reported, or one this worker is already running
        comes back ``skipped``.
        """
        with self._lock:
            if request_id in self._in_flight:
                return self._finish(ExecutionResult(request_id, "skipped", reason="in_flight"))
            self._in_flight.add(request_id)
        try:
            return self._execute(request_id)
        finally:
            with self._lock:
                self._in_flight.discard(request_id)

    def _execute(self, request_id: str) -> ExecutionResult:
        try:
            claim = self._outis.requests.claim(request_id, lease=self._lease)
        except APIError as exc:
            if exc.status in (409, 410) or exc.code in _SKIP_CODES:
                reason = exc.code or f"http_{exc.status}"
                return self._finish(ExecutionResult(request_id, "skipped", reason=reason, error=exc.message))
            raise
        req = claim.request
        try:
            call = self._verify(req)
        except IntentError as exc:
            return self._report(claim.claim_id, ExecutionResult(request_id, "failed", reason=exc.reason, error=str(exc)))
        ctx = ExecutionContext(request_id, req, claim.claim_id, call.client, call.method)
        try:
            value = self._invoke(call, ctx)
        except IntentError as exc:
            result = ExecutionResult(request_id, "failed", reason=exc.reason, error=str(exc), context=ctx)
        except Exception as exc:
            result = ExecutionResult(
                request_id, "failed", reason="execution_error",
                error=f"execution_error: {str(exc) or type(exc).__name__}", context=ctx
            )
        else:
            result = ExecutionResult(request_id, "succeeded", reference=_reference(value), result=value, context=ctx)
        return self._report(claim.claim_id, result)

    def _report(self, claim_id: str, result: ExecutionResult) -> ExecutionResult:
        reported = True
        try:
            self._outis.requests.report(
                result.request_id,
                claim_id=claim_id,
                status=result.status,
                reference=result.reference,
                error=result.error,
            )
        except OutisError:
            # The call already ran; after the lease another worker may retry it,
            # which the downstream idempotency key makes harmless.
            log.exception("outis: couldn't report %s for %s", result.status, result.request_id)
            reported = False
        return self._finish(
            ExecutionResult(
                result.request_id,
                result.status,
                reason=result.reason,
                reference=result.reference,
                error=result.error,
                result=result.result,
                context=result.context,
                reported=reported,
            )
        )

    def _finish(self, result: ExecutionResult) -> ExecutionResult:
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:
                log.exception("outis: on_result raised for %s", result.request_id)
        return result

    def _safe_execute(self, request_id: str) -> None:
        try:
            self.execute(request_id)
        except Exception:
            log.exception("outis: executing %s failed", request_id)

    def run(self, *, every: Duration = 15, stop: Optional[threading.Event] = None) -> None:
        """Poll for executable requests and run them until ``stop`` is set.

        Up to ``concurrency`` run at once. On stop it finishes what it started,
        then returns.
        """
        interval = parse_duration(every)
        if interval <= 0:
            raise ValueError("every must be positive")
        stop = stop or threading.Event()
        limit = min(100, self._concurrency * 4)
        with ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="outis-worker") as pool:
            while not stop.is_set():
                try:
                    batch = self._outis.requests.list_executable(limit=limit)
                except OutisError:
                    log.exception("outis: listing executable requests failed")
                    batch = []
                wait([pool.submit(self._safe_execute, r.id) for r in batch])
                if len(batch) < limit:
                    stop.wait(interval)

    def poll(self, *, limit: Optional[int] = None) -> list[ExecutionResult]:
        """List what's executable once, run it ``concurrency`` at a time, and return the results.

        For cron jobs and serverless hosts that can't keep a loop running.
        """
        batch = self._outis.requests.list_executable(limit=limit or min(100, self._concurrency * 4))
        with ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="outis-worker") as pool:
            futures = [pool.submit(self.execute, r.id) for r in batch]
        results: list[ExecutionResult] = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception:
                log.exception("outis: executing a polled request failed")
        return results

    def start(self, *, every: Duration = 15) -> None:
        """Run the poll loop until SIGTERM or SIGINT, then finish in-flight calls and return.

        Signal handlers are installed only on the main thread; a second signal
        gets the previous handler (Ctrl-C twice exits at once).
        """
        stop = threading.Event()
        previous: dict[int, Any] = {}

        def drain(signum: int, _frame: Any) -> None:
            log.info("outis: draining the worker after signal %d", signum)
            stop.set()
            for sig, handler in previous.items():
                signal.signal(sig, handler)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, drain)
        try:
            self.run(every=every, stop=stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def handle_webhook(
        self, raw_body: bytes, headers: Any, secret: Union[Secret, Sequence[Secret]]
    ) -> tuple[int, dict[str, Any]]:
        """Verify an Outis event and execute it if it's an authorized intent.

        Returns ``(status, body)`` for any framework's view: 400 on a bad
        signature, 503 if Outis couldn't be reached (it redelivers), else 200.
        """
        try:
            event = self._outis.webhooks.verify(raw_body, headers, secret)
        except WebhookVerificationError as exc:
            return 400, {"error": str(exc)}
        if event.type != "request.authorized" or not event.request.intent:
            return 200, {"status": "ignored"}
        try:
            result = self.execute(event.request.id)
        except OutisError as exc:
            log.exception("outis: executing %s from a webhook failed", event.request.id)
            return 503, {"error": str(exc)}
        return 200, {"status": result.status, "reason": result.reason}

    def wsgi_app(self, secret: Union[Secret, Sequence[Secret]]) -> Callable[..., Iterable[bytes]]:
        """A WSGI app that answers Outis webhooks. Mount it at your webhook path."""

        def app(environ: Mapping[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
            if environ.get("REQUEST_METHOD") != "POST":
                return _wsgi_reply(start_response, 405, {"error": "POST only"})
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            body = environ["wsgi.input"].read(length) if length > 0 else b""
            headers = {k[5:].replace("_", "-"): str(v) for k, v in environ.items() if k.startswith("HTTP_")}
            status, payload = self.handle_webhook(body, headers, secret)
            return _wsgi_reply(start_response, status, payload)

        return app

    def asgi_app(self, secret: Union[Secret, Sequence[Secret]]) -> Callable[..., Awaitable[None]]:
        """An ASGI app that answers Outis webhooks, for ``app.mount(...)`` in FastAPI or Starlette.

        Execution runs in a thread so it doesn't block the event loop.
        """

        async def app(
            scope: Mapping[str, Any],
            receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
            send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
        ) -> None:
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            if scope["type"] != "http":
                return
            if scope.get("method") != "POST":
                await _asgi_reply(send, 405, {"error": "POST only"})
                return
            chunks: list[bytes] = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
            status, payload = await asyncio.to_thread(self.handle_webhook, b"".join(chunks), headers, secret)
            await _asgi_reply(send, status, payload)

        return app


_REASONS = {200: "OK", 400: "Bad Request", 405: "Method Not Allowed", 503: "Service Unavailable"}


def _json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _wsgi_reply(start_response: Callable[..., Any], status: int, payload: Mapping[str, Any]) -> list[bytes]:
    data = _json(payload)
    start_response(
        f"{status} {_REASONS.get(status, '')}".strip(),
        [("Content-Type", "application/json"), ("Content-Length", str(len(data)))],
    )
    return [data]


async def _asgi_reply(
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]], status: int, payload: Mapping[str, Any]
) -> None:
    data = _json(payload)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": data})
