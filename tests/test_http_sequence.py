"""GL-039 at the HTTP edge: what /act does with a sequence and with rebind, and
what /state now says about capture.

The rule carried over from the other kinds: a malformed request fails with 400
*before* anything reaches the runtime, because a raise after injection turns a
delivered keystroke into an error and invites a retry that sends it twice.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gamelens.app import Dispatch, GameLens
from gamelens.capture import Backend, CaptureSupervisor
from gamelens.input import KeyDown, KeyUp, key_code
from gamelens.server import create_app
from tests.test_http_auth import FakeRuntime, agent


class SeqRuntime(FakeRuntime):
    def __init__(self, capacity: float = 10.0) -> None:
        super().__init__()
        self.capacity = capacity

    def sequence_capacity(self) -> float:
        return self.capacity

    def submit_sequence(self, **kwargs) -> Dispatch:
        self.submitted.append(("sequence", kwargs))
        return self.next_dispatch

    def submit_key(self, **kwargs) -> Dispatch:
        self.submitted.append(("key", kwargs))
        return self.next_dispatch


@pytest.fixture
def runtime() -> SeqRuntime:
    return SeqRuntime()


@pytest.fixture
def client(runtime) -> TestClient:
    return TestClient(create_app(runtime), base_url="http://127.0.0.1:8777")


def act(client, runtime, body):
    return client.post("/act", headers=agent(runtime),
                       json={"observation_id": "obs", **body})


def test_a_sequence_reaches_the_runtime_parsed(client, runtime):
    r = act(client, runtime, {"kind": "sequence", "steps": [
        {"do": "key_down", "key": "w"}, {"do": "wait", "ms": 100}]})
    assert r.status_code == 200, r.text
    kind, kwargs = runtime.submitted[-1]
    assert kind == "sequence"
    w = key_code("w")
    assert kwargs["steps"][0] == KeyDown(w) and kwargs["steps"][-1] == KeyUp(w)
    assert "rebind" not in kwargs


@pytest.mark.parametrize("steps", [
    None,
    [],
    [{"do": "key_down", "key": "alt"}, {"do": "tap", "key": "tab"}],
    [{"do": "wait", "ms": 100}],
    [{"do": "tap", "key": "w"}, {"do": "wait", "ms": 10**400}],
    [{"do": "look", "dx": 1, "dy": 0}] * 11,        # over the capacity of 10
])
def test_a_bad_sequence_is_a_400_and_nothing_is_submitted(client, runtime, steps):
    r = act(client, runtime, {"kind": "sequence", "steps": steps})
    assert r.status_code == 400, r.text
    assert runtime.submitted == []


def test_rebind_true_is_passed_through(client, runtime):
    r = act(client, runtime, {"kind": "key", "key": "w", "rebind": True})
    assert r.status_code == 200
    assert runtime.submitted[-1][1]["rebind"] is True


def test_rebind_false_or_absent_is_not(client, runtime):
    act(client, runtime, {"kind": "key", "key": "w", "rebind": False})
    act(client, runtime, {"kind": "key", "key": "w"})
    assert all("rebind" not in kw for _, kw in runtime.submitted)


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [True]])
def test_rebind_must_be_a_real_boolean(client, runtime, value):
    """"false" is a truthy string; a flag that loosens binding must not turn on
    by accident."""
    r = act(client, runtime, {"kind": "key", "key": "w", "rebind": value})
    assert r.status_code == 400
    assert runtime.submitted == []


def test_binding_details_reach_the_caller(client, runtime):
    runtime.next_dispatch = Dispatch("SCREEN_CHANGED", "denied", "look again",
                                     binding={"bound_to": None, "patch_max": 90})
    r = act(client, runtime, {"kind": "key", "key": "w", "rebind": True})
    assert r.status_code == 409
    assert r.json()["bound_to"] is None and r.json()["patch_max"] == 90


# --- GL039-R7: /state says which capture session, and whether failover is off --


def _supervisor(forced):
    import threading

    sup = object.__new__(CaptureSupervisor)
    sup._lock = threading.Lock()
    sup._backend = type("B", (), {
        "kind": Backend.PRINTWINDOW, "session_id": 7, "distinct": 1, "duplicates": 0,
        "error": None, "healthy": lambda self, d: True, "publish_rate": lambda self: 60.0,
    })()
    sup.frames = type("F", (), {"acquire": lambda self, timeout=None: None})()
    sup.pool = type("P", (), {"stats": lambda self: {"exhausted": 0}})()
    sup.transitions = []
    sup.deadline = 0.5
    sup._forced = forced
    return sup


def _lens(forced) -> GameLens:
    from gamelens.app import ObservationRegistry
    from gamelens.safety import Denial

    lens = object.__new__(GameLens)
    lens.capture = _supervisor(forced)
    lens._frame_ages = []
    lens.target = type("T", (), {"hwnd": 0})()
    lens.agent = None
    lens._agent_intent = ""
    lens.safety = type("S", (), {"snapshot": lambda self: {},
                                 "check": lambda self, **kw: Denial.NOT_ARMED})()
    lens.executor = type("E", (), {"snapshot": lambda self: {}})()
    lens.arbiter = type("A", (), {"stats": lambda self: {}})()
    lens.log = type("L", (), {"marks": lambda self: [], "entries": lambda self: []})()
    lens.observations = ObservationRegistry()
    return lens


@pytest.mark.parametrize("forced,expected", [
    (Backend.PRINTWINDOW, "printwindow"), (Backend.WGC, "wgc"), (None, None)])
def test_state_reports_the_session_and_the_forced_backend(forced, expected):
    lens = _lens(forced)
    capture = GameLens.state(lens)["capture"]
    assert capture["session_id"] == 7
    assert capture["forced_backend"] == expected


def test_the_metadata_survives_serialisation_through_state_endpoint():
    lens = _lens(Backend.PRINTWINDOW)
    rt = SeqRuntime()
    rt.state = lambda: GameLens.state(lens)
    client = TestClient(create_app(rt), base_url="http://127.0.0.1:8777")
    body = client.get("/state", headers=agent(rt)).json()
    assert body["capture"]["session_id"] == 7
    assert body["capture"]["forced_backend"] == "printwindow"
