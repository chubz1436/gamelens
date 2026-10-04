"""Authored, NOT RUN. All writes below belong to disposable test fixtures."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import CANARY, FakeSocket, FixtureCase, checker, http, response, session


class ReadOnlyTests(FixtureCase):
    def test_offline_no_credentials_network_subprocess_or_file_mutation(self):
        self.claude_config()
        before = self.snapshots()
        with mock.patch.object(session, "read_selected_agent", side_effect=AssertionError("offline credential read")), \
             mock.patch.object(http, "probe_state", side_effect=AssertionError("offline network")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("runtime startup")), \
             mock.patch("os.system", side_effect=AssertionError("shell execution")):
            _, report, errors = self.run_checker(["--mcp-host", "claude-code", "--mcp-config", str(self.root / ".mcp.json")])
        self.assertIn("CONNECT_NOT_REQUESTED", self.codes(report))
        self.assertEqual(before, self.snapshots())
        self.assertEqual(errors, "")

    def test_connected_exactly_one_read_request_and_no_mutations(self):
        self.write("GameLens/agent-8777.token", session.MAGIC + b"sealed-fixture")
        self.write("GameLens/operator-8777.token", b"must-not-read")
        self.claude_config()
        before = self.snapshots()
        sock = FakeSocket()
        dpapi = mock.Mock()
        dpapi.CryptUnprotectData.return_value = ("ignored", CANARY.encode())
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), \
             mock.patch.object(session, "_load_dpapi", return_value=dpapi), \
             mock.patch.object(http.socket, "socket", side_effect=sock.factory), \
             mock.patch.object(http.time, "monotonic", side_effect=sock.clock), \
             mock.patch.object(http.select, "select", side_effect=sock.wait), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("runtime startup")):
            _, report, errors = self.run_checker(["--connect", "--url", "http://127.0.0.1:8777"])
        request = bytes(sock.sent)
        self.assertEqual(request.count(b"GET /state HTTP/1.1"), 1)
        for forbidden in (b"POST ", b"/frame.jpg", b"/act", b"/arm", b"/live", b"/stop", b"/record"):
            self.assertNotIn(forbidden, request)
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(before, self.snapshots())
        self.assertNotIn(CANARY, json.dumps(report) + errors)
        self.assertFalse(report["input_authorization"]["granted_by_checker"])

    def test_secret_error_response_is_not_in_either_output_stream(self):
        sock = FakeSocket([response(CANARY.encode(), status=500, extra_headers=("X-Private: " + CANARY + "\r\n").encode())])
        with mock.patch.object(session, "read_selected_agent", return_value=CANARY), \
             mock.patch.object(http.socket, "socket", side_effect=sock.factory), \
             mock.patch.object(http.time, "monotonic", side_effect=sock.clock), \
             mock.patch.object(http.select, "select", side_effect=sock.wait):
            code, report, errors = self.run_checker(["--connect", "--url", "http://127.0.0.1:8777"])
        self.assertEqual(code, 2)
        self.assertIn("HTTP_STATUS_REJECTED", self.codes(report))
        self.assertNotIn(CANARY, json.dumps(report) + errors)
        self.assertEqual(errors, "")

    def test_invalid_selected_clients_never_read_a_token_or_connect(self):
        clients = self.write("clients.json", '{"clients":[{"name":"a","url":"http://127.0.0.1:8777"}]}')
        with mock.patch.object(session, "read_selected_agent") as reader, mock.patch.object(http, "probe_state") as probe:
            code, report, _ = self.run_checker(["--connect", "--client", "missing", "--clients-file", str(clients)])
        self.assertEqual(code, 2)
        reader.assert_not_called()
        probe.assert_not_called()

    def test_descriptor_closed_on_read_failure(self):
        file = self.write("read-error.txt", b"fixture")
        original_close = os.close
        with mock.patch.object(session.os, "read", side_effect=OSError(CANARY)), \
             mock.patch.object(session.os, "close", wraps=original_close) as closed:
            with self.assertRaises(session.FileBoundaryError) as error:
                session.read_local_bytes(file)
        self.assertEqual(error.exception.args, ("FILE_UNREADABLE",))
        closed.assert_called_once()
        self.assertNotIn(CANARY, str(error.exception))

    def test_changed_file_and_reparse_point_are_refused(self):
        file = self.write("changing.txt", b"initial")
        original_read = os.read
        replaced = [False]
        def racing_read(fd, size):
            result = original_read(fd, size)
            if not replaced[0]:
                replaced[0] = True
                file.write_bytes(b"changed-to-a-different-size")
            return result
        with mock.patch.object(session.os, "read", side_effect=racing_read):
            with self.assertRaises(session.FileBoundaryError) as error:
                session.read_local_bytes(file)
        self.assertEqual(error.exception.args, ("FILE_CHANGED_DURING_READ",))
        original_lstat = Path.lstat
        def reparse(path, *args, **kwargs):
            actual = original_lstat(path, *args, **kwargs)
            if path == file:
                return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=1024)
            return actual
        with mock.patch.object(Path, "lstat", reparse):
            with self.assertRaises(session.FileBoundaryError) as error:
                session.read_local_bytes(file)
        self.assertEqual(error.exception.args, ("FILE_LOCATION_UNSUPPORTED",))

    def test_metadata_inventory_is_bounded(self):
        entries = (SimpleNamespace(name="ordinary") for _ in range(checker.MAX_METADATA_ENTRIES + 1))
        with mock.patch.object(checker.os, "scandir", return_value=contextlib.nullcontext(entries)):
            _, report, errors = self.run_checker()
        self.assertIn("PROJECT_METADATA_UNREADABLE", self.codes(report))
        self.assertEqual(errors, "")

    def test_checker_imports_never_reference_gamelens_or_mutation_clients(self):
        root = Path(checker.__file__).parent
        for name in ("check_setup.py", "setup_check_http.py", "setup_check_session.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    modules.append(node.module or "")
            self.assertFalse(any(module == "gamelens" or module.startswith("gamelens.") for module in modules))
            self.assertNotIn("subprocess", modules)
            self.assertNotIn("tempfile", modules)
