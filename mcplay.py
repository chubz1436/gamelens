"""Drive the Minecraft window through GameLens."""
import json, sys, time, urllib.request, urllib.error, win32gui, win32con
from gamelens.windows import describe

BASE = "http://127.0.0.1:8777"
HWND = 5442666
OP = open(r"\?\C:\Users\CHUBZS~1\AppData\Local\Temp\op.tok").read().strip() if False else None


def tokens():
    import pathlib
    d = pathlib.Path(r"C:\Users\CHUBZS~1\AppData\Local\Temp")
    return (d / "op.tok").read_text().strip(), (d / "ag.tok").read_text().strip()


def req(path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"X-GameLens-Token": token,
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def focus():
    win32gui.ShowWindow(HWND, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(HWND)
    time.sleep(0.5)
    return win32gui.GetForegroundWindow() == HWND


def click(x, y, label, op, ag, arm=True):
    """x, y are in the pixels of the 1280-wide served image."""
    if not focus():
        return "NOT_FOREGROUND"
    if arm:
        req("/arm", op, "POST")
        req("/live", op, "POST")
    _, _, h = req("/frame.jpg?quality=80", ag)
    obs = h.get("x-gamelens-observation")
    st, body, _ = req("/act", ag, "POST",
                      {"observation_id": obs, "x": x, "y": y, "label": label})
    try:
        r = json.loads(body)
    except ValueError:
        return body.decode()
    # Both, always. "verdict" alone is what the arbiter thought of the request;
    # "outcome" is what the executor actually did with it. Printing only the
    # first is how this script reported ok for clicks the game never saw.
    detail = f" ({r['detail']})" if r.get("detail") else ""
    return f"{r.get('verdict')}/{r.get('outcome')}{detail}"


def shot(name, op):
    import pathlib
    _, data, h = req("/frame.jpg?quality=80", op)
    out = pathlib.Path(r"C:\Users\CHUBZS~1\AppData\Local\Temp\claude\B--AI-Agent-folder-GAME-VIDEO\20d12316-c40d-48f3-ae5e-bc7d2232e694\scratchpad") / name
    out.write_bytes(data)
    return str(out)


if __name__ == "__main__":
    op, ag = tokens()
    cmd = sys.argv[1]
    if cmd == "click":
        x, y, label = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
        print(f"click {label} at image({x},{y}) -> {click(x, y, label, op, ag)}", flush=True)
        time.sleep(float(sys.argv[5]) if len(sys.argv) > 5 else 1.5)
        print("shot:", shot(sys.argv[6] if len(sys.argv) > 6 else "mc.jpg", op), flush=True)
    elif cmd == "shot":
        print("shot:", shot(sys.argv[2], op), flush=True)
    elif cmd == "state":
        _, body, _ = req("/state", op)
        s = json.loads(body)
        print(json.dumps({"capture": s["capture"], "safety": s["safety"],
                          "log": s["log"][-4:]}, indent=2), flush=True)
