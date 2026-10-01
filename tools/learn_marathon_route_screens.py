"""Import inspected route-screen evidence from the completed October1 race."""
import argparse
from pathlib import Path

import cv2
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("evidence", type=Path)
parser.add_argument("profile", type=Path)
args = parser.parse_args()
p = args.evidence
assets = dict(np.load(args.profile / "screens.npz"))

def action(number):
    if number == 31:
        # Inspected legacy-named proof, before timestamped saving was added.
        return cv2.imread(str(p / "marathon-action-31.jpg"))
    files = sorted(p.glob(f"marathon-20*-action-{number}.jpg"))
    if not files:
        raise ValueError(f"Missing inspected action {number}")
    # Counter restarted with the Owner runtime. The latest dated proof is the
    # fresh October1 race recorded in the attributed action log.
    return cv2.imread(str(files[-1]))

def yellow(frame):
    return cv2.inRange(frame, np.array([0,120,120]), np.array([150,255,255]))

def receipt(frame):
    crop = frame[174:209, 394:970]
    white = cv2.inRange(crop, np.array([190]*3), np.array([255]*3))
    cyan = cv2.inRange(crop, np.array([140,140,0]), np.array([255,255,150]))
    return white | cyan

assets.pop("fitness",None)
for chapter, number in enumerate([31,39,47,54,61,73,81,88,99,107,115,123,131,139,147,155], 1):
    assets[f"receipt{chapter}"] = receipt(action(number))
finished = action(165)
assets["finish"] = cv2.inRange(finished[174:193,405:733], np.array([190]*3), np.array([255]*3))
assets["suburb_map"] = cv2.imread(str(p / "marathon-map-reference.jpg"))[200:250,300:400]
assets["city_map"] = action(93)[200:250,300:400]
assets["suburb_title"] = cv2.inRange(action(90)[35:54,877:1014], np.array([190]*3), np.array([255]*3))
assets["city_title"] = cv2.inRange(action(94)[35:54,877:1014], np.array([190]*3), np.array([255]*3))
np.savez_compressed(args.profile / "screens.npz", **assets)
print({"receipt_count":16})
