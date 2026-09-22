"""HTTP boundary tests.

The premise these defend: binding to loopback is not an access control. Any
process on the machine can reach 127.0.0.1, and any page the operator opens can
POST to it from their browser. So the shell must hand out nothing, the tokens
must be distinguishable by capability, and cross-origin callers must bounce.
"""

from __future__ import annotations

import pytest
import win32gui
from fastapi.testclient import TestClient

from gamelens.app import Dispatch
from gamelens.server import Tokens, create_app


class FakeSafety:
    def __init__(self) -> None:
        self.armed = False
        self.live = False
        self.killed = False
        self.dry_run = True

    def arm(self) -> None:
        self.armed = True

    def go_live(self) -> None:
        self.dry_run = False

    def kill(self, reason: str = "") -> None:
        self.killed = True

    def snapshot(self) -> dict:
        return {"armed": self.armed, "killed": self.killed, "dry_run": self.dry_run}


class FakeRuntime:
    """Just enough runtime for the HTTP surface; capture is not the subject here."""

    port = 8777

    def __init__(self) -> None:
        self.tokens = Tokens()
        self.safety = FakeSafety()
        self.log = type("L", (), {
            "add": lambda self, *a, **k: 1,
            "mark": lambda self, *a, **k: None,
            "resolve": lambda self, *a, **k: None,
            "entries": lambda self: [],
            "marks": lambda self: [],
        })()
        self.submitted: list = []
        self.next_dispatch = Dispatch("ok", "sent", "", 1)

    def state(self) -> dict:
        return {"safety": self.safety.snapshot(), "capture": {}, "agent": {}}

    def encode_latest(self, quality: int = 70):
        return None, ""

    def submit_click(self, **kwargs) -> Dispatch:
        self.submitted.append(kwargs)
        return self.next_dispatch


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def client(runtime: FakeRuntime) -> TestClient:
    # base_url matters: the app checks Host against an allowlist, and
    # TestClient's default "testserver" is (correctly) not on it.
    return TestClient(create_app(runtime), base_url="http://127.0.0.1:8777")


def op(runtime: FakeRuntime) -> dict:
    return {"X-GameLens-Token": runtime.tokens.operator}


def agent(runtime: FakeRuntime) -> dict:
    return {"X-GameLens-Token": runtime.tokens.agent}


# --- GL-007: the shell must not be a credential dispenser ------------------


def test_shell_leaks_no_credential(client, runtime):
    """An unauthenticated GET / is allowed -- but must hand out nothing usable."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert runtime.tokens.operator not in body
    assert runtime.tokens.agent not in body


def test_state_requires_a_token(client):
    assert client.get("/state").status_code == 401


def test_state_rejects_a_wrong_token(client):
    assert client.get("/state", headers={"X-GameLens-Token": "nope"}).status_code == 401


def test_state_accepts_either_token(client, runtime):
    assert client.get("/state", headers=op(runtime)).status_code == 200
    assert client.get("/state", headers=agent(runtime)).status_code == 200


# --- capability separation -------------------------------------------------


def test_agent_token_cannot_arm(client, runtime):
    response = client.post("/arm", headers=agent(runtime))
    assert response.status_code == 403
    assert not runtime.safety.armed, "an agent credential must never arm the system"


def test_agent_token_cannot_go_live(client, runtime):
    assert client.post("/live", headers=agent(runtime)).status_code == 403
    assert runtime.safety.dry_run is True


def test_agent_token_cannot_stop(client, runtime):
    assert client.post("/stop", headers=agent(runtime)).status_code == 403


def test_operator_token_can_arm(client, runtime):
    assert client.post("/arm", headers=op(runtime)).status_code == 200
    assert runtime.safety.armed


def test_agent_token_can_act(client, runtime):
    response = client.post(
        "/act", headers=agent(runtime),
        json={"observation_id": "abc123", "x": 10, "y": 20},
    )
    assert response.status_code == 200
    assert runtime.submitted and runtime.submitted[0]["source"] == "agent"
    assert response.json()["outcome"] == "sent"


# --- GL-034: acceptance is not execution -----------------------------------


def test_act_reports_a_press_time_refusal_not_ok(client, runtime):
    """An action the arbiter accepted can still be refused at press time.

    This is the defect that cost a whole debugging session: the driver printed
    "ok" for clicks the game never received, because "ok" described the queue.
    A caller must be able to tell the two apart from the response alone.
    """
    runtime.next_dispatch = Dispatch("ok", "denied", "target window is not in the foreground", 7)
    response = client.post(
        "/act", headers=agent(runtime),
        json={"observation_id": "abc123", "x": 10, "y": 20},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["verdict"] == "ok", "the arbiter did accept it"
    assert body["outcome"] == "denied", "the executor did not press it"
    assert "foreground" in body["detail"]


def test_act_reports_pending_rather_than_guessing(client, runtime):
    """A caller that outlasts the wait is told so, never told it succeeded."""
    runtime.next_dispatch = Dispatch("ok", "pending", "", 8)
    response = client.post(
        "/act", headers=agent(runtime),
        json={"observation_id": "abc123", "x": 10, "y": 20},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "pending"


def test_act_distinguishes_dry_run_from_a_real_press(client, runtime):
    runtime.next_dispatch = Dispatch("ok", "dry", "", 9)
    response = client.post(
        "/act", headers=agent(runtime),
        json={"observation_id": "abc123", "x": 10, "y": 20},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "dry", "nothing reached the game"


def test_act_requires_an_observation_id(client, runtime):
    """A coordinate with no observation is meaningless: the server cannot know
    which picture the caller was looking at, or at what scale."""
    response = client.post("/act", headers=agent(runtime), json={"x": 10, "y": 20})
    assert response.status_code == 400
    assert not runtime.submitted


def test_windows_listing_is_operator_only(client, runtime):
    assert client.get("/windows", headers=agent(runtime)).status_code == 403
    assert client.get("/windows", headers=op(runtime)).status_code == 200


# --- browser-origin defences -----------------------------------------------


def test_foreign_origin_is_refused(client, runtime):
    response = client.post(
        "/arm", headers={**op(runtime), "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert not runtime.safety.armed


def test_same_origin_is_allowed(client, runtime):
    response = client.post(
        "/arm", headers={**op(runtime), "Origin": "http://127.0.0.1:8777"},
    )
    assert response.status_code == 200


def test_unexpected_host_is_refused(client, runtime):
    response = client.get("/state", headers={**op(runtime), "Host": "gamelens.attacker.test"})
    assert response.status_code == 403


def test_absent_origin_is_allowed(client, runtime):
    """curl and same-origin fetches send no Origin; that is normal, not hostile."""
    assert client.get("/state", headers=op(runtime)).status_code == 200


# --- stream credential -----------------------------------------------------


def test_query_token_is_accepted(client, runtime):
    """<img> cannot set a header, so the same secret is accepted as a query.

    Asserted against /state rather than /stream.mjpg: the stream is an endless
    generator by design, so consuming it in a test would simply never return.
    The credential path is identical -- one dependency serves both.
    """
    assert client.get(f"/state?token={runtime.tokens.operator}").status_code == 200
    assert client.get("/state?token=wrong").status_code == 401


def test_stream_rejects_a_missing_token(client):
    assert client.get("/stream.mjpg").status_code == 401
