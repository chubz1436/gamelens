"""Held-out map paper proof with the actual inspected Atong69 frame."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from gamelens.marathon_race import RaceController

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def controller():
    result = RaceController.__new__(RaceController)
    with np.load(ROOT / "profiles/godsarena/characters/atong69/screens.npz") as learned:
        result.assets = {name: learned[name].copy() for name in ("suburb_map", "city_map")}
    result.frame = cv2.imread(str(ROOT / "tests/fixtures/godsarena/atong-suburb-map-translucent.jpg"))
    assert result.frame is not None and result.frame.shape[:2] == (800, 1026)
    return result


def test_actual_translucent_suburb_map_accepts_correct_area_rejects_city(controller):
    assert controller.map_matches("suburb")
    assert not controller.map_matches("city")


@pytest.mark.parametrize("area", ["suburb", "city"])
def test_original_paper_templates_are_still_accepted(controller, area):
    controller.frame[200:250, 300:400] = controller.assets[area + "_map"]
    assert controller.map_matches(area)


def test_blank_no_map_frame_is_not_accepted(controller):
    controller.frame = np.zeros_like(controller.frame)
    assert not controller.map_matches("suburb")
    assert not controller.map_matches("city")


def test_large_changed_paper_region_rejects_actual_suburb_map(controller):
    controller.frame[200:250, 300:350] = 0
    assert not controller.map_matches("suburb")


def test_sparse_world_outlier_allowed_but_broad_paper_corruption_rejected(controller):
    controller.frame[200:250, 300:400] = controller.assets["suburb_map"]
    # A few strong changed pixels under otherwise matching paper are tolerated.
    controller.frame[210:211, 310:313] = 255 - controller.frame[210:211, 310:313]
    assert controller.map_matches("suburb")
    # An obscured rectangular portion of the comparison paper is not tolerated.
    controller.frame[210:220, 310:330] = 0
    assert not controller.map_matches("suburb")
