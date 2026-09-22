"""Acceptance A5: does a click land where the coordinate said it would?

    .venv/Scripts/python.exe tools/coord_probe.py --target "Your Game Window"

Run it, read the error column. Everything else in the test suite checks that
the transforms are self-consistent, which is a different and much weaker claim:
a sign error in the virtual-desktop origin round-trips perfectly and still puts
every click on the wrong monitor. The only thing that settles it is moving the
real cursor and asking Windows where it ended up.

**The case this exists for is a monitor placed left of or above the primary**,
which makes `SM_XVIRTUALSCREEN` negative. On a single monitor the origin is
(0, 0), every origin term is zero, and the probe cannot distinguish a correct
implementation from one that dropped the term entirely -- it will report zero
error either way. It says so rather than implying it proved something.

Nothing is clicked. The pointer is moved and read back, and put where it
started afterwards.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from gamelens.coords import GeometryTracker, virtual_desktop           # noqa: E402
from gamelens.dpi import require_trustworthy_dpi, thread_dpi_state     # noqa: E402
from gamelens.input import (                                           # noqa: E402
    _send,
    cursor_position,
    move_events,
)
from gamelens.windows import AmbiguousTarget, find_window              # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", required=True, help="window title, substring, or hwnd")
    parser.add_argument("--settle", type=float, default=0.08,
                        help="pause between the move and reading the cursor back")
    args = parser.parse_args(argv)

    state = thread_dpi_state()
    print(f"DPI awareness  : {state.requested} -> {state.effective}"
          f"{'' if state.trustworthy else '  (NOT TRUSTWORTHY: ' + state.reason + ')'}")
    require_trustworthy_dpi()

    vd = virtual_desktop()
    print(f"virtual desktop: origin ({vd.left}, {vd.top})  size {vd.width}x{vd.height}")
    if vd.left == 0 and vd.top == 0:
        print("                 origin is (0, 0) -- this run CANNOT prove A5. A monitor")
        print("                 left of or above the primary is what makes it negative,")
        print("                 and that is the case the origin term exists for.")

    try:
        target = find_window(args.target)
    except AmbiguousTarget as exc:
        print(exc, file=sys.stderr)
        return 2
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2

    tracker = GeometryTracker(target.hwnd)
    tracker.refresh()
    geom = tracker.current
    print(f"target         : {target.title!r} (hwnd {target.hwnd})")

    started_at = cursor_position()
    width, height = target.width, target.height

    # Corners and centre, inset so nothing lands on a resize border.
    inset = 8
    points = [
        ("top-left", inset, inset),
        ("top-right", width - inset, inset),
        ("centre", width // 2, height // 2),
        ("bottom-left", inset, height - inset),
        ("bottom-right", width - inset, height - inset),
    ]

    print()
    print(f"{'point':<14} {'frame':>12} {'screen':>14} {'cursor':>14} {'error':>8}")
    print("-" * 68)

    worst = 0.0
    try:
        for name, fx, fy in points:
            try:
                sx, sy = geom.frame_to_screen(fx, fy, width, height)
            except Exception as exc:
                print(f"{name:<14} {f'{fx},{fy}':>12}  mapping refused: {exc}")
                worst = float("inf")
                continue

            _send(move_events(sx, sy, vd))
            time.sleep(args.settle)
            got = cursor_position()
            if got is None:
                print(f"{name:<14}  cursor unreadable")
                worst = float("inf")
                continue

            err = max(abs(got[0] - sx), abs(got[1] - sy))
            worst = max(worst, err)
            print(f"{name:<14} {f'{fx},{fy}':>12} {f'{sx},{sy}':>14} "
                  f"{f'{got[0]},{got[1]}':>14} {err:>8}")
    finally:
        if started_at:
            _send(move_events(started_at[0], started_at[1], vd))

    print()
    # One pixel is the normalization's own rounding: the 0..65535 range does not
    # divide evenly into a desktop of arbitrary width.
    if worst <= 1:
        print(f"PASS  worst error {worst}px (<=1px is normalization rounding)")
        if vd.left == 0 and vd.top == 0:
            print("      but see the note above -- A5 remains unproven on one monitor.")
        return 0
    print(f"FAIL  worst error {worst}px")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
