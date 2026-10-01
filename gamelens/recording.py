"""Video-only MP4 recording on its own thread, independent of agent inputs."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import cv2


class Recorder:
    def __init__(self, read_frame, directory: Path | None = None):
        # read_frame returns a private BGR copy and capture-session identity.
        self.read_frame = read_frame
        self.directory = directory or Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Videos" / "GameLens"
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._thread = None
        self._active = False
        self._path = None
        self._fps = 30
        self._frames = 0
        self._size = None
        self._error = ""

    def snapshot(self):
        with self._lock:
            return {"active": self._active, "fps": self._fps, "frames": self._frames,
                    "seconds": round(self._frames / self._fps, 2),
                    "file": str(self._path) if self._path else None,
                    "folder": str(self.directory), "size": self._size,
                    "audio": False, "error": self._error}

    def start(self, fps: int = 30):
        if isinstance(fps, bool) or not isinstance(fps, int) or fps not in (15, 30, 60):
            raise ValueError("Recording FPS must be 15, 30 or 60")
        with self._lock:
            if self._active:
                raise ValueError("A recording is already running")
            sample = self.read_frame()
            if sample is None:
                raise ValueError("No fresh capture frame. Open the selected game first.")
            frame, session = sample
            height, width = frame.shape[:2]
            size = (width + width % 2, height + height % 2)
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (datetime.now().strftime("GameLens-%Y%m%d-%H%M%S-") + uuid4().hex[:8] + ".mp4")
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
            if not writer.isOpened():
                writer.release()
                path.unlink(missing_ok=True)
                raise ValueError("The MP4 encoder could not open. Check the recording folder.")
            self._path, self._fps, self._frames, self._size = path, fps, 0, list(size)
            self._error = ""
            self._event.clear()
            self._active = True
            self._thread = threading.Thread(target=self._record, args=(writer, frame, session, size),
                                            daemon=True, name="gamelens-recorder")
            self._thread.start()
        return self.snapshot()

    def _record(self, writer, first, session, size):
        due = time.monotonic()
        missing_since = None
        original_shape = first.shape
        sample = (first, session)
        try:
            while not self._event.is_set():
                if self._event.wait(max(0, due - time.monotonic())):
                    break
                if sample is None:
                    missing_since = missing_since or time.monotonic()
                    if time.monotonic() - missing_since > 2:
                        raise ValueError("Capture unavailable; recording stopped.")
                    frame = first
                else:
                    frame, identity = sample
                    if identity != session or frame.shape != original_shape:
                        raise ValueError("Capture session or size changed; recording stopped.")
                    missing_since = None
                    first = frame
                height, width = frame.shape[:2]
                if (width, height) != size:
                    frame = cv2.copyMakeBorder(frame, 0, size[1]-height, 0, size[0]-width,
                                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
                writer.write(frame)
                with self._lock:
                    self._frames += 1
                due += 1 / self._fps
                if time.monotonic() - due > 2:
                    raise ValueError("Encoder cannot keep up; recording stopped. Try lower FPS.")
                sample = self.read_frame()
        except Exception as exc:
            with self._lock:
                self._error = str(exc) if isinstance(exc, ValueError) else "Recording failed. Check disk space and capture."
        finally:
            # OpenCV can fail writes without raising. Only publish a finished
            # clip when the container can actually be reopened and decoded.
            valid = False
            try:
                writer.release()
                if self._frames:
                    capture = cv2.VideoCapture(str(self._path))
                    try:
                        valid, decoded = capture.read()
                        valid = bool(valid and decoded is not None and decoded.size)
                    finally:
                        capture.release()
            except Exception:
                pass
            with self._lock:
                if not valid:
                    self._error = self._error or "Recording could not be finalized as a playable MP4."
                    invalid_path, self._path = self._path, None
                    # A failed encoder may still hold the file on Windows.
                    # Do not let cleanup conceal the failed recording state.
                    try:
                        invalid_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._active = False

    def stop(self):
        with self._lock:
            thread = self._thread
            self._event.set()
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                raise ValueError("Recording is still finishing. Check its status before starting another.")
        return self.snapshot()

    def open_folder(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.directory))
