"""Learned GodsArena Marathon decisions. No input, arming or paid model calls.

Only a verified exchange receipt advances a chapter. Unknown screens, glyphs,
operators and receipt mismatches are explicit stops, never action retries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# Observed Oct 1, 2026. Chapter 17 is the final turn-in, also with arithmetic.
DESTINATIONS = (3, 4, 5, 3, 2, 4, 3, 1, 6, 7, 9, 10, 9, 8, 7, 6, 1)
WAYPOINTS = {
    1: (498, 532), 2: (659, 463), 3: (541, 451), 4: (528, 292),
    5: (363, 451), 6: (578, 492), 7: (531, 571), 8: (415, 489),
    9: (475, 424), 10: (429, 454),
}


class UnknownScreen(ValueError):
    pass


def glyphs(frame: np.ndarray, threshold: int=190) -> list[np.ndarray]:
    if frame.shape[:2] != (800, 1026):
        raise UnknownScreen("Learned geometry changed")
    crop = frame[282:298, 414:510, :3]
    white = cv2.inRange(crop, np.array([threshold]*3), np.array([255]*3))
    columns = (white.max(axis=0) > 0).astype(int)
    edges = np.diff(np.pad(columns, (1, 1)))
    spans=[]
    for start,end in zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1)):
        # JPEG can split one small digit (notably9). Merge only when the
        # combined width still fits one glyph; never merge adjacent80 digits.
        if spans and start-spans[-1][1]<=1 and end-spans[-1][0]<=7:
            spans[-1]=(spans[-1][0],end)
        else:
            spans.append((start,end))
    parts = []
    for start, end in spans:
        part = white[:, start:end] > 0
        rows = np.flatnonzero(part.any(axis=1))
        if not len(rows) or end - start > 12:
            raise UnknownScreen("Equation glyph segmentation failed")
        parts.append(part[rows[0]:rows[-1] + 1])
    if not 4 <= len(parts) <= 7:
        raise UnknownScreen("Equation missing or obscured")
    return parts


class MathReader:
    def __init__(self, bank: dict):
        self.bank = {char: [np.asarray(p, dtype=bool) for p in patterns]
                     for char, patterns in bank.items()}
        self.variants={}
        for char,patterns in self.bank.items():
            canvases=[]; shapes=[]; penalties=[]
            for pattern in patterns:
                height,width=pattern.shape
                for dy in (1,2,3):
                    for dx in (1,2,3):
                        canvas=np.zeros((20,16),bool)
                        canvas[dy:dy+height,dx:dx+width]=pattern
                        canvases.append(canvas)
                        shapes.append((height,width))
                        penalties.append(abs(dy-2)+abs(dx-2))
            self.variants[char]=(np.asarray(canvases),np.asarray(shapes),np.asarray(penalties))

    @classmethod
    def load(cls, path: Path) -> "MathReader":
        return cls(json.loads(path.read_text()))

    def read_at(self, frame: np.ndarray, threshold: int) -> tuple[str, int]:
        chars = []
        for part in glyphs(frame,threshold):
            canvas=np.zeros((20,16),bool)
            height,width=part.shape
            canvas[2:2+height,2:2+width]=part
            scores = []
            for char,(variants,shapes,penalties) in self.variants.items():
                dimensions=np.abs(shapes-np.array([height,width]))
                valid=(dimensions.max(axis=1)<=1)
                if valid.any():
                    distances=(np.count_nonzero(variants[valid]!=canvas,axis=(1,2))
                               +penalties[valid]+dimensions[valid].sum(axis=1))
                    scores.append((int(distances.min()),char))
            scores.sort()
            # Different text positions rasterize the same small font slightly
            # differently. Bound noise by both pixel count and glyph area, and
            # require a larger class margin as the match becomes less exact.
            distance=scores[0][0] if scores else 999
            margin=scores[1][0]-distance if len(scores)>1 else 999
            if (not scores or distance>6 or distance/part.size>.20
                    or margin<max(3,(distance+1)//2+2)):
                raise UnknownScreen("Unknown or ambiguous equation glyph")
            chars.append(scores[0][1])
        equation = "".join(chars)
        match = re.fullmatch(r"(\d{1,2})\+(\d{1,2})=", equation)
        if not match:
            raise UnknownScreen("Unsupported or unreadable arithmetic operator")
        return equation, int(match[1]) + int(match[2])

    def read(self,frame:np.ndarray)->tuple[str,int]:
        recognized=[]
        for threshold in (170,180,190,200):
            try:
                recognized.append(self.read_at(frame,threshold))
            except UnknownScreen:
                pass
        if len(recognized)<2 or len(set(recognized))!=1:
            raise UnknownScreen("Unknown or ambiguous equation glyph")
        return recognized[0]


@dataclass
class RaceProgress:
    chapter: int = 1
    complete: bool = False

    @property
    def trainer(self) -> int:
        if self.complete or not 1 <= self.chapter <= 17:
            raise UnknownScreen("No active learned chapter")
        return DESTINATIONS[self.chapter - 1]

    def navigation(self, current_trainer: int, area: str) -> list[tuple[int, int]]:
        destination = self.trainer
        expected_area = "suburb" if destination <= 5 else "city"
        if area != expected_area:
            raise UnknownScreen("Map transition must be verified first")
        # Direct mounted2->4 blocks at the market. A closer600,451 midpoint
        # reached the actual Trainer4 from that blocked approach in74.310s.
        if current_trainer == 2 and destination == 4:
            return [(600,451), WAYPOINTS[4]]
        return [WAYPOINTS[destination]]

    def exchange_confirmed(self, old: int, new: int) -> None:
        if self.complete or old != self.chapter or new != old + 1 or old >= 17:
            raise UnknownScreen("Exchange receipt does not match active chapter")
        self.chapter = new

    def finish_confirmed(self, *, final_manual: int, reward: int, seconds: float) -> None:
        if self.complete or self.chapter != 17 or final_manual != 17 or reward not in (40, 1200) or seconds <= 0:
            raise UnknownScreen("Completion receipt not verified")
        self.complete = True
