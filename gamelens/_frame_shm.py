"""What a capture worker process and GameLens share: one frame slot in shared memory.

Both capture methods that can take GameLens down with them run in a child
process -- PrintWindow because it can block forever, WGC because windows-capture
can crash natively when a session restarts -- and hand frames back through this
slot. The child writes; the parent reads, and never trusts a read that overlapped
a write.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import sys

# Layout of the shared control array (all 64-bit ints, typecode "q"):
CTRL_STOP = 0        # parent sets 1 to ask for a clean exit
CTRL_COUNTER = 1     # child bumps on each frame written
CTRL_WIDTH = 2
CTRL_HEIGHT = 3
CTRL_ERROR = 4       # child sets one of the ERR_* codes on a fatal error
CTRL_VERSION = 5     # seqlock: odd while writing, even when a frame is settled
CTRL_TIMESPAN = 6    # native timestamp of the frame, 0 when there is none
CTRL_SIZE = 7
CTRL_TYPECODE = "q"

ERR_FAILED = 1       # an exception in the worker
ERR_TOO_BIG = 2      # the frame outgrew the buffer the parent allocated
ERR_CLOSED = 3       # the capture session ended on its own

ERROR_TEXT = {
    ERR_FAILED: "reported a fatal error",
    ERR_TOO_BIG: "got a frame larger than its shared buffer",
    ERR_CLOSED: "saw its capture session end",
}


def write_frame(shm, ctrl, data, width: int, height: int, timespan: int = 0) -> None:
    """Publish one BGRA frame of ``width * height * 4`` contiguous bytes.

    Seqlock. The pixels and the numbers that describe them must be read together
    or not at all: a reader that catches the writer mid-frame would otherwise copy
    half of one frame and half of the next, or read this frame's pixels with the
    previous frame's width.
    """
    view = memoryview(data).cast("B")
    ctrl[CTRL_VERSION] = ctrl[CTRL_VERSION] + 1     # -> odd
    shm.buf[: len(view)] = view
    ctrl[CTRL_WIDTH] = width
    ctrl[CTRL_HEIGHT] = height
    ctrl[CTRL_TIMESPAN] = timespan
    ctrl[CTRL_COUNTER] = ctrl[CTRL_COUNTER] + 1
    ctrl[CTRL_VERSION] = ctrl[CTRL_VERSION] + 1     # -> even


def orphaned() -> bool:
    """True once the process that started this worker is gone.

    Covers the moment before the parent ties the worker to its kill-on-close job
    (``capture.die_with_this_process``): a parent killed then leaves a worker
    that has to notice by itself (RV03-I03).
    """
    parent = multiprocessing.parent_process()
    return parent is not None and not parent.is_alive()


def no_crash_dialog() -> None:
    """Let a native crash end this process at once instead of waiting on a dialog.

    With Windows Error Reporting's "stopped working" box up, a crashed worker
    stays alive -- and silent -- until someone clicks it away, which on an
    unattended VM is never. The parent would still kill it on the frame deadline;
    this makes the death immediate and the exit code honest.
    """
    if sys.platform.startswith("win"):
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        try:
            ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
        except Exception:
            pass
