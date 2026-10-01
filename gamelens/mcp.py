"""GameLens as MCP tools: see the game, act in it, see the result -- one call.

    python -m gamelens.mcp [--url http://127.0.0.1:8777] [--token-file PATH]

A stdio MCP server that is an HTTP *client* of a running GameLens. It holds the
agent token and nothing else: it never calls /arm, /live, /stop or /windows, and
the server would refuse it if it did. Arming stays with the operator.

Why it exists (GL-039). Driving GameLens from an agent meant three HTTP round
trips per primitive -- fetch a frame for its observation id, POST /act, fetch
the frame after -- each one a separate tool call and a separate model turn.
Here `gamelens_act` does the act and returns the frame after it in the same
result, and a `sequence` action covers walk-while-turning in one call.

Binding. The image an agent decides on is nearly always past the arbiter's
0.8s limit by the time the decision arrives, because a model's turn is longer
than that. This server remembers the observation of the last image it actually
returned, sends that, and asks the server to rebind (`"rebind": true`) -- which
the server allows only when age is all that changed, and for a click only when
the pixels at the click point are still the ones shown. A refusal comes back
with a fresh image and is never retried here: deciding again is the agent's
job. `strict: true` turns rebinding off for a call.

Standard library only, on purpose: the protocol is five methods of
newline-delimited JSON-RPC, and a dependency installed into the Owner's venv is
not worth that. Logs go to stderr; stdout is the protocol and nothing else.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from gamelens.session import agent_token_path, read_agent_token

log = logging.getLogger("gamelens.mcp")

SERVER_NAME = "gamelens"
SERVER_VERSION = "0.46.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
HTTP_TIMEOUT = 10.0

ACTION_SCHEMA = {
    "type": "object",
    "description": (
        "One GameLens action, as the body of POST /act without observation_id. "
        "kind=click {x, y, button?}: x,y are pixels in the LAST IMAGE YOU WERE SHOWN. "
        "kind=key {key, hold? seconds}. kind=press {button?, hold? seconds}: hold a mouse "
        "button where the cursor already is (fire/attack/use in a game that hides the "
        "cursor). kind=look {dx, dy}: relative mouse move, turns a first/third-person "
        "camera. kind=scroll {clicks, horizontal?}: wheel notches, negative = down. "
        "kind=sequence {steps:[...]}: overlapping steps, each {do: key_down|key_up|tap|"
        "button_down|button_up|click|move|scroll|look|wait, key?, button?, x?, y?, dx?, "
        "dy?, clicks?, ms?}; move/click x,y are image pixels (drag = move, button_down, "
        "move, button_up; shift-click = key_down shift, click); everything pressed is "
        "released at the end; max 64 steps and 5s of waits; alt and escape only via "
        "kind=key. Keys: a-z, 0-9, f1-f11, space, enter, tab, escape, backspace, shift, "
        "ctrl, alt, rshift, rctrl, ralt, arrows, insert, delete, home, end, pageup, "
        "pagedown, numpad0-9, multiply, add, subtract, decimal, divide, and punctuation "
        "(minus equals lbracket rbracket backslash semicolon quote comma period slash "
        "backtick, or the character). Buttons: left, right, middle, x1/mouse4, x2/mouse5. "
        "Optional on any kind: measure, settle_ms, label."
        " Click may opt into anchor={x,y,width,height,color:'yellow'}: a visible yellow "
        "NPC/command label from the shown image, 20-256 pixels wide and 8-48 high. "
        "The click must be within 40 pixels of that label. GameLens verifies its glyphs "
        "at the same position on the newest frame, allowing nearby animation without "
        "loosening ordinary click checks. A changed/hidden label refuses; no retry. "
        "Anchor requires strict=false. Do not use anchors for unlabeled inventory items."
    ),
    "properties": {"kind": {"type": "string",
                            "enum": ["click", "key", "press", "look", "scroll", "sequence"]}},
    "required": ["kind"],
}

TOOLS = [
    {
        "name": "gamelens_state",
        "description": ("GameLens status: whether it is armed and live (the operator's "
                        "switches, which this tool cannot change), kill switch, capture "
                        "backend and fps, target window, executor counters."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "gamelens_see",
        "description": ("Look at the game: returns the newest frame. Coordinates for a "
                        "later click are pixels of this image."),
        "inputSchema": {
            "type": "object",
            "properties": {"quality": {"type": "integer", "minimum": 1, "maximum": 100,
                                       "default": 50}},
        },
    },
    {
        "name": "gamelens_act",
        "description": (
            "Act in the game and (by default) see the result in the same call. The action "
            "is bound to the last image you were shown; if that has aged out, GameLens "
            "carries it to the newest frame only when nothing but time has changed (and "
            "for a click, only when the screen at the click point is unchanged). If it "
            "refuses, you get the current image instead: decide again on it. An outcome "
            "of \"sent\" means the input was delivered. The picture after it is taken "
            "after_frames later, and a menu opening or closing can take longer than that "
            "to draw (Minecraft Bedrock: about 4 frames to pause, 13 to resume, at 32 fps) "
            "-- if nothing seems to have changed, call gamelens_see before repeating it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": ACTION_SCHEMA,
                "see_after": {"type": "boolean", "default": True},
                "after_frames": {"type": "integer", "minimum": 1, "maximum": 30,
                                 "default": 3,
                                 "description": "How many new frames to wait for before the "
                                                "picture after the action. 3 shows a camera "
                                                "turn; a menu transition may need 15."},
                "quality": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "strict": {"type": "boolean", "default": False,
                           "description": "Never rebind: act on the shown image or not at all."},
            },
            "required": ["action"],
        },
    },
]


class ToolError(Exception):
    """A tool that ran and failed: reported as a result with isError, not a
    protocol error, so the agent sees it and can act on it."""


class GameLensClient:
    def __init__(self, url: str, token_file: str | None) -> None:
        self.url = url.rstrip("/")
        self.token_file = token_file
        # The observation of the last image this server returned to the agent.
        # Replaced only by an image actually handed over, never by one fetched
        # and discarded -- it is the agent's view, not the server's.
        self.shown: str | None = None

    # --- transport ----------------------------------------------------------

    def token(self) -> str:
        """Read per request, so restarting GameLens (new tokens) needs no
        restart here once the token file is updated."""
        env = os.environ.get("GAMELENS_AGENT_TOKEN", "").strip()
        if env:
            return env
        port = urllib.parse.urlsplit(self.url).port or 80
        path = Path(self.token_file) if self.token_file else agent_token_path(port)
        try:
            return read_agent_token(path)
        except Exception:
            raise ToolError(
                f"no agent token: session at {path} is missing or unreadable. Start python -m gamelens "
                "--target <game> --no-agent. The operator uses Arm / Go live; "
                "these agent tools cannot. Explicit GAMELENS_AGENT_TOKEN or "
                "--token-file is also supported.") from None

    def request(self, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method="POST" if data is not None else "GET",
            headers={"X-GameLens-Token": self.token(), "Content-Type": "application/json"})
        # Body reads are inside the handlers too: a truncated body raises
        # http.client.IncompleteRead, which is neither URLError nor OSError, and
        # escaping as a bare exception it would lose the "may have been sent"
        # handling of an /act and the kept result of a later picture (GL040-RV01).
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), exc.headers
            except (http.client.HTTPException, OSError) as inner:
                raise ToolError(f"GameLens answered HTTP {exc.code} but the body was lost: "
                                f"{inner!r}") from inner
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise ToolError(f"GameLens is not reachable at {self.url}: {exc!r}") from exc

    # --- pieces ---------------------------------------------------------------

    def frame(self, quality: int, after: int | None = None, frames: int = 3):
        """``(content_items, detail_or_None)``; becomes the shown image on success."""
        query = {"quality": quality}
        if after is not None:
            query.update(after=after, frames=frames, wait_ms=1500)
        status, body, headers = self.request("/frame.jpg?" + urllib.parse.urlencode(query))
        if status != 200:
            detail = _detail(body) or f"HTTP {status}"
            return [], f"no frame: {detail}"
        obs = headers.get("X-GameLens-Observation")
        if not obs:
            return [], "frame came without an observation id"
        self.shown = obs
        meta = {"frame": _int(headers.get("X-GameLens-Frame")), "observation": obs}
        retention = headers.get("X-GameLens-Observation-Retention")
        if retention:
            meta["retention_seconds"] = retention
        return [
            {"type": "image", "data": base64.b64encode(body).decode("ascii"),
             "mimeType": "image/jpeg"},
            {"type": "text", "text": "image " + json.dumps(meta)},
        ], None

    # --- tools ----------------------------------------------------------------

    def tool_state(self, args: dict) -> tuple[list, bool]:
        status, body, _ = self.request("/state")
        if status != 200:
            raise ToolError(f"/state answered HTTP {status}: {_detail(body)}")
        state = json.loads(body)
        # The dashboard's log and overlay marks are long and are not state.
        for key in ("log", "marks"):
            state.pop(key, None)
        return [{"type": "text", "text": json.dumps(state, indent=1)}], False

    def tool_see(self, args: dict) -> tuple[list, bool]:
        quality = _bounded(args, "quality", 50, 1, 100)
        content, problem = self.frame(quality)
        if problem:
            raise ToolError(problem)
        return content, False

    def tool_act(self, args: dict) -> tuple[list, bool]:
        action = args.get("action")
        if not isinstance(action, dict) or not action.get("kind"):
            raise ToolError("action must be an object with a kind")
        quality = _bounded(args, "quality", 50, 1, 100)
        after_frames = _bounded(args, "after_frames", 3, 1, 30)
        # Real booleans only (GL039-I02): "true" for strict must not quietly
        # mean "rebind", which is the looser of the two.
        see_after = _flag(args, "see_after", True)
        strict = _flag(args, "strict", False)

        if "anchor" in action:
            if strict or action.get("kind") != "click":
                raise ToolError("anchor requires a click with strict=false")
            status, raw, _ = self.request("/state")
            try:
                supported = status == 200 and json.loads(raw).get("capabilities", {}).get("anchored_click") == "yellow-label-v1"
            except (ValueError, AttributeError):
                supported = False
            if not supported:
                raise ToolError("The running GameLens does not support label anchors. Restart the updated runtime; operator Arm/Go live is required again.")

        if self.shown is None:
            content, problem = self.frame(quality)
            text = "nothing was dispatched: you have not been shown the game yet; decide on this image"
            return [{"type": "text", "text": text}] + content, True

        body = {k: v for k, v in action.items() if k not in ("observation_id", "rebind")}
        body["observation_id"] = self.shown
        if not strict:
            body["rebind"] = True
        try:
            status, raw, _ = self.request("/act", body)
        except ToolError as exc:
            # No answer is not "not sent" (GL040-I04): a timeout or a reset
            # connection can come after the server has queued and injected
            # the action. Only a refused connection proves nothing arrived.
            if not _may_have_arrived(exc):
                raise
            content = [{"type": "text", "text": (
                "act OUTCOME UNKNOWN: the request was sent but no answer came back "
                f"({exc}). The action MAY HAVE BEEN CARRIED OUT -- do not repeat it; "
                "look at the picture below (or call gamelens_see) and decide again.")}]
            content += self._look_safely(quality, None, after_frames, outcome="unknown")
            return content, True
        try:
            result = json.loads(raw)
        except ValueError:
            result = {"detail": raw.decode("utf-8", "replace")}
        result["http_status"] = status
        content = [{"type": "text", "text": "act " + json.dumps(result)}]

        if status != 200:
            # Refused or failed: the agent needs the world as it is now to decide
            # again. Exactly one /act per call -- a retry here would be a second
            # decision nobody made.
            content += self._look_safely(quality, None, after_frames, outcome=None)
            return content, True

        if see_after:
            after = result.get("after_frame")
            content += self._look_safely(
                quality, after if isinstance(after, int) else None, after_frames,
                outcome=result.get("outcome"))
        return content, False

    def _look_safely(self, quality: int, after: int | None, frames: int, *, outcome) -> list:
        """The image after an /act, without ever losing the /act's own answer.

        Once /act has answered, what it said is the one thing the agent must
        not lose: a failure fetching the picture afterwards, reported instead of
        it, reads as "the action failed" and invites the same action again --
        which for a sent keystroke is a second one (GL039-I01).

        What it says about the action follows the executor's own outcome, not
        the HTTP status (GL039-I11): a 200 also covers "dry" (nothing injected)
        and "pending" (not yet decided), and calling those "sent" would be the
        confusion between acceptance and execution that GL-034 removed.
        """
        try:
            fresh, problem = self.frame(quality, after, frames) if after is not None \
                else self.frame(quality)
            if problem and after is not None:
                # Waiting past the action timed out (a still screen publishes
                # nothing new on WGC). Say so, then show what there is.
                fresh, second = self.frame(quality)
                return [{"type": "text",
                         "text": f"after-frame unavailable ({problem}); showing newest"}] + \
                    (fresh or [{"type": "text", "text": second}])
            return fresh or [{"type": "text", "text": problem}]
        except ToolError as exc:
            note = _AFTER_FAILED.get(outcome,
                                     "and the current picture could not be fetched either")
            return [{"type": "text", "text": f"{note} ({exc})"}]


def _may_have_arrived(exc: BaseException) -> bool:
    """Could the /act request have reached the server before this failure?

    No for a failure before sending (no token: no network cause at all) and for
    a refused connection. Yes for everything else -- a timeout or a reset can
    come after the server has already acted."""
    cause = exc.__cause__
    if cause is None:
        return False
    reason = getattr(cause, "reason", cause)
    return not isinstance(reason, ConnectionRefusedError)


_AFTER_FAILED = {
    "unknown": ("the action above may or may not have run; the picture afterwards also "
                "failed -- do not repeat it, call gamelens_state and gamelens_see"),
    "sent": ("the action above WAS SENT; only the picture afterwards failed -- do not "
             "repeat it, call gamelens_see"),
    "dry": ("the action above was NOT injected (dry run: GameLens is not live); the picture "
            "afterwards also failed -- call gamelens_state and gamelens_see"),
    "pending": ("the action above is still UNDECIDED (queued behind other input; it may yet "
                "run or be refused); the picture afterwards also failed -- do not repeat it, "
                "call gamelens_see"),
}


def _detail(body: bytes) -> str:
    try:
        value = json.loads(body)
        return str(value.get("detail", value)) if isinstance(value, dict) else str(value)
    except ValueError:
        return body.decode("utf-8", "replace")[:200]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be true or false")
    return value


def _bounded(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ToolError(f"{key} must be an integer from {lo} to {hi}")
    return value


# --- JSON-RPC over stdio -------------------------------------------------------


class Server:
    def __init__(self, client: GameLensClient) -> None:
        self.client = client
        self.handlers = {
            "gamelens_state": client.tool_state,
            "gamelens_see": client.tool_see,
            "gamelens_act": client.tool_act,
        }

    def handle(self, message: dict) -> dict | None:
        """One JSON-RPC message in, the response out (None for a notification)."""
        method = message.get("method")
        msg_id = message.get("id")
        is_request = "id" in message
        params = message.get("params")
        if params is None:
            params = {}
        # Checked before anything reads them (GL039-I03): a list where an object
        # belongs used to raise here and take the whole session down.
        if not isinstance(method, str):
            return _err(msg_id, -32600, "invalid request: method") if is_request else None
        if not isinstance(params, dict):
            return _err(msg_id, -32602, "invalid params: expected an object") if is_request else None

        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _ok(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Call gamelens_state and gamelens_see before gamelens_act. Coordinates "
                    "are pixels of the last shown image. Snapshots last up to 120s; strict "
                    "actions still use the original short freshness limits. Rebind checks "
                    "session, geometry and point pixels. A denial returns a new image: "
                    "inspect it and decide again, never blindly retry. sent means injected, "
                    "not proof of game effect. The operator arms/goes live; tools cannot. "
                    "Live input uses the shared foreground mouse/keyboard."),
            })
        if method == "ping":
            return _ok(msg_id, {})
        if method == "tools/list":
            return _ok(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            handler = self.handlers.get(name) if isinstance(name, str) else None
            if handler is None:
                return _err(msg_id, -32602, f"unknown tool {name!r}")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _err(msg_id, -32602, "arguments must be an object")
            try:
                content, is_error = handler(args)
            except ToolError as exc:
                content, is_error = [{"type": "text", "text": str(exc)}], True
            except Exception as exc:                 # a bug here must not kill the session
                log.exception("tool %s failed", name)
                content, is_error = [{"type": "text", "text": f"internal error: {exc}"}], True
            return _ok(msg_id, {"content": content, "isError": is_error})
        if not is_request:
            return None                              # notifications/initialized and friends
        return _err(msg_id, -32601, f"method not found: {method}")

    def serve(self, stdin, stdout) -> None:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                _write(stdout, _err(None, -32700, "parse error"))
                continue
            if not isinstance(message, dict):
                _write(stdout, _err(None, -32600, "invalid request"))
                continue
            try:
                reply = self.handle(message)
            except Exception as exc:
                # Last line of defence: one bad message must never end the
                # session every later call depends on.
                log.exception("request failed")
                reply = (_err(message.get("id"), -32603, f"internal error: {exc}")
                         if "id" in message else None)
            if reply is not None:
                _write(stdout, reply)


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(stdout, message: dict) -> None:
    stdout.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
    stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gamelens.mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=os.environ.get("GAMELENS_URL", "http://127.0.0.1:8777"))
    parser.add_argument("--token-file", default=os.environ.get("GAMELENS_TOKEN_FILE"),
                        help="agent token file; default is the Windows-encrypted "
                             "local GameLens session (GAMELENS_AGENT_TOKEN takes precedence)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Server(GameLensClient(args.url, args.token_file)).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
