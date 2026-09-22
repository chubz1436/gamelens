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

PW_RENDERFULLCONTENT = 0x00000002

# Layout of the shared control array (all ints):
CTRL_STOP = 0        # parent sets 1 to ask for a clean exit
CTRL_COUNTER = 1     # child bumps on each successful capture
CTRL_WIDTH = 2
CTRL_HEIGHT = 3
CTRL_ERROR = 4       # child sets 1 on a fatal error
CTRL_VERSION = 5     # seqlock: odd while writing, even when a frame is settled
CTRL_SIZE = 6


def run(hwnd: int, shm_name: str, ctrl, interval: float = 0.008) -> None:
    """Capture *hwnd* into shared memory until asked to stop.

    Never raises into the parent: a fatal problem is signalled through the
    control array, because an exception here would just kill a process the parent
    is already watching.
    """
    import win32gui
    import win32ui

    shm: SharedMemory | None = None
    try:
        shm = SharedMemory(name=shm_name)
        user32 = ctypes.windll.user32

        while not ctrl[CTRL_STOP]:
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
                    ctrl[CTRL_ERROR] = 1
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
                        bits = bitmap.GetBitmapBits(True)
                        # Seqlock. The pixels and the dimensions that describe
                        # them must be read together or not at all: a reader
                        # that catches us mid-write would otherwise copy half of
                        # one frame and half of the next, or read this frame's
                        # pixels with the previous frame's width.
                        ctrl[CTRL_VERSION] = ctrl[CTRL_VERSION] + 1     # -> odd
                        shm.buf[: len(bits)] = bits
                        ctrl[CTRL_WIDTH] = width
                        ctrl[CTRL_HEIGHT] = height
                        ctrl[CTRL_COUNTER] = ctrl[CTRL_COUNTER] + 1
                        ctrl[CTRL_VERSION] = ctrl[CTRL_VERSION] + 1     # -> even
                finally:
                    try:
                        win32gui.DeleteObject(bitmap.GetHandle())
                        save_dc.DeleteDC()
                        mfc_dc.DeleteDC()
                        win32gui.ReleaseDC(hwnd, window_dc)
                    except Exception:
                        pass
            except Exception:
                ctrl[CTRL_ERROR] = 1
                return

            time.sleep(interval)
    except Exception:
        try:
            ctrl[CTRL_ERROR] = 1
        except Exception:
            pass
    finally:
        if shm is not None:
            try:
                shm.close()
            except Exception:
                pass
