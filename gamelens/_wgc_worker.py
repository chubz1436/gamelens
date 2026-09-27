"""Child-process Windows Graphics Capture session.

A process rather than a thread because windows-capture can crash natively when a
session is restarted, and a native crash takes the whole process with it, no
Python traceback. Found on the Hyper-V VM on both versions tried: 2.0.1 faulted
on Java world reloads, and 1.4.2 -- the version kept because of that -- faulted
inside windows_capture.pyd when the Owner connected with Enhanced Session and the
supervisor tried WGC again (GL-044, 2026-09-26). In here, that crash ends one
worker; GameLens sees the exit and fails over.

The worker only captures. Everything that decides whether a frame may be
trusted -- the first-frame size check, title ownership, deduplication -- stays in
the parent, which reads the frames back out of shared memory.
"""

from __future__ import annotations

import time
from multiprocessing.shared_memory import SharedMemory

from gamelens._frame_shm import (
    CTRL_ERROR, CTRL_STOP, ERR_CLOSED, ERR_FAILED, ERR_TOO_BIG, no_crash_dialog, orphaned,
    write_frame,
)

POLL = 0.02


def run(target: dict, draw_border: bool | None, shm_name: str, ctrl) -> None:
    """Capture the window *target* names into shared memory until asked to stop.

    *target* is the keyword windows-capture binds by: ``{"window_hwnd": ...}``
    on 2.0+, ``{"window_name": ...}`` on 1.4.2. Never raises into the parent.
    """
    shm: SharedMemory | None = None
    control = None
    no_crash_dialog()
    try:
        shm = SharedMemory(name=shm_name)

        # Looked up at call time, so a test can replace it.
        from gamelens import capture
        capture.pin_graphics_capture()

        from windows_capture import WindowsCapture

        cap = WindowsCapture(cursor_capture=False, draw_border=draw_border, **target)

        @cap.event
        def on_frame_arrived(frame, capture_control):  # noqa: ANN001
            try:
                if ctrl[CTRL_STOP] or ctrl[CTRL_ERROR]:
                    capture_control.stop()
                    return
                array = frame.frame_buffer
                height, width = array.shape[:2]
                if width * height * 4 > shm.size:
                    # Signal rather than crop: a cropped frame maps coordinates wrong.
                    ctrl[CTRL_ERROR] = ERR_TOO_BIG
                    capture_control.stop()
                    return
                if not array.flags.c_contiguous:
                    array = array.copy()          # padded rows (1.4.2 hands a strided view)
                write_frame(shm, ctrl, array, width, height, getattr(frame, "timespan", 0) or 0)
            except Exception:
                ctrl[CTRL_ERROR] = ERR_FAILED
                capture_control.stop()

        @cap.event
        def on_closed():  # noqa: ANN001
            pass

        control = cap.start_free_threaded()
        while not ctrl[CTRL_STOP] and not ctrl[CTRL_ERROR]:
            if orphaned():
                return
            if control.is_finished():
                ctrl[CTRL_ERROR] = ERR_CLOSED
                return
            time.sleep(POLL)
    except Exception:
        try:
            ctrl[CTRL_ERROR] = ERR_FAILED
        except Exception:
            pass
    finally:
        if control is not None:
            try:
                control.stop()
            except Exception:
                pass
        # The shared memory is not closed here: the callback thread may still be
        # finishing a write, and closing under it faults. Process exit releases it.
