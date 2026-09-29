import json
from datetime import timedelta
from pathlib import Path

import pytest

from outis import operation_hash, parse_duration

VECTORS = json.loads((Path(__file__).parent.parent / "vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["operation_hash"], ids=lambda c: c["hash"][7:19])
def test_operation_hash_vectors(case):
    assert operation_hash(case["action"], case["params"]) == case["hash"]


def test_operation_hash_ignores_param_order_and_treats_none_as_empty():
    assert operation_hash("a", {"x": "1", "y": "2"}) == operation_hash("a", {"y": "2", "x": "1"})
    assert operation_hash("deploy.production") == VECTORS["operation_hash"][0]["hash"]


def test_operation_hash_rejects_non_string_values():
    with pytest.raises(TypeError):
        operation_hash("a", {"amount": 5})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "value, seconds",
    [(30, 30.0), (1.5, 1.5), ("30s", 30.0), ("5m", 300.0), ("1h30m", 5400.0), ("250ms", 0.25), ("7d", 604800.0),
     (timedelta(minutes=2), 120.0)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "5", "five minutes", "5w"])
def test_parse_duration_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_duration(value)
