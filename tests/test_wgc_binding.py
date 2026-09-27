"""WGC binds by HWND when windows-capture can, by title only when it cannot.

windows-capture 1.4.2 selected a window by title alone, so GameLens demanded
sole title ownership before binding and on every frame. 2.0 added
``window_hwnd`` -- and turned ``window_name`` into a substring match, which
makes the title path worse than it was.

The session itself runs in a worker process (GL-044), so the binding is checked
in two places: what the parent tells the worker to bind, and what the worker
asks the library for. The library here is a fake module whose constructor
records what it was asked to bind, with or without the new keyword.
"""

from __future__ import annotations

import sys
import threading
import types
from multiprocessing.shared_memory import SharedMemory
from types import SimpleNamespace

import numpy as np
import pytest

from gamelens import _frame_shm as slot
from gamelens import _wgc_worker, capture
from gamelens.capture import (
    Backend, CaptureSupervisor, FramePool, IdentityLost, LatestFrame, TargetBinding, WgcBackend,
)


def fake_library(*, hwnd_keyword: bool, frames=(), finish=False):
    """A windows-capture stand-in. *frames* are delivered when a session starts."""
    calls: list = []

    class Control:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True
            calls.append("stopped")

        def is_finished(self):
            return self.stopped or finish

    class WindowsCapture:
        def __init__(self, cursor_capture=True, draw_border=None, monitor_index=None,
                     window_name=None, **rest):
            calls.append(dict(window_name=window_name, **rest))
            self.handlers = {}

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        def start_free_threaded(self):
            control = Control()
            for array, timespan in frames:
                self.handlers["on_frame_arrived"](
                    SimpleNamespace(frame_buffer=array, timespan=timespan), control)
            return control

    if hwnd_keyword:
        init = WindowsCapture.__init__

        def __init__(self, cursor_capture=True, draw_border=None, monitor_index=None,
                     window_name=None, window_hwnd=None):
            init(self, cursor_capture, draw_border, monitor_index, window_name,
                 window_hwnd=window_hwnd)

        WindowsCapture.__init__ = __init__

    module = types.ModuleType("windows_capture")
    module.WindowsCapture = WindowsCapture
    return module, calls


class RecordedProcess:
    """Stands in for mp.Process: records what the worker would have been given."""

    started: list = []

    def __init__(self, *, target, args, **kw):
        self.target, self.args = target, args
        self.pid = 4321
        self.exitcode = None

    def start(self):
        RecordedProcess.started.append(self)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


@pytest.fixture
def library(monkeypatch):
    def install(*, hwnd_keyword: bool, title_owned: bool, **kw):
        module, calls = fake_library(hwnd_keyword=hwnd_keyword, **kw)
        monkeypatch.setitem(sys.modules, "windows_capture", module)
        monkeypatch.setattr(capture, "_wgc_hwnd", None)
        monkeypatch.setattr(capture, "is_alive", lambda hwnd: True)
        monkeypatch.setattr(capture, "title_still_owned_by", lambda hwnd, title: title_owned)
        monkeypatch.setattr(capture, "describe", lambda hwnd: SimpleNamespace(
            width=8, height=6, client_width=8, client_height=6))
        monkeypatch.setattr(capture, "pin_graphics_capture",
                            lambda: calls.append("pinned") or True)
        return calls
    return install


@pytest.fixture
def spawned(monkeypatch):
    RecordedProcess.started = []
    monkeypatch.setattr(capture.mp, "Process", RecordedProcess)
    monkeypatch.setattr(capture, "die_with_this_process", lambda pid: True)
    return RecordedProcess.started


def backend():
    return WgcBackend(TargetBinding(hwnd=4242, title="Minecraft", width=8, height=6),
                      FramePool(), LatestFrame(), max_pixels=64)


def started_backend(spawned):
    wgc = backend()
    wgc.start()
    return wgc


def test_capability_is_read_from_the_signature(library):
    library(hwnd_keyword=True, title_owned=True)
    assert capture.wgc_selects_by_hwnd() is True
    library(hwnd_keyword=False, title_owned=True)
    assert capture.wgc_selects_by_hwnd() is False


def test_the_session_runs_in_a_worker_process(library, spawned):
    library(hwnd_keyword=True, title_owned=True)
    wgc = started_backend(spawned)
    try:
        assert [p.target for p in spawned] == [_wgc_worker.run]
    finally:
        wgc.stop()


def test_binds_by_hwnd_and_never_by_title(library, spawned):
    library(hwnd_keyword=True, title_owned=True)
    wgc = started_backend(spawned)
    try:
        assert spawned[0].args[0] == {"window_hwnd": 4242}   # never window_name: a substring in 2.0
    finally:
        wgc.stop()


def test_a_shared_title_does_not_stop_an_hwnd_binding(library, spawned):
    # Another window called "Minecraft ..." exists. Bound by handle, that is harmless,
    # at start and on every frame.
    library(hwnd_keyword=True, title_owned=False)
    wgc = started_backend(spawned)
    try:
        wgc._check(np.zeros((6, 8, 4), np.uint8))
        wgc._check(np.zeros((6, 8, 4), np.uint8))
    finally:
        wgc.stop()


def test_old_library_still_binds_by_title_and_demands_ownership(library, spawned):
    library(hwnd_keyword=False, title_owned=True)
    wgc = started_backend(spawned)
    wgc.stop()
    assert spawned[0].args[0] == {"window_name": "Minecraft"}

    library(hwnd_keyword=False, title_owned=False)
    with pytest.raises(IdentityLost):
        backend().start()
    assert len(spawned) == 1                                        # nothing more spawned


def test_a_title_binding_is_checked_on_every_frame(library, spawned, monkeypatch):
    library(hwnd_keyword=False, title_owned=True)
    wgc = started_backend(spawned)
    try:
        wgc._check(np.zeros((6, 8, 4), np.uint8))
        monkeypatch.setattr(capture, "title_still_owned_by", lambda hwnd, title: False)
        with pytest.raises(IdentityLost):
            wgc._check(np.zeros((6, 8, 4), np.uint8))
    finally:
        wgc.stop()


def test_a_first_frame_of_the_wrong_size_is_refused(library, spawned):
    library(hwnd_keyword=True, title_owned=True)
    wgc = started_backend(spawned)
    try:
        with pytest.raises(IdentityLost):
            wgc._check(np.zeros((5, 8, 4), np.uint8))
    finally:
        wgc.stop()


def fake_class(kind, started):
    class Fake(capture.CaptureBackend):
        def start(self):
            started.append(kind)
            self._publish(np.zeros((4, 4, 4), np.uint8), 0)

        def stop(self):
            self.retire()

    Fake.kind = kind
    return Fake


@pytest.mark.parametrize("by_hwnd, first", [(True, Backend.WGC), (False, Backend.PRINTWINDOW),
                                            (None, Backend.PRINTWINDOW)])
def test_supervisor_skips_wgc_for_a_shared_title_only_when_bound_by_title(
        monkeypatch, by_hwnd, first):
    started: list = []
    monkeypatch.setattr(capture, "_BACKEND_CLASSES",
                        {k: fake_class(k, started) for k in Backend})
    monkeypatch.setattr(capture, "wgc_selects_by_hwnd", lambda: by_hwnd)
    monkeypatch.setattr(capture, "title_still_owned_by", lambda hwnd, title: False)
    sup = CaptureSupervisor(SimpleNamespace(hwnd=1, title="Game", width=4, height=4))
    sup.start()
    try:
        assert started[0] is first
        assert sup.backend.kind is first
    finally:
        sup.stop()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_pinning_works_on_this_machine(monkeypatch):
    monkeypatch.setattr(capture, "_graphics_capture_pinned", False)
    assert capture.pin_graphics_capture() is True
    assert capture.pin_graphics_capture() is True          # idempotent


def test_an_unreadable_library_is_unknown_not_legacy(library, monkeypatch):
    # RV03-I02 (Codex): a failed import taken as "1.4.2" would later ask 2.x by
    # title, which 2.x matches as a substring.
    monkeypatch.setitem(sys.modules, "windows_capture", None)      # import raises
    monkeypatch.setattr(capture, "_wgc_hwnd", None)
    assert capture.wgc_selects_by_hwnd() is None
    assert capture._wgc_hwnd is None                               # not cached
    library(hwnd_keyword=True, title_owned=True)
    assert capture.wgc_selects_by_hwnd() is True


def test_wgc_refuses_to_start_when_the_library_is_unknown(library, spawned, monkeypatch):
    library(hwnd_keyword=True, title_owned=True)
    monkeypatch.setattr(capture, "wgc_selects_by_hwnd", lambda: None)
    with pytest.raises(RuntimeError):
        backend().start()
    assert spawned == []                                           # no worker, nothing bound


# --- the worker, run on a thread against the fake library -----------------------


class Slot:
    def __init__(self, size=4096):
        self.shm = SharedMemory(create=True, size=size)
        self.ctrl = [0] * slot.CTRL_SIZE

    def run(self, target=None, timeout=2.0):
        t = threading.Thread(target=_wgc_worker.run,
                             args=(target or {"window_hwnd": 4242}, None, self.shm.name, self.ctrl),
                             daemon=True)
        t.start()
        t.join(timeout)
        return t

    def close(self, t):
        self.ctrl[slot.CTRL_STOP] = 1
        t.join(2.0)
        self.shm.close()
        self.shm.unlink()


def test_the_worker_pins_the_dll_before_the_first_session(library):
    # An unpinned GraphicsCapture.dll can be unloaded under a session that is
    # still ending: a native crash, no traceback (Java world reloads, 2026-09-26).
    calls = library(hwnd_keyword=True, title_owned=True, finish=True)
    s = Slot()
    t = s.run()
    s.close(t)
    assert calls[0] == "pinned" and calls[1]["window_hwnd"] == 4242


def test_the_worker_asks_the_library_for_exactly_its_target(library):
    calls = library(hwnd_keyword=False, title_owned=True, finish=True)
    s = Slot()
    t = s.run({"window_name": "Minecraft"})
    s.close(t)
    assert calls[1] == {"window_name": "Minecraft"}


def test_the_worker_writes_a_padded_frame_whole(library):
    # 1.4.2 hands a strided view when rows are padded; the slot needs plain rows.
    padded = np.arange(6 * 40, dtype=np.uint16).astype(np.uint8).reshape(6, 40)
    frame = padded[:, :32].reshape(6, 8, 4)
    assert not frame.flags.c_contiguous
    library(hwnd_keyword=True, title_owned=True, frames=[(frame, 777)], finish=True)
    s = Slot()
    t = s.run()
    try:
        c = s.ctrl
        assert (c[slot.CTRL_WIDTH], c[slot.CTRL_HEIGHT], c[slot.CTRL_TIMESPAN]) == (8, 6, 777)
        assert c[slot.CTRL_COUNTER] == 1 and c[slot.CTRL_VERSION] % 2 == 0
        got = np.frombuffer(s.shm.buf[:8 * 6 * 4], np.uint8).reshape(6, 8, 4).copy()
        assert np.array_equal(got, frame)
    finally:
        s.close(t)


def test_the_worker_refuses_a_frame_larger_than_the_slot(library):
    library(hwnd_keyword=True, title_owned=True,
            frames=[(np.zeros((64, 64, 4), np.uint8), 1)])
    s = Slot(size=1024)
    t = s.run()
    try:
        assert not t.is_alive()
        assert s.ctrl[slot.CTRL_ERROR] == slot.ERR_TOO_BIG
        assert s.ctrl[slot.CTRL_COUNTER] == 0
    finally:
        s.close(t)


def test_the_worker_reports_a_session_that_ended_on_its_own(library):
    library(hwnd_keyword=True, title_owned=True, finish=True)
    s = Slot()
    t = s.run()
    try:
        assert not t.is_alive()
        assert s.ctrl[slot.CTRL_ERROR] == slot.ERR_CLOSED
    finally:
        s.close(t)


def test_the_worker_stops_its_session_when_asked(library):
    calls = library(hwnd_keyword=True, title_owned=True)
    s = Slot()
    t = s.run(timeout=0.2)
    assert t.is_alive()                                  # capturing until told otherwise
    s.close(t)
    assert not t.is_alive()
    assert "stopped" in calls
    assert s.ctrl[slot.CTRL_ERROR] == 0


def test_the_worker_exits_on_its_own_once_orphaned(library, monkeypatch):
    import multiprocessing

    calls = library(hwnd_keyword=True, title_owned=True)
    monkeypatch.setattr(multiprocessing, "parent_process",
                        lambda: type("Gone", (), {"is_alive": lambda self: False})())
    s = Slot()
    t = s.run()
    try:
        assert not t.is_alive()
        assert "stopped" in calls
    finally:
        s.close(t)


# --- a real worker process that really crashes ----------------------------------

CRASHING_LIBRARY = '''
import ctypes, threading, time
from types import SimpleNamespace

import numpy as np


class Control:
    def __init__(self):
        self.done = False

    def stop(self):
        self.done = True

    def is_finished(self):
        return self.done


class WindowsCapture:
    def __init__(self, cursor_capture=True, draw_border=None, monitor_index=None,
                 window_name=None, window_hwnd=None):
        self.handlers = {}

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    def start_free_threaded(self):
        control = Control()

        def feed():
            for i in range(1, 4):
                self.handlers["on_frame_arrived"](
                    SimpleNamespace(frame_buffer=np.full((6, 8, 4), i, np.uint8), timespan=i),
                    control)
                time.sleep(0.05)
            time.sleep(0.5)
            # What 1.4.2 did on the VM: a native thread faults, and there is no
            # Python frame anywhere to catch it. A thread started at an unmapped
            # address is exactly that.
            k32 = ctypes.windll.kernel32
            k32.CreateThread.restype = ctypes.c_void_p
            k32.CreateThread.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
            k32.CreateThread(None, 0, 0x10, None, 0, None)
            time.sleep(10)

        threading.Thread(target=feed, daemon=True).start()
        return control
'''


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_a_native_crash_ends_the_worker_not_gamelens(tmp_path, monkeypatch):
    import time

    pkg = tmp_path / "windows_capture"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(CRASHING_LIBRARY, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))             # the spawned child inherits sys.path
    monkeypatch.delitem(sys.modules, "windows_capture", raising=False)
    monkeypatch.setattr(capture, "_wgc_hwnd", None)
    monkeypatch.setattr(capture, "is_alive", lambda hwnd: True)
    monkeypatch.setattr(capture, "describe", lambda hwnd: SimpleNamespace(
        width=8, height=6, client_width=8, client_height=6))

    sink = LatestFrame()
    wgc = WgcBackend(TargetBinding(hwnd=4242, title="Minecraft", width=8, height=6),
                     FramePool(), sink)
    wgc.start()
    try:
        deadline = time.monotonic() + 30
        while wgc.error is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert wgc.distinct >= 1                           # it worked, then it crashed
        assert wgc.error and "0xc0000005" in wgc.error and "crashed" in wgc.error
        assert not wgc.healthy()                           # so the supervisor fails over
        frame = sink.acquire()
        assert frame is not None and frame.backend is Backend.WGC
        frame.release()
    finally:
        wgc.stop()
