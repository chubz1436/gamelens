"""Run a learned NPC exchange locally via the existing guarded GameLens API.

Usage: python -m gamelens.marathon_control --trainer 3 --evidence-dir PATH
The caller selects the NPC. This checks its portrait, handles the learned menu,
reads arithmetic, submits once, and returns the receipt for verification. It
never arms, focuses, retries a sent action, or advances an unverified chapter.
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

import cv2
import numpy as np

from gamelens.marathon import MathReader, UnknownScreen
from gamelens.mcp import GameLensClient


class FastExchange:
    def __init__(self, client, profile: Path, evidence: Path):
        self.client = client
        with np.load(profile / "screens.npz") as learned:
            self.assets = {name:learned[name] for name in learned.files}
        self.math = MathReader.load(profile / "math-glyphs.json")
        self.evidence = evidence
        evidence.mkdir(parents=True, exist_ok=True)
        self.frame = None
        self.count = 0
        self.identity = self.state_identity()

    def state_identity(self):
        items, error = self.client.tool_state({})
        if error:
            raise UnknownScreen("GameLens state unavailable")
        state = json.loads(items[0]["text"])
        target = state.get("target")
        if not target or not target["foreground"] or not state["capture"]["healthy"]:
            raise UnknownScreen("Game target/focus/capture unavailable")
        return target["hwnd"], target["width"], target["height"], state["capture"]["session_id"]

    def read(self):
        items, error = self.client.tool_see({"quality": 90})
        if error:
            raise UnknownScreen("Game observation failed")
        raw = base64.b64decode(next(i["data"] for i in items if i["type"] == "image"))
        self.frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if self.frame.shape[:2] != (800, 1026):
            raise UnknownScreen("Learned geometry changed")
        self.count += 1
        (self.evidence / f"frame-{self.count:04}.jpg").write_bytes(raw)
        return self.frame

    def act(self, steps, label):
        if self.state_identity() != self.identity:
            raise UnknownScreen("Target/session changed")
        started = time.monotonic()
        items, error = self.client.tool_act({
            "action": {"kind": "sequence", "steps": steps, "label": label},
            "quality": 90, "after_frames": 8, "strict": True,
        })
        result = next((json.loads(i["text"][4:]) for i in items
                       if i["type"] == "text" and i["text"].startswith("act ")), {})
        with (self.evidence / "actions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at":time.time(),"before_frame":self.count,
                                     "label": label, "result": result,
                                     "seconds": time.monotonic() - started}) + "\n")
        if error or result.get("outcome") != "sent":
            raise UnknownScreen("Action refused or uncertain; no retry")

    def label_present(self, template, x, y):
        h, w = template.shape[:2]
        mask = cv2.inRange(template, np.array([0, 120, 120]), np.array([150, 255, 255]))
        if cv2.countNonZero(mask) < 40:
            raise UnknownScreen("Learned label lacks evidence")
        region = self.frame[y:y+26, x:x+w]
        scores = cv2.matchTemplate(region, template, cv2.TM_CCORR_NORMED, mask=mask)
        _, score, _, location = cv2.minMaxLoc(scores)
        patch = region[location[1]:location[1]+h, :w]
        delta = np.abs(patch.astype(np.int16) - template.astype(np.int16))[mask != 0]
        return np.isfinite(score) and score >= .995 and delta.max() <= 60

    def wait_label(self, name, x, y):
        deadline = time.monotonic() + 12
        while True:
            self.read()
            if self.label_present(self.assets[name], x, y):
                return
            if time.monotonic() >= deadline:
                raise UnknownScreen("Unknown menu screen")
            time.sleep(.15)

    def run(self, trainer: int):
        started = time.monotonic()
        self.read()
        # White name glyphs, not animated portrait art. Reject wrong Trainer.
        title = cv2.inRange(self.frame[35:55, 480:640], np.array([190]*3), np.array([255]*3))
        expected = self.assets[f"trainer{trainer}"]
        if np.count_nonzero(title != expected) > 2:
            raise UnknownScreen("Selected NPC is not the expected Trainer")
        category_y = 280 if trainer == 1 else 226
        turnin_y = 297 if trainer == 1 else 277
        self.wait_label("category", 90, category_y)
        self.act([{"do": "click", "x": 173, "y": 291 if trainer == 1 else 237}], "Marathon category")
        self.wait_label("turnin", 408, turnin_y)
        self.act([{"do": "click", "x": 515, "y": 309 if trainer == 1 else 289},
                  {"do": "wait", "ms": 150}, {"do": "click", "x": 850, "y": 407}],
                 "Marathon exchange")
        return self.solve_current(started)

    def solve_current(self, started=None):
        if started is None:
            started=time.monotonic()
        self.read()
        self.act([{"do": "move", "x": 700, "y": 260}], "Marathon neutral pointer")
        deadline = time.monotonic() + 5
        while True:
            frame = self.read()
            try:
                equation, answer = self.math.read(frame)
                break
            except UnknownScreen:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.15)
        # No existing answer may be overwritten; blank input + visible OK.
        white = cv2.inRange(frame[282:302, 548:643], np.array([190]*3), np.array([255]*3))
        if cv2.countNonZero(white) > 20:
            raise UnknownScreen("Answer box is not empty")
        ok = cv2.inRange(frame[397:418, 825:877], np.array([190]*3), np.array([255]*3))
        if cv2.countNonZero(ok) < 12:
            raise UnknownScreen("Math confirmation is hidden")
        self.act([{"do": "click", "x": 590, "y": 292}] +
                 [{"do": "tap", "key": char, "ms": 40} for char in str(answer)] +
                 [{"do": "wait", "ms": 100}, {"do": "click", "x": 850, "y": 407}],
                 f"Marathon arithmetic {equation}{answer}")
        deadline=time.monotonic()+5
        while True:
            self.read()
            try:
                self.math.read(self.frame)
            except UnknownScreen:
                break
            if time.monotonic()>=deadline:
                raise UnknownScreen("Answer response not observed; no retry")
            time.sleep(.1)
        receipt = self.evidence / f"receipt-{self.count:04}.jpg"
        cv2.imwrite(str(receipt), self.frame)
        return {"equation": equation, "answer": answer,
                "seconds": round(time.monotonic() - started, 3),
                "receipt": str(receipt.resolve()), "next": "Verify actual exchange receipt"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trainer", type=int, choices=range(1, 11), required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=Path("profiles/godsarena"))
    args = parser.parse_args()
    print(json.dumps(FastExchange(GameLensClient("http://127.0.0.1:8777", None),
                                 args.profile, args.evidence_dir).run(args.trainer)))


if __name__ == "__main__":
    main()
