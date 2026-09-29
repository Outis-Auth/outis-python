import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from outis import IntentError, Outis, callback_secret, operation_hash
from outis import _intent

VECTORS = json.loads((Path(__file__).parent.parent / "intent-vectors.json").read_text())
KEY = bytes.fromhex(VECTORS["key_hex"])
NONCE = bytes.fromhex(VECTORS["nonce_hex"])


def test_vector_keys_parse_from_either_base64_alphabet():
    assert _intent.load_key(VECTORS["key"]) == KEY == _intent.load_key(VECTORS["key_url"])
    assert _intent.key_id(KEY) == VECTORS["kid"]


@pytest.mark.parametrize("vector", VECTORS["intents"], ids=lambda v: v["action"])
def test_vectors_decrypt_and_reseal_byte_for_byte(vector):
    plaintext = vector["plaintext"].encode("utf-8")
    assert _intent.open_envelope([KEY], vector["action"], vector["envelope"]) == plaintext
    assert _intent.seal(KEY, vector["action"], plaintext, nonce=NONCE) == vector["envelope"]
    assert _intent.digest(plaintext) == vector["digest"] == vector["params"]["intent"]
    assert operation_hash(vector["action"], vector["params"]) == vector["operation_hash"]
    assert (_intent.AAD_PREFIX + vector["action"].encode()).hex() == vector["aad_hex"]


@pytest.mark.parametrize("vector", VECTORS["intents"], ids=lambda v: v["action"])
def test_encode_reproduces_the_vector_plaintext(vector):
    call = _intent.parse(vector["plaintext"].encode("utf-8"))
    assert _intent.encode(call.client, call.method, call.args) == vector["plaintext"].encode("utf-8")


def test_vector_callback_secrets():
    for v in VECTORS["callback_secret"]:
        assert callback_secret(v["api_key"]).hex() == v["secret_hex"]


def test_decrypt_is_bound_to_the_action_and_key():
    v = VECTORS["intents"][0]
    with pytest.raises(IntentError) as err:
        _intent.open_envelope([KEY], "db.restore", v["envelope"])
    assert err.value.reason == "decrypt_failed"
    with pytest.raises(IntentError) as err:
        _intent.open_envelope([bytes(32)], v["action"], v["envelope"])
    assert err.value.reason == "unknown_key"
    tampered = {**v["envelope"], "ciphertext": "A" + v["envelope"]["ciphertext"][1:]}
    with pytest.raises(IntentError) as err:
        _intent.open_envelope([KEY], v["action"], tampered)
    assert err.value.reason == "decrypt_failed"
    with pytest.raises(IntentError) as err:
        _intent.open_envelope([KEY], v["action"], {**v["envelope"], "alg": "none"})
    assert err.value.reason == "bad_intent"


def test_rotation_finds_the_key_by_kid():
    v = VECTORS["intents"][0]
    assert _intent.open_envelope([bytes(32), KEY], v["action"], v["envelope"]) == v["plaintext"].encode()


def test_encode_accepts_plain_data_and_refuses_the_rest():
    when = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    out = json.loads(_intent.encode("db", "restore", ("payments", {"at": when, "ids": (1, 2)}), {"dry_run": True}))
    assert out == {"v": 1, "client": "db", "method": "restore",
                   "args": ["payments", {"at": "2026-09-27T12:00:00+00:00", "ids": [1, 2]}],
                   "kwargs": {"dry_run": True}}
    for bad in (Decimal("1.5"), float("nan"), object(), {1: "x"}, lambda: 1):
        with pytest.raises(TypeError):
            _intent.encode("db", "restore", [bad])


def test_keys_come_from_the_environment(monkeypatch):
    a, b = _intent.generate_key(), _intent.generate_key()
    monkeypatch.setenv("OUTIS_INTENT_KEYS", f"{a}, {b}")
    monkeypatch.setenv("OUTIS_INTENT_KEY", "ignored")
    assert Outis(api_key="k").intent_keys == [_intent.load_key(a), _intent.load_key(b)]
    monkeypatch.delenv("OUTIS_INTENT_KEYS")
    monkeypatch.setenv("OUTIS_INTENT_KEY", a)
    assert Outis(api_key="k").intent_keys == [_intent.load_key(a)]
    with pytest.raises(ValueError):
        Outis(api_key="k", intent_key="too-short")


def test_propose_seals_the_call(fake, monkeypatch):
    monkeypatch.delenv("OUTIS_INTENT_KEYS", raising=False)
    client = Outis(api_key="k", base_url=fake.base_url, intent_key=KEY)
    pending = client.intents.propose(action="db.restore", requester="keith", client="db", method="restore",
                                     args=["payments"], params={"db": "payments"}, idempotency_key="restore-1")
    body = fake.log[0]["body"]
    plaintext = _intent.open_envelope([KEY], "db.restore", body["intent"])
    assert plaintext == b'{"v":1,"client":"db","method":"restore","args":["payments"]}'
    assert body["params"] == {"db": "payments", "intent": _intent.digest(plaintext)}
    assert body["execute_within"] == 7 * 86400
    assert pending.intent_digest == body["params"]["intent"] and pending.intent.method == "db.restore"
    with pytest.raises(ValueError, match="reserved"):
        client.intents.propose(action="a", requester="k", client="db", method="m", params={"intent": "x"})


def test_propose_without_a_key_says_so(fake, monkeypatch):
    monkeypatch.delenv("OUTIS_INTENT_KEYS", raising=False)
    monkeypatch.delenv("OUTIS_INTENT_KEY", raising=False)
    with pytest.raises(ValueError, match="OUTIS_INTENT_KEY"):
        Outis(api_key="k", base_url=fake.base_url).intents.propose(action="a", requester="k", client="c", method="m")


def test_parse_refuses_unknown_fields() -> None:
    from outis._intent import IntentError, parse

    with pytest.raises(IntentError) as err:
        parse(b'{"v":1,"client":"stripe","method":"transfers.create","args":[],"extra":1}')
    assert "unknown field extra" in str(err.value)
