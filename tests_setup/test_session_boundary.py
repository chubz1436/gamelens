"""Authored, NOT RUN. Never decrypt a real user's credentials in these tests."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from helpers import CANARY, FixtureCase, checker, http, session


class SessionBoundaryTests(FixtureCase):
    def make_token(self, port=8777, raw=None):
        return self.write("GameLens/agent-%d.token" % port, session.MAGIC + b"sealed-fixture" if raw is None else raw)

    def test_selected_port_only_and_no_shared_or_operator_fallback(self):
        self.make_token(8777)
        self.make_token(8778, b"do-not-read-other-client")
        self.write("GameLens/operator-8777.token", CANARY.encode())
        before = self.snapshots()
        dpapi = mock.Mock()
        dpapi.CryptUnprotectData.return_value = ("ignored description", b"fixture-clear-token")
        reader = session.read_local_bytes
        seen = []
        def tracked(path, *args, **kwargs):
            seen.append(Path(path).name)
            return reader(path, *args, **kwargs)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "GAMELENS_AGENT_TOKEN": CANARY,
                                           "GAMELENS_OPERATOR_TOKEN": CANARY}), \
             mock.patch.object(session, "_load_dpapi", return_value=dpapi), \
             mock.patch.object(session, "read_local_bytes", side_effect=tracked):
            self.assertEqual(session.read_selected_agent(8777), "fixture-clear-token")
        self.assertEqual(seen, ["agent-8777.token"])
        dpapi.CryptUnprotectData.assert_called_once_with(b"sealed-fixture", session.ENTROPY, None, None, 1)
        self.assertEqual(before, self.snapshots())

    def test_missing_selected_token_never_substitutes_other_client(self):
        self.make_token(8778)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), \
             mock.patch.object(session, "_load_dpapi") as loader:
            with self.assertRaises(session.SessionError) as error:
                session.read_selected_agent(8777)
        self.assertEqual(error.exception.args, ("SESSION_TOKEN_MISSING",))
        loader.assert_not_called()

    def test_missing_location_does_not_probe_temp_or_discover_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("tempfile.gettempdir", side_effect=AssertionError("no temp probe")), \
             mock.patch.object(session, "read_local_bytes") as read:
            with self.assertRaises(session.SessionError) as error:
                session.read_selected_agent(8777)
        self.assertEqual(error.exception.args, ("SESSION_LOCATION_UNVERIFIED",))
        read.assert_not_called()

    def test_plaintext_operator_magic_empty_and_oversize_handoff_rejected(self):
        cases = [(CANARY.encode(), "SESSION_FORMAT_UNSUPPORTED"),
                 (b"GameLens-operator-DPAPI-v1\0sealed", "SESSION_FORMAT_UNSUPPORTED"),
                 (session.MAGIC, "SESSION_FORMAT_UNSUPPORTED"),
                 (b"x" * 65537, "SESSION_TOKEN_TOO_LARGE")]
        for raw, expected in cases:
            self.make_token(raw=raw)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), \
                 mock.patch.object(session, "_load_dpapi") as loader:
                with self.assertRaises(session.SessionError) as error:
                    session.read_selected_agent(8777)
            self.assertEqual(error.exception.args, (expected,))
            self.assertNotIn(CANARY, str(error.exception))
            loader.assert_not_called()

    def test_missing_dpapi_and_failed_decryption_are_sanitized(self):
        self.make_token()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            with mock.patch.object(session, "_load_dpapi", side_effect=ImportError(CANARY)):
                with self.assertRaises(session.SessionError) as error:
                    session.read_selected_agent(8777)
                self.assertEqual(error.exception.args, ("SESSION_DECRYPTION_UNAVAILABLE",))
            dpapi = mock.Mock()
            dpapi.CryptUnprotectData.side_effect = RuntimeError(CANARY)
            with mock.patch.object(session, "_load_dpapi", return_value=dpapi):
                with self.assertRaises(session.SessionError) as error:
                    session.read_selected_agent(8777)
                self.assertEqual(error.exception.args, ("SESSION_DECRYPTION_FAILED",))
                self.assertNotIn(CANARY, str(error.exception))

    def test_import_and_decrypt_stdout_stderr_are_discarded(self):
        self.make_token()
        output, errors = io.StringIO(), io.StringIO()
        def noisy_decrypt(*args):
            print(CANARY)
            print(CANARY, file=__import__("sys").stderr)
            return "ignored", b"fixture"
        dpapi = mock.Mock()
        dpapi.CryptUnprotectData.side_effect = noisy_decrypt
        def noisy_import(name):
            print(CANARY)
            return dpapi
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), \
             mock.patch.object(session.importlib, "import_module", side_effect=noisy_import), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(session.read_selected_agent(8777), "fixture")
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_invalid_clear_token_cannot_inject_http_headers(self):
        self.make_token()
        for clear in (b"", b"x\r\nX-Injected: yes", b"x" * 4097, b"\xff"):
            dpapi = mock.Mock()
            dpapi.CryptUnprotectData.return_value = ("ignored", clear)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), \
                 mock.patch.object(session, "_load_dpapi", return_value=dpapi):
                with self.assertRaises(session.SessionError):
                    session.read_selected_agent(8777)

    def test_regular_file_limit_paths_and_symlinks(self):
        normal = self.write("bounded.txt", b"a" * 65536)
        self.assertEqual(len(session.read_local_bytes(normal)), 65536)
        self.write("bounded.txt", b"a" * 65537)
        with self.assertRaises(session.FileBoundaryError) as error:
            session.read_local_bytes(normal)
        self.assertEqual(error.exception.args, ("FILE_TOO_LARGE",))
        for path in ("relative.txt", "//server/share/file", "\\\\server\\share\\file", self.root / ".." / "escape"):
            with self.assertRaises(session.FileBoundaryError):
                session.read_local_bytes(path)
        target = self.write("target.txt", b"fixture")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation unavailable; no elevation requested")
        with self.assertRaises(session.FileBoundaryError):
            session.read_local_bytes(link)

    def test_client_exact_shape_and_uniqueness(self):
        valid = {"clients": [{"name": "a", "url": "http://127.0.0.1:8777"},
                             {"name": "b", "url": "http://[::1]:8778"}]}
        path = self.write("clients.json", json.dumps(valid))
        self.assertEqual([item["port"] for item in checker.load_clients(path)], [8777, 8778])
        invalid = ["{\"clients\":[],\"clients\":[]}", json.dumps({"clients": [], "secret": CANARY}),
                   json.dumps({"clients": [{"name": "a", "url": "http://127.0.0.1:8777", "token": CANARY}]}),
                   json.dumps({"clients": [{"name": "a", "url": "http://127.0.0.1:8777"}, {"name": "a", "url": "http://127.0.0.1:8778"}]}),
                   json.dumps({"clients": [{"name": "a", "url": "http://127.0.0.1:8777"}, {"name": "b", "url": "http://[::1]:8777"}]}),
                   json.dumps({"clients": [{"name": "BAD", "url": "http://127.0.0.1:8777"}]}),
                   json.dumps({"clients": [{"name": "x%d" % n, "url": "http://127.0.0.1:%d" % (8800 + n)} for n in range(17)]})]
        for data in invalid:
            path.write_text(data)
            with self.assertRaises(ValueError):
                checker.load_clients(path)

    def test_source_constant_conformance_without_package_import(self):
        root = Path(checker.__file__).resolve().parents[1]
        source = ast.parse((root / "gamelens/session.py").read_text(encoding="utf-8"))
        constants = {node.targets[0].id: ast.literal_eval(node.value) for node in source.body
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id in {"MAGIC", "ENTROPY"}}
        self.assertEqual(constants, {"MAGIC": session.MAGIC, "ENTROPY": session.ENTROPY})
        source = ast.parse((root / "gamelens/multi_client.py").read_text(encoding="utf-8"))
        constants = {node.targets[0].id: ast.literal_eval(node.value) for node in source.body
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id in {"MAX_CLIENTS", "MAX_CONFIG_BYTES"}}
        self.assertEqual(constants, {"MAX_CLIENTS": checker.MAX_CLIENTS, "MAX_CONFIG_BYTES": checker.MAX_CONFIG_BYTES})

    def test_origin_vectors_against_isolated_runtime_function(self):
        root = Path(checker.__file__).resolve().parents[1]
        source = ast.parse((root / "gamelens/transport.py").read_text(encoding="utf-8"))
        node = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "validate_url")
        # Compile only this reviewed pure function, not package/module startup.
        namespace = {"urlsplit": urlsplit}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "isolated_validate_url", "exec"), namespace)
        values = ["http://127.0.0.1:8777", "http://127.0.0.1:8777/", "http://[::1]:8778",
                  "http://localhost:8777", "http://127.0.0.2:8777", "http://127.0.0.1:08777",
                  "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:8777/path",
                  "http://user@127.0.0.1:8777", "https://127.0.0.1:8777", "http://127.0.0.1:8777\n"]
        for value in values:
            with self.subTest(value=value):
                try:
                    expected = namespace["validate_url"](value)
                except ValueError:
                    with self.assertRaises(ValueError):
                        http.validate_origin(value)
                else:
                    self.assertEqual(http.validate_origin(value)[0], expected)
