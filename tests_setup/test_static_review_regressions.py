"""Authored, NOT RUN. Regressions for issues found during source read-through."""
import json
from unittest import mock

from helpers import CANARY, FakeSocket, FixtureCase, http, response, session, state_fixture


class StaticReviewRegressions(FixtureCase):
    def test_printwindow_is_a_known_reported_backend(self):
        state = state_fixture()
        state["capture"]["backend"] = "printwindow"
        self.assertEqual(http.project_state(state)["reported_capture"]["backend"], "printwindow")

    def test_overflowing_float_is_rejected_even_in_unknown_field(self):
        sock = FakeSocket([response(b'{"ignored":1e9999}')])
        result = sock.probe()
        self.assertEqual(result["reason_code"], "STATE_JSON_INVALID")
        self.assertEqual(sock.close_calls, 1)

    def test_custom_probe_error_cannot_smuggle_a_secret_reason(self):
        sock = FakeSocket([http.ProbeFailure(CANARY)])
        result = sock.probe()
        self.assertEqual(result["reason_code"], "HTTP_PROBE_FAILED")
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertEqual(sock.close_calls, 1)

    def test_custom_session_error_cannot_smuggle_a_secret_reason(self):
        with mock.patch.object(session, "read_selected_agent", side_effect=session.SessionError(CANARY)), \
             mock.patch.object(http, "probe_state") as probe:
            code, report, errors = self.run_checker(["--connect", "--url", "http://127.0.0.1:8777"])
        self.assertEqual(code, 2)
        self.assertIn("SESSION_TOKEN_UNREADABLE", self.codes(report))
        self.assertNotIn(CANARY, json.dumps(report) + errors)
        self.assertEqual(report["reported_capture"]["reason_code"], "STATE_NOT_VERIFIED")
        probe.assert_not_called()

    def test_toml_nested_tables_have_explicit_depth_limit(self):
        path = self.write("chosen/config.toml", "[" + ".".join(["deep"] * 40) + "]\nvalue = 1\n")
        _, report, errors = self.run_checker(["--mcp-host", "codex", "--mcp-config", str(path)])
        self.assertIn("MCP_CONFIG_UNREADABLE_OR_INVALID", self.codes(report))
        self.assertEqual(errors, "")

    def test_codex_documented_timeouts_are_inspected_not_executed(self):
        path = self.write("chosen/config.toml", "[mcp_servers.gamelens]\ncommand = " + json.dumps(str(self.python_path))
                          + "\nargs = [\"-m\", \"gamelens.mcp\"]\ncwd = " + json.dumps(str(self.root))
                          + "\nstartup_timeout_sec = 20\ntool_timeout_sec = 45\n")
        _, report, errors = self.run_checker(["--mcp-host", "codex", "--mcp-config", str(path)])
        self.assertIn("MCP_STATIC_CONFIG_VALID", self.codes(report))
        self.assertEqual(errors, "")
        path.write_text(path.read_text().replace("tool_timeout_sec = 45", "tool_timeout_sec = nan"))
        _, report, _ = self.run_checker(["--mcp-host", "codex", "--mcp-config", str(path)])
        self.assertIn("MCP_CONFIG_UNSUPPORTED", self.codes(report))
