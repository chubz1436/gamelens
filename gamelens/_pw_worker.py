"""Child-process PrintWindow loop.

This lives in its own module because Windows spawns child processes rather than
forking them, so the entry point has to be importable by name.

It runs in a *process* rather than a thread for one reason: ``PrintWindow`` is
dispatched to the target application and a hung game can make it never return.
A blocked thread cannot be interrupted from outside -- a blocked process can be
terminated. The parent owns the deadline and kills this process if it stops
producing; it never waits for the blocked call to come back.
"""

from __future__ import annotations

import ctypes
import time
from multiprocessing.shared_memory import SharedMemory

from gamelens._frame_shm import (  # noqa: F401  (the layout is re-exported for callers)
    CTRL_COUNTER, CTRL_ERROR, CTRL_HEIGHT, CTRL_SIZE, CTRL_STOP, CTRL_TIMESPAN, CTRL_TYPECODE,
    CTRL_VERSION, CTRL_WIDTH, ERR_FAILED, ERR_TOO_BIG, no_crash_dialog, orphaned, write_frame,
)

PW_RENDERFULLCONTENT = 0x00000002


def run(hwnd: int, shm_name: str, ctrl, interval: float = 0.008) -> None:
    """Capture *hwnd* into shared memory until asked to stop.

    Never raises into the parent: a fatal problem is signalled through the
    control array, because an exception here would just kill a process the parent
    is already watching.
    """
    import win32gui
    import win32ui

    shm: SharedMemory | None = None
    no_crash_dialog()
    try:
        shm = SharedMemory(name=shm_name)
        user32 = ctypes.windll.user32

        while not ctrl[CTRL_STOP]:
            if orphaned():
                return
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width, height = right - left, bottom - top
                if width <= 0 or height <= 0:
                    time.sleep(interval)
                    continue
                if width * height * 4 > shm.size:
                    # Window grew past the buffer the parent allocated. Signal
                    # rather than truncate: a silently cropped frame would map
                    # coordinates wrong.
                    ctrl[CTRL_ERROR] = ERR_TOO_BIG
                    return

                window_dc = win32gui.GetWindowDC(hwnd)
                mfc_dc = win32ui.CreateDCFromHandle(window_dc)
                save_dc = mfc_dc.CreateCompatibleDC()
                bitmap = win32ui.CreateBitmap()
                try:
                    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
                    save_dc.SelectObject(bitmap)

                    # The call that can block forever. Everything above is cheap
                    # setup; this is why we are a separate process.
                    ok = user32.PrintWindow(
                        hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT
                    )
                    if ok:
                        write_frame(shm, ctrl, bitmap.GetBitmapBits(True), width, height)
                finally:
                    try:
                        win32gui.DeleteObject(bitmap.GetHandle())
                        save_dc.DeleteDC()
                        mfc_dc.DeleteDC()
                        win32gui.ReleaseDC(hwnd, window_dc)
                    except Exception:
                        pass
            except Exception:
                ctrl[CTRL_ERROR] = ERR_FAILED
                return

            time.sleep(interval)
    except Exception:
        try:
            ctrl[CTRL_ERROR] = ERR_FAILED
        except Exception:
            pass
    finally:
        if shm is not None:
            try:
                shm.close()
            except Exception:
                pass
