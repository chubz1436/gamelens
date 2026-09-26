"""WGC binds by HWND when windows-capture can, by title only when it cannot.

windows-capture 1.4.2 selected a window by title alone, so GameLens demanded
sole title ownership before binding and on every frame. 2.0 added
``window_hwnd`` -- and turned ``window_name`` into a substring match, which
makes the title path worse than it was. The library here is a fake module whose
constructor records what it was asked to bind, with or without the new keyword.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from gamelens import capture
from gamelens.capture import (
    Backend, CaptureSupervisor, FramePool, IdentityLost, LatestFrame, TargetBinding, WgcBackend,
)


def fake_library(*, hwnd_keyword: bool):
    calls: list[dict] = []

    class Control:
        def stop(self):
            pass

    class WindowsCapture:
        def __init__(self, cursor_capture=True, draw_border=None, monitor_index=None,
                     window_name=None, **rest):
            calls.append(dict(window_name=window_name, **rest))
            self.handlers = {}

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        def start_free_threaded(self):
            calls[-1]["capture"] = self
            return Control()

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


@pytest.fixture
def library(monkeypatch):
    def install(*, hwnd_keyword: bool, title_owned: bool):
        module, calls = fake_library(hwnd_keyword=hwnd_keyword)
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


def backend():
    return WgcBackend(TargetBinding(hwnd=4242, title="Minecraft", width=8, height=6),
                      FramePool(), LatestFrame())


def test_capability_is_read_from_the_signature(library):
    library(hwnd_keyword=True, title_owned=True)
    assert capture.wgc_selects_by_hwnd() is True
    library(hwnd_keyword=False, title_owned=True)
    assert capture.wgc_selects_by_hwnd() is False


def test_binds_by_hwnd_and_never_by_title(library):
    calls = library(hwnd_keyword=True, title_owned=True)
    wgc = backend()
    wgc.start()
    assert calls[1]["window_hwnd"] == 4242
    assert calls[1]["window_name"] is None      # a substring match in 2.0: never used


def test_a_shared_title_does_not_stop_an_hwnd_binding(library):
    # Another window called "Minecraft ..." exists. Bound by handle, that is harmless,
    # at start and on every frame.
    calls = library(hwnd_keyword=True, title_owned=False)
    wgc = backend()
    wgc.start()
    handler = calls[1]["capture"].handlers["on_frame_arrived"]
    stopped = []
    handler(SimpleNamespace(frame_buffer=np.zeros((6, 8, 4), np.uint8), timespan=7),
            SimpleNamespace(stop=lambda: stopped.append(True)))
    assert not stopped and wgc.error is None
    assert wgc.distinct == 1


def test_old_library_still_binds_by_title_and_demands_ownership(library):
    calls = library(hwnd_keyword=False, title_owned=True)
    backend().start()
    assert calls[1] == {"window_name": "Minecraft", "capture": calls[1]["capture"]}

    library(hwnd_keyword=False, title_owned=False)
    with pytest.raises(IdentityLost):
        backend().start()


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


def test_the_dll_is_pinned_before_the_first_session(library):
    # An unpinned GraphicsCapture.dll can be unloaded under a session that is
    # still ending: a native crash, no traceback (Java world reloads, 2026-09-26).
    calls = library(hwnd_keyword=True, title_owned=True)
    backend().start()
    assert calls[0] == "pinned" and "window_hwnd" in calls[1]


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


def test_wgc_refuses_to_start_when_the_library_is_unknown(library, monkeypatch):
    calls = library(hwnd_keyword=True, title_owned=True)
    monkeypatch.setattr(capture, "wgc_selects_by_hwnd", lambda: None)
    with pytest.raises(RuntimeError):
        backend().start()
    assert not any(isinstance(c, dict) for c in calls)             # nothing was bound
