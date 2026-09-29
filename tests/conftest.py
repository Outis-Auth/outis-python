from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest

from outis import Outis, operation_hash


class FakeClock:
    """A monotonic clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeOutis:
    """An in-process stand-in for the Outis API's /v1/requests routes."""

    def __init__(self) -> None:
        self.requests: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, tuple[str, str]] = {}
        self.decide: dict[str, tuple[int, str]] = {}
        self.polls: dict[str, int] = {}
        self.next_outcome: Optional[tuple[int, str]] = None
        self.script: list[tuple[int, Any, dict[str, str]]] = []
        self.log: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.claims: dict[str, str] = {}
        self.reports: list[tuple[str, dict[str, Any]]] = []
        self.holds: dict[str, threading.Event] = {}

    def add(self, rid: str, action: str, params: dict[str, str], *, outcome: Optional[str] = None,
            hash_override: Optional[str] = None) -> dict[str, Any]:
        req = {
            "id": rid,
            "action": action,
            "requester": "keith",
            "state": outcome or "notified",
            "live": outcome is None,
            "outcome": outcome,
            "approvers": ["maya", "sam"] if outcome == "authorized" else [],
            "params": params,
            "operation_hash": hash_override or operation_hash(action, params),
            "created_at": 1788350100000,
            "decided_at": None if outcome is None else 1788350400000,
            "intent": None,
            "execution": {"state": "none"},
        }
        self.requests[rid] = req
        return req

    def handle(self, method: str, path: str, headers: dict[str, str], body: Any) -> tuple[int, Any, dict[str, str]]:
        hold = self.holds.get(body.get("action", "")) if method == "POST" and isinstance(body, dict) else None
        if hold is not None:
            hold.wait(5)
        with self.lock:
            self.log.append({"method": method, "path": path, "headers": headers, "body": body})
            if self.script:
                return self.script.pop(0)
            if method == "POST" and path == "/v1/requests":
                return self._create(headers, body)
            if method == "GET" and path.startswith("/v1/requests?"):
                ready = [r for rid, r in self.requests.items()
                         if r["outcome"] == "authorized" and r.get("intent") and rid not in self.claims]
                return 200, {"requests": ready}, {}
            if method == "POST" and path.endswith("/claim"):
                return self._claim(path.split("/")[3])
            if method == "POST" and path.endswith("/execution"):
                rid = path.split("/")[3]
                self.reports.append((rid, body))
                self.requests[rid]["execution"]["state"] = body["status"]
                return 200, {"request": self.requests[rid]}, {}
            if method == "GET" and path.startswith("/v1/requests/"):
                return self._read(path.rsplit("/", 1)[1])
            return 404, {"error": "no route", "kind": "not_found"}, {}

    def _create(self, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
        h = operation_hash(body["action"], body["params"])
        key = headers.get("idempotency-key")
        if key and key in self.keys:
            prior_hash, rid = self.keys[key]
            if prior_hash != h:
                return 409, {"error": "that key was used for a different operation", "code": "idempotency_conflict"}, {}
            return 202, {"server_now": 1, "request": self.requests[rid]}, {"Idempotent-Replayed": "true"}
        rid = f"req-{len(self.requests) + 1:04d}"
        req = self.add(rid, body["action"], body["params"])
        req["requester"] = body["requester"]
        req["intent"] = body.get("intent")
        req["execute_within"] = body.get("execute_within")
        if key:
            self.keys[key] = (h, rid)
        if self.next_outcome:
            self.decide[rid] = self.next_outcome
        return 202, {"server_now": 1, "request": req}, {}

    def authorize(self, rid: str) -> None:
        self.requests[rid].update(state="authorized", live=False, outcome="authorized", approvers=["maya", "sam"])

    def _claim(self, rid: str) -> tuple[int, Any, dict[str, str]]:
        req = self.requests[rid]
        if req["outcome"] != "authorized":
            return 409, {"error": "not authorized", "kind": "not_authorized"}, {}
        if req["execution"]["state"] in ("succeeded", "failed"):
            return 409, {"error": "reported", "kind": "already_reported"}, {}
        if rid in self.claims:
            return 409, {"error": "claimed", "kind": "already_claimed"}, {}
        self.claims[rid] = f"clm_{len(self.claims) + 1}"
        req["execution"]["state"] = "claimed"
        return 200, {"claim_id": self.claims[rid], "lease_expires_at": 1788350999000, "request": req}, {}

    def _read(self, rid: str) -> tuple[int, Any, dict[str, str]]:
        req = self.requests.get(rid)
        if req is None:
            return 404, {"error": "no such request", "kind": "not_found"}, {}
        self.polls[rid] = self.polls.get(rid, 0) + 1
        if rid in self.decide and self.polls[rid] >= self.decide[rid][0]:
            outcome = self.decide[rid][1]
            req.update(state=outcome, live=False, outcome=outcome, decided_at=1788350400000)
            if outcome == "authorized":
                req["approvers"] = ["maya", "sam"]
        return 200, {"server_now": 1, "request": req}, {}


@pytest.fixture
def fake() -> Any:
    state = FakeOutis()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload, extra = state.handle(self.command, self.path, headers, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(fake: FakeOutis, clock: FakeClock) -> Outis:
    return Outis(api_key="ok_test_123", base_url=fake.base_url, clock=clock, sleep=clock.sleep, rng=lambda: 0.5)
