"""Authored, NOT RUN. Report assertions do not authorize live game input."""
import copy
import io
import json
from unittest import mock

from helpers import CANARY, FixtureCase, checker, http, session, state_fixture


class ReportContractTests(FixtureCase):
    def connected(self, state=None, extra=None):
        projection = http.project_state(state_fixture() if state is None else state)
        with mock.patch.object(session, "read_selected_agent", return_value="fixture-token") as token, \
             mock.patch.object(http, "probe_state", return_value={"ok": True, "reason_code": "STATE_REPORTED",
                              "http_status": 200, "reported_state": projection}) as probe:
            result = self.run_checker(["--connect", "--url", "http://127.0.0.1:8777"] + list(extra or []))
        token.assert_called_once_with(8777)
        probe.assert_called_once_with("http://127.0.0.1:8777", "fixture-token")
        return result

    def test_healthy_report_does_not_verify_game_frame_or_authorize_input(self):
        code, report, errors = self.connected()
        self.assertTrue(report["reported_capture"]["healthy"])
        self.assertEqual(report["actual_frame_verification"], {"status": "unverified", "reason_code": "FRAME_NOT_REQUESTED"})
        self.assertEqual(report["input_authorization"], {"granted_by_checker": False})
        self.assertEqual(report["target_match"]["status"], "unverified")
        self.assertIn(code, (0, 1))
        self.assertEqual(errors, "")
        self.assertNotIn(CANARY, json.dumps(report))

    def test_disarmed_and_dry_run_do_not_become_setup_failures(self):
        for armed, dry_run, expected in ((False, True, "INPUT_REPORTED_DISARMED"),
                                         (True, True, "INPUT_REPORTED_DRY_RUN")):
            state = state_fixture()
            state["safety"].update(armed=armed, dry_run=dry_run)
            code, report, _ = self.connected(state)
            self.assertIn(expected, self.codes(report))
            row = next(row for row in report["checks"] if row["reason_code"] == expected)
            self.assertFalse(row["affects_exit"])
            self.assertNotEqual(code, 2)
            self.assertFalse(report["input_authorization"]["granted_by_checker"])

    def test_reported_capture_failure_and_hwnd_mismatch_are_distinct(self):
        state = state_fixture()
        state["capture"]["healthy"] = False
        code, report, _ = self.connected(state, ["--expected-target-hwnd", "43"])
        self.assertEqual(code, 2)
        self.assertIn("CAPTURE_REPORTED_UNHEALTHY", self.codes(report))
        self.assertIn("TARGET_REPORTED_MISMATCH", self.codes(report))
        _, matching, _ = self.connected(extra=["--expected-target-hwnd", "42"])
        self.assertEqual(matching["target_match"]["status"], "pass")
        self.assertFalse(matching["input_authorization"]["granted_by_checker"])

    def test_missing_target_is_not_replaced_with_a_default(self):
        state = state_fixture()
        state["target"] = None
        code, report, _ = self.connected(state, ["--expected-target-hwnd", "42"])
        self.assertEqual(code, 2)
        self.assertIsNone(report["reported_target"])
        self.assertEqual(report["target_match"]["reason_code"], "TARGET_REPORTED_MISMATCH")

    def test_missing_essential_fields_and_wrong_types_fail_schema(self):
        paths = (("target",), ("capture", "healthy"), ("capture", "age_ms"),
                 ("capture", "frame_id"), ("safety", "armed"), ("safety", "killed"))
        for path in paths:
            state = state_fixture()
            value = state
            for key in path[:-1]:
                value = value[key]
            del value[path[-1]]
            with self.assertRaises(ValueError):
                http.project_state(state)
        for invalid in (True, -1, float("nan"), float("inf"), CANARY):
            state = state_fixture()
            state["capture"]["age_ms"] = invalid
            with self.assertRaises(ValueError):
                http.project_state(state)

    def test_unknown_backend_and_executor_details_do_not_echo_strings(self):
        state = state_fixture()
        state["capture"]["backend"] = CANARY
        state["safety"]["executor"]["unreleased"] = [CANARY]
        projection = http.project_state(state)
        self.assertEqual(projection["reported_capture"]["backend"], "unknown")
        self.assertEqual(projection["reported_input_state"]["unreleased_count"], 1)
        self.assertNotIn(CANARY, json.dumps(projection))

    def test_exit_code_precedence_and_non_affecting_limitations(self):
        report = checker.new_report("offline")
        checker.add(report, "frame", "capture", "unverified", "FRAME_NOT_REQUESTED", affects_exit=False)
        self.assertEqual(checker.exit_code(report), 0)
        checker.add(report, "config", "mcp", "unverified", "MCP_CONFIG_UNSUPPORTED")
        self.assertEqual(checker.exit_code(report), 1)
        checker.add(report, "env", "environment", "blocked", "DEPENDENCY_MISSING")
        self.assertEqual(checker.exit_code(report), 2)
        report["checks"][-1]["status"] = "warning"
        self.assertEqual(checker.exit_code(report), 1)

    def test_cli_rejects_ambiguous_selectors_before_token_or_http(self):
        invalid = [["--connect"], ["--client", "a"], ["--mcp-host", "codex"],
                   ["--expected-target-hwnd", "42"], ["--connect", "--url", "http://127.0.0.1:8777", "--expected-target-hwnd", "0"],
                   ["--url", "http://127.0.0.1:8777", "--client", "a", "--clients-file", str(self.root / "clients.json")],
                   ["--scope", CANARY], ["--bad-option", CANARY], ["--mcp-config", CANARY]]
        for args in invalid:
            with mock.patch.object(session, "read_selected_agent") as token, mock.patch.object(http, "probe_state") as probe:
                code, report, errors = self.run_checker(args)
            self.assertEqual(code, 64)
            token.assert_not_called()
            probe.assert_not_called()
            self.assertNotIn(CANARY, json.dumps(report) + errors)

    def test_unexpected_failure_and_cancellation_no_tracebacks(self):
        for failure, expected in ((RuntimeError(CANARY), 70), (KeyboardInterrupt(), 130)):
            with mock.patch.object(checker, "build_report", side_effect=failure):
                code, report, errors = self.run_checker()
            self.assertEqual(code, expected)
            self.assertNotIn(CANARY, json.dumps(report) + errors)
            self.assertEqual(errors, "")

    def test_json_is_one_document_and_text_is_sanitized(self):
        with mock.patch.object(session, "read_selected_agent", side_effect=session.SessionError("SESSION_DECRYPTION_FAILED")):
            output = io.StringIO()
            code = checker.main(["--connect", "--url", "http://127.0.0.1:8777", "--format", "text"],
                                out=output, root=self.root)
        self.assertEqual(code, 2)
        self.assertNotIn(CANARY, output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertIn("Actual frame: unverified", output.getvalue())
        code, report, _ = self.run_checker()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["exit_code"], code)
        for row in report["checks"]:
            self.assertEqual(set(row), {"id", "scope", "status", "affects_exit", "reason_code", "safe_evidence", "manual_next_step"})
