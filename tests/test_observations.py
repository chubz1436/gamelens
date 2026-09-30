"""Observation records: what the server handed out, and what it means.

The defect these guard against is quiet. A caller gets a picture, picks a point
on it, and posts the coordinate back. If the server scaled the image for
transport and forgot, or rebuilt the provenance from the present instead of from
the moment the picture was issued, nothing errors -- the click just lands
somewhere else, or a stale decision passes a freshness check it should fail.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from gamelens.app import ObservationRegistry
from gamelens.arbiter import Observation
from gamelens.server import encode_jpeg


def make_observation(scale: float = 1.0, **kw) -> Observation:
    fields = dict(
        target_hwnd=1, backend_session_id=1, frame_id=7,
        frame_captured_at=time.monotonic(), frame_width=1920, frame_height=1080,
        geometry_generation=1, preemption_counter=0, scale=scale,
    )
    fields.update(kw)
    return Observation(**fields)


# --- the registry ----------------------------------------------------------


def test_issued_record_round_trips():
    registry = ObservationRegistry()
    observation = make_observation()
    token = registry.issue(observation)
    assert registry.resolve(token) is observation


def test_unknown_token_resolves_to_nothing():
    assert ObservationRegistry().resolve("never-issued") is None


def test_expired_record_is_refused():
    registry = ObservationRegistry(ttl=0.05)
    token = registry.issue(make_observation())
    time.sleep(0.08)
    assert registry.resolve(token) is None


def test_registry_is_bounded():
    """Records must not accumulate: this runs for hours at tens of frames a second."""
    registry = ObservationRegistry(capacity=8)
    tokens = [registry.issue(make_observation()) for _ in range(40)]
    assert len(registry._records) <= 8
    assert registry.resolve(tokens[-1]) is not None
    assert registry.resolve(tokens[0]) is None


def test_tokens_are_unguessable():
    registry = ObservationRegistry()
    tokens = {registry.issue(make_observation()) for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 10 for t in tokens)


def test_agent_snapshot_survives_thinking_and_a_busy_dashboard(monkeypatch):
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    registry = ObservationRegistry()
    observation = make_observation()
    token = registry.issue(observation)
    clock[0] += 30
    for _ in range(1000):
        registry.issue(make_observation(), retain=False)
    assert registry.resolve(token) is observation
    assert len(registry._records) == 1
    assert len(registry._stream_records) <= 256
    clock[0] += 91
    assert registry.resolve(token) is None


def test_stream_record_keeps_its_short_retention(monkeypatch):
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    registry = ObservationRegistry()
    token = registry.issue(make_observation(), retain=False)
    assert registry.resolve(token) is not None
    clock[0] += 6
    assert registry.resolve(token) is None


# --- GL-030: the transport scale must survive the round trip ---------------


def test_encode_reports_the_scale_it_applied():
    wide = np.zeros((1080, 1920, 4), dtype=np.uint8)
    jpeg, scale = encode_jpeg(wide, max_width=1280)
    assert jpeg[:2] == b"\xff\xd8"
    assert scale == pytest.approx(1280 / 1920)


def test_encode_reports_unity_when_no_downscale_happens():
    small = np.zeros((400, 600, 4), dtype=np.uint8)
    _, scale = encode_jpeg(small, max_width=1280)
    assert scale == 1.0


def test_downscaled_image_coordinates_map_back_to_native():
    """A 1920-wide frame served at 1280: image x=640 is native x=960, not 640.

    Recording scale=1.0 would put every click two thirds of the way to where it
    belonged -- plausible enough on screen that it would be blamed on the model.
    """
    _, scale = encode_jpeg(np.zeros((1080, 1920, 4), dtype=np.uint8), max_width=1280)
    observation = make_observation(scale=scale)
    native_x, native_y = observation.to_frame_xy(640, 360)
    assert native_x == pytest.approx(960, abs=1)
    assert native_y == pytest.approx(540, abs=1)


def test_scale_is_carried_per_observation_not_assumed():
    half = make_observation(scale=0.5)
    full = make_observation(scale=1.0)
    assert half.to_frame_xy(100, 100) == (200.0, 200.0)
    assert full.to_frame_xy(100, 100) == (100.0, 100.0)
