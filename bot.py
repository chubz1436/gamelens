"""Local reflex helpers for driving the demo.

Two things learned the hard way live in here. Hold times are the game's, not
guesses: dirt barehanded is 0.75s and wood 3.0s, and a hold shorter than that
breaks nothing while every outcome still reads "sent". And a frame fetched the
instant an action returns can still show the world before it -- the action is
acknowledged when injected, not when rendered -- so anything that reads pixels
to decide has to let the screen catch up first.
"""
import json, math, sys, time
import cv2, numpy as np
sys.path.insert(0, r"B:\AI_Agent_folder\GAME VIDEO")
import play

OP, AG = play.tokens()
DEG = 1400 / 180.0        # measured against the two pitch stops
SETTLE = 0.12             # render lag: a frame read sooner shows the old view

# barehanded break times, seconds, plus margin
DIRT, WOOD, STONE_HAND = 1.30, 3.60, 8.00
VFOV = 70.0                # Minecraft's FOV setting is the vertical one


def frame(q=50):
    _, data, h = play.req(f"/frame.jpg?quality={q}", AG)
    return (cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR),
            h.get("x-gamelens-observation"))


def do(**body):
    _, obs = frame(35)
    body["observation_id"] = obs
    r = json.loads(play.req("/act", AG, "POST", body)[1])
    if r.get("outcome") not in ("sent", "pending"):
        print(f"    !! {body.get('kind')} -> {r.get('outcome')} {r.get('detail')}", flush=True)
    return r


def look(dx=0, dy=0):
    do(kind="look", dx=int(dx), dy=int(dy), label="bot")


def turn(deg):
    """Yaw in degrees, split so each piece stays inside the clamp."""
    n = int(abs(deg) * DEG // 500) + 1
    for _ in range(n):
        look(dx=deg * DEG / n)


def pitch_to(deg_above_horizon):
    """Absolute pitch: pin at the down stop, then climb a known number."""
    for _ in range(4):
        look(dy=600)
    remaining = (90 + deg_above_horizon) * DEG
    while remaining > 0:
        look(dy=-min(500, remaining))
        remaining -= 500


def mine(seconds):
    do(kind="press", button="left", hold=seconds, label="bot-mine")


def key(name, hold=0.10):
    do(kind="key", key=name, hold=hold, label="bot-key")


def place():
    do(kind="press", button="right", hold=0.08, label="bot-place")


def see(name, q=80, lift=1.0):
    arr, _ = frame(q)
    if lift != 1.0:
        arr = cv2.convertScaleAbs(arr, alpha=lift, beta=10)
    cv2.imwrite(str(play.SHOTS / name), arr)
    return arr


def grid(tiles, name, scale=0.62):
    rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)]
    cv2.imwrite(str(play.SHOTS / name),
                cv2.resize(np.vstack(rows), None, fx=scale, fy=scale))


def hotbar(name, scale=3.0):
    """The hotbar, enlarged. Item counts are two tiny digits at 856x512."""
    arr, _ = frame(90)
    h, w = arr.shape[:2]
    crop = arr[int(h * 0.88):, int(w * 0.27):int(w * 0.74)]
    crop = cv2.convertScaleAbs(crop, alpha=1.9, beta=8)
    cv2.imwrite(str(play.SHOTS / name),
                cv2.resize(crop, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_NEAREST))


def norm(arr):
    """Stretch so the scene's own highlights land near white."""
    f = arr.astype(np.float32)
    hi = max(np.percentile(f, 97), 1.0)
    return np.clip(f * (200.0 / hi), 0, 255)


def hfov(w, h):
    return math.degrees(2 * math.atan(math.tan(math.radians(VFOV / 2)) * (w / h)))


# A trunk classifier keyed on appearance used to live here and has been removed.
# It was refuted four times in one session by the world changing around it: a
# red-dominant rule written for oak against a birch forest, then a pale-column
# rule that matched sky through the canopy, then the same rule with a width
# gate that matched more sky, then a texture gate that matched falling rain --
# which is pale, vertical and streaked, and so is a birch trunk.
#
# Collision survives all four: walking forward changes the view and walking
# into a block does not, and the sky cannot fake that. Identification belongs
# in the slow tier; this tier should execute a bearing it is handed.
