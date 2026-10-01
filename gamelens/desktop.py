"""Native Windows dashboard. Start with python -m gamelens.desktop.

An existing session is attached with agent access only. A session created by
this app is owned by it and is stopped when its window closes. Credentials are
never included in URLs, files, console output, or a Python/JavaScript bridge.
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.request

from gamelens import DPI
from gamelens.session import (
    agent_token_path, clear_agent_token, publish_agent_token, read_agent_token,
    operator_token_path, clear_operator_token, publish_operator_token, read_operator_token,
)


class DesktopError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DesktopError("The local session redirected. Refusing to connect.")


def existing_session(port: int) -> str | None:
    """Return the verified agent credential, or None when no server is listening."""
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) != 0:
            return None
    try:
        token = read_agent_token(agent_token_path(port))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/state", headers={"X-GameLens-Token": token},
        )
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            state = json.loads(response.read(1_000_000))
        if state.get("role") != "agent" or not isinstance(state.get("capture"), dict):
            raise ValueError("not an authenticated GameLens session")
        try:
            operator = read_operator_token(operator_token_path(port))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/state", headers={"X-GameLens-Token": operator},
            )
            with opener.open(request, timeout=3) as response:
                owner_state = json.loads(response.read(1_000_000))
            if owner_state.get("role") == "operator":
                return operator
        except Exception:
            pass  # Old sessions stay usable with agent access; no token popup.
        return token
    except Exception:
        raise DesktopError(
            f"Port {port} is busy, but its GameLens session could not be verified. "
            "Close the old server or choose another --port."
        ) from None


class OwnedSession:
    """Lifecycle of a server started by this window; never owns an attached server."""
    def __init__(self, port: int):
        self.port = port
        self.lens = None
        self.server = None
        self.thread = None
        self.listener = None
        self.closed = False

    def start(self, target):
        import uvicorn
        from gamelens.app import GameLens
        from gamelens.server import create_app

        try:
            # Reserve before capture starts; another process cannot take our port.
            self.listener = socket.socket()
            self.listener.bind(("127.0.0.1", self.port))
            self.listener.listen(128)
            self.lens = GameLens(target, port=self.port)
            self.lens.start()
            publish_agent_token(agent_token_path(self.port), self.lens.tokens.agent)
            publish_operator_token(operator_token_path(self.port), self.lens.tokens.operator)
            self.server = uvicorn.Server(uvicorn.Config(
                create_app(self.lens), host="127.0.0.1", port=self.port,
                log_level="warning", log_config=None, access_log=False,
                timeout_graceful_shutdown=1,
            ))
            self.thread = threading.Thread(
                target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True,
                name="gamelens-desktop-http",
            )
            self.thread.start()
            deadline = time.monotonic() + 10
            while not self.server.started:
                if not self.thread.is_alive() or time.monotonic() >= deadline:
                    raise DesktopError("The local dashboard could not start.")
                time.sleep(0.05)
            return self.lens.tokens.operator
        except Exception:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.lens is not None:
            # Close the gate and release inputs before winding down HTTP/capture.
            self.lens.safety.kill("desktop window closed")
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.lens is not None:
            try:
                self.lens.stop()
            finally:
                clear_agent_token(agent_token_path(self.port), self.lens.tokens.agent)
                clear_operator_token(operator_token_path(self.port), self.lens.tokens.operator)
        if self.listener is not None:
            self.listener.close()


def choose_target(port: int = 8777):
    """Native picker; return only the exact HWND selected by the owner."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    from gamelens.windows import find_window, list_windows

    root = tk.Tk()
    root.title("GameLens — Choose game" + (f" [{port}]" if port != 8777 else ""))
    root.geometry("760x430")
    root.minsize(600, 350)
    root.configure(background="#0e1116")
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Choose your game window", font=("Segoe UI", 16)).pack(anchor="w")
    ttk.Label(frame, text="Open the game, then Refresh. Capture starts disarmed and in dry-run.").pack(
        anchor="w", pady=(5, 12))
    tree = ttk.Treeview(frame, columns=("title", "exe", "size"), show="headings", selectmode="browse")
    for col, label, width in (("title", "Window", 360), ("exe", "App", 150), ("size", "Size", 95)):
        tree.heading(col, text=label)
        tree.column(col, width=width)
    tree.pack(fill="both", expand=True)
    selected = []

    def refresh():
        tree.delete(*tree.get_children())
        for window in list_windows():
            if window.title.startswith("GameLens"):
                continue
            tree.insert("", "end", iid=str(window.hwnd), values=(
                window.title, window.exe, f"{window.client_width} × {window.client_height}",
            ))

    def open_target():
        choices = tree.selection()
        if not choices:
            return
        try:
            target = find_window(choices[0])
        except LookupError:
            messagebox.showinfo("GameLens", "That window closed. Refresh and choose again.", parent=root)
            refresh()
            return
        selected.append(target)
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(12, 0))
    ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
    ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="Open GameLens", command=open_target).pack(side="right", padx=8)
    tree.bind("<Double-1>", lambda event: open_target())
    refresh()
    root.mainloop()
    return selected[0] if selected else None


def bootstrap(window, url: str, token: str):
    """Authenticate only our exact local page; no exposed native JS API."""
    if window.get_current_url() != url:
        return
    window.evaluate_js(
        "(() => { const input = document.getElementById('gate-input'); "
        "const unlock = document.getElementById('gate-save'); "
        "if (input && unlock) { input.value = " + json.dumps(token) +
        "; unlock.click(); } const access = document.getElementById('btn-access'); "
        "if (access) access.style.display = 'none'; })()"
    )


def run(port: int = 8777, target_query: str | None = None) -> int:
    import webview
    from gamelens.windows import find_window

    if not DPI.trustworthy:
        raise DesktopError("Windows DPI setup is unavailable; capture cannot safely start.")
    token = existing_session(port)
    owned = None
    attached = token is not None
    try:
        if not attached:
            target = find_window(target_query) if target_query else choose_target() if port == 8777 else choose_target(port)
            if target is None:
                return 0
            owned = OwnedSession(port)
            token = owned.start(target)
        url = f"http://127.0.0.1:{port}/"
        window = webview.create_window(
            ("GameLens — Connected session" if attached else "GameLens") +
            (f" [{port}]" if port != 8777 else ""),
            url, width=1240, height=860, min_size=(900, 600), background_color="#0e1116",
        )
        # Native login uses the local operator handoff when available;
        # legacy agent attachments also support authorized Arm/Live controls.
        window.events.loaded += lambda: bootstrap(window, url, token)
        webview.start(gui="edgechromium", private_mode=True, debug=False)
        return 0
    finally:
        if owned is not None:
            owned.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GameLens desktop app")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--target", help="skip picker with an exact HWND or window title")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        return run(args.port, args.target)
    except Exception as exc:
        # Pythonw has no console; show a useful error without leaking credentials.
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        detail = str(exc) if isinstance(exc, DesktopError) else (
            "GameLens could not open. Check that the selected game is still open, "
            "the port is available, and Microsoft Edge WebView2 Runtime is installed. "
            "Install requirements-desktop.txt using the project's Python environment."
        )
        messagebox.showerror("GameLens", detail, parent=root)
        root.destroy()
        return 1


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
