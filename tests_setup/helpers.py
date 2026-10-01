"""Fixtures for future validation only. No tests were run while authoring."""
import io
import json
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr

from tools import check_setup as checker
from tools import setup_check_http as http
from tools import setup_check_session as session

CANARY = "SECRET_CANARY_DO_NOT_PRINT_9f823c"
PINS = {"windows-capture": "1.4.2", "mss": "9.0.1", "pywin32": "306",
        "numpy": "1.26.4", "opencv-python": "4.10.0.84", "fastapi": "0.115.0",
        "uvicorn": "0.30.6", "anthropic": "0.39.0"}


def state_fixture():
    return {
        "capture": {"backend": "wgc", "healthy": True, "age_ms": 12.0,
                    "frame_id": 7, "width": 1280, "height": 720},
        "target": {"hwnd": 42, "foreground": True, "width": 1280, "height": 720,
                   "title": CANARY},
        "safety": {"armed": False, "dry_run": True, "killed": False,
                   "executor": {"unreleased": []}},
        "log": [{"message": CANARY}], "session": {"message": CANARY},
    }


def response(raw=None, status=200, extra_headers=b"", declared_length=None):
    raw = json.dumps(state_fixture()).encode() if raw is None else raw
    length = len(raw) if declared_length is None else declared_length
    return (b"HTTP/1.1 " + str(status).encode() + b" status\r\nContent-Type: application/json\r\n"
            + b"Content-Length: " + str(length).encode() + b"\r\n" + extra_headers + b"\r\n" + raw)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeSocket:
    def __init__(self, chunks=None, partial_write=100000):
        self.chunks = list(chunks if chunks is not None else [response()])
        self.partial_write = partial_write
        self.send_results = []
        self.sent = bytearray()
        self.factory_calls = []
        self.connect_calls = []
        self.close_calls = 0
        self.blocking = None
        self.connect_code = 0
        self.socket_error = 0
        self.write_stall = False
        self.tick = 0.001
        self.clock = FakeClock()

    def factory(self, family, kind):
        self.factory_calls.append((family, kind))
        return self

    def setblocking(self, value):
        self.blocking = value

    def connect_ex(self, address):
        self.connect_calls.append(address)
        return self.connect_code

    def getsockopt(self, *args):
        return self.socket_error

    def send(self, data):
        outcome = self.send_results.pop(0) if self.send_results else self.partial_write
        if isinstance(outcome, BaseException):
            raise outcome
        count = min(outcome, len(data))
        self.sent.extend(data[:count])
        return count

    def recv(self, size):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        if chunk is None:
            raise AssertionError("stalled socket should not be read")
        result = chunk[:size]
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
        return result

    def wait(self, reading, writing, exceptional, timeout):
        stalled = (bool(writing) and self.write_stall) or (
            bool(reading) and bool(self.chunks) and self.chunks[0] is None)
        if stalled:
            self.clock.value += timeout
            return [], [], []
        self.clock.value += self.tick
        return reading, writing, []

    def close(self):
        self.close_calls += 1

    def probe(self, **kwargs):
        return http.probe_state("http://127.0.0.1:8777", "fixture-agent-token",
                                socket_factory=self.factory, clock=self.clock,
                                waiter=self.wait, **kwargs)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.seed_environment()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def seed_environment(self):
        lines = [name + ("[standard]" if name == "uvicorn" else "") + "==" + version
                 for name, version in PINS.items()]
        self.write("requirements.txt", "\n".join(lines) + "\n")
        self.write("requirements-desktop.txt", "-r requirements.txt\npywebview==6.2.1\n")
        self.python_path = self.write(".venv/Scripts/python.exe", b"fixture: NEVER EXECUTE")
        self.launcher = self.write("plugins/gamelens/scripts/mcp_server.py", "raise RuntimeError('must not run')\n")
        self.write("gamelens/mcp.py", "raise RuntimeError('must not import')\n")
        for name, version in dict(PINS, pywebview="6.2.1").items():
            self.write(".venv/Lib/site-packages/%s-%s.dist-info/METADATA" % (name.replace("-", "_"), version),
                       "Metadata-Version: 2.1\nName: %s\nVersion: %s\n\nfixture\n" % (name, version))

    def run_checker(self, args=None):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stderr(errors):
            code = checker.main(["--format", "json"] + list(args or []), out=output, root=self.root)
        return code, json.loads(output.getvalue()), errors.getvalue()

    def codes(self, report):
        return {item["reason_code"] for item in report["checks"]}

    def claude_config(self, entry=None):
        if entry is None:
            entry = {"command": str(self.python_path), "args": [str(self.launcher)],
                     "env": {"GAMELENS_PROJECT_DIR": str(self.root)}}
        return self.write(".mcp.json", json.dumps({"mcpServers": {"gamelens": entry}}))

    def snapshots(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}
