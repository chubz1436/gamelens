"""Authored, NOT RUN. Static configuration inspection only; no host launch."""
import json
from unittest import mock

from helpers import CANARY, FixtureCase, checker, session


class McpConfigTests(FixtureCase):
    def inspect(self, path, host="claude-code", extra=None):
        return self.run_checker(["--mcp-host", host, "--mcp-config", str(path)] + list(extra or []))

    def test_claude_project_script_registration(self):
        path = self.claude_config()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not execute config")):
            code, report, errors = self.inspect(path)
        self.assertIn("MCP_STATIC_CONFIG_VALID", self.codes(report))
        self.assertIn("MCP_HANDSHAKE_NOT_ATTEMPTED", self.codes(report))
        self.assertIn("MCP_EFFECTIVE_CONFIG_UNVERIFIED", self.codes(report))
        self.assertEqual(errors, "")

    def test_codex_explicit_toml(self):
        path = self.write("chosen/config.toml", "[mcp_servers.gamelens]\ncommand = " + json.dumps(str(self.python_path))
                          + "\nargs = [" + json.dumps(str(self.launcher)) + "]\nenabled = true\n"
                          + "[mcp_servers.gamelens.env]\nGAMELENS_PROJECT_DIR = " + json.dumps(str(self.root)) + "\n")
        _, report, _ = self.inspect(path, "codex")
        self.assertIn("MCP_STATIC_CONFIG_VALID", self.codes(report))

    def test_module_launch_resolution_remains_unverified(self):
        path = self.claude_config({"command": str(self.python_path), "args": ["-m", "gamelens.mcp"],
                                   "env": {"GAMELENS_URL": "http://127.0.0.1:8777"}})
        _, report, _ = self.inspect(path, extra=["--url", "http://127.0.0.1:8777"])
        self.assertIn("MCP_STATIC_CONFIG_VALID", self.codes(report))
        self.assertIn("MCP_MODULE_RESOLUTION_UNVERIFIED", self.codes(report))

    def test_missing_and_disabled_registration(self):
        missing = self.write(".mcp.json", '{}')
        _, report, _ = self.inspect(missing)
        self.assertIn("MCP_REGISTRATION_MISSING", self.codes(report))
        disabled = self.write("chosen/config.toml", '[mcp_servers.gamelens]\nenabled = false\n')
        _, report, _ = self.inspect(disabled, "codex")
        self.assertIn("MCP_REGISTRATION_DISABLED", self.codes(report))

    def test_no_configuration_discovery_by_default(self):
        self.write(".claude.json", CANARY)
        self.write(".codex/config.toml", CANARY)
        read = session.read_local_bytes
        seen = []
        def tracked(path, *args, **kwargs):
            seen.append(str(path))
            return read(path, *args, **kwargs)
        with mock.patch.object(session, "read_local_bytes", side_effect=tracked):
            _, report, _ = self.run_checker()
        self.assertIn("MCP_NOT_SELECTED", self.codes(report))
        self.assertFalse(any(".claude.json" in path or ".codex" in path for path in seen))

    def test_claude_other_scope_not_opened(self):
        path = self.write(".claude.json", CANARY)
        _, report, errors = self.inspect(path)
        self.assertIn("MCP_CONFIG_SOURCE_UNSUPPORTED", self.codes(report))
        self.assertNotIn(CANARY, json.dumps(report) + errors)

    def test_shell_wrapper_and_python_code_argument_not_executed(self):
        shell = self.write("cmd.exe", b"fixture")
        for command, args in ((str(shell), ["/c", CANARY]), (str(self.python_path), ["-c", CANARY])):
            with self.subTest(command=command):
                path = self.claude_config({"command": command, "args": args})
                with mock.patch("subprocess.Popen", side_effect=AssertionError("config execution")):
                    _, report, errors = self.inspect(path)
                self.assertIn("MCP_LAUNCHER_UNSUPPORTED", self.codes(report))
                self.assertNotIn(CANARY, json.dumps(report) + errors)

    def test_unknown_secret_bearing_settings_not_echoed(self):
        path = self.claude_config({"command": str(self.python_path), "args": [str(self.launcher)],
                                   "env": {"API_KEY": CANARY}})
        _, report, errors = self.inspect(path)
        self.assertIn("MCP_CONFIG_UNSUPPORTED", self.codes(report))
        self.assertNotIn(CANARY, json.dumps(report) + errors)
        self.assertNotIn("API_KEY", json.dumps(report))

    def test_unresolved_substitution_and_stale_interpreter(self):
        path = self.claude_config({"command": "${HOME}/python.exe", "args": [str(self.launcher)]})
        _, report, _ = self.inspect(path)
        self.assertIn("MCP_CONFIG_UNRESOLVED", self.codes(report))
        path = self.claude_config({"command": str(self.root / "missing/python.exe"), "args": [str(self.launcher)]})
        _, report, _ = self.inspect(path)
        self.assertIn("MCP_INTERPRETER_MISSING_OR_UNSUPPORTED", self.codes(report))

    def test_bad_json_duplicate_keys_and_limit(self):
        for data in ('{"mcpServers":{},"mcpServers":{}}', CANARY, '[' * 40 + ']' * 40, ' ' * 65537):
            with self.subTest(length=len(data)):
                path = self.write(".mcp.json", data)
                _, report, errors = self.inspect(path)
                self.assertIn("MCP_CONFIG_UNREADABLE_OR_INVALID", self.codes(report))
                self.assertNotIn(CANARY, json.dumps(report) + errors)

    def test_origin_comparison_and_no_network(self):
        path = self.claude_config({"command": str(self.python_path), "args": ["-m", "gamelens.mcp"],
                                   "env": {"GAMELENS_URL": "http://127.0.0.1:8778"}})
        with mock.patch.object(checker.transport, "probe_state", side_effect=AssertionError("offline")):
            _, report, _ = self.inspect(path, extra=["--url", "http://127.0.0.1:8777"])
        self.assertIn("MCP_ORIGIN_MISMATCH", self.codes(report))

    def test_linked_clients_file_is_not_discovered(self):
        linked = self.write("unselected-clients.json", CANARY)
        path = self.claude_config({"command": str(self.python_path), "args": [str(self.launcher)],
                                   "env": {"GAMELENS_PROJECT_DIR": str(self.root), "GAMELENS_CLIENTS_FILE": str(linked)}})
        original = session.read_local_bytes
        seen = []
        def tracked(path, *args, **kwargs):
            seen.append(str(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(session, "read_local_bytes", side_effect=tracked):
            _, report, _ = self.inspect(path)
        self.assertIn("MCP_CLIENTS_FILE_NOT_EXPLICITLY_SELECTED", self.codes(report))
        self.assertNotIn(str(linked), seen)

    def test_explicit_named_clients_match(self):
        clients = self.write("clients.json", json.dumps({"clients": [{"name": "game", "url": "http://127.0.0.1:8778"}]}))
        path = self.claude_config({"command": str(self.python_path), "args": [str(self.launcher)],
                                   "env": {"GAMELENS_PROJECT_DIR": str(self.root), "GAMELENS_CLIENTS_FILE": str(clients)}})
        _, report, _ = self.inspect(path, extra=["--client", "game", "--clients-file", str(clients)])
        self.assertIn("MCP_STATIC_CONFIG_VALID", self.codes(report))
        self.assertEqual(report["selected_session"]["port"], 8778)

    def test_unknown_other_server_is_ignored_not_executed_or_printed(self):
        path = self.claude_config()
        data = json.loads(path.read_text())
        data["mcpServers"]["other"] = {"command": CANARY, "env": {"SECRET": CANARY}}
        path.write_text(json.dumps(data))
        _, report, errors = self.inspect(path)
        self.assertIn("MCP_STATIC_CONFIG_VALID", self.codes(report))
        self.assertNotIn(CANARY, json.dumps(report) + errors)
