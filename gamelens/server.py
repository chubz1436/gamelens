"""Local HTTP surface: live stream, state, and the control endpoints.

Binding to 127.0.0.1 is not an access control. Every process on the machine can
reach loopback, and so can any web page the operator happens to open, which is
why `Origin` and `Host` are checked and why the dashboard is served without a
credential in it. Two capabilities, two tokens: the operator can arm and go
live; an agent can only propose actions.
"""

from __future__ import annotations

import asyncio
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

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

MJPEG_BOUNDARY = "gamelens-frame"


class Tokens:
    """Two capabilities. The agent's credential can never arm the system."""

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
            "  agent token (submit actions only):\n"
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
        return JSONResponse(runtime.state())

    @app.get("/windows")
    def windows(role: str = Depends(operator_only)) -> JSONResponse:
        from gamelens.windows import list_windows
        return JSONResponse([w.to_dict() for w in list_windows()])

    @app.get("/frame.jpg")
    def frame_jpg(quality: int = 70, role: str = Depends(any_role)) -> Response:
        jpeg, observation_id = runtime.encode_latest(quality=quality)
        if jpeg is None:
            raise HTTPException(503, "no frame available yet")
        # The caller needs this to act on what it just looked at. Without it the
        # only way to name a frame is to guess an id from /state, which will
        # already be several frames out of date at 120fps.
        return Response(
            jpeg, media_type="image/jpeg",
            headers={"X-GameLens-Observation": observation_id},
        )

    @app.get("/stream.mjpg")
    async def stream(fps: int = 20, quality: int = 65, role: str = Depends(any_role)):
        interval = 1.0 / max(1, min(fps, 60))

        async def frames():
            last_id = ""
            while True:
                # Off the loop: this acquires a pool buffer and JPEG-encodes it.
                # A 3441x1440 frame is tens of milliseconds, every frame, and
                # the stream is the one endpoint that runs continuously.
                jpeg, observation_id = await asyncio.to_thread(
                    runtime.encode_latest, quality
                )
                if jpeg is not None and observation_id != last_id:
                    last_id = observation_id
                    # Each part names the observation it is, so a consumer
                    # reading the stream can act on the exact image it saw.
                    yield (
                        b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"X-GameLens-Observation: " + observation_id.encode() + b"\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        + jpeg + b"\r\n"
                    )
                await asyncio.sleep(interval)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )

    # --- control paths ----------------------------------------------------

    @app.post("/arm")
    def arm(role: str = Depends(operator_only)) -> JSONResponse:
        runtime.safety.arm()
        runtime.log.add("ARMED (still dry-run)", "dry")
        return JSONResponse(runtime.state())

    @app.post("/live")
    def live(role: str = Depends(operator_only)) -> JSONResponse:
        runtime.safety.go_live()
        runtime.log.add("LIVE -- input will be injected", "sent")
        return JSONResponse(runtime.state())

    @app.post("/stop")
    def stop(role: str = Depends(operator_only)) -> JSONResponse:
        """The one endpoint whose latency is a safety property.

        Sync on purpose, so it is served from a worker thread. It also takes the
        dispatch boundary and runs the release callbacks, which reach SendInput
        -- none of that belongs on the loop that has to stay free to accept the
        request in the first place.
        """
        runtime.safety.kill("dashboard stop")
        runtime.log.add("STOPPED by operator", "denied")
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
        result = await asyncio.to_thread(
            runtime.submit_click,
            observation_id=str(observation_id),
            x=float(body["x"]),
            y=float(body["y"]),
            label=str(body.get("label", "http")),
            source=role,
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
