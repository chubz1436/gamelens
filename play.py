"""Play driver: clicks, keys and camera through GameLens's HTTP surface.

    python play.py look 60 0
    python play.py key w 0.5
    python play.py click 427 478 done
    python play.py seq "key w 0.6; look 40 0; key space 0.05" shot.jpg
"""
import json, pathlib, sys, time, urllib.error, urllib.request
import win32con, win32gui

BASE = "http://127.0.0.1:8777"
HWND = 5442666
TMP = pathlib.Path(r"C:\Users\CHUBZS~1\AppData\Local\Temp")
SHOTS = pathlib.Path(r"C:\Users\CHUBZS~1\AppData\Local\Temp\claude\B--AI-Agent-folder-GAME-VIDEO\20d12316-c40d-48f3-ae5e-bc7d2232e694\scratchpad")


def tokens():
    return (TMP / "op.tok").read_text().strip(), (TMP / "ag.tok").read_text().strip()


def req(path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"X-GameLens-Token": token,
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def focus():
    if win32gui.GetForegroundWindow() == HWND:
        return True
    win32gui.ShowWindow(HWND, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(HWND)
    except Exception:
        pass
    time.sleep(0.35)
    return win32gui.GetForegroundWindow() == HWND


def act(ag, **body):
    focus()
    _, _, h = req("/frame.jpg?quality=50", ag)
    body["observation_id"] = h.get("x-gamelens-observation")
    st, raw, _ = req("/act", ag, "POST", body)
    r = json.loads(raw)
    return f"{r['verdict']}/{r['outcome']}" + (f" ({r['detail']})" if r.get("detail") else "")


def shot(name, op):
    _, data, _ = req("/frame.jpg?quality=80", op)
    out = SHOTS / name
    out.write_bytes(data)
    return name


def run_step(ag, text):
    parts = text.split()
    kind = parts[0]
    if kind == "key":
        return act(ag, kind="key", key=parts[1],
                   hold=float(parts[2]) if len(parts) > 2 else 0.08,
                   label=f"key-{parts[1]}")
    if kind == "look":
        return act(ag, kind="look", dx=float(parts[1]), dy=float(parts[2]), label="look")
    if kind == "press":
        # No movement: the cursor is locked to the crosshair, so aiming is done
        # with `look` and this only presses where the player is already aimed.
        return act(ag, kind="press", button=parts[1],
                   hold=float(parts[2]) if len(parts) > 2 else 0.08,
                   label=f"press-{parts[1]}")
    if kind == "click":
        return act(ag, kind="click", x=float(parts[1]), y=float(parts[2]),
                   label=parts[3] if len(parts) > 3 else "click")
    if kind == "wait":
        time.sleep(float(parts[1]))
        return "slept"
    raise SystemExit(f"unknown step {text!r}")


if __name__ == "__main__":
    op, ag = tokens()
    cmd = sys.argv[1]
    if cmd == "arm":
        req("/arm", op, "POST"); print(req("/live", op, "POST")[0], "armed+live")
    elif cmd == "shot":
        print("shot:", shot(sys.argv[2], op))
    elif cmd == "seq":
        for step in [s.strip() for s in sys.argv[2].split(";") if s.strip()]:
            print(f"  {step:<24} -> {run_step(ag, step)}", flush=True)
        if len(sys.argv) > 3:
            time.sleep(float(sys.argv[4]) if len(sys.argv) > 4 else 0.6)
            print("  shot:", shot(sys.argv[3], op), flush=True)
    else:
        print(f"  {' '.join(sys.argv[1:]):<24} -> {run_step(ag, ' '.join(sys.argv[1:]))}")
