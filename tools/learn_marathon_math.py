"""Extract supervised game glyphs from explicitly labelled local evidence."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from gamelens.marathon import glyphs

parser = argparse.ArgumentParser()
parser.add_argument("manifest", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
bank = {}
for sample in json.loads(args.manifest.read_text()):
    frame = cv2.imread(sample["image"])
    text = sample["equation"].replace(" ", "")
    valid=0
    for threshold in (170,180,190,200):
        try:
            parts=glyphs(frame,threshold)
        except ValueError:
            continue
        if len(parts)!=len(text):
            continue
        valid+=1
        for char,part in zip(text,parts):
            bank.setdefault(char,[])
            value=part.astype(int).tolist()
            if value not in bank[char]:
                bank[char].append(value)
    if valid<2:
        raise ValueError(f"Supervised segmentation unproven: {sample['image']}")
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(bank, indent=2))
print(json.dumps({"glyphs": sorted(bank), "samples": sum(map(len, bank.values()))}))
