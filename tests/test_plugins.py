"""Plugin manifests and saved profile must remain tied to the learned route."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

from gamelens.marathon import DESTINATIONS, WAYPOINTS
from gamelens.mcp import Server

ROOT = Path(__file__).resolve().parent.parent


def test_saved_profile_uses_fixed_route_without_http_or_inputs():
    client = Mock()
    response = Server(client).handle({"id": 1, "method": "tools/call", "params": {
        "name": "gamelens_profile", "arguments": {"profile": "godsarena-marathon"},
    }})["result"]
    assert not response["isError"]
    profile = json.loads(response["content"][0]["text"])
    assert tuple(profile["chapter_destinations"]) == DESTINATIONS
    for trainer, point in WAYPOINTS.items():
        assert profile["trainers"][str(trainer)]["map"] == list(point)
    assert profile["evidence"]["original_target_achieved"] is False
    assert not client.mock_calls


def test_profile_rejects_arbitrary_paths():
    client = Mock()
    for arguments in ({"profile": "../../secret"}, {"path": "C:/secret"}):
        response = Server(client).handle({"id": 1, "method": "tools/call", "params": {
            "name": "gamelens_profile", "arguments": arguments,
        }})["result"]
        assert response["isError"]
    assert not client.mock_calls


def test_marketplace_paths_are_contained_and_only_core_bundles_mcp():
    market = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert [p["name"] for p in market["plugins"]] == ["gamelens", "gamelens-godsarena"]
    for entry in market["plugins"]:
        plugin = (ROOT / entry["source"]["path"]).resolve()
        assert plugin.is_relative_to(ROOT)
        manifest = json.loads((plugin / "plugin.json").read_text())
        assert manifest["name"] == entry["name"]
        assert list((plugin / "skills").glob("*/SKILL.md"))
    core = json.loads((ROOT / "plugins/gamelens/mcp.json").read_text())["mcpServers"]
    assert list(core) == ["gamelens"]
    assert core["gamelens"]["type"] == "stdio"
    assert not (ROOT / "plugins/gamelens-godsarena/mcp.json").exists()
    assert "TOKEN" not in json.dumps(core)


def test_plugin_bootstrap_serves_profile_from_unrelated_cwd(tmp_path):
    env = os.environ.copy()
    env["GAMELENS_PROJECT_DIR"] = str(ROOT)
    # No HTTP server or credential is needed for the fixed local guide.
    env.pop("GAMELENS_AGENT_TOKEN", None)
    messages = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {
            "name": "gamelens_profile", "arguments": {"profile": "godsarena-marathon"},
        }},
    ]
    result = subprocess.run(
        [sys.executable, str(ROOT / "plugins/gamelens/scripts/mcp_server.py")],
        cwd=tmp_path, env=env, input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "gamelens"
    assert {tool["name"] for tool in replies[1]["result"]["tools"]} == {
        "gamelens_profile", "gamelens_state", "gamelens_see", "gamelens_act", "gamelens_recording", "gamelens_session", "gamelens_metrics", "gamelens_skills", "gamelens_learning",
    }
    assert not replies[2]["result"]["isError"]
    guide = json.loads(replies[2]["result"]["content"][0]["text"])
    assert tuple(guide["chapter_destinations"]) == DESTINATIONS


def test_plugin_bootstrap_rejects_missing_companion(tmp_path):
    env = os.environ.copy()
    env["GAMELENS_PROJECT_DIR"] = str(tmp_path / "missing-companion")
    result = subprocess.run(
        [sys.executable, str(ROOT / "plugins/gamelens/scripts/mcp_server.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert "GameLens project missing" in result.stderr
