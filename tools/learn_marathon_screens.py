"""Import only inspected screenshot crops; never learn from an arbitrary frame."""
import argparse
from pathlib import Path

import cv2
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("evidence", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
p = args.evidence
assets = {
    "category": cv2.imread(str(p / "upgrade-game-capture.jpg"))[282:300, 90:233],
    "turnin": cv2.imread(str(p / "marathon-turnin-reference.jpg"))[281:299, 408:592],
}
ids = {1:85, 2:57, 3:50, 4:69, 5:43, 6:95, 7:103, 8:135, 9:111, 10:119}
for trainer, action in ids.items():
    matches = list(p.glob(f"marathon-20*-action-{action}.jpg"))
    if len(matches) != 1:
        raise ValueError(f"Ambiguous inspected Trainer{trainer} evidence")
    crop = cv2.imread(str(matches[0]))[35:55, 480:640]
    assets[f"trainer{trainer}"] = cv2.inRange(crop, np.array([190]*3), np.array([255]*3))
args.output.parent.mkdir(parents=True, exist_ok=True)
np.savez_compressed(args.output, **assets)
print(sorted(assets))
