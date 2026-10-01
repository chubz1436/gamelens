"""Credential-bearing HTTP stays on an explicit loopback origin.

Both MCP entry points use this transport. Never honor environment proxies or
follow redirects, even to another local port with a different capability.
"""
from urllib.parse import urlsplit
import urllib.request


def validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("GameLens URL must be a literal-loopback HTTP origin")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid GameLens loopback URL") from None
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
            or parsed.username is not None or parsed.password is not None
            or port is None or not 1 <= port <= 65535
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Use http://127.0.0.1:PORT or http://[::1]:PORT without credentials or paths")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    canonical = f"http://{host}:{port}"
    if url not in (canonical, canonical + "/"):
        raise ValueError("GameLens URL must use canonical literal-loopback form")
    return canonical


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
