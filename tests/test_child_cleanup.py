"""A capture child must not outlive GameLens.

Found on the Hyper-V VM: GameLens was stopped hard (Stop-Process), and its
PrintWindow worker -- a daemon multiprocessing child -- kept running with no
parent, holding open the log file the next run needed. ``daemon=True`` only
helps on a clean interpreter exit. The fix ties each worker to a job object that
the kernel closes when GameLens dies.

This test does it for real: a parent process ties a sleeping child to itself,
the parent is terminated without any chance to clean up, and the child has to
be gone.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows job objects")

ROOT = Path(__file__).resolve().parents[1]

PARENT = textwrap.dedent("""
    import subprocess, sys, time
    sys.path.insert(0, {root!r})
    from gamelens.capture import die_with_this_process
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    tied = die_with_this_process(child.pid) if {tie} else False
    print(child.pid, tied, flush=True)
    time.sleep(60)
""")

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


def run_parent(tie: bool):
    parent = subprocess.Popen([sys.executable, "-c", PARENT.format(root=str(ROOT), tie=tie)],
                              stdout=subprocess.PIPE, text=True)
    pid, tied = parent.stdout.readline().split()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    assert handle, "child not found"
    exited = False
    try:
        parent.kill()                       # TerminateProcess: no atexit, no finally
        parent.wait(5)
        exited = kernel32.WaitForSingleObject(handle, 5000) == WAIT_OBJECT_0
    finally:
        if not exited:
            subprocess.run(["taskkill", "/f", "/pid", pid], capture_output=True)
        kernel32.CloseHandle(handle)
    return tied == "True", exited


def test_a_tied_child_dies_with_a_hard_killed_parent():
    tied, exited = run_parent(tie=True)
    assert tied
    assert exited


def test_an_untied_child_would_have_survived():
    # The control: without the job the same kill leaves the child running,
    # so the test above is measuring the fix and not the kill.
    tied, exited = run_parent(tie=False)
    assert not tied
    assert not exited


def test_the_printwindow_worker_is_tied_when_it_starts(monkeypatch):
    from types import SimpleNamespace

    from gamelens import capture

    class FakeProc:
        pid = 4321
        exitcode = None

        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def is_alive(self):
            return True

        def terminate(self):
            pass

        def kill(self):
            pass

        def join(self, timeout=None):
            pass

    tied = []
    monkeypatch.setattr(capture.mp, "Process", FakeProc)
    monkeypatch.setattr(capture, "die_with_this_process", lambda pid: tied.append(pid) or True)
    monkeypatch.setattr(capture, "is_alive", lambda hwnd: True)
    backend = capture.PrintWindowBackend(
        SimpleNamespace(hwnd=1, title="Game", width=4, height=4,
                        verify=lambda **kw: None),
        capture.FramePool(), capture.LatestFrame(), max_pixels=16)
    backend.start()
    try:
        assert tied == [4321]
    finally:
        backend.stop()


def test_an_untied_worker_is_refused_not_run(monkeypatch):
    # RV03-I03 (Codex): a worker that could not be tied would outlive a hard kill.
    from types import SimpleNamespace

    from gamelens import capture

    events = []

    class FakeProc:
        pid = 4321
        exitcode = None

        def __init__(self, **kw):
            self.alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            events.append("terminate")
            self.alive = False

        def kill(self):
            self.alive = False

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(capture.mp, "Process", FakeProc)
    monkeypatch.setattr(capture, "die_with_this_process", lambda pid: False)
    backend = capture.PrintWindowBackend(
        SimpleNamespace(hwnd=1, title="Game", width=4, height=4,
                        verify=lambda **kw: None),
        capture.FramePool(), capture.LatestFrame(), max_pixels=16)
    with pytest.raises(RuntimeError):
        backend.start()
    assert events == ["terminate"]
    assert backend.retired and backend._reader is None and backend._shm is None


def test_the_worker_exits_on_its_own_once_orphaned(monkeypatch):
    # The moment before the job assignment: a parent killed then leaves a worker
    # that must notice by itself.
    import multiprocessing
    from multiprocessing.shared_memory import SharedMemory

    from gamelens import _pw_worker as worker

    import win32gui

    monkeypatch.setattr(multiprocessing, "parent_process",
                        lambda: type("Gone", (), {"is_alive": lambda self: False})())
    looked = []
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda h: looked.append(h) or (0, 0, 2, 2))
    shm = SharedMemory(create=True, size=64)
    ctrl = [0] * worker.CTRL_SIZE
    import threading
    t = threading.Thread(target=worker.run, args=(0, shm.name, ctrl), daemon=True)
    try:
        t.start()
        t.join(2.0)
        still_running = t.is_alive()
    finally:
        ctrl[worker.CTRL_STOP] = 1               # end it either way
        t.join(2.0)
        shm.close()
        shm.unlink()
    assert not still_running
    assert looked == []                          # left before touching the window
