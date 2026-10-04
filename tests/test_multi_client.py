"""Named-client routing against two real HTTP stubs and a real stdio process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gamelens.multi_client import ConfigError, MultiClient, load_clients
from gamelens.multi_mcp import MultiServer
from gamelens.session import agent_token_path, publish_agent_token

ROOT = Path(__file__).resolve().parent.parent


class Stub:
    def __init__(self, name):
        self.name, self.requests, self.frame, self.recording = name, [], 0, False
        self.redirect = None
        self.safety = {"armed": False, "dry_run": True, "killed": False}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, image=False):
                if stub.redirect:
                    self.send_response(302)
                    self.send_header("Location", stub.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = b"fake-jpeg" if image else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg" if image else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if image:
                    stub.frame += 1
                    self.send_header("X-GameLens-Observation", f"{stub.name}-obs{stub.frame}")
                    self.send_header("X-GameLens-Frame", str(stub.frame))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                stub.requests.append(("GET", self.path, None, self.headers.get("X-GameLens-Token")))
                if self.path.startswith("/frame.jpg?"):
                    self.reply({}, image=True)
                elif self.path == "/recording":
                    self.reply({"active": stub.recording, "source": stub.name})
                else:
                    assert self.path == "/state"
                    self.reply({"source": stub.name, "safety": {"armed": False, "live": False}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(("POST", self.path, body, self.headers.get("X-GameLens-Token")))
                if self.path in ("/arm", "/live", "/stop"):
                    if self.path == "/arm":
                        stub.safety["armed"] = True
                    elif self.path == "/live":
                        stub.safety["dry_run"] = False
                    else:
                        stub.safety.update(killed=True, armed=False, dry_run=True)
                    self.reply({"safety": stub.safety, "source": stub.name})
                    return
                if self.path == "/act":
                    self.reply({"outcome": "dry", "bound_to": body["observation_id"]})
                else:
                    assert self.path in ("/recording/start", "/recording/stop")
                    stub.recording = self.path.endswith("start")
                    self.reply({"active": stub.recording, "source": stub.name})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)

    def acts(self):
        return [body for method, path, body, _ in self.requests if path == "/act"]


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # A legacy process-wide override must never replace either client's token.
    monkeypatch.setenv("GAMELENS_AGENT_TOKEN", "wrong-shared-token")
    monkeypatch.setenv("GAMELENS_TOKEN_FILE", str(tmp_path / "wrong-shared-file"))
    a, b = Stub("alpha"), Stub("beta")
    config = tmp_path / "clients.json"
    config.write_text(json.dumps({"clients": [{"name": s.name, "url": s.url} for s in (a, b)]}))
    for s in (a, b):
        publish_agent_token(agent_token_path(s.port), f"{s.name}-token")
    yield a, b, config
    a.close()
    b.close()


class Stdio:
    def __init__(self, config):
        self.proc = subprocess.Popen([sys.executable, "-m", "gamelens.multi_mcp", "--clients-file", str(config)],
            cwd=ROOT, env=dict(os.environ), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id,
            "method": method, "params": params or {}}).encode() + b"\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id and reply["jsonrpc"] == "2.0"
        return reply

    def tool(self, name, args=None):
        return self.call("tools/call", {"name": name, "arguments": args or {}})["result"]

    def close(self):
        self.proc.stdin.close()
        assert self.proc.stdout.read() == b""
        assert self.proc.wait(5) == 0
        assert self.proc.stderr.read() == b""


def text(result):
    return "\n".join(item["text"] for item in result["content"] if item["type"] == "text")


def test_stdio_handshake_inventory_and_selected_state_without_input(pair):
    a, b, config = pair
    c = Stdio(config)
    try:
        init = c.call("initialize", {"protocolVersion": "2024-11-05"})["result"]
        assert init["serverInfo"]["name"] == "gamelens-multi"
        assert init["protocolVersion"] == "2024-11-05"
        tools = c.call("tools/list")["result"]["tools"]
        assert len(tools) == 10
        for tool in tools:
            if tool["name"] not in ("gamelens_clients", "gamelens_profile"):
                schema = tool["inputSchema"]
                assert "client" in schema["required"]
                assert schema["properties"]["client"]["enum"] == ["alpha", "beta"]
        assert "alpha" in text(c.tool("gamelens_clients"))
        assert not a.requests and not b.requests
        assert "beta" in text(c.tool("gamelens_state", {"client": "beta"}))
        assert not a.requests
        assert b.requests == [("GET", "/state", None, "beta-token")]
        assert not a.acts() and not b.acts()
    finally:
        c.close()


def test_stdio_observations_auth_and_recording_are_isolated(pair):
    a, b, config = pair
    c = Stdio(config)
    try:
        c.call("initialize")
        assert "alpha-obs1" in text(c.tool("gamelens_see", {"client": "alpha"}))
        # Alpha's seen image cannot enable beta input; beta only gets its first frame.
        fresh = c.tool("gamelens_act", {"client": "beta", "action": {"kind": "key", "key": "w"}})
        assert fresh["isError"] and "nothing was dispatched" in text(fresh)
        assert "beta-obs1" in text(fresh) and not b.acts()
        result = c.tool("gamelens_act", {"client": "alpha", "action": {"kind": "key", "key": "w"}, "see_after": False})
        assert not result["isError"]
        assert a.acts()[0]["observation_id"] == "alpha-obs1" and not b.acts()
        c.tool("gamelens_act", {"client": "beta", "action": {"kind": "key", "key": "s"}, "see_after": False})
        assert b.acts()[0]["observation_id"] == "beta-obs1"
        c.tool("gamelens_recording", {"client": "alpha", "action": "start"})
        assert a.recording and not b.recording
        assert '"active": false' in text(c.tool("gamelens_recording", {"client": "beta", "action": "status"}))
        c.tool("gamelens_recording", {"client": "alpha", "action": "stop"})
        assert not a.recording and not b.recording
        assert all(req[3] == "alpha-token" for req in a.requests)
        assert all(req[3] == "beta-token" for req in b.requests)
        assert not any(path.split("?")[0] in ("/arm", "/live", "/stop", "/windows")
            for stub in (a, b) for _, path, _, _ in stub.requests)
    finally:
        c.close()


@pytest.mark.parametrize("selector", [None, "", "missing", "ALPHA", "alpha,beta", ["alpha", "beta"], {"client": "alpha"}, 1])
def test_missing_or_invalid_selection_never_contacts_any_client(pair, selector):
    a, b, config = pair
    server = MultiServer(MultiClient(load_clients(config)))
    for tool in ("gamelens_state", "gamelens_see", "gamelens_act", "gamelens_recording"):
        reply = server.handle({"id": 1, "method": "tools/call", "params": {"name": tool,
            "arguments": {"client": selector, "action": {"kind": "key", "key": "w"}}}})
        assert reply["result"]["isError"]
    assert not a.requests and not b.requests


def test_auth_rotates_only_selected_port_and_missing_token_sends_nothing(pair):
    a, b, config = pair
    router = MultiClient(load_clients(config))
    publish_agent_token(agent_token_path(a.port), "alpha-rotated")
    router.tool_state({"client": "alpha"})
    router.tool_state({"client": "beta"})
    assert a.requests[-1][3] == "alpha-rotated" and b.requests[-1][3] == "beta-token"
    agent_token_path(a.port).unlink()
    before = len(a.requests)
    server = MultiServer(router)
    result = server.handle({"id": 1, "method": "tools/call", "params": {
        "name": "gamelens_state", "arguments": {"client": "alpha"}}})["result"]
    assert result["isError"] and len(a.requests) == before
    assert "alpha-rotated" not in text(result) and "beta-token" not in text(result)


def test_redirect_cannot_forward_a_client_credential_or_broadcast(pair):
    a, b, config = pair
    router = MultiClient(load_clients(config))
    router.tool_see({"client": "alpha"})
    a.redirect = b.url + "/state"
    server = MultiServer(router)
    for tool, extra in (("gamelens_state", {}), ("gamelens_see", {}),
            ("gamelens_recording", {"action": "start"}),
            ("gamelens_act", {"action": {"kind": "key", "key": "w"}})):
        result = server.handle({"id": 1, "method": "tools/call", "params": {
            "name": tool, "arguments": {"client": "alpha", **extra}}})["result"]
        assert result["isError"]
    assert not b.requests
    assert len(a.acts()) == 1, "redirected actions are refused, never retried"


def test_local_transport_bypasses_shared_proxy_settings(pair, monkeypatch):
    a, b, config = pair
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    router = MultiClient(load_clients(config))
    assert not router.tool_state({"client": "alpha"})[1]
    assert len(a.requests) == 1 and not b.requests


@pytest.mark.parametrize("url", ["https://127.0.0.1:8781", "http://example.com:8781", "http://localhost:8781",
    "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:8781/state",
    "http://user:secret@127.0.0.1:8781", "http://127.0.0.1:8781?token=secret", "http://127.0.0.1:8781#x",
    "http://127.0.0.1:08781", " http://127.0.0.1:8781", "http://127.0.0.1:8781\n", 8781])
def test_invalid_origin_config_is_refused(tmp_path, url):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"clients": [{"name": "alpha", "url": url}]}))
    with pytest.raises(ConfigError):
        load_clients(path)


@pytest.mark.parametrize("config", [{}, {"clients": []}, {"clients": {}}, {"clients": [{"name": "A", "url": "http://127.0.0.1:8781"}]},
    {"clients": [{"name": "alpha", "url": "http://127.0.0.1:8781", "token": "secret"}]},
    {"clients": [{"name": "alpha", "url": "http://127.0.0.1:8781"}, {"name": "alpha", "url": "http://127.0.0.1:8782"}]},
    {"clients": [{"name": "alpha", "url": "http://127.0.0.1:8781"}, {"name": "beta", "url": "http://[::1]:8781"}]},
    {"clients": [{"name": "alpha", "url": "http://127.0.0.1:8781"}], "default": "alpha"}])
def test_bad_shape_or_ambiguous_config_fails_closed(tmp_path, config):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigError):
        load_clients(path)


def test_config_duplicate_property_and_size_bound(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text('{"clients": [], "clients": [{"name":"a","url":"http://127.0.0.1:8781"}]}')
    with pytest.raises(ConfigError, match="Duplicate"):
        load_clients(path)
    path.write_bytes(b" " * 65537)
    with pytest.raises(ConfigError, match="64 KiB"):
        load_clients(path)
    path.write_text("[" * 1100 + "0" + "]" * 1100)
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_clients(path)


def test_invalid_config_stdio_exits_without_protocol_or_credentials(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text('{"clients": [{"name": "a", "url": "http://user:secret@example.com:80"}]}')
    result = subprocess.run([sys.executable, "-m", "gamelens.multi_mcp", "--clients-file", str(path)],
        cwd=ROOT, input=b'{"id":1,"method":"initialize"}\n', capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
    assert b"refused" in result.stderr and b"secret" not in result.stderr


def test_ten_client_example_inventory_isolation_and_unknown_eleventh_fail_closed(monkeypatch):
    from gamelens.mcp import TOOLS
    from gamelens.multi_client import PortSessionClient

    requests = []

    def unexpected_http(self, path, body=None):
        requests.append((self.port, path))
        raise AssertionError("Inventory and unknown selectors must not contact any server")

    monkeypatch.setattr(PortSessionClient, "request", unexpected_http)
    specs = load_clients(ROOT / "profiles/clients.example.json")
    names = [f"client{i:02d}" for i in range(1, 11)]
    assert [s.name for s in specs] == names
    assert [s.port for s in specs] == list(range(8777, 8787))
    router = MultiClient(specs)
    assert len({id(client) for client in router.clients.values()}) == 10
    assert len({agent_token_path(client.port) for client in router.clients.values()}) == 10
    router.clients["client01"].shown = "client01-only-observation"
    assert all(router.clients[name].shown is None for name in names[1:])
    server = MultiServer(router)
    inventory = server.handle({"id": 1, "method": "tools/call", "params": {"name": "gamelens_clients"}})["result"]
    assert not inventory["isError"]
    assert [item["client"] for item in json.loads(text(inventory))["clients"]] == names
    tools = server.handle({"id": 2, "method": "tools/list"})["result"]["tools"]
    for tool in tools:
        if tool["name"] not in ("gamelens_clients", "gamelens_profile"):
            assert tool["inputSchema"]["properties"]["client"]["enum"] == names
            assert "client" in tool["inputSchema"]["required"]
            result = server.handle({"id": 3, "method": "tools/call", "params": {"name": tool["name"],
                "arguments": {"client": "client11"}}})["result"]
            assert result["isError"] and "Nothing was dispatched" in text(result)
    assert requests == []
    assert len(TOOLS) == 9
    assert all("client" not in t["inputSchema"]["properties"] for t in TOOLS)
