"""Persist inspected portal/mount proofs from this session's real captures."""
import argparse
from pathlib import Path

import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    root = args.evidence / "portal-inspection"
    mounted = cv2.imread(str(root / "marathon-cast/after.jpg"))
    ready = cv2.imread(str(root / "bind/bound.jpg"))
    spawn = cv2.imread(str(root / "athens-cast/after.jpg"))
    if any(frame is None or frame.shape[:2] != (800, 1026)
           for frame in (mounted, ready, spawn)):
        raise ValueError("Inspected geometry/evidence missing")
    path = Path("profiles/godsarena/screens.npz")
    with np.load(path) as prior:
        assets = {name: prior[name] for name in prior.files}
    assets["mounted_buff"] = cv2.inRange(mounted[64:85,768:788],
                                        np.array([190]*3), np.array([255]*3))
    assets["xp_buff"] = cv2.inRange(spawn[64:85,796:816],
                                   np.array([190]*3), np.array([255]*3))
    assets["athens_portal_ready"] = ready[748:779,414:436]
    assets["riding_ready"] = ready[748:779,627:651]
    assets["athens_spawn_gps"] = cv2.inRange(spawn[59:74,896:986],
                                           np.array([190]*3), np.array([255]*3))
    np.savez_compressed(path, **assets)
    print("Added inspected mount buff, ready Portal2 and Athens150,-150 GPS proofs")


if __name__ == "__main__":
    main()
