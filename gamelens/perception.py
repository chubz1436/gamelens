"""Read-only cropped images with exact mapping to an unchanged full Observation."""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from types import MappingProxyType

import cv2
import numpy as np

from gamelens.arbiter import ACTION_TTL, OBSERVATION_DEADLINE, Observation

# Explicit generic hints, not game calibration or inferred UI state.
PRESETS = MappingProxyType({
    "hud": (0.0, 0.75, 1.0, 1.0),
    "minimap": (0.70, 0.0, 1.0, 0.30),
    "dialog": (0.20, 0.20, 0.80, 0.80),
})
PROFILES = MappingProxyType({"generic": PRESETS})
MAX_IMAGE_DIMENSION = 32768
MAX_OUTPUT_WIDTH = 16384


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{label} must be finite") from None
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _integer(value, label: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{label} must be an integer in [{lower}, {upper}]")
    return value


def _validate_parent(parent: Observation) -> None:
    if not isinstance(parent, Observation):
        raise ValueError("a full parent Observation is required")
    for name in ("frame_width", "frame_height"):
        _integer(getattr(parent, name), name, 1, MAX_IMAGE_DIMENSION)
    for name in ("target_hwnd", "backend_session_id", "frame_id",
                 "geometry_generation", "preemption_counter"):
        _integer(getattr(parent, name), name, 0, 2**64 - 1)
    for name in ("crop_left", "crop_top"):
        _integer(getattr(parent, name), name, 0, 0)
    if _number(parent.scale, "parent scale") <= 0:
        raise ValueError("parent scale must be positive")
    _number(parent.frame_captured_at, "frame capture timestamp")
    _number(parent.observed_at, "observation timestamp")


def _freshness(parent: Observation, now: float) -> tuple[float, float, bool]:
    age, observed_age = now - parent.frame_captured_at, now - parent.observed_at
    if not math.isfinite(age) or not math.isfinite(observed_age):
        raise ValueError("observation ages must be finite")
    fresh = 0 <= age <= OBSERVATION_DEADLINE and 0 <= observed_age <= ACTION_TTL
    return age, observed_age, fresh


def _requested_rect(crop):
    if isinstance(crop, str):
        return PRESETS.get(crop)
    if isinstance(crop, dict):
        if set(crop) != {"profile", "region"}:
            return None
        profile, region = crop["profile"], crop["region"]
        if not isinstance(profile, str) or not isinstance(region, str):
            return None
        return PROFILES.get(profile, {}).get(region)
    if isinstance(crop, (tuple, list)) and len(crop) == 4:
        return crop
    return None


@dataclass(frozen=True)
class CropView:
    """Display geometry, never an action-authorizing Observation.

    Resolve a trusted retained view to its parent before applying action guards.
    Cropped pixels use half-open bounds; the two actual axis ratios can differ.
    """
    parent: Observation
    rect: tuple[int, int, int, int]
    image_width: int
    image_height: int
    selection: str = "full"
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_parent(self.parent)
        if not isinstance(self.rect, tuple) or len(self.rect) != 4:
            raise ValueError("rect must be an integer ltrb tuple")
        left, top, right, bottom = self.rect
        for value in self.rect:
            _integer(value, "rect component", 0, MAX_IMAGE_DIMENSION)
        if not (0 <= left < right <= self.parent.frame_width
                and 0 <= top < bottom <= self.parent.frame_height):
            raise ValueError("rect is outside the parent frame")
        _integer(self.image_width, "image width", 1, MAX_OUTPUT_WIDTH)
        _integer(self.image_height, "image height", 1, MAX_IMAGE_DIMENSION)
        if (not isinstance(self.selection, str) or len(self.selection) > 64
                or (self.fallback_reason is not None
                    and (not isinstance(self.fallback_reason, str)
                         or len(self.fallback_reason) > 64))):
            raise ValueError("invalid crop selection metadata")

    @property
    def is_full_frame(self) -> bool:
        return self.rect == (0, 0, self.parent.frame_width, self.parent.frame_height)

    @property
    def scale_x(self) -> float:
        return self.image_width / (self.rect[2] - self.rect[0])

    @property
    def scale_y(self) -> float:
        return self.image_height / (self.rect[3] - self.rect[1])

    def to_frame_xy(self, x: float, y: float) -> tuple[float, float]:
        """Bound displayed coordinates, then map each axis to native pixels."""
        x, y = _number(x, "x"), _number(y, "y")
        if not (0 <= x < self.image_width and 0 <= y < self.image_height):
            raise ValueError("coordinate is outside the displayed image")
        left, top, right, bottom = self.rect
        fx = left + x * (right - left) / self.image_width
        fy = top + y * (bottom - top) / self.image_height
        # Near-edge floats may round to an exclusive boundary.
        if not (left <= fx < right and top <= fy < bottom):
            raise ValueError("mapped coordinate is outside the source rectangle")
        return fx, fy

    def to_parent_image_xy(self, x: float, y: float) -> tuple[float, float]:
        """Map for the parent's existing arbiter interface, without authority."""
        fx, fy = self.to_frame_xy(x, y)
        px, py = fx * self.parent.scale, fy * self.parent.scale
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError("mapped parent coordinate must be finite")
        return px, py

    def metadata(self, *, now: float | None = None) -> dict:
        """JSON-safe provenance; fresh is diagnostic, never permission."""
        now = time.monotonic() if now is None else _number(now, "now")
        age, observed_age, fresh = _freshness(self.parent, now)
        return {
            "parent": asdict(self.parent), "rect": list(self.rect),
            "image_width": self.image_width, "image_height": self.image_height,
            "scale_x": self.scale_x, "scale_y": self.scale_y,
            "selection": self.selection, "full_frame": self.is_full_frame,
            "fallback_reason": self.fallback_reason,
            "frame_age_seconds": age, "observation_age_seconds": observed_age,
            "fresh": fresh, "coordinate_space": "crop_image_pixels",
            "mapped_coordinate_space": "native_full_frame_pixels",
            "action_authority": "none",
        }


@dataclass(frozen=True)
class PerceptionFrame:
    jpeg: bytes
    view: CropView


def encode_perception(array: np.ndarray, parent_observation: Observation, *,
                      crop=None, max_width: int = 1280, quality: int = 70,
                      uncertain: bool = False, now: float | None = None) -> PerceptionFrame:
    """Crop one leased frame or fall back full, preserving its provenance.

    Missing/invalid crop, staleness or uncertainty returns full with a reason.
    Invalid source/parent fails closed: fallback cannot fix mismatched evidence.
    Never acquires/releases a lease, refreshes timestamps or authorizes input.
    """
    _validate_parent(parent_observation)
    _integer(max_width, "max width", 1, MAX_OUTPUT_WIDTH)
    _integer(quality, "JPEG quality", 1, 100)
    if (not isinstance(array, np.ndarray) or array.dtype != np.uint8
            or array.ndim != 3 or array.shape[2] not in (3, 4)):
        raise ValueError("source must be a uint8 BGR/BGRA frame")
    height, width = array.shape[:2]
    if (width, height) != (parent_observation.frame_width, parent_observation.frame_height):
        raise ValueError("source dimensions do not match the full parent observation")
    now = time.monotonic() if now is None else _number(now, "now")
    _, _, fresh = _freshness(parent_observation, now)
    rect, selection, fallback = (0, 0, width, height), "full", None
    if not isinstance(uncertain, bool) or uncertain:
        fallback = "uncertain"
    elif not fresh:
        fallback = "stale_or_future_parent"
    elif crop is None:
        fallback = "missing_crop"
    elif isinstance(crop, str) and crop == "full":
        pass
    else:
        requested = _requested_rect(crop)
        try:
            if requested is None:
                raise ValueError("unknown crop")
            left, top, right, bottom = (_number(v, "normalized crop") for v in requested)
            if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                raise ValueError("normalized crop is outside the frame")
            rect = (math.floor(left * width), math.floor(top * height),
                    math.ceil(right * width), math.ceil(bottom * height))
            selection = crop if isinstance(crop, str) else "normalized_rect"
            if isinstance(crop, dict):
                selection = f"{crop['profile']}:{crop['region']}"
        except (ValueError, TypeError, OverflowError):
            fallback = "invalid_crop"
    left, top, right, bottom = rect
    image = array[top:bottom, left:right, :3]
    source_height, source_width = image.shape[:2]
    output_width = min(source_width, max_width)
    output_height = max(1, int(source_height * output_width / source_width))
    if output_width != source_width:
        image = cv2.resize(image, (output_width, output_height), interpolation=cv2.INTER_AREA)
    view = CropView(parent_observation, rect, output_width, output_height, selection, fallback)
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("perception JPEG encode failed")
    return PerceptionFrame(buffer.tobytes(), view)
