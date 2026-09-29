import hashlib
import hmac
import json

import pytest

from outis import Outis, WebhookVerificationError, callback_secret, verify_webhook

SECRET = "whsec_test"
NOW = 1788350400
BODY = json.dumps({
    "id": "evt_1",
    "type": "request.authorized",
    "created_at": "2026-09-01T12:00:00Z",
    "org": "org_1",
    "data": {"request": {"id": "req-1", "action": "a", "state": "authorized", "live": False,
                         "outcome": "authorized", "approvers": ["maya"], "params": {},
                         "created_at": 1, "decided_at": 2}},
}).encode()


def sign(body: bytes, secret: str = SECRET, t: int = NOW) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def test_verify_good():
    event = verify_webhook(BODY, {"Outis-Signature": f"t={NOW},v1={sign(BODY)}"}, SECRET, now=NOW + 10)
    assert event.id == "evt_1" and event.type == "request.authorized" and event.org == "org_1"
    assert event.request.is_authorized and event.data["request"]["id"] == "req-1"


def test_verify_header_names_are_case_insensitive_and_pairs_work():
    headers = [("outis-signature", f"t={NOW},v1={sign(BODY)}")]
    assert verify_webhook(BODY.decode(), headers, SECRET, now=NOW).id == "evt_1"


def test_verify_stale():
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_webhook(BODY, {"Outis-Signature": f"t={NOW},v1={sign(BODY)}"}, SECRET, now=NOW + 301)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(BODY, {"Outis-Signature": f"t={NOW},v1={sign(BODY)}"}, SECRET, now=NOW - 301)


def test_verify_tampered():
    tampered = BODY.replace(b"authorized", b"denied", 1)
    with pytest.raises(WebhookVerificationError, match="match"):
        verify_webhook(tampered, {"Outis-Signature": f"t={NOW},v1={sign(BODY)}"}, SECRET, now=NOW)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(BODY, {"Outis-Signature": f"t={NOW + 1},v1={sign(BODY)}"}, SECRET, now=NOW)


def test_verify_rotated_secret():
    header = f"t={NOW},v1={sign(BODY, 'whsec_old')},v1={sign(BODY, 'whsec_new')}"
    assert verify_webhook(BODY, {"Outis-Signature": header}, "whsec_new", now=NOW).id == "evt_1"
    only_old = f"t={NOW},v1={sign(BODY, 'whsec_old')}"
    assert verify_webhook(BODY, {"Outis-Signature": only_old}, ["whsec_new", "whsec_old"], now=NOW).id == "evt_1"


@pytest.mark.parametrize("header", [None, "", "v1=abc", f"t={NOW}", "t=soon,v1=abc"])
def test_verify_malformed(header):
    headers = {} if header is None else {"Outis-Signature": header}
    with pytest.raises(WebhookVerificationError):
        verify_webhook(BODY, headers, SECRET, now=NOW)


def test_client_verify_uses_its_wall_clock():
    client = Outis(api_key="k", wall_clock=lambda: NOW + 5)
    assert client.webhooks.verify(BODY, {"Outis-Signature": f"t={NOW},v1={sign(BODY)}"}, SECRET).id == "evt_1"


def test_verify_accepts_key_bytes_and_the_callback_secret():
    key = callback_secret("outis_sk_test")
    assert key.hex() == "640d74bb93646ec2c790001a14516d3507d38673bc2b7bfb2917287c5ea1ff2b"
    sig = hmac.new(key, f"{NOW}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert verify_webhook(bytearray(BODY), {"Outis-Signature": f"t={NOW},v1={sig}"}, key, now=NOW).id == "evt_1"
    assert verify_webhook(BODY, {"Outis-Signature": f"t={NOW},v1={sig}"}, [SECRET, key], now=NOW).id == "evt_1"


def test_callback_signature_matches_the_server():
    body = b'{"a":1}'
    sig = hmac.new(callback_secret("outis_sk_test"), b"1788000000." + body, hashlib.sha256).hexdigest()
    assert sig == "056ac9d7cbda9eefbe24d29b063c7dda390567762060a3a88700a77f30551bf8"
