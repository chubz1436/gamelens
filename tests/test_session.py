"""Agent credential rotation and honest session diagnostics."""
import pytest

from gamelens.app import session_feedback
from gamelens.session import MAGIC, clear_agent_token, publish_agent_token, read_agent_token


def test_current_user_encryption_rotation_and_owned_cleanup(tmp_path):
    path = tmp_path / "agent.token"
    publish_agent_token(path, "first-agent-capability")
    assert path.read_bytes().startswith(MAGIC)
    assert b"first-agent-capability" not in path.read_bytes()
    assert read_agent_token(path) == "first-agent-capability"
    publish_agent_token(path, "second-agent-capability")
    clear_agent_token(path, "first-agent-capability")
    assert read_agent_token(path) == "second-agent-capability"
    clear_agent_token(path, "second-agent-capability")
    assert not path.exists()
    assert not list(tmp_path.glob(".agent-*"))


def test_corrupt_encrypted_file_is_not_treated_as_plaintext(tmp_path):
    path = tmp_path / "agent.token"
    path.write_bytes(MAGIC + b"not-a-DPAPI-payload")
    with pytest.raises(Exception):
        read_agent_token(path)
    clear_agent_token(path, "old-token")
    assert path.exists()


@pytest.mark.parametrize("change, expected", [
    ({}, "live"), ({"killed": True}, "stopped"),
    ({"armed": False}, "disarmed"), ({"dry_run": True}, "dry-run"),
    ({"executor": {"unreleased": ["w"]}}, "blocked"),
])
def test_feedback_never_claims_blocked_or_dry_input_is_live(change, expected):
    safety = {"armed": True, "killed": False, "dry_run": False, "executor": {}}
    safety.update(change)
    status = session_feedback({"foreground": True}, {"healthy": True}, safety, "ok")
    assert status["status"] == expected
    assert status["input_ready"] == (expected == "live")


def test_focus_capture_and_actual_guard_failures_are_visible():
    safety = {"armed": True, "dry_run": False}
    assert session_feedback(None, {"healthy": True}, safety, "ok")["status"] == "no-target"
    assert session_feedback({"foreground": False}, {"healthy": True}, safety, "ok")["status"] == "needs-focus"
    assert session_feedback({"foreground": True}, {"healthy": False}, safety, "ok")["status"] == "no-capture"
    status = session_feedback({"foreground": True}, {"healthy": True}, safety, "safety watchdog heartbeat is stale")
    assert not status["input_ready"] and "heartbeat is stale" in status["message"]
