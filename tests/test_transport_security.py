"""Both MCP clients must keep test credentials on their selected loopback origin."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from gamelens.mcp import GameLensClient
from gamelens.multi_client import ClientSpec, PortSessionClient
from gamelens.transport import validate_url


@contextmanager
def stub(redirect=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            requests.append((self.command, self.path, self.headers.get("X-GameLens-Token")))
            if redirect:
                self.send_response(302)
                self.send_header("Location", redirect)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def client(kind, url):
    result = (GameLensClient(url, None) if kind == "single" else
              PortSessionClient(ClientSpec("test", url, int(url.rsplit(":", 1)[1]))))
    result.token = lambda: "synthetic-test-token"
    return result


@pytest.mark.parametrize("kind", ["single", "multi"])
@pytest.mark.parametrize("body", [None, {}])
def test_redirect_never_forwards_credential(kind, body):
    with stub() as (other, other_requests):
        with stub(other + "/state") as (origin, requests):
            status, _, _ = client(kind, origin).request("/state", body)
    assert status == 302 and len(requests) == 1
    assert other_requests == []


@pytest.mark.parametrize("kind", ["single", "multi"])
def test_environment_proxy_is_not_used(kind, monkeypatch):
    with stub() as (proxy, proxy_requests):
        with stub() as (origin, requests):
            for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(key, proxy)
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("no_proxy", "")
            assert client(kind, origin).request("/state")[0] == 200
    assert len(requests) == 1 and proxy_requests == []


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8777", "http://example.com:8777",
    "http://127.0.0.1", "http://127.0.0.1:8777/private",
    "http://user:password@127.0.0.1:8777", "http://127.0.0.1:8777?x=1",
    "http://127.0.0.1:8777#fragment", "http://127.0.0.1:08777",
])
def test_invalid_origin_rejected_before_token_read(url):
    with pytest.raises(ValueError):
        GameLensClient(url, None)


def test_canonical_loopback_normalizes_trailing_slash():
    assert validate_url("http://127.0.0.1:8777/") == "http://127.0.0.1:8777"
