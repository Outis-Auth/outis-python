from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, TypedDict


class Recipe(TypedDict):
    """The action id, what the approvers see, and the worker's dotted ``call``. Spread it into ``guard_method`` with ``**``."""

    action: str
    show_approvers: Callable[..., Mapping[str, str]]
    call: str
