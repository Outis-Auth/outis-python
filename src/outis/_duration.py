from __future__ import annotations

import re
from datetime import timedelta
from typing import Union

Duration = Union[int, float, str, timedelta]
"""Seconds as a number, a ``timedelta``, or a string like ``"30s"``, ``"5m"``, ``"1h30m"`` or ``"7d"``."""

_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value: Duration) -> float:
    """Return ``value`` in seconds. A bare number is seconds."""
    if isinstance(value, bool):
        raise TypeError("a duration can't be a bool")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text and _PART.sub("", text) == "":
            return sum(float(n) * _UNIT[u] for n, u in _PART.findall(text))
        raise ValueError(f"can't parse duration {value!r}; use something like '30s', '5m' or '1h'")
    raise TypeError(f"unsupported duration type {type(value).__name__}")
