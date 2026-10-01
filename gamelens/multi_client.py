"""Named, isolated clients of existing loopback GameLens servers.

Configuration holds names and URLs only. Authentication comes from each port's
existing encrypted agent session file, never the legacy shared environment token.
"""

from __future__ import annotations

import json
import http.client
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from gamelens.mcp import GameLensClient, HTTP_TIMEOUT, ToolError
from gamelens.session import agent_token_path, read_agent_token

MAX_CLIENTS = 16
MAX_CONFIG_BYTES = 65536
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class ConfigError(ValueError):
    """Invalid configuration; no clients should be constructed or contacted."""


@dataclass(frozen=True)
class ClientSpec:
    name: str
    url: str
    port: int


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate JSON property in clients configuration")
        result[key] = value
    return result


def load_clients(path: str | Path) -> tuple[ClientSpec, ...]:
    """Read a bounded exact-shape config and reject ambiguous ports or names."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError("Clients configuration exceeds 64 KiB")
        config = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Clients configuration is missing, unreadable or invalid JSON") from None
    if not isinstance(config, dict) or set(config) != {"clients"}:
        raise ConfigError("Configuration must contain only clients")
    entries = config["clients"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CLIENTS:
        raise ConfigError("Configure between 1 and 16 clients")
    specs, names, ports = [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "url"}:
            raise ConfigError("Each client must contain only name and url; no credentials")
        name, url = entry["name"], entry["url"]
        if not isinstance(name, str) or NAME.fullmatch(name) is None:
            raise ConfigError("Client names must be 1-32 lowercase letters, digits, _ or -, starting with a letter")
        if not isinstance(url, str):
            raise ConfigError("Client URL must be an HTTP literal-loopback URL with an explicit port")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ConfigError("Invalid client URL") from None
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
                or parsed.username is not None or parsed.password is not None
                or port is None or not 1 <= port <= 65535
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ConfigError("Client URL must be http://127.0.0.1:PORT or http://[::1]:PORT without credentials, paths or queries")
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        canonical = f"http://{host}:{port}"
        if url not in (canonical, canonical + "/"):
            raise ConfigError("Client URL must use the canonical literal-loopback form")
        # Session token paths are keyed by port, even across address families.
        if name in names or port in ports:
            raise ConfigError("Client names and ports must be unique")
        names.add(name)
        ports.add(port)
        specs.append(ClientSpec(name, canonical, port))
    return tuple(specs)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PortSessionClient(GameLensClient):
    """Reuse HTTP/actions/binding; keep credentials local to this client's port."""

    def __init__(self, spec: ClientSpec):
        super().__init__(spec.url, None)
        self.port = spec.port
        # Never forward this port's credential to a redirected client/host, nor
        # route it through a process-wide proxy. Opener is owned by this client.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def token(self) -> str:
        try:
            return read_agent_token(agent_token_path(self.port))
        except Exception:
            raise ToolError(f"No readable agent session for selected client port {self.port}; start its desktop session. Multi-client mode ignores shared token environment variables.") from None

    def request(self, path: str, body: dict | None = None):
        """Same bounded HTTP semantics as legacy MCP, with no origin escapes.

        Keep transport exceptions chained: inherited tool_act uses that cause
        to distinguish an unknown outcome from an action proven not to arrive.
        No input authorization/arbiter guards are replicated here.
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data,
            method="POST" if data is not None else "GET",
            headers={"X-GameLens-Token": self.token(), "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), exc.headers
            except (http.client.HTTPException, OSError) as inner:
                raise ToolError(f"GameLens answered HTTP {exc.code} but the body was lost: {inner!r}") from inner
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise ToolError(f"GameLens is not reachable at {self.url}: {exc!r}") from exc


class MultiClient:
    def __init__(self, specs: tuple[ClientSpec, ...]):
        self.specs = specs
        self.clients = {spec.name: PortSessionClient(spec) for spec in specs}

    def tool_clients(self, args: dict):
        if args:
            raise ToolError("gamelens_clients accepts no arguments")
        inventory = [{"client": spec.name, "url": spec.url} for spec in self.specs]
        return [{"type": "text", "text": json.dumps({"clients": inventory,
            "selection_required": True, "input": "selected foreground window only"})}], False

    def _route(self, tool: str, args: dict):
        name = args.get("client")
        if not isinstance(name, str) or name not in self.clients:
            raise ToolError("An explicit configured client name is required; call gamelens_clients. Nothing was dispatched.")
        forwarded = {key: value for key, value in args.items() if key != "client"}
        try:
            content, is_error = getattr(self.clients[name], tool)(forwarded)
        except ToolError as exc:
            raise ToolError(f"client {name}: {exc}") from exc
        # Mark all images/results, including a fresh image returned on refusal.
        return [{"type": "text", "text": json.dumps({"client": name})}] + content, is_error

    def tool_state(self, args: dict):
        return self._route("tool_state", args)

    def tool_see(self, args: dict):
        return self._route("tool_see", args)

    def tool_act(self, args: dict):
        return self._route("tool_act", args)

    def tool_recording(self, args: dict):
        return self._route("tool_recording", args)

    def tool_session(self, args: dict):
        return self._route("tool_session", args)
