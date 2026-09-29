from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import errors

USER_AGENT = "outis-python/0.1.0"


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: Any


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _api_error(status: int, headers: Mapping[str, str], body: Any) -> errors.APIError:
    message, code = f"HTTP {status}", None
    if isinstance(body, dict):
        message = str(body.get("error") or message)
        code = body.get("kind") or body.get("code")
    kwargs: dict[str, Any] = {"status": status, "code": code, "body": body}
    if status == 401:
        return errors.AuthenticationError(message, **kwargs)
    if status == 403:
        return errors.PermissionDeniedError(message, **kwargs)
    if status == 404:
        return errors.NotFoundError(message, **kwargs)
    if status == 409 and code == "idempotency_conflict":
        return errors.IdempotencyConflictError(message, **kwargs)
    if status == 429:
        return errors.RateLimitError(message, retry_after=_retry_after(headers), **kwargs)
    return errors.APIError(message, **kwargs)


class Transport:
    """JSON over ``urllib.request``, with bounded retries for calls that are safe to repeat."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        http_timeout: float,
        max_retries: int,
        sleep: Callable[[float], None],
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_timeout = http_timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self._rng = rng

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        retry: bool,
    ) -> Response:
        attempt = 0
        while True:
            try:
                return self._once(method, path, body, headers or {})
            except (errors.APIConnectionError, errors.APIError) as exc:
                retryable = isinstance(exc, errors.APIConnectionError) or (
                    isinstance(exc, errors.APIError) and (exc.status == 429 or exc.status >= 500)
                )
                if not retry or not retryable or attempt >= self._max_retries:
                    raise
                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = min(8.0, 0.5 * (2**attempt)) * (0.5 + self._rng() / 2)
                self._sleep(delay)
                attempt += 1

    def _once(self, method: str, path: str, body: Any, extra: Mapping[str, str]) -> Response:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in extra.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), _decode(resp.read()))
        except urllib.error.HTTPError as exc:
            headers = dict(exc.headers.items()) if exc.headers else {}
            raise _api_error(exc.code, headers, _decode(exc.read())) from None
        except (urllib.error.URLError, OSError) as exc:
            raise errors.APIConnectionError(f"couldn't reach {self._base_url}: {exc}") from exc


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")
