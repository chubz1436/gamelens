"""Authored, NOT RUN. Uses standalone unittest, not tests/conftest.py."""
import ast
import builtins
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

from helpers import CANARY, FixtureCase, checker, session


class BootstrapTests(FixtureCase):
    def test_missing_venv_is_reported_without_native_imports(self):
        shutil.rmtree(self.root / ".venv")
        with mock.patch.object(session, "_load_dpapi", side_effect=AssertionError("native import")):
            code, report, errors = self.run_checker()
        self.assertEqual(code, 2)
        self.assertIn("PROJECT_ENVIRONMENT_MISSING_OR_UNREADABLE", self.codes(report))
        self.assertEqual(errors, "")

    def test_offline_import_barrier_with_native_dependencies_missing(self):
        original_import = builtins.__import__
        calls = []
        def guard(name, *args, **kwargs):
            calls.append(name)
            if name.split(".")[0] in {"gamelens", "win32crypt", "win32api", "windows_capture", "mss", "numpy", "cv2"}:
                raise AssertionError("forbidden native/runtime import")
            return original_import(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=guard):
            code, report, errors = self.run_checker()
        self.assertIn(code, (0, 1))  # non-Windows inspection is explicitly unverified
        self.assertIn("NATIVE_IMPORTS_NOT_ATTEMPTED", self.codes(report))
        self.assertEqual(errors, "")
        self.assertNotIn("gamelens", calls)

    def test_missing_pywin32_and_capture_metadata_are_distinct_checks(self):
        for stem in ("pywin32-306", "windows_capture-1.4.2"):
            shutil.rmtree(self.root / (".venv/Lib/site-packages/" + stem + ".dist-info"))
        code, report, errors = self.run_checker()
        missing = {item["safe_evidence"].get("distribution") for item in report["checks"]
                   if item["reason_code"] == "DEPENDENCY_MISSING"}
        self.assertEqual(missing, {"pywin32", "windows-capture"})
        self.assertEqual(code, 2)
        self.assertEqual(errors, "")

    def test_metadata_mismatch_never_executes_package(self):
        self.write(".venv/Lib/site-packages/numpy-1.26.4.dist-info/METADATA", "Name: numpy\nVersion: 9.0\n")
        self.write(".venv/Lib/site-packages/numpy/__init__.py", "raise RuntimeError('" + CANARY + "')\n")
        code, report, errors = self.run_checker()
        self.assertEqual(code, 2)
        self.assertIn("DEPENDENCY_VERSION_MISMATCH", self.codes(report))
        self.assertNotIn(CANARY, json.dumps(report) + errors)

    def test_desktop_includes_core_and_pywebview_without_importing_it(self):
        code, report, errors = self.run_checker(["--scope", "desktop"])
        matches = [item for item in report["checks"] if item["reason_code"] == "DEPENDENCY_METADATA_MATCH"]
        self.assertEqual(len(matches), 9)
        self.assertIn("pywebview", {item["safe_evidence"]["distribution"] for item in matches})
        self.assertEqual(errors, "")

    def test_system_interpreter_does_not_prove_project_environment(self):
        with mock.patch.object(sys, "executable", str(self.root / "system/python.exe")):
            _, report, _ = self.run_checker()
        row = next(item for item in report["checks"] if item["id"] == "python")
        self.assertEqual(row["safe_evidence"]["executing_interpreter"], "external")
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_bootstrap_parses_as_old_python3(self):
        source = Path(checker.__file__).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 5))

    def test_unsupported_gate_precedes_all_modern_imports(self):
        source_path = Path(checker.__file__)
        code = compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")
        original_import = builtins.__import__
        for version in ((2, 7, 18), (3, 5, 10), (3, 10, 14)):
            with self.subTest(version=version):
                output = io.StringIO()
                imports = []
                def only_sys(name, *args, **kwargs):
                    imports.append(name)
                    if name != "sys":
                        raise AssertionError("modern import before version guard")
                    return original_import(name, *args, **kwargs)
                with mock.patch.object(sys, "version_info", version), \
                     mock.patch.object(sys, "argv", ["check_setup.py", "--format", "json", CANARY]), \
                     mock.patch("builtins.__import__", side_effect=only_sys), redirect_stdout(output):
                    with self.assertRaises(SystemExit) as stopped:
                        exec(code, {"__name__": "__main__", "__file__": str(source_path)})
                self.assertEqual(stopped.exception.code, 2)
                self.assertEqual(imports, ["sys"])
                self.assertEqual(json.loads(output.getvalue())["checks"][0]["reason_code"], "PYTHON_UNSUPPORTED")
                self.assertNotIn(CANARY, output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())

    def test_old_real_interpreter_when_explicitly_available(self):
        old_python = os.environ.get("GAMELENS_TEST_OLD_PYTHON")
        if not old_python:
            self.skipTest("No owner-selected older interpreter; simulated gate is tested separately")
        result = subprocess.run([old_python, "-E", "-B", str(Path(checker.__file__).resolve()), "--format", "json"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["checks"][0]["reason_code"], "PYTHON_UNSUPPORTED")
        self.assertEqual(result.stderr, "")

    def test_windows_wrapper_missing_explicit_interpreter_no_fallback(self):
        if os.name != "nt":
            self.skipTest("Requires Windows PowerShell; do not install it for this test")
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell unavailable")
        wrapper = Path(checker.__file__).with_suffix(".ps1")
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(wrapper),
                                 "-PythonPath", str(self.root / "missing/python.exe"), "-Format", "json"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["checks"][0]["reason_code"], "PYTHON_NOT_FOUND")
        self.assertEqual(result.stderr, "")

    def test_wrapper_does_not_include_setup_or_policy_mutations(self):
        source = Path(checker.__file__).with_suffix(".ps1").read_text(encoding="utf-8")
        for forbidden in ("Set-ExecutionPolicy", "-Verb RunAs", "pip install", "winget install", "Invoke-Expression"):
            self.assertNotIn(forbidden, source)
        self.assertIn("$start.UseShellExecute = $false", source)
