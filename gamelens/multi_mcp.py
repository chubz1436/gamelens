"""Opt-in named-client MCP: python -m gamelens.multi_mcp --clients-file PATH."""

from __future__ import annotations

import argparse
import copy
import logging
import sys

from gamelens.mcp import Server, TOOLS
from gamelens.multi_client import ConfigError, MultiClient, load_clients


class MultiServer(Server):
    """Keep the existing protocol loop/error handling and curated profile tool."""

    def __init__(self, client: MultiClient):
        super().__init__(client)
        self.handlers["gamelens_clients"] = client.tool_clients
        self.tools = copy.deepcopy(TOOLS)
        for tool in self.tools:
            if tool["name"] == "gamelens_profile":
                continue
            schema = tool["inputSchema"]
            schema["properties"]["client"] = {"type": "string", "enum": list(client.clients),
                "description": "Required configured client name; there is no default or broadcast."}
            schema.setdefault("required", []).append("client")
            tool["description"] = "For the explicitly selected client only. " + tool["description"]
        self.tools.insert(0, {"name": "gamelens_clients", "description":
            "List configured named local clients without contacting capture servers or sending input. Each tool call requires its own client selector; no automatic switching or broadcast.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False}})

    def handle(self, message: dict):
        reply = super().handle(message)
        if reply and "result" in reply:
            if message.get("method") == "tools/list":
                reply["result"]["tools"] = self.tools
            elif message.get("method") == "initialize":
                result = reply["result"]
                result["serverInfo"]["name"] = "gamelens-multi"
                result["instructions"] = (
                    "Call gamelens_clients to discover names. Select client explicitly on every session, state, see, act and recording call. Observations and port session credentials are isolated; seeing one client never authorizes acting in another. No default, broadcast, focus or automatic lifecycle control. Session arm/live/stop are explicit owner-authorized calls. Multiple capture sessions may run, but keyboard/mouse input is shared and only the selected foreground target may receive input. "
                    + result["instructions"])
        return reply


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gamelens.multi_mcp", description=__doc__)
    parser.add_argument("--clients-file", required=True, help="Local JSON file containing named loopback URLs; no credentials")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        specs = load_clients(args.clients_file)
    except ConfigError as exc:
        print(f"GameLens multi-client configuration refused: {exc}", file=sys.stderr)
        return 2
    MultiServer(MultiClient(specs)).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
