"""GL-039: the MCP server, driven exactly as an MCP client drives it.

`python -m gamelens.mcp` is spawned as a real process and spoken to over its
real stdin/stdout, against a stub standing in for GameLens's HTTP surface. The
stub records every request, which is what the binding tests need: most of them
are about what the server must *not* send.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


class Stub:
    """GameLens's HTTP surface, as far as the MCP server uses it."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None, str | None]] = []
        self.act_status = 200
        self.act_body = {"verdict": "ok", "outcome": "sent", "detail": "", "action_id": 1,
                         "churn": None, "after_frame": 40, "bound_to": "fresh"}
        self.frame_status = 200
        self.after_status = 200
        self.drop_frames = False
        self.recording_active = False
        self.session_safety = {"armed": True, "dry_run": True, "killed": False}
        self.n = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.requests.append(("GET", self.path, None, self.headers.get("X-GameLens-Token")))
                if self.path == "/state":
                    self._send(200, json.dumps({"safety": stub.session_safety, "log": [1] * 50,
                                                "marks": [1]}).encode())
                elif self.path == "/recording":
                    self._send(200, json.dumps({"active": stub.recording_active}).encode())
                elif self.path.startswith("/frame.jpg"):
                    if stub.drop_frames:
                        self.close_connection = True      # hang up: no response at all
                        return
                    status = stub.after_status if "after=" in self.path else stub.frame_status
                    if status != 200:
                        self._send(status, json.dumps({"detail": "no frame"}).encode())
                        return
                    stub.n += 1
                    self._send(200, JPEG, "image/jpeg", [
                        ("X-GameLens-Observation", f"obs{stub.n}"),
                        ("X-GameLens-Frame", str(40 + stub.n))])
                else:
                    self._send(404, b"{}")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                stub.requests.append(("POST", self.path, body, self.headers.get("X-GameLens-Token")))
                if self.path in ("/arm", "/live", "/stop"):
                    safety = stub.session_safety
                    if self.path != "/stop" and (safety["killed"] or (self.path == "/live" and not safety["armed"])):
                        self._send(409, b'{"detail":"session interlock"}')
                        return
                    if self.path == "/arm":
                        safety["armed"] = True
                    elif self.path == "/live":
                        safety["dry_run"] = False
                    else:
                        safety.update(armed=False, dry_run=True, killed=True)
                    self._send(200, json.dumps({"safety": safety, "log": ["session log"]}).encode())
                    return
                if self.path == "/recording/start":
                    if stub.recording_active:
                        self._send(409, b'{"detail":"Recording already active"}')
                    else:
                        stub.recording_active = True
                        self._send(200, json.dumps({"active": True, "fps": body["fps"]}).encode())
                    return
                if self.path == "/recording/stop":
                    stub.recording_active = False
                    self._send(200, b'{"active":false}')
                    return
                self._send(stub.act_status, json.dumps(stub.act_body).encode())

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def acts(self) -> list[dict]:
        return [b for m, p, b, _ in self.requests if m == "POST" and p == "/act"]

    def paths(self) -> list[str]:
        return [f"{m} {p}" for m, p, _, _ in self.requests]


class Client:
    def __init__(self, url: str, env_token: str | None = "agent-token", token_file=None) -> None:
        import os

        env = dict(os.environ)
        env.pop("GAMELENS_AGENT_TOKEN", None)
        if env_token:
            env["GAMELENS_AGENT_TOKEN"] = env_token
        args = [sys.executable, "-m", "gamelens.mcp", "--url", url]
        if token_file:
            args += ["--token-file", str(token_file)]
        self.proc = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                   **({"params": params} if params is not None else {})})
        line = self.proc.stdout.readline()
        reply = json.loads(line)             # every stdout line must be JSON-RPC
        assert reply["jsonrpc"] == "2.0" and reply["id"] == self.next_id
        return reply

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments or {}})["result"]

    def close(self) -> bytes:
        self.proc.stdin.close()
        rest = self.proc.stdout.read()
        self.proc.wait(5)
        return rest


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.http.shutdown()


@pytest.fixture
def client(stub):
    c = Client(stub.url)
    c.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "test", "version": "0"}})
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield c
    assert c.close() == b"", "nothing but replies may ever reach stdout"


def texts(result) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def images(result) -> list:
    return [c for c in result["content"] if c["type"] == "image"]


# --- protocol ------------------------------------------------------------------


def test_initialize_negotiates_and_describes_the_server(stub):
    c = Client(stub.url)
    reply = c.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "gamelens"
    assert "tools" in reply["result"]["capabilities"]
    c.close()


def test_an_unknown_protocol_version_gets_the_newest_supported(stub):
    c = Client(stub.url)
    reply = c.call("initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    c.close()


def test_tools_are_listed_with_schemas(client):
    tools = {t["name"]: t for t in client.call("tools/list")["result"]["tools"]}
    assert set(tools) == {"gamelens_state", "gamelens_see", "gamelens_act", "gamelens_profile", "gamelens_recording", "gamelens_session", "gamelens_metrics", "gamelens_skills", "gamelens_learning"}
    assert tools["gamelens_act"]["inputSchema"]["required"] == ["action"]


def test_explicit_session_controls_through_stdio_without_game_input(client, stub):
    stub.session_safety["armed"] = False
    assert not client.tool("gamelens_session", {"action": "status"})["isError"]
    assert client.tool("gamelens_session", {"action": "live"})["isError"]
    for action in ("arm", "live", "stop"):
        result = client.tool("gamelens_session", {"action": action})
        assert not result["isError"]
        assert "log" not in json.loads(result["content"][0]["text"])
    assert client.tool("gamelens_session", {"action": "arm"})["isError"]
    assert stub.paths() == ["GET /state", "POST /live", "POST /arm", "POST /live", "POST /stop", "POST /arm"]
    assert stub.acts() == []
    assert all(token == "agent-token" for _, _, _, token in stub.requests)


@pytest.mark.parametrize("args", [{}, {"action": "reset"}, {"action": "/arm"}, {"action": "arm", "path": "/windows"}, {"action": "live", "target": 1}])
def test_invalid_session_control_sends_nothing(client, stub, args):
    assert client.tool("gamelens_session", args)["isError"]
    assert stub.paths() == []


def test_uncertain_session_control_is_not_retried():
    from gamelens.mcp import GameLensClient, ToolError
    c = GameLensClient("http://127.0.0.1:8777", None)
    calls = []
    def fail(path, body=None):
        calls.append(path)
        raise ToolError("response lost")
    c.request = fail
    with pytest.raises(ToolError, match="outcome unknown.*Check session status"):
        c.tool_session({"action": "live"})
    assert calls == ["/live"]


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"safety":{"armed":true}}'])
def test_session_response_must_prove_usable_safety_state(body):
    from gamelens.mcp import GameLensClient, ToolError
    c = GameLensClient("http://127.0.0.1:8777", None)
    c.request = lambda *args: (200, body, {})
    with pytest.raises(ToolError, match="check session status"):
        c.tool_session({"action": "arm"})


def test_ping_and_unknown_methods(client):
    assert client.call("ping")["result"] == {}
    assert client.call("resources/list")["error"]["code"] == -32601
    assert client.call("tools/call", {"name": "nope"})["error"]["code"] == -32602


def test_malformed_json_is_a_parse_error_not_a_crash(client):
    client.proc.stdin.write(b"{not json\n")
    client.proc.stdin.flush()
    assert json.loads(client.proc.stdout.readline())["error"]["code"] == -32700
    assert client.call("ping")["result"] == {}


# --- tools -----------------------------------------------------------------------


def test_state_drops_the_dashboard_log_and_uses_the_agent_token(client, stub):
    result = client.tool("gamelens_state")
    assert result["isError"] is False
    state = json.loads(texts(result))
    assert state["safety"]["armed"] is True and "log" not in state and "marks" not in state
    assert stub.requests[-1][3] == "agent-token"


def test_recording_status_start_stop_use_agent_auth_and_no_game_input(client, stub):
    assert json.loads(texts(client.tool("gamelens_recording", {"action": "status"}))) == {"active": False}
    started = client.tool("gamelens_recording", {"action": "start"})
    assert not started["isError"]
    assert json.loads(texts(started)) == {"active": True, "fps": 30}
    duplicate = client.tool("gamelens_recording", {"action": "start"})
    assert duplicate["isError"] and "409" in texts(duplicate)
    assert "already active" in texts(duplicate)
    stopped = client.tool("gamelens_recording", {"action": "stop"})
    assert not stopped["isError"] and json.loads(texts(stopped)) == {"active": False}
    assert stub.paths() == ["GET /recording", "POST /recording/start", "POST /recording/start", "POST /recording/stop"]
    assert all(token == "agent-token" for _, _, _, token in stub.requests)
    assert not stub.acts()


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_recording_fps_is_forwarded(client, stub, fps):
    assert not client.tool("gamelens_recording", {"action": "start", "fps": fps})["isError"]
    assert stub.requests == [("POST", "/recording/start", {"fps": fps}, "agent-token")]


@pytest.mark.parametrize("arguments", [
    {}, {"action": "open"}, {"action": "start", "folder": "C:/secret"},
    {"action": "start", "fps": True}, {"action": "start", "fps": 24},
    {"action": "start", "fps": "30"}, {"action": "status", "fps": 30},
    {"action": "stop", "fps": 30}, {"action": "start", "path": "C:/secret"},
])
def test_recording_invalid_args_send_no_http(client, stub, arguments):
    assert client.tool("gamelens_recording", arguments)["isError"]
    assert not stub.requests


def test_recording_transport_failure_does_not_retry(monkeypatch):
    from gamelens.mcp import GameLensClient, ToolError
    from unittest.mock import Mock

    transport = Mock(side_effect=ToolError("connection lost"))
    client = GameLensClient("http://127.0.0.1:8777", None)
    monkeypatch.setattr(client, "request", transport)
    with pytest.raises(ToolError, match="outcome unknown.*Check recording status"):
        client.tool_recording({"action": "start"})
    transport.assert_called_once_with("/recording/start", {"fps": 30})


def test_see_returns_the_image(client):
    result = client.tool("gamelens_see", {"quality": 60})
    (img,) = images(result)
    assert img["mimeType"] == "image/jpeg" and base64.b64decode(img["data"]) == JPEG
    assert '"observation": "obs1"' in texts(result)


def test_act_before_seeing_anything_dispatches_nothing(client, stub):
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    assert result["isError"] is True
    assert stub.acts() == [], "an agent that has seen nothing has decided nothing"
    assert images(result), "it gets something to decide on instead"


def test_act_binds_to_the_shown_image_asks_to_rebind_and_shows_the_result(client, stub):
    client.tool("gamelens_see")
    result = client.tool("gamelens_act", {"action": {"kind": "click", "x": 10, "y": 20}})
    assert result["isError"] is False
    (body,) = stub.acts()
    assert body == {"kind": "click", "x": 10, "y": 20, "observation_id": "obs1", "rebind": True}
    assert "GET /frame.jpg?quality=50&after=40&frames=3&wait_ms=1500" in stub.paths()
    assert len(images(result)) == 1


def test_the_after_image_becomes_the_shown_one(client, stub):
    client.tool("gamelens_see")
    client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    assert [b["observation_id"] for b in stub.acts()] == ["obs1", "obs2"]


def test_strict_does_not_ask_to_rebind(client, stub):
    client.tool("gamelens_see")
    client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}, "strict": True})
    assert "rebind" not in stub.acts()[0]


def test_a_caller_cannot_smuggle_its_own_observation_or_rebind(client, stub):
    client.tool("gamelens_see")
    client.tool("gamelens_act", {"action": {"kind": "key", "key": "w",
                                            "observation_id": "forged", "rebind": False},
                                 "strict": True})
    body = stub.acts()[0]
    assert body["observation_id"] == "obs1" and "rebind" not in body


@pytest.mark.parametrize("status,verdict", [(409, "SCREEN_CHANGED"), (409, "PREEMPTED"),
                                            (409, "GEOMETRY_MOVED"), (400, "bad"),
                                            (500, "ERROR")])
def test_a_refusal_is_reported_with_a_fresh_image_and_never_retried(client, stub, status,
                                                                    verdict):
    client.tool("gamelens_see")
    stub.act_status = status
    stub.act_body = {"verdict": verdict, "outcome": "denied", "detail": "look again"}
    result = client.tool("gamelens_act", {"action": {"kind": "click", "x": 1, "y": 1}})
    assert result["isError"] is True
    assert len(stub.acts()) == 1, "exactly one /act per call: a retry is a decision nobody made"
    assert verdict in texts(result) and len(images(result)) == 1
    assert '"observation": "obs2"' in texts(result), "the fresh image is now the shown one"


def test_see_after_false_fetches_nothing_after(client, stub):
    client.tool("gamelens_see")
    before = len(stub.requests)
    result = client.tool("gamelens_act", {"action": {"kind": "look", "dx": 5, "dy": 0},
                                          "see_after": False})
    assert [p for p in stub.paths()[before:]] == ["POST /act"]
    assert images(result) == []


def test_an_after_frame_timeout_falls_back_to_the_newest_and_says_so(client, stub):
    client.tool("gamelens_see")
    stub.after_status = 504
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    assert result["isError"] is False
    assert "after-frame unavailable" in texts(result) and len(images(result)) == 1


def test_a_dry_run_action_with_no_after_frame_still_shows_the_screen(client, stub):
    client.tool("gamelens_see")
    stub.act_body = {"verdict": "ok", "outcome": "dry", "after_frame": None}
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    assert not any("after=" in p for p in stub.paths()) and len(images(result)) == 1


@pytest.mark.parametrize("args", [
    {"action": "click"}, {"action": {}}, {"action": {"kind": "key"}, "quality": 0},
    {"action": {"kind": "key"}, "after_frames": 31}, {"action": {"kind": "key"}, "quality": True},
])
def test_bad_tool_arguments_are_tool_errors(client, stub, args):
    client.tool("gamelens_see")
    result = client.tool("gamelens_act", args)
    assert result["isError"] is True and stub.acts() == []


def test_a_failed_picture_after_a_sent_action_keeps_the_action_result(client, stub):
    """GL039-I01: reporting only "not reachable" here reads as a failed action
    and invites the same keystroke again."""
    client.tool("gamelens_see")
    stub.drop_frames = True
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    text = texts(result)
    assert '"outcome": "sent"' in text and "WAS SENT" in text and "do not repeat" in text
    assert result["isError"] is False, "the action succeeded; only the picture failed"
    assert len(stub.acts()) == 1


@pytest.mark.parametrize("outcome,says,never", [
    ("dry", "NOT injected", "WAS SENT"),
    ("pending", "UNDECIDED", "WAS SENT"),
])
def test_a_failed_picture_describes_the_actual_outcome(client, stub, outcome, says, never):
    """GL039-I11: HTTP 200 also covers dry and pending; neither was sent."""
    client.tool("gamelens_see")
    stub.drop_frames = True
    stub.act_body = {"verdict": "ok", "outcome": outcome, "after_frame": None}
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    text = texts(result)
    assert says in text and never not in text
    assert len(stub.acts()) == 1


def test_a_failed_picture_after_a_refusal_still_reports_the_refusal(client, stub):
    client.tool("gamelens_see")
    stub.drop_frames = True
    stub.act_status, stub.act_body = 409, {"verdict": "PREEMPTED", "outcome": "denied"}
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    assert result["isError"] is True and "PREEMPTED" in texts(result)


@pytest.mark.parametrize("key,value", [("strict", "true"), ("strict", 1), ("strict", None),
                                       ("see_after", "false"), ("see_after", 0)])
def test_flags_must_be_real_booleans(client, stub, key, value):
    """GL039-I02: "true" for strict used to mean rebind -- the looser binding."""
    client.tool("gamelens_see")
    result = client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}, key: value})
    assert result["isError"] is True and stub.acts() == []


@pytest.mark.parametrize("message,code", [
    ({"jsonrpc": "2.0", "id": 90, "method": "initialize", "params": [1]}, -32602),
    ({"jsonrpc": "2.0", "id": 91, "method": "tools/call", "params": {"name": []}}, -32602),
    ({"jsonrpc": "2.0", "id": 92, "method": "tools/call", "params": {"name": {"a": 1}}}, -32602),
    ({"jsonrpc": "2.0", "id": 93, "method": ["tools/list"]}, -32600),
    ({"jsonrpc": "2.0", "id": 94, "method": "tools/call", "params": "gamelens_see"}, -32602),
])
def test_malformed_requests_get_the_right_error_and_the_session_survives(client, message, code):
    """GL039-I03: these used to raise outside every handler and end the process.
    The codes are JSON-RPC's own, so a client can tell a bad envelope from a
    server fault (-32603)."""
    client.send(message)
    reply = json.loads(client.proc.stdout.readline())
    assert reply["id"] == message["id"] and reply["error"]["code"] == code
    assert client.call("ping")["result"] == {}


def test_a_malformed_notification_is_ignored_and_the_session_survives(client):
    client.send({"jsonrpc": "2.0", "method": "notifications/x", "params": [1]})
    assert client.call("ping")["result"] == {}


def test_it_never_touches_operator_routes(client, stub):
    client.tool("gamelens_see")
    client.tool("gamelens_state")
    client.tool("gamelens_act", {"action": {"kind": "key", "key": "w"}})
    for path in ("/arm", "/live", "/stop", "/windows"):
        assert not any(p.split("?")[0].endswith(path) for p in stub.paths())


# --- token handling ---------------------------------------------------------------


def test_default_encrypted_session_is_read_and_rotates_without_mcp_restart(stub, tmp_path, monkeypatch):
    from urllib.parse import urlsplit
    from gamelens.session import agent_token_path, publish_agent_token

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = agent_token_path(urlsplit(stub.url).port)
    publish_agent_token(path, "agent-first")
    c = Client(stub.url, env_token=None)
    try:
        c.call("initialize", {})
        assert not c.tool("gamelens_state")["isError"]
        assert stub.requests[-1][3] == "agent-first"
        publish_agent_token(path, "agent-second")
        assert not c.tool("gamelens_state")["isError"]
        assert stub.requests[-1][3] == "agent-second"
        path.unlink()
        result = c.tool("gamelens_state")
        assert result["isError"] and "no agent token" in texts(result)
        assert len(stub.requests) == 2
    finally:
        c.close()


def test_the_token_can_come_from_a_file_and_is_reread(stub, tmp_path):
    tok = tmp_path / "ag.tok"
    tok.write_text("first\n")
    c = Client(stub.url, env_token=None, token_file=tok)
    c.call("initialize", {})
    c.tool("gamelens_state")
    tok.write_text("second")
    c.tool("gamelens_state")
    assert [r[3] for r in stub.requests] == ["first", "second"]
    c.close()


def test_no_token_is_a_tool_error_not_a_crash(stub, tmp_path):
    c = Client(stub.url, env_token=None, token_file=tmp_path / "missing.tok")
    c.call("initialize", {})
    result = c.tool("gamelens_state")
    assert result["isError"] is True and "no agent token" in texts(result)
    assert stub.requests == []
    c.close()


def test_gamelens_down_is_a_tool_error(tmp_path):
    c = Client("http://127.0.0.1:9")
    c.call("initialize", {})
    result = c.tool("gamelens_see")
    assert result["isError"] is True and "not reachable" in texts(result)
    c.close()


# --- GL040-I04: a lost answer to /act is not "not sent" --------------------------


def _client_with(act_failure):
    import socket
    import urllib.error

    from gamelens.mcp import GameLensClient, ToolError

    c = GameLensClient("http://127.0.0.1:1", None)
    c.shown = "obs"
    calls = []

    def request(path, body=None):
        calls.append(path)
        if path == "/act":
            if act_failure is None:
                raise ToolError("no agent token")            # before sending: no cause
            try:
                raise act_failure
            except (urllib.error.URLError, OSError) as exc:
                raise ToolError(f"GameLens is not reachable: {exc}") from exc
        raise AssertionError(path)

    c.request = request
    c.frame = lambda quality, after=None, frames=3: (
        [{"type": "text", "text": "image {}"}], None)
    return c, calls, ToolError, socket


@pytest.mark.parametrize("failure", ["timeout", "reset"])
def test_an_act_without_an_answer_is_reported_as_possibly_sent(failure):
    import socket

    exc = socket.timeout("timed out") if failure == "timeout" else ConnectionResetError()
    c, calls, _, _ = _client_with(exc)
    content, is_error = c.tool_act({"action": {"kind": "key", "key": "w"}})
    text = "\n".join(i["text"] for i in content if i["type"] == "text")
    assert is_error is True
    assert "MAY HAVE BEEN CARRIED OUT" in text and "do not repeat" in text
    assert calls == ["/act"], "never retried"
    assert "image" in text, "the agent gets the current picture to decide on"


@pytest.mark.parametrize("failure", ["refused", "no token"])
def test_an_act_that_provably_never_arrived_is_a_plain_error(failure):
    import urllib.error

    exc = (urllib.error.URLError(ConnectionRefusedError()) if failure == "refused" else None)
    c, calls, ToolError, _ = _client_with(exc)
    with pytest.raises(ToolError):
        c.tool_act({"action": {"kind": "key", "key": "w"}})
    assert calls == ["/act"]


# --- GL040-RV01: a truncated body is a transport failure, not a crash ------------


class _Truncated:
    def __init__(self, status=200):
        self.status, self.headers = status, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        import http.client
        raise http.client.IncompleteRead(b"{\"verd", 100)


def test_a_truncated_act_answer_is_reported_as_possibly_sent(monkeypatch):
    import urllib.request

    from gamelens.mcp import GameLensClient

    monkeypatch.setenv("GAMELENS_AGENT_TOKEN", "t")
    c = GameLensClient("http://127.0.0.1:1", None)
    c.shown = "obs"
    monkeypatch.setattr(c.opener, "open", lambda req, timeout: _Truncated())
    content, is_error = c.tool_act({"action": {"kind": "key", "key": "w"}})
    text = "\n".join(i["text"] for i in content if i["type"] == "text")
    assert is_error is True and "MAY HAVE BEEN CARRIED OUT" in text


def test_a_truncated_picture_after_a_sent_action_keeps_the_action_result(monkeypatch):
    import io
    import json
    import urllib.request

    from gamelens.mcp import GameLensClient

    monkeypatch.setenv("GAMELENS_AGENT_TOKEN", "t")
    c = GameLensClient("http://127.0.0.1:1", None)
    c.shown = "obs"

    class Ok(_Truncated):
        def read(self):
            return json.dumps({"verdict": "ok", "outcome": "sent", "after_frame": 7}).encode()

    monkeypatch.setattr(c.opener, "open",
                        lambda req, timeout: Ok() if req.full_url.endswith("/act") else _Truncated())
    content, is_error = c.tool_act({"action": {"kind": "key", "key": "w"}})
    text = "\n".join(i["text"] for i in content if i["type"] == "text")
    assert is_error is False
    assert '"outcome": "sent"' in text and "WAS SENT" in text and "do not repeat" in text
