"""Named, isolated clients of existing loopback GameLens servers.

Configuration holds names and URLs only. Authentication comes from each port's
existing encrypted agent session file, never the legacy shared environment token.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from gamelens.mcp import GameLensClient, ToolError
from gamelens.session import agent_token_path, read_agent_token
from gamelens.transport import validate_url

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
            canonical = validate_url(url)
        except ValueError:
            raise ConfigError("Client URL must use canonical literal-loopback HTTP form with an explicit port") from None
        port = int(canonical.rsplit(":", 1)[1])
        # Session token paths are keyed by port, even across address families.
        if name in names or port in ports:
            raise ConfigError("Client names and ports must be unique")
        names.add(name)
        ports.add(port)
        specs.append(ClientSpec(name, canonical, port))
    return tuple(specs)


class PortSessionClient(GameLensClient):
    """Reuse HTTP/actions/binding; keep credentials local to this client's port."""

    def __init__(self, spec: ClientSpec):
        super().__init__(spec.url, None)
        self.port = spec.port

    def token(self) -> str:
        try:
            return read_agent_token(agent_token_path(self.port))
        except Exception:
            raise ToolError(f"No readable agent session for selected client port {self.port}; start its desktop session. Multi-client mode ignores shared token environment variables.") from None



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
