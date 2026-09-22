"""Two tiers of decision-making, running at very different speeds.

The split exists because the costs are three orders of magnitude apart. A pixel
probe or a template match costs under a millisecond; a vision model call costs
hundreds. Routing everything through the model would make the system as slow as
its slowest component for decisions that never needed it, and routing nothing
through it would leave a bot that can only react to things it was told about in
advance.

So: reflexes handle what must be fast and can be recognised mechanically, and
the model handles what needs judgement. When a reflex fires it preempts the
model, because whatever the model is currently reasoning about has just been
invalidated by the reflex's own action.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from gamelens.arbiter import ActionRejected, Rejection

log = logging.getLogger(__name__)


# --- fast tier -------------------------------------------------------------


@dataclass
class Reflex:
    """A mechanical rule: when *test* sees something, act immediately.

    ``test`` receives the BGRA frame and must be cheap -- it runs on every
    distinct frame. Anything that needs to look at the whole image at full
    resolution does not belong here.
    """

    name: str
    test: Callable[[np.ndarray], tuple[float, float] | None]
    label: str = ""
    cooldown: float = 0.5
    _last_fired: float = field(default=0.0, init=False)

    def ready(self) -> bool:
        return (time.monotonic() - self._last_fired) >= self.cooldown

    def fired(self) -> None:
        self._last_fired = time.monotonic()


def template_reflex(
    name: str, template_path: str, *, threshold: float = 0.87, cooldown: float = 0.5
) -> Reflex:
    """Fire when a template image appears, returning its centre."""
    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if template is None:
        raise FileNotFoundError(f"template not found: {template_path}")
    th, tw = template.shape[:2]

    def test(frame: np.ndarray):
        result = cv2.matchTemplate(frame[:, :, :3], template, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        if score < threshold:
            return None
        return loc[0] + tw / 2, loc[1] + th / 2

    return Reflex(name=name, test=test, label=name, cooldown=cooldown)


def pixel_reflex(
    name: str, x: int, y: int, bgr: tuple[int, int, int],
    *, tolerance: int = 12, click_at: tuple[int, int] | None = None,
    cooldown: float = 0.5,
) -> Reflex:
    """Fire when one pixel takes a given colour. The cheapest possible check."""
    target = np.array(bgr, dtype=np.int16)
    where = click_at or (x, y)

    def test(frame: np.ndarray):
        if y >= frame.shape[0] or x >= frame.shape[1]:
            return None
        if np.abs(frame[y, x, :3].astype(np.int16) - target).max() <= tolerance:
            return float(where[0]), float(where[1])
        return None

    return Reflex(name=name, test=test, label=name, cooldown=cooldown)


# --- slow tier -------------------------------------------------------------


VISION_SYSTEM_PROMPT = """You are controlling a game through a screenshot.

Reply with a single JSON object and nothing else:
  {"action": "click", "x": <int>, "y": <int>, "why": "<short reason>"}
  {"action": "wait", "why": "<short reason>"}

Coordinates are in the pixels of the image you were given. If you are not
confident about what to do, choose "wait" -- a wrong click is worse than a
pause, because it may be irreversible in the game."""


class VisionTier:
    """Claude, looking at a downscaled frame a few times a second."""

    def __init__(self, model: str = "claude-sonnet-5", max_width: int = 1024) -> None:
        self.model = model
        self.max_width = max_width
        self._client = None
        self.available = False
        self.last_error: str | None = None

        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.last_error = "ANTHROPIC_API_KEY is not set"
            log.warning("vision tier disabled: %s", self.last_error)
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic()
            self.available = True
        except Exception as exc:                          # pragma: no cover
            self.last_error = repr(exc)
            log.warning("vision tier disabled: %r", exc)

    def prepare(self, frame_array: np.ndarray) -> tuple[bytes, float]:
        """Downscale and JPEG-encode. Returns the bytes and the scale applied."""
        bgr = frame_array[:, :, :3]
        height, width = bgr.shape[:2]
        scale = min(1.0, self.max_width / width)
        if scale < 1.0:
            bgr = cv2.resize(
                bgr, (int(width * scale), int(height * scale)),
                interpolation=cv2.INTER_AREA,
            )
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return buf.tobytes(), scale

    def decide(self, jpeg: bytes, goal: str) -> dict:
        import json

        message = self._client.messages.create(
            model=self.model,
            max_tokens=200,
            system=VISION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode(),
                    }},
                    {"type": "text", "text": f"Goal: {goal}\nWhat is the next single action?"},
                ],
            }],
        )
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        return json.loads(text)


def _validate_decision(decision):
    """Return (action, x, y, why) or None if the reply is not usable.

    Deliberately strict and deliberately quiet: a malformed model reply is an
    ordinary event, not an error worth tearing the loop down for.
    """
    import math

    if not isinstance(decision, dict):
        return None
    action = decision.get("action")
    if action not in ("click", "wait"):
        return None
    why = str(decision.get("why", ""))[:120]

    if action == "wait":
        return "wait", 0.0, 0.0, why

    try:
        x = float(decision["x"])
        y = float(decision["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return "click", x, y, why


# --- the loop --------------------------------------------------------------


class Agent:
    """Runs both tiers against one GameLens runtime."""

    def __init__(
        self,
        lens,
        *,
        goal: str = "play the game",
        reflexes: list[Reflex] | None = None,
        vision: VisionTier | None = None,
        vision_fps: float = 2.0,
    ) -> None:
        self.lens = lens
        self.goal = goal
        self.reflexes = reflexes or []
        self.vision = vision if vision is not None else VisionTier()
        self.vision_interval = 1.0 / max(vision_fps, 0.1)

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._reflex_times: deque = deque(maxlen=200)
        self._vision_times: deque = deque(maxlen=40)
        self._intent = "idle"
        self._tier = "off"

    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._reflex_loop, name="gamelens-reflex", daemon=True),
        ]
        if self.vision.available:
            self._threads.append(
                threading.Thread(target=self._vision_loop, name="gamelens-vision", daemon=True)
            )
        else:
            log.warning("running reflexes only: %s", self.vision.last_error)
        for t in self._threads:
            t.start()
        self._tier = "reflex+vision" if self.vision.available else "reflex"

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []
        self._tier = "off"

    # -- fast --

    def _reflex_loop(self) -> None:
        last_frame_id = -1
        while not self._stop.wait(0.004):
            frame = self.lens.capture.frames.acquire()
            if frame is None:
                continue
            try:
                if frame.frame_id == last_frame_id:
                    continue
                last_frame_id = frame.frame_id

                started = time.perf_counter()
                array = frame.array
                for reflex in self.reflexes:
                    if not reflex.ready():
                        continue
                    hit = reflex.test(array)
                    if hit is None:
                        continue
                    reflex.fired()
                    # Invalidate anything the slow tier is still thinking about:
                    # it was reasoning about a world this action is changing.
                    self.lens.arbiter.preempt(reflex.name)
                    self._intent = f"reflex: {reflex.name}"
                    observation = self.lens.arbiter.observation_for(frame)
                    self.lens.submit_observation_click(
                        observation, hit[0], hit[1],
                        label=reflex.label or reflex.name, source="reflex",
                    )
                    break
                self._reflex_times.append((time.perf_counter() - started) * 1000)
            except Exception:
                log.exception("reflex loop error")
            finally:
                frame.release()

    # -- slow --

    def _vision_loop(self) -> None:
        while not self._stop.wait(self.vision_interval):
            started = time.perf_counter()
            try:
                # Provenance is taken here, *before* the model call. Stamping it
                # on the way out would reset the freshness clock and hide any
                # preemption that happened while the model was thinking.
                frame, observation = self.lens.arbiter.observe()
            except ActionRejected:
                continue

            # One owner, one release. The frame is only needed long enough to
            # make the JPEG; holding it across the model call would pin a pool
            # buffer for the whole inference.
            try:
                jpeg, scale = self.vision.prepare(frame.array)
            except Exception:
                log.exception("vision preprocessing failed")
                continue
            finally:
                frame.release()

            # Carry the preprocessing scale so the model's coordinates can be
            # mapped back. Everything else in the observation stays exactly as
            # it was captured -- replacing the whole thing here would reset the
            # clock the staleness checks rely on.
            observation = dataclasses.replace(observation, scale=scale)

            try:
                decision = self.vision.decide(jpeg, self.goal)
            except Exception as exc:
                log.warning("vision call failed: %r", exc)
                self._intent = f"vision error: {exc}"
                continue

            self._vision_times.append(time.perf_counter() - started)

            # Parsing as JSON says nothing about shape. A bare list, a missing
            # x, a string coordinate -- each of these used to raise outside the
            # handler and kill this thread, while `tier` went on reporting that
            # vision was running.
            action_spec = _validate_decision(decision)
            if action_spec is None:
                self._intent = "vision returned an unusable decision"
                self.lens.log.add(f"vision: unusable reply {decision!r:.80}", "denied")
                continue

            kind, vx, vy, why = action_spec
            if kind != "click":
                self._intent = f"wait -- {why}"
                continue

            self._intent = f"click -- {why}"
            frame_x, frame_y = observation.to_frame_xy(vx, vy)
            # No wait: this loop must not block on the input queue. The entry
            # the dispatch created is resolved by the executor when the press
            # is finally decided, so the refusal still reaches the log -- just
            # not on this thread.
            dispatch = self.lens.submit_observation_click(
                observation, vx, vy, label="vision", source="vision",
            )
            if dispatch.verdict != "ok":
                self.lens.log.add(f"vision: {why} -- {dispatch.verdict}", "denied")

    # -- reporting --

    def stats(self) -> dict:
        reflex = list(self._reflex_times)
        p95 = (
            statistics.quantiles(reflex, n=20)[-1]
            if len(reflex) >= 20 else (max(reflex) if reflex else 0.0)
        )
        calls = list(self._vision_times)
        alive = [t for t in self._threads if t.is_alive()]
        tier = self._tier
        if self._threads and not alive:
            tier = "stopped (all workers exited)"
        elif self.vision.available and len(alive) < len(self._threads):
            tier = "reflex (vision worker exited)"
        return {
            "tier": tier,
            "workers_alive": len(alive),
            "workers_expected": len(self._threads),
            "reflex_p95_ms": p95,
            "reflex_samples": len(reflex),
            "vision_fps": (1.0 / statistics.mean(calls)) if calls else 0.0,
            "vision_available": self.vision.available,
            "vision_error": self.vision.last_error,
            "intent": self._intent,
            "reflexes": [r.name for r in self.reflexes],
        }
