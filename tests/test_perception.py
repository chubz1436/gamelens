"""Synthetic crop geometry/provenance checks: no captures, live windows or input."""
from dataclasses import FrozenInstanceError, replace
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from gamelens.arbiter import Action, ActionRejected, Arbiter, Observation, Rejection
from gamelens.coords import Geometry
from gamelens.perception import CropView, encode_perception


@pytest.fixture
def source():
    # Odd dimensions force real axis ratios to differ after resizing.
    array = np.zeros((101, 203, 4), dtype=np.uint8)
    array[:, :, :3] = (40, 90, 170)
    parent = Observation(123, 7, 42, 99.9, 203, 101, 3, 0,
                         scale=0.5, observed_at=99.95)
    return array, parent


def decoded(frame):
    return cv2.imdecode(np.frombuffer(frame.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)


def test_exact_dimensions_and_independent_axis_mapping(source):
    array, parent = source
    frame = encode_perception(array, parent, crop=(0.1, 0.2, 0.9, 0.8),
                              max_width=77, now=100)
    view = frame.view
    assert view.rect == (20, 20, 183, 81)
    assert (view.image_width, view.image_height) == (77, 28)
    assert decoded(frame).shape[:2] == (28, 77)
    assert view.scale_x != view.scale_y
    assert view.to_frame_xy(38.5, 14) == (101.5, 50.5)
    assert view.to_parent_image_xy(38.5, 14) == (50.75, 25.25)
    assert parent.to_frame_xy(*view.to_parent_image_xy(76, 27)) == pytest.approx(
        view.to_frame_xy(76, 27))
    assert view.parent is parent
    assert (parent.frame_id, parent.frame_captured_at, parent.observed_at) == (42, 99.9, 99.95)
    with pytest.raises(FrozenInstanceError):
        view.image_width = 1


@pytest.mark.parametrize('crop,rect,selection', [
    ('hud', (0, 75, 203, 101), 'hud'),
    ('minimap', (142, 0, 203, 31), 'minimap'),
    ('dialog', (40, 20, 163, 81), 'dialog'),
    ({'profile': 'generic', 'region': 'minimap'}, (142, 0, 203, 31), 'generic:minimap'),
    ([0, 0, 1, 1], (0, 0, 203, 101), 'normalized_rect'),
    ('full', (0, 0, 203, 101), 'full'),
])
def test_explicit_presets_and_rectangles(source, crop, rect, selection):
    frame = encode_perception(*source, crop=crop, now=100)
    assert frame.view.rect == rect
    assert frame.view.selection == selection
    assert frame.view.fallback_reason is None
    assert decoded(frame).shape[:2] == (rect[3] - rect[1], rect[2] - rect[0])


@pytest.mark.parametrize('crop', [
    '', 'unknown', True, 1, [], [0, 0, 1], [0, 0, 1, 1, 1],
    [False, 0, 1, 1], [0, 0, float('nan'), 1], [0, 0, float('inf'), 1],
    [-0.1, 0, 1, 1], [0, 0, 1.01, 1], [0.5, 0, 0.5, 1], [1, 0, 0, 1],
    [0, 0, '1', 1], [0, 0, 10**400, 1],
    {'profile': 'unknown', 'region': 'hud'}, {'profile': [], 'region': 'hud'},
    {'profile': 'generic', 'region': 'hud', 'authority': True}, {},
])
def test_invalid_crop_falls_back_full(source, crop):
    frame = encode_perception(*source, crop=crop, max_width=100, now=100)
    assert frame.view.is_full_frame
    assert frame.view.fallback_reason == 'invalid_crop'
    assert decoded(frame).shape[:2] == (49, 100)
    assert frame.view.to_frame_xy(50, 24.5) == (101.5, 50.5)


@pytest.mark.parametrize('crop,uncertain,reason', [
    (None, False, 'missing_crop'), ('hud', True, 'uncertain'),
    ('hud', 'false', 'uncertain'),
])
def test_missing_and_uncertain_fallback(source, crop, uncertain, reason):
    view = encode_perception(*source, crop=crop, uncertain=uncertain, now=100).view
    assert view.is_full_frame
    assert view.fallback_reason == reason
    assert view.parent is source[1]


@pytest.mark.parametrize('capture,observed', [(98, 99.95), (99.9, 98), (101, 99.95), (99.9, 101)])
def test_stale_or_future_fallback_retains_original_clocks(source, capture, observed):
    array, parent = source
    parent = replace(parent, frame_captured_at=capture, observed_at=observed)
    view = encode_perception(array, parent, crop='hud', now=100).view
    assert view.parent is parent and view.is_full_frame
    assert view.fallback_reason == 'stale_or_future_parent'
    assert view.metadata(now=100)['fresh'] is False
    assert view.metadata(now=100)['frame_age_seconds'] == 100 - capture


def test_freshness_boundaries_and_metadata_are_json_safe(source):
    array, parent = source
    parent = replace(parent, frame_captured_at=99, observed_at=99.25)
    view = encode_perception(array, parent, crop='hud', now=100).view
    assert not view.is_full_frame
    meta = view.metadata(now=100)
    assert meta['fresh'] and meta['action_authority'] == 'none'
    assert meta['parent']['backend_session_id'] == 7
    json.dumps(meta, allow_nan=False)
    assert view.metadata(now=100.1)['fresh'] is False
    assert not isinstance(view, Observation)


@pytest.mark.parametrize('x,y', [
    (-1, 0), (77, 0), (0, 28), (0, -0.01), (True, 0), (0, False),
    (float('nan'), 0), (0, float('inf')), (10**400, 0), ('1', 0), (None, 0),
])
def test_display_coordinate_validation(source, x, y):
    view = encode_perception(*source, crop=(0.1, 0.2, 0.9, 0.8), max_width=77, now=100).view
    with pytest.raises(ValueError):
        view.to_frame_xy(x, y)
    with pytest.raises(ValueError):
        view.to_parent_image_xy(x, y)


@pytest.mark.parametrize('changes', [
    {'frame_width': True}, {'frame_width': 0}, {'frame_height': 32769},
    {'crop_left': 1}, {'crop_top': False}, {'scale': 0}, {'scale': True},
    {'scale': float('nan')}, {'frame_captured_at': float('inf')},
    {'observed_at': float('nan')}, {'frame_id': True}, {'geometry_generation': -1},
])
def test_invalid_parent_fails_closed(source, changes):
    array, parent = source
    with pytest.raises(ValueError):
        encode_perception(array, replace(parent, **changes), crop='hud', now=100)


@pytest.mark.parametrize('kwargs', [
    {'max_width': True}, {'max_width': 0}, {'max_width': 16385},
    {'quality': False}, {'quality': 0}, {'quality': 101},
    {'now': float('nan')}, {'now': float('inf')}, {'now': True},
])
def test_encoding_option_validation(source, kwargs):
    with pytest.raises(ValueError):
        encode_perception(*source, crop='hud', **kwargs)


@pytest.mark.parametrize('array', [
    np.zeros((100, 203, 4), dtype=np.uint8), np.zeros((101, 203), dtype=np.uint8),
    np.zeros((101, 203, 5), dtype=np.uint8), np.zeros((101, 203, 3), dtype=float), None,
])
def test_bad_or_mismatched_source_fails_closed(source, array):
    with pytest.raises(ValueError):
        encode_perception(array, source[1], crop=None, now=100)


def test_single_pixel_and_thin_resize_are_nonempty():
    parent = Observation(123, 7, 42, 99.9, 203, 1, 3, 0, observed_at=99.95)
    array = np.zeros((1, 203, 3), dtype=np.uint8)
    frame = encode_perception(array, parent, crop=(0.1, 0, 0.1001, 1), now=100)
    assert decoded(frame).shape[:2] == (1, 1)
    frame = encode_perception(array, parent, crop='full', max_width=1, now=100)
    assert decoded(frame).shape[:2] == (1, 1)


def test_jpeg_failure_is_explicit(source, monkeypatch):
    monkeypatch.setattr(cv2, 'imencode', lambda *args: (False, None))
    with pytest.raises(RuntimeError, match='JPEG encode failed'):
        encode_perception(*source, crop='hud', now=100)


def arbiter_for(parent):
    width, height = parent.frame_width, parent.frame_height
    geometry = Geometry(parent.geometry_generation, parent.target_hwnd, 99.9,
                        -1920, -1080, width, height,
                        -1920, -1080, width, height,
                        -1920, -1080, width, height, 1.0)
    capture = SimpleNamespace(binding=SimpleNamespace(hwnd=parent.target_hwnd),
                              backend=SimpleNamespace(session_id=parent.backend_session_id, retired=False))
    tracker = SimpleNamespace(current=geometry, generation=geometry.generation)
    return Arbiter(capture, tracker, SimpleNamespace()), capture, tracker


def test_negative_screen_origin_and_half_open_edges(source):
    view = encode_perception(*source, crop=(0.7, 0, 1, 1), now=100).view
    arbiter, _, _ = arbiter_for(view.parent)
    for point, expected in [((0, 0), (-1778, -1080)), ((60, 100), (-1718, -980))]:
        mapped = arbiter._map_point(view.parent, *view.to_parent_image_xy(*point))
        assert mapped[:2] == expected
    with pytest.raises(ValueError):
        view.to_parent_image_xy(61, 100)
    # Fractional pixels can round to a screen edge; the existing arbiter must reject.
    with pytest.raises(ActionRejected) as rejected:
        arbiter._map_point(view.parent, *view.to_parent_image_xy(60.9, 100))
    assert rejected.value.reason is Rejection.OUT_OF_BOUNDS


@pytest.mark.parametrize('change,expected', [
    ('stale', Rejection.STALE_OBSERVATION), ('expired', Rejection.EXPIRED),
    ('target', Rejection.WRONG_TARGET), ('backend', Rejection.RETIRED_SESSION),
    ('geometry', Rejection.GEOMETRY_MOVED), ('preempt', Rejection.PREEMPTED),
])
def test_crop_mapping_preserves_all_parent_guards(source, monkeypatch, change, expected):
    array, parent = source
    if change == 'stale':
        parent = replace(parent, frame_captured_at=98)
    if change == 'expired':
        parent = replace(parent, observed_at=98)
    view = encode_perception(array, parent, crop='dialog', now=100).view
    arbiter, capture, tracker = arbiter_for(parent)
    if change == 'target':
        capture.binding.hwnd += 1
    elif change == 'backend':
        capture.backend.session_id += 1
    elif change == 'geometry':
        tracker.generation += 1
    elif change == 'preempt':
        arbiter.preempt('synthetic test')
    monkeypatch.setattr('gamelens.arbiter.time.monotonic', lambda: 100)
    view.to_parent_image_xy(0, 0)
    assert arbiter.evaluate(Action(view.parent, [])) is expected


def test_view_constructor_validation_and_overflow(source):
    parent = source[1]
    for rect in [(0, 0, 204, 101), (-1, 0, 203, 101), (0, 0, 0, 101), (False, 0, 203, 101)]:
        with pytest.raises(ValueError):
            CropView(parent, rect, 100, 49)
    with pytest.raises(ValueError):
        CropView(parent, (0, 0, 203, 101), True, 49)
    view = CropView(replace(parent, scale=1e308), (0, 0, 203, 101), 203, 101)
    with pytest.raises(ValueError, match='finite'):
        view.to_parent_image_xy(202, 100)


def test_encoded_pixels_come_from_selected_region_and_source_is_unchanged(source):
    array, parent = source
    array[:, :, :3] = 0
    array[20:81, 20:183, :3] = (13, 87, 231)
    before = array.copy()
    frame = encode_perception(array, parent, crop=(0.1, 0.2, 0.9, 0.8),
                              max_width=77, quality=95, now=100)
    np.testing.assert_array_equal(array, before)
    pixels = decoded(frame)
    assert pixels.shape[:2] == (28, 77)
    assert np.max(np.abs(pixels.astype(int) - np.array([13, 87, 231]))) <= 3


@pytest.mark.parametrize('now', [True, float('nan'), float('inf'), '100'])
def test_metadata_rejects_nonfinite_or_nonnumeric_clock(source, now):
    view = encode_perception(*source, crop='dialog', now=100).view
    with pytest.raises(ValueError):
        view.metadata(now=now)
