"""Recorder HTTP capabilities using an isolated synthetic capture source."""
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gamelens.recording import Recorder
from gamelens.server import Tokens, create_app


class Runtime:
    port = 8777

    def __init__(self, directory):
        self.tokens = Tokens()
        self.available = True
        self.recorder = Recorder(self.read_frame, directory)
        self.inputs = []
        self.safety = {"armed": False, "dry_run": True, "killed": False}

    def read_frame(self):
        if self.available:
            return np.full((48, 64, 3), 100, dtype=np.uint8), 1

    def state(self):
        return {"safety": dict(self.safety), "recording": self.recorder.snapshot()}

    def submit_click(self, **kwargs):
        self.inputs.append(kwargs)
        raise AssertionError("recording must never inject input")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    opened = []
    monkeypatch.setattr("gamelens.recording.os.startfile", opened.append)
    with TestClient(create_app(runtime), base_url="http://127.0.0.1:8777") as client:
        yield runtime, client, opened
    runtime.recorder.stop()


def headers(runtime, role):
    return {"X-GameLens-Token": getattr(runtime.tokens, role)}


@pytest.mark.parametrize("route", ["start", "stop", "folder"])
def test_recording_mutations_require_authentication(setup, route):
    runtime, client, opened = setup
    response = client.post("/recording/" + route, json={"fps": 30})
    assert response.status_code == 401
    assert not runtime.recorder.snapshot()["active"]
    assert not opened and not runtime.inputs


def test_agent_can_stop_owners_active_recording_without_input(setup):
    runtime, client, _ = setup
    assert client.post("/recording/start", headers=headers(runtime, "operator")).status_code == 200
    assert client.post("/recording/stop", headers=headers(runtime, "agent")).status_code == 200
    assert not runtime.recorder.snapshot()["active"]
    assert not runtime.inputs


def test_agent_cannot_open_native_folder(setup):
    runtime, client, opened = setup
    assert client.post("/recording/folder", headers=headers(runtime, "agent")).status_code == 403
    assert not opened and not runtime.inputs


@pytest.mark.parametrize("route", ["start", "stop", "folder"])
def test_foreign_origin_cannot_mutate_recording(setup, route):
    runtime, client, opened = setup
    owner = {**headers(runtime, "operator"), "Origin": "https://foreign.example"}
    assert client.post("/recording/" + route, headers=owner).status_code == 403
    assert not runtime.recorder.snapshot()["active"]
    assert not opened and not runtime.inputs


def test_recording_status_requires_authentication_but_accepts_both_roles(setup):
    runtime, client, _ = setup
    assert client.get("/recording").status_code == 401
    for role in ("operator", "agent"):
        response = client.get("/recording", headers=headers(runtime, role))
        assert response.status_code == 200
        assert response.json()["active"] is False and response.json()["audio"] is False


@pytest.mark.parametrize("body", [{"fps": 24}, {"fps": True}, {"fps": "30"}, {"fps": 30.0}, {"fps": None}])
def test_invalid_rate_refuses_before_recording(setup, body):
    runtime, client, _ = setup
    response = client.post("/recording/start", headers=headers(runtime, "operator"), json=body)
    assert response.status_code == 409
    assert not runtime.recorder.snapshot()["active"]
    assert not list(runtime.recorder.directory.iterdir())
    assert not runtime.inputs


@pytest.mark.parametrize("body", [[30], "30", 30])
def test_non_object_body_is_rejected(setup, body):
    runtime, client, _ = setup
    assert client.post("/recording/start", headers=headers(runtime, "operator"), json=body).status_code == 422
    assert not runtime.recorder.snapshot()["active"]


def test_missing_capture_refuses_and_repeated_start_refuses(setup):
    runtime, client, _ = setup
    owner = headers(runtime, "operator")
    runtime.available = False
    assert client.post("/recording/start", headers=owner).status_code == 409
    runtime.available = True
    first = client.post("/recording/start", headers=owner, json={"fps": 15})
    assert first.status_code == 200
    assert client.post("/recording/start", headers=owner).status_code == 409
    assert runtime.recorder.snapshot()["file"] == first.json()["file"]
    assert not runtime.inputs


@pytest.mark.parametrize("role", ["operator", "agent"])
def test_both_roles_can_record_disarmed_without_game_input(setup, role):
    runtime, client, opened = setup
    original_safety = dict(runtime.safety)
    recorder_role = headers(runtime, role)
    response = client.post("/recording/start", headers=recorder_role)
    assert response.status_code == 200
    assert response.json()["fps"] == 30 and response.json()["audio"] is False
    deadline = time.monotonic() + 2
    while runtime.recorder.snapshot()["frames"] < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    state = client.get("/state", headers=headers(runtime, "agent")).json()
    assert state["recording"]["active"]
    stopped = client.post("/recording/stop", headers=recorder_role)
    assert stopped.status_code == 200 and stopped.json()["frames"] >= 2
    assert stopped.json()["file"] and not stopped.json()["error"]
    assert client.post("/recording/folder", headers=headers(runtime, "operator")).status_code == 200
    assert opened == [str(runtime.recorder.directory)]
    assert runtime.safety == original_safety and not runtime.inputs
