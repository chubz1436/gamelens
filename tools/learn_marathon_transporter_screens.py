"""Import visually inspected free Transporter destination/menu evidence."""
import argparse
from pathlib import Path

import cv2
import numpy as np


parser=argparse.ArgumentParser()
parser.add_argument("evidence",type=Path)
args=parser.parse_args()
root=args.evidence
search=cv2.imread(str(root/"transporter-stage6/search-menu.jpg"))
destination=cv2.imread(str(root/"transporter-first-suburb/destination-menu.jpg"))
transmit=cv2.imread(str(root/"transporter-current/transmit-menu.jpg"))
if any(frame is None or frame.shape[:2]!=(800,1026)
       for frame in (search,destination,transmit)):
    raise ValueError("Inspected Transporter evidence missing")
path=Path("profiles/godsarena/screens.npz")
with np.load(path) as prior:
    assets={name:prior[name] for name in prior.files}
white=lambda crop:cv2.inRange(crop,np.array([190]*3),np.array([255]*3))
assets["transfer_search"]=white(search[283:299,827:950])
assets["transporter"]=white(destination[35:55,480:640])
assets["arrivaltransporter"]=white(destination[59:74,896:986])
assets["transmit"]=transmit[229:244,90:153]
assets["suburb_destination"]=destination[282:298,408:618]
np.savez_compressed(path,**assets)
print("Saved inspected Transfer(Location),169,-39 Transporter and suburb warp proofs")
