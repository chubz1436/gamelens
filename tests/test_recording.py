"""Isolated recorder tests: real MP4 encoding, no desktop or game input."""
from pathlib import Path
import time

import cv2
import numpy as np
import pytest

from gamelens.recording import Recorder


class Source:
    def __init__(self, shape=(49, 65, 3)):
        self.frame = np.full(shape, (40, 100, 210), dtype=np.uint8)
        self.session = 1
        self.available = True

    def __call__(self):
        return (self.frame.copy(), self.session) if self.available else None


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "recorder did not reach expected state"


def decode(path):
    capture = cv2.VideoCapture(str(path))
    try:
        assert capture.isOpened()
        fps = capture.get(cv2.CAP_PROP_FPS)
        frames = []
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
        return fps, frames
    finally:
        capture.release()


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_encodes_playable_video_at_requested_rate_and_reasonable_duration(tmp_path, fps):
    recorder = Recorder(Source(), tmp_path)
    started = time.monotonic()
    try:
        assert recorder.start(fps)["active"]
        time.sleep(.35)
    finally:
        state = recorder.stop()
    elapsed = time.monotonic() - started
    encoded_fps, frames = decode(state["file"])
    assert not state["active"] and not state["error"]
    assert state["audio"] is False
    assert state["size"] == [66, 50]  # odd capture geometry is padded, not cropped
    assert encoded_fps == fps
    assert len(frames) == state["frames"]
    assert frames[0].shape == (50, 66, 3)
    assert np.max(np.abs(frames[0][10, 10].astype(int) - [40, 100, 210])) < 15
    assert abs(len(frames) / fps - elapsed) < .2
    assert abs(state["seconds"] - len(frames) / fps) <= .01


def test_each_start_uses_unique_file_and_double_start_is_refused(tmp_path):
    recorder = Recorder(Source(), tmp_path)
    try:
        first = recorder.start()["file"]
        with pytest.raises(ValueError, match="already running"):
            recorder.start()
        wait_for(lambda: recorder.snapshot()["frames"] >= 2)
        recorder.stop()
        second = recorder.start()["file"]
        wait_for(lambda: recorder.snapshot()["frames"] >= 2)
    finally:
        recorder.stop()
    assert first != second
    assert len(list(tmp_path.glob("*.mp4"))) == 2
    assert decode(first)[1] and decode(second)[1]


def test_no_first_frame_refuses_without_creating_file(tmp_path):
    recorder = Recorder(lambda: None, tmp_path / "clips")
    with pytest.raises(ValueError, match="No fresh capture frame"):
        recorder.start()
    assert not recorder.snapshot()["active"]
    assert recorder.snapshot()["file"] is None
    assert not recorder.directory.exists()


@pytest.mark.parametrize("fps", [True, False, 0, 24, 120, 30.0, "30", None, [], {}])
def test_invalid_fps_is_refused(tmp_path, fps):
    recorder = Recorder(Source(), tmp_path)
    with pytest.raises(ValueError, match="15, 30 or 60"):
        recorder.start(fps)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["session", "geometry"])
def test_session_or_geometry_change_stops_and_finalizes_previous_frames(tmp_path, change):
    source = Source()
    recorder = Recorder(source, tmp_path)
    try:
        recorder.start()
        wait_for(lambda: recorder.snapshot()["frames"] >= 2)
        if change == "session":
            source.session = 2
        else:
            source.frame = np.zeros((60, 70, 3), dtype=np.uint8)
        wait_for(lambda: not recorder.snapshot()["active"])
    finally:
        state = recorder.stop()
    assert "session or size changed" in state["error"]
    assert len(decode(state["file"])[1]) == state["frames"]


def test_stale_capture_stops_after_bounded_grace(tmp_path):
    source = Source()
    recorder = Recorder(source, tmp_path)
    try:
        recorder.start(15)
        wait_for(lambda: recorder.snapshot()["frames"] >= 2)
        source.available = False
        unavailable_at = time.monotonic()
        wait_for(lambda: not recorder.snapshot()["active"], timeout=3.5)
    finally:
        state = recorder.stop()
    assert 2 <= time.monotonic() - unavailable_at < 3.5
    assert "Capture unavailable" in state["error"]
    assert decode(state["file"])[1]


@pytest.mark.parametrize("failure", ["silent_write", "release"])
def test_encoder_failure_does_not_publish_unplayable_clip(tmp_path, monkeypatch, failure):
    class BrokenWriter:
        def __init__(self, path, *args):
            Path(path).write_bytes(b"invalid MP4")

        def isOpened(self):
            return True

        def write(self, frame):
            pass

        def release(self):
            if failure == "release":
                raise OSError("finalization failed")

    monkeypatch.setattr(cv2, "VideoWriter", BrokenWriter)
    recorder = Recorder(Source(), tmp_path)
    try:
        recorder.start()
        wait_for(lambda: recorder.snapshot()["frames"] >= 1)
    finally:
        state = recorder.stop()
    assert not state["active"]
    assert state["file"] is None
    assert "playable MP4" in state["error"]
    assert not list(tmp_path.glob("*.mp4"))


def test_zero_frame_stop_reports_failure_instead_of_an_empty_mp4(tmp_path):
    class Writer:
        def release(self):
            pass

    source = Source()
    recorder = Recorder(source, tmp_path)
    recorder._path = tmp_path / "empty.mp4"
    recorder._path.write_bytes(b"")
    recorder._active = True
    recorder._event.set()
    frame, session = source()
    recorder._record(Writer(), frame, session, (66, 50))
    state = recorder.snapshot()
    assert not state["active"] and state["file"] is None and state["error"]


def test_encoder_open_failure_refuses_and_removes_partial_file(tmp_path, monkeypatch):
    class ClosedWriter:
        def __init__(self, path, *args):
            Path(path).write_bytes(b"partial")

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoWriter", ClosedWriter)
    recorder = Recorder(Source(), tmp_path)
    with pytest.raises(ValueError, match="encoder could not open"):
        recorder.start()
    assert recorder.snapshot()["file"] is None
    assert not list(tmp_path.glob("*.mp4"))
