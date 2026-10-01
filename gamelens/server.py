"""Local HTTP surface: live stream, state, and the control endpoints.

Binding to 127.0.0.1 is not an access control. Every process on the machine can
reach loopback, and so can any web page the operator happens to open, which is
why `Origin` and `Host` are checked and why the dashboard has no embedded
credential. Both authenticated roles can explicitly Arm/Live/Stop for an
owner-authorized task; window enumeration and desktop handoff remain separate.
"""

from __future__ import annotations

import asyncio
import math
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from gamelens.arbiter import _scroll_clicks, parse_sequence
from gamelens.target import parse_anchor

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

MJPEG_BOUNDARY = "gamelens-frame"

# At most this many `/frame.jpg?after=` requests may be waiting at once; the
# next is told 429 before it waits at all. The wait itself runs on the event
# loop and holds no worker thread, so this is not what protects /stop -- it
# bounds how many pollers the loop carries for callers that ask for a frame
# that is never coming.
MAX_FRAME_WAITS = 4

# How often a waiting `/frame.jpg?after=` looks at the newest frame id. Well
# under one frame interval at the rates seen (50-90fps), and each look is a
# lock-guarded integer read.
FRAME_POLL = 0.004


class Encoded:
    """One encoded frame and the ids that name it."""

    __slots__ = ("jpeg", "observation_id", "frame_id")

    def __init__(self, jpeg: bytes, observation_id: str, frame_id: int) -> None:
        self.jpeg = jpeg
        self.observation_id = observation_id
        self.frame_id = frame_id


class _Outcome:
    """A named non-frame result, so `is NO_FRAME` reads as what it means."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


# Nothing new enough in the slot -- empty, older than asked for, or cleared by
# a failover. Worth waiting through.
NO_FRAME = _Outcome("NO_FRAME")
# A frame was leased and could not be encoded. Waiting would re-encode the same
# frame and fail the same way.
ENCODE_FAILED = _Outcome("ENCODE_FAILED")


class Tokens:
    """Separate credentials; session controls accept either authenticated role."""

    def __init__(self) -> None:
        self.operator = secrets.token_urlsafe(24)
        self.agent = secrets.token_urlsafe(24)

    def role_for(self, presented: str | None) -> str | None:
        if not presented:
            return None
        # Constant-time both ways so a wrong token leaks nothing by timing.
        if secrets.compare_digest(presented, self.operator):
            return "operator"
        if secrets.compare_digest(presented, self.agent):
            return "agent"
        return None

    def banner(self, host: str, port: int) -> str:
        return (
            "\n"
            "  GameLens is listening on http://%s:%d/\n"
            "\n"
            "  operator token (arm / go live / stop, paste into the dashboard):\n"
            "    %s\n"
            "  agent token (observations, actions and authorized session controls):\n"
            "    %s\n"
            "\n"
            "  The dashboard is served without a token in it, so anything else on\n"
            "  this machine that fetches it still cannot control the game.\n"
        ) % (host, port, self.operator, self.agent)


class ActionLog:
    """Recent decisions, for the dashboard. Bounded; the newest matter most.

    Entries are mutable after the fact on purpose. An action is written down
    when it is queued and *resolved* when the executor says what became of it,
    because the two can differ: the arbiter accepting an action says the world
    was right at submission, and the press happens later, behind interlocks
    that get another vote. A log that only ever records the first half reads as
    a list of things that happened, which is a claim it cannot support.

    Written by the input thread, read by the HTTP thread, so the whole thing is
    behind one lock.
    """

    def __init__(self, capacity: int = 200) -> None:
        self._entries: deque = deque(maxlen=capacity)
        self._marks: deque = deque(maxlen=24)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, text: str, status: str = "sent") -> int:
        """Record an entry and return its id, so it can be resolved later."""
        with self._lock:
            self._seq += 1
            entry_id = self._seq
            self._entries.append({
                "id": entry_id,
                "time": time.strftime("%H:%M:%S"),
                "text": text,
                "status": status,
            })
        return entry_id

    def mark(self, x: float, y: float, status: str, label: str = "",
             entry_id: int | None = None) -> None:
        with self._lock:
            self._marks.append({
                "x": float(x), "y": float(y), "status": status,
                "label": label, "at": time.monotonic(), "entry_id": entry_id,
            })

    def resolve(self, entry_id: int, status: str, detail: str = "") -> None:
        """Replace a queued entry's status with what actually happened.

        The overlay mark moves with it. A mark that stays green after the press
        was refused is worse than no mark: it is drawn on the exact spot the
        click did not land.
        """
        with self._lock:
            for entry in reversed(self._entries):
                if entry["id"] == entry_id:
                    entry["status"] = status
                    if detail:
                        entry["text"] = f"{entry['text']} -- {detail}"
                    break
            for mark in self._marks:
                if mark.get("entry_id") == entry_id:
                    mark["status"] = status

    def entries(self) -> list:
        with self._lock:
            return [dict(e) for e in list(self._entries)[-40:]]

    def marks(self) -> list:
        now = time.monotonic()
        with self._lock:
            return [
                {**m, "age_ms": (now - m["at"]) * 1000}
                for m in self._marks
                if now - m["at"] < 2.0
            ]


def _button_name(value) -> str:
    """The caller's button name, rejected here if it is not one."""
    from gamelens.app import _button

    _button(value)                 # raises ValueError on anything unknown
    return str(value).lower()


def create_app(runtime) -> FastAPI:
    """Build the app around a GameLens runtime (see gamelens.app)."""
    app = FastAPI(title="GameLens", docs_url=None, redoc_url=None)
    tokens: Tokens = runtime.tokens

    allowed_hosts = {
        f"127.0.0.1:{runtime.port}", f"localhost:{runtime.port}",
        "127.0.0.1", "localhost",
    }
    allowed_origins = {f"http://127.0.0.1:{runtime.port}", f"http://localhost:{runtime.port}"}

    def _check_headers(request: Request) -> None:
        """Reject requests that a browser on another origin could have sent.

        A missing Origin is normal for same-origin fetches and for curl. A
        *foreign* Origin means a page somewhere else tried to drive this, and an
        unexpected Host is the shape of a DNS-rebinding attempt.
        """
        host = (request.headers.get("host") or "").lower()
        if host and host not in allowed_hosts:
            raise HTTPException(403, f"unexpected Host header: {host!r}")
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            raise HTTPException(403, f"cross-origin request refused: {origin!r}")

    def require_role(*allowed: str):
        def dependency(
            request: Request,
            x_gamelens_token: str | None = Header(default=None),
            token: str | None = Query(default=None),
        ) -> str:
            _check_headers(request)
            # The query parameter exists for <img src> on the MJPEG stream,
            # which cannot carry a custom header. It is the same secret.
            role = tokens.role_for(x_gamelens_token or token)
            if role is None:
                raise HTTPException(401, "missing or invalid session token")
            if role not in allowed:
                raise HTTPException(
                    403,
                    f"the {role} token cannot perform this action; "
                    f"it requires one of: {', '.join(allowed)}",
                )
            return role
        return dependency

    any_role = require_role("operator", "agent")
    operator_only = require_role("operator")

    # --- the shell: deliberately unauthenticated and deliberately empty ---

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        _check_headers(request)
        html = (_STATIC / "dashboard.html").read_text(encoding="utf-8")
        # No substitution happens here. Injecting the token into this page is
        # what would make it worthless: any local process could GET / and read
        # the credential straight back out.
        return HTMLResponse(html)

    # --- read paths -------------------------------------------------------
    #
    # Everything below that touches Win32, the buffer pool or an encoder is a
    # plain `def`, not `async def`. FastAPI runs a sync endpoint in a worker
    # thread; an `async def` that blocks runs *on the event loop* and stops the
    # server answering anything else while it does. That is not a throughput
    # concern here -- it is the Stop button. Measured before this changed: a
    # /state issued 50ms into an /act took 159ms, because it was waiting for
    # the click sequence to finish.

    @app.get("/state")
    def state(role: str = Depends(any_role)) -> JSONResponse:
        return JSONResponse({**runtime.state(), "role": role})

    @app.get("/windows")
    def windows(role: str = Depends(operator_only)) -> JSONResponse:
        from gamelens.windows import list_windows
        return JSONResponse([w.to_dict() for w in list_windows()])

    waiting = {"frames": 0}          # in-flight `after=` waits; loop-only, no lock

    @app.get("/frame.jpg")
    async def frame_jpg(
        quality: int = 70,
        after: int | None = Query(default=None, ge=0),
        frames: int = Query(default=1, ge=1, le=30),
        wait_ms: int = Query(default=500, ge=0, le=2000),
        role: str = Depends(any_role),
    ) -> Response:
        """The newest frame -- or, with ``after``, the newest frame at least
        ``frames`` past the id an /act returned as ``after_frame``.

        An action is acknowledged when injected, not when drawn, so the frame
        that is newest the moment /act answers can still show the world before
        it. ``after`` replaces the guessed sleep a caller would otherwise need.

        `async` on purpose, unlike the other read paths. The wait can last
        seconds, and a sync endpoint would spend all of it holding a worker
        from the pool that /stop is served from. So the wait polls on the loop,
        which holds nothing, and only the encode goes to a thread.
        """
        if after is None:
            result = await asyncio.to_thread(runtime.encode_frame, quality)
            if result is ENCODE_FAILED:
                raise HTTPException(503, "frame encode failed")
            if not isinstance(result, Encoded):
                raise HTTPException(503, "no frame available yet")
            return _frame_response(result)

        if waiting["frames"] >= MAX_FRAME_WAITS:
            raise HTTPException(429, "too many frame waits in flight")
        waiting["frames"] += 1
        try:
            target = after + frames
            deadline = time.monotonic() + wait_ms / 1000.0
            while True:
                if runtime.latest_frame_id() >= target:
                    result = await asyncio.to_thread(
                        runtime.encode_frame, quality, min_frame_id=target
                    )
                    if isinstance(result, Encoded):
                        return _frame_response(result)
                    if result is ENCODE_FAILED:
                        raise HTTPException(503, "frame encode failed")
                    # NO_FRAME: the qualifying frame went between the look and
                    # the lease -- a failover cleared the slot. The deadline has
                    # not moved, so keep waiting for its replacement.
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(FRAME_POLL)
        finally:
            waiting["frames"] -= 1
        # Never the stale frame: getting past it is what the caller asked for.
        raise HTTPException(
            504, f"no frame with id >= {target} within {wait_ms}ms",
            headers={"X-GameLens-Latest-Frame": str(runtime.latest_frame_id())},
        )

    def _frame_response(result: Encoded) -> Response:
        # The observation id is what a caller acts on; without it the only way
        # to name a frame is to guess an id from /state, already several frames
        # stale at 120fps. The frame id is what it waits past next time.
        headers = {
            "X-GameLens-Observation": result.observation_id,
            "X-GameLens-Frame": str(result.frame_id),
        }
        registry = getattr(runtime, "observations", None)
        if registry is not None:
            headers["X-GameLens-Observation-Retention"] = str(registry.retention_seconds)
        return Response(
            result.jpeg, media_type="image/jpeg",
            headers=headers,
        )

    @app.get("/stream.mjpg")
    async def stream(fps: int = 20, quality: int = 65, role: str = Depends(any_role)):
        interval = 1.0 / max(1, min(fps, 60))

        async def frames():
            last_frame = 0
            while True:
                # Off the loop: this acquires a pool buffer and JPEG-encodes it.
                # A 3441x1440 frame is tens of milliseconds, every frame, and
                # the stream is the one endpoint that runs continuously.
                result = await asyncio.to_thread(runtime.encode_frame, quality, retain=False)
                if isinstance(result, Encoded) and result.frame_id != last_frame:
                    last_frame = result.frame_id
                    # Each part names the observation it is, so a consumer
                    # reading the stream can act on the exact image it saw, and
                    # the frame it came from -- taken off the encoded frame, not
                    # the slot, which may already hold a newer one.
                    yield (
                        b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"X-GameLens-Observation: " + result.observation_id.encode() + b"\r\n"
                        b"X-GameLens-Frame: " + str(result.frame_id).encode() + b"\r\n"
                        b"Content-Length: " + str(len(result.jpeg)).encode() + b"\r\n\r\n"
                        + result.jpeg + b"\r\n"
                    )
                await asyncio.sleep(interval)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )

    # --- control paths ----------------------------------------------------

    @app.get("/recording")
    def recording_state(role: str = Depends(any_role)) -> JSONResponse:
        recorder = getattr(runtime, "recorder", None)
        if recorder is None:
            raise HTTPException(503, "Recording is unavailable in this runtime")
        return JSONResponse(recorder.snapshot())

    @app.post("/recording/start")
    def recording_start(body: dict | None = None, role: str = Depends(any_role)) -> JSONResponse:
        recorder = getattr(runtime, "recorder", None)
        if recorder is None:
            raise HTTPException(503, "Recording is unavailable in this runtime")
        try:
            return JSONResponse(recorder.start((body or {}).get("fps", 30)))
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/recording/stop")
    def recording_stop(role: str = Depends(any_role)) -> JSONResponse:
        recorder = getattr(runtime, "recorder", None)
        if recorder is None:
            raise HTTPException(503, "Recording is unavailable in this runtime")
        try:
            return JSONResponse(recorder.stop())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/recording/folder")
    def recording_folder(role: str = Depends(operator_only)) -> JSONResponse:
        recorder = getattr(runtime, "recorder", None)
        if recorder is None:
            raise HTTPException(503, "Recording is unavailable in this runtime")
        try:
            recorder.open_folder()
        except OSError:
            raise HTTPException(409, "Could not open the recordings folder") from None
        return JSONResponse({"folder": str(recorder.directory)})

    @app.post("/arm")
    def arm(role: str = Depends(any_role)) -> JSONResponse:
        from gamelens.safety import NotPermitted
        try:
            runtime.safety.arm()
        except NotPermitted as exc:
            raise HTTPException(409, str(exc)) from None
        runtime.log.add(f"ARMED by {role}", "dry")
        return JSONResponse(runtime.state())

    @app.post("/live")
    def live(role: str = Depends(any_role)) -> JSONResponse:
        from gamelens.safety import NotPermitted
        try:
            runtime.safety.go_live()
        except NotPermitted as exc:
            raise HTTPException(409, str(exc)) from None
        runtime.log.add(f"LIVE by {role} -- input will be injected", "sent")
        return JSONResponse(runtime.state())

    @app.post("/stop")
    def stop(role: str = Depends(any_role)) -> JSONResponse:
        """The one endpoint whose latency is a safety property.

        Sync on purpose, so it is served from a worker thread. It also takes the
        dispatch boundary and runs the release callbacks, which reach SendInput
        -- none of that belongs on the loop that has to stay free to accept the
        request in the first place.
        """
        runtime.safety.kill(f"session stop by {role}")
        runtime.log.add(f"STOPPED by {role}", "denied")
        return JSONResponse(runtime.state())

    @app.post("/act")
    async def act(request: Request, role: str = Depends(any_role)) -> JSONResponse:
        """Act on an image the server issued, named by its observation id.

        The caller sends the id it was given with the picture, plus a coordinate
        in that picture's pixels. Everything the arbiter checks -- when the frame
        was captured, the geometry generation, the preemption counter, the
        transport scale -- comes from the record the server made when it handed
        out the image. A caller cannot assert its own freshness, and coordinates
        are interpreted in the image it actually saw.

        The response reports **two** things, because they differ. ``verdict`` is
        acceptance; ``outcome`` is what the executor did about it afterwards. An
        accepted action is still refused at press time if focus moved during the
        queue and the dwell, and a caller told only ``"verdict": "ok"`` has been
        told the queue took it, not that the game saw it. The request waits
        briefly for the real answer; ``"outcome": "pending"`` means it did not
        arrive in time, never that it succeeded.

        ``churn`` is a third thing, and weaker on purpose: how much the screen
        differs from before the action once it has settled afterwards, or null
        when it was not measured. It straddles the action rather than spanning
        it, because a game animates feedback for as long as a button is held --
        so a comparison taken *during* an action reports motion whether or not
        anything came of it. It is there
        because "injected correctly" and "had any effect" are different
        questions, and a caller that can only see the first cannot tell a
        working action from a mining hold shorter than the block's break time.
        It is evidence rather than a verdict -- rain moves pixels on its own,
        and walking into a wall moves none -- so it never decides the status
        code. It is null unless the request asks for it with ``"measure": true``,
        because obtaining it means waiting for the screen to settle afterwards.
        """
        body = await request.json()
        observation_id = body.get("observation_id")
        if not observation_id:
            raise HTTPException(
                400,
                "observation_id is required; it is returned with every image in "
                "the X-GameLens-Observation header",
            )
        # Off the loop. This one waits -- up to DISPATCH_WAIT -- for the
        # executor to report what it did, and an `async def` that waits on a
        # threading primitive holds the whole server still while it does. The
        # first version of this blocked /stop for the length of every click.
        kind = str(body.get("kind", "click")).lower()
        label = str(body.get("label", "http"))
        # Parsed and validated here, with the other numeric fields, because a
        # malformed duration has to fail the request *before* anything is
        # injected. Raising later -- after the game already has the keystroke --
        # would turn a delivered action into a 500 and invite a retry that
        # sends it twice.
        extra: dict = {}
        try:
            if "settle_ms" in body and body["settle_ms"] is not None:
                settle = float(body["settle_ms"]) / 1000.0
                if not math.isfinite(settle):
                    raise ValueError("settle_ms must be finite")
                extra["settle"] = settle
            if kind == "click":
                call = dict(
                    fn=runtime.submit_click,
                    x=float(body["x"]), y=float(body["y"]),
                    # Validated here, not just inside submit_click: a bad
                    # field must fail the request before anything is injected,
                    # the same way a malformed coordinate does. _button raises
                    # ValueError, which the handler below turns into a 400.
                    button=_button_name(body.get("button", "left")),
                )
            elif kind == "key":
                call = dict(
                    fn=runtime.submit_key,
                    key=str(body["key"]), hold=float(body.get("hold", 0.08)),
                )
            elif kind == "press":
                call = dict(
                    fn=runtime.submit_press,
                    button=str(body.get("button", "left")),
                    hold=float(body.get("hold", 0.08)),
                )
            elif kind == "look":
                call = dict(
                    fn=runtime.submit_look,
                    dx=float(body["dx"]), dy=float(body["dy"]),
                )
            elif kind == "scroll":
                horizontal = body.get("horizontal", False)
                if not isinstance(horizontal, bool):
                    raise ValueError("horizontal must be true or false")
                call = dict(
                    fn=runtime.submit_scroll,
                    # Checked here so a bad count is a 400 before anything is
                    # queued; scroll_action clamps what passes.
                    clicks=_scroll_clicks(body.get("clicks")),
                    horizontal=horizontal,
                )
            elif kind == "sequence":
                # Parsed whole here, before anything is queued: every rule
                # about what a sequence may contain raises ValueError, which is
                # this request's 400 rather than a half-run sequence.
                call = dict(
                    fn=runtime.submit_sequence,
                    steps=parse_sequence(body.get("steps"),
                                         capacity=runtime.sequence_capacity()),
                )
            else:
                raise HTTPException(
                    400,
                    f"unknown kind {kind!r}; use click, press, key, look, scroll or sequence")
            # Opt-in, and it has to be a real boolean: "false" is a truthy
            # string, and a flag that loosens binding must not switch on by
            # accident. See GameLens._bind.
            rebind = body.get("rebind", False)
            if not isinstance(rebind, bool):
                raise ValueError("rebind must be true or false")
            if rebind:
                call["rebind"] = True
            if "anchor" in body:
                if kind != "click" or not rebind:
                    raise ValueError("anchor is only supported for click with rebind=true")
                call["anchor"] = parse_anchor(body["anchor"], call["x"], call["y"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # OverflowError is in the list because JSON has no float limit: an
            # integer literal with four hundred zeros is valid JSON and a valid
            # Python int, and float() raises on it rather than returning inf.
            # Without this it is a 500 for a request the caller can see is bad.
            raise HTTPException(400, f"bad {kind} action: {exc}")

        fn = call.pop("fn")
        result = await asyncio.to_thread(
            fn,
            observation_id=str(observation_id),
            label=label,
            source=role,
            # Opt-in: the effect measurement waits for the screen to settle, so
            # it costs roughly 150ms on top of the action. A caller checking
            # whether its actions are landing wants that; a caller running a
            # reflex loop at forty actions a second does not.
            measure=bool(body.get("measure", False)),
            **extra,
            **call,
        )
        payload = result.to_dict()
        if result.outcome == "error":
            return JSONResponse(payload, status_code=500)
        return JSONResponse(payload, status_code=200 if result.ok else 409)

    return app


def encode_jpeg(
    array: np.ndarray, quality: int = 70, max_width: int = 1280
) -> tuple[bytes, float]:
    """BGRA frame -> (JPEG bytes, scale applied).

    The scale is returned, not discarded. Transport downscaling changes what a
    coordinate in the delivered image means: on a 1920-wide frame served at
    1280, image x=640 is native x=960. A caller that never learns the factor
    cannot place a click correctly, and nothing about the request would look
    wrong.
    """
    bgr = array[:, :, :3]
    height, width = bgr.shape[:2]
    scale = 1.0
    if width > max_width:
        scale = max_width / width
        bgr = cv2.resize(
            bgr, (max_width, int(height * scale)), interpolation=cv2.INTER_AREA
        )
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes(), scale
