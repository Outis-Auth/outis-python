"""Sealed intents: the call a worker replays, encrypted under a key only the customer holds."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional, Union

from .errors import IntentError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD_PREFIX = b"outis.intent.v1\x00"
ALG = "A256GCM"
DIGEST_PARAM = "intent"

KeyInput = Union[str, bytes]


def _aesgcm(key: bytes) -> "AESGCM":
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:  # pragma: no cover
        raise ImportError(
            "sealed intents need the cryptography package: pip install 'outis[durable]'"
        ) from None
    return AESGCM(key)


def _b64decode(text: str) -> bytes:
    s = text.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def load_key(value: KeyInput) -> bytes:
    """Return the 32 key bytes from raw bytes or base64 (standard or URL alphabet)."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        try:
            raw = _b64decode(value)
        except (binascii.Error, ValueError):
            raise ValueError("an intent key is base64 of 32 random bytes") from None
    if len(raw) != 32:
        raise ValueError(f"an intent key is 32 bytes, got {len(raw)}")
    return raw


def load_keys(value: Union[KeyInput, Sequence[KeyInput], None]) -> list[bytes]:
    """Parse one key or a list; with None, read ``OUTIS_INTENT_KEYS`` then ``OUTIS_INTENT_KEY``."""
    if value is None:
        env = os.environ.get("OUTIS_INTENT_KEYS") or os.environ.get("OUTIS_INTENT_KEY") or ""
        value = [part for part in env.split(",") if part.strip()]
    if isinstance(value, (str, bytes, bytearray)):
        value = [value]
    return [load_key(v) for v in value]


def key_id(key: bytes) -> str:
    """The first 16 hex characters of SHA-256 of the key: names a key without revealing it."""
    return hashlib.sha256(key).hexdigest()[:16]


def digest(plaintext: bytes) -> str:
    return "sha256:" + hashlib.sha256(plaintext).hexdigest()


def generate_key() -> str:
    """A fresh intent key, base64url, for ``OUTIS_INTENT_KEY``."""
    return _b64url(secrets.token_bytes(32))


def _plain(value: Any, where: str) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{where} is {value}, which JSON can't carry")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"{where} has a non-string key {k!r}")
            out[k] = _plain(v, f"{where}.{k}")
        return out
    raise TypeError(
        f"{where} is a {type(value).__name__}; intent arguments must be plain JSON data "
        "(dicts, lists, strings, numbers, bools, None)"
    )


def encode(client: str, method: str, args: Sequence[Any], kwargs: Optional[Mapping[str, Any]] = None) -> bytes:
    """Serialize a call once, as the compact UTF-8 JSON the digest covers.

    ``kwargs`` is a Python extension to the shared format, written only when
    non-empty; workers in other languages don't replay it.
    """
    if not client or not method:
        raise ValueError("an intent names a client and a method")
    body: dict[str, Any] = {"v": 1, "client": client, "method": method, "args": _plain(list(args), "args")}
    if kwargs:
        body["kwargs"] = _plain(dict(kwargs), "kwargs")
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def seal(key: bytes, action: str, plaintext: bytes, *, nonce: Optional[bytes] = None) -> dict[str, Any]:
    """Encrypt ``plaintext`` for ``action`` and return the envelope sent to Outis."""
    nonce = secrets.token_bytes(12) if nonce is None else nonce
    if len(nonce) != 12:
        raise ValueError("the nonce is 12 bytes")
    ct = _aesgcm(key).encrypt(nonce, plaintext, AAD_PREFIX + action.encode("utf-8"))
    return {"v": 1, "alg": ALG, "kid": key_id(key), "nonce": _b64url(nonce), "ciphertext": _b64url(ct)}


def open_envelope(keys: Sequence[bytes], action: str, envelope: Mapping[str, Any]) -> bytes:
    """Decrypt an envelope with whichever key its ``kid`` names. Raises :class:`IntentError`."""
    if not isinstance(envelope, Mapping) or envelope.get("v") != 1 or envelope.get("alg") != ALG:
        raise IntentError("bad_intent", "the intent envelope isn't v1 A256GCM")
    kid = envelope.get("kid")
    key = next((k for k in keys if key_id(k) == kid), None)
    if key is None:
        raise IntentError("unknown_key", f"no intent key has kid {kid!r}")
    try:
        nonce = _b64decode(str(envelope["nonce"]))
        ct = _b64decode(str(envelope["ciphertext"]))
    except (KeyError, binascii.Error, ValueError):
        raise IntentError("bad_intent", "the intent envelope isn't base64url") from None
    aes = _aesgcm(key)
    from cryptography.exceptions import InvalidTag

    try:
        return aes.decrypt(nonce, ct, AAD_PREFIX + action.encode("utf-8"))
    except (InvalidTag, ValueError):
        raise IntentError("decrypt_failed", "the intent didn't decrypt for this action and key") from None


@dataclass(frozen=True)
class Call:
    """A decrypted intent: which registered client, which dotted method, and its arguments."""

    client: str
    method: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return f"{self.client}.{self.method}"


_FIELDS = frozenset({"v", "client", "method", "args", "kwargs"})


def parse(plaintext: bytes) -> Call:
    try:
        body = json.loads(plaintext)
    except ValueError:
        raise IntentError("bad_intent", "the intent isn't JSON") from None
    if not isinstance(body, dict) or body.get("v") != 1:
        raise IntentError("bad_intent", "the intent isn't v1")
    unknown = sorted(set(body) - _FIELDS)
    if unknown:
        raise IntentError("bad_intent", f"unknown field {unknown[0]}")
    client, method, args = body.get("client"), body.get("method"), body.get("args", [])
    kwargs = body.get("kwargs") or {}
    if not isinstance(client, str) or not isinstance(method, str) or not client or not method:
        raise IntentError("bad_intent", "the intent has no client or method")
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise IntentError("bad_intent", "the intent's args aren't a list")
    return Call(client, method, tuple(args), kwargs)
