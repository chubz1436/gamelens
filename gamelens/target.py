"""Explicit fixed-position text anchors for animated game click targets.

Default clicks still require an unchanged patch. An agent may instead name a
yellow label it actually saw, adjacent to the intended click. Only those label
glyphs are compared; the surrounding world may animate. No target searching,
coordinate guessing, threshold knob, or automatic retry is performed.
"""
import math

import cv2
import numpy as np


def parse_anchor(value, x, y):
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height", "color"}:
        raise ValueError("anchor requires x, y, width, height and color")
    if value["color"] != "yellow":
        raise ValueError("anchor color must be yellow")
    rect = []
    for key in ("x", "y", "width", "height"):
        n = value[key]
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or int(n) != n:
            raise ValueError("anchor rectangle must contain finite whole pixels")
        rect.append(int(n))
    ax, ay, w, h = rect
    if ax < 0 or ay < 0 or not 20 <= w <= 256 or not 8 <= h <= 48:
        raise ValueError("anchor must be a small visible label (20-256 by 8-48 pixels)")
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("click coordinates must be finite")
    dx, dy = max(ax-x, 0, x-(ax+w-1)), max(ay-y, 0, y-(ay+h-1))
    if math.hypot(dx, dy) > 40:
        raise ValueError("click must be within 40 pixels of its anchor label")
    return dict(zip(("x", "y", "width", "height"), rect), color="yellow")


def same_at_anchor(shown_jpeg, fresh_jpeg, anchor, x, y):
    out = {"anchor_ok": False, "anchor_pixels": 0, "anchor_mean": None,
           "anchor_p95": None, "anchor_max": None, "anchor_overlap": None}
    a = cv2.imdecode(np.frombuffer(shown_jpeg, np.uint8), cv2.IMREAD_COLOR)
    b = cv2.imdecode(np.frombuffer(fresh_jpeg, np.uint8), cv2.IMREAD_COLOR)
    if a is None or b is None or a.shape != b.shape:
        return out
    ax, ay, w, h = (anchor[k] for k in ("x", "y", "width", "height"))
    fh, fw = a.shape[:2]
    if ax+w > fw or ay+h > fh or not (0 <= x < fw and 0 <= y < fh):
        return out
    a, b = a[ay:ay+h, ax:ax+w], b[ay:ay+h, ax:ax+w]
    low, high = np.array([0, 120, 120]), np.array([150, 255, 255])
    mask, now = cv2.inRange(a, low, high) != 0, cv2.inRange(b, low, high) != 0
    count = int(mask.sum())
    out["anchor_pixels"] = count
    # Flat yellow surfaces are not identifying text evidence.
    if count < 40 or mask.mean() > 0.45:
        return out
    ys, xs = np.where(mask)
    if np.ptp(xs) < 12 or np.ptp(ys) < 5:
        return out
    diff = np.abs(a.astype(np.int16)-b.astype(np.int16))[mask]
    mean, p95 = float(diff.mean()), float(np.percentile(diff, 95))
    overlap = float((mask & now).sum()) / max(int((mask | now).sum()), 1)
    peak = int(diff.max())
    out.update(anchor_mean=round(mean, 3), anchor_p95=round(p95, 3), anchor_max=peak,
               anchor_overlap=round(overlap, 4))
    out["anchor_ok"] = mean <= 8 and p95 <= 20 and peak <= 60 and overlap >= 0.90
    return out
