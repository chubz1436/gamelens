"""Learn HUD arrival proofs from an independently reviewed complete race."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


parser=argparse.ArgumentParser()
parser.add_argument("completed_race",type=Path)
args=parser.parse_args()
path=Path("profiles/godsarena/screens.npz")
with np.load(path) as prior:
    assets={name:prior[name] for name in prior.files}
found={}
for line in (args.completed_race/"actions.jsonl").read_text().splitlines():
    event=json.loads(line)
    prefix="Marathon select Trainer"
    if not event["label"].startswith(prefix):
        continue
    trainer=int(event["label"][len(prefix):])
    if trainer in found:
        continue
    frame=cv2.imread(str(args.completed_race/f"frame-{event['before_frame']:04}.jpg"))
    if frame is None or frame.shape[:2]!=(800,1026):
        raise ValueError("Reviewed arrival frame missing or changed geometry")
    assets[f"arrival{trainer}"]=cv2.inRange(frame[59:74,896:986],
                                           np.array([190]*3),np.array([255]*3))
    found[trainer]=event["before_frame"]
if set(found)!=set(range(1,11)):
    raise ValueError("Complete ten-Trainer arrival evidence required")
np.savez_compressed(path,**assets)
print(found)
