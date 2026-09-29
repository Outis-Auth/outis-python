from __future__ import annotations

import hashlib
from collections.abc import Mapping

_DOMAIN = b"outis.operation.v1\x00"


def operation_hash(action: str, params: Mapping[str, str] | None = None) -> str:
    """Return the operation hash Outis records for ``action`` and ``params``.

    It covers the operation only, not who asked or when, so an executor can
    prove the request it holds authorizes exactly what it's about to run.
    """
    if not isinstance(action, str):
        raise TypeError("action must be a str")
    items: list[tuple[bytes, bytes]] = []
    for key, value in (params or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("params must map str to str")
        items.append((key.encode("utf-8"), value.encode("utf-8")))
    items.sort(key=lambda kv: kv[0])

    h = hashlib.sha256(_DOMAIN)

    def write(b: bytes) -> None:
        h.update(len(b).to_bytes(4, "big"))
        h.update(b)

    write(action.encode("utf-8"))
    for k, v in items:
        write(k)
        write(v)
    return "sha256:" + h.hexdigest()
