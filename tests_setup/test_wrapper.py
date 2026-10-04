"""Wrapper regressions AUTHORED, NOT RUN.

Windows-only execution uses disposable fixtures and existing Python/PowerShell.
No GameLens runtime, real credentials, network shares, mapped drives, installation,
elevation, Pester or policy bypass. Tests create disposable no-pip venvs to exercise
REAL interpreter paths with spaces and launcher cleanup; nothing is installed.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv

HERE = Path(__file__).resolve().parent
WRAPPER = HERE.parent / "tools" / "check_setup.ps1"
PROBE = HERE / "wrapper_probe.ps1"
CANARY = "SECRET_CANARY_WRAPPER_29c1"

CHILD_SOURCE = r'''
import json
import os
from pathlib import Path
import sys
import threading
import time

root = Path(__file__).resolve().parents[1]
options = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
(root / "child.pid").write_text(str(os.getpid()), encoding="ascii")
# Give the owning test time to acquire a process HANDLE (not just a reusable PID).
time.sleep(0.15)
mode = options["mode"]
canary = "SECRET_CANARY_WRAPPER_29c1"
code = options.get("code", 0)
report = {
    "schema_version": 1, "mode": "offline", "exit_code": code,
    "checks": [{"id": "fixture", "scope": "environment", "status": "pass",
                "affects_exit": True, "reason_code": "FIXTURE_COMPLETE",
                "safe_evidence": {}, "manual_next_step": "Synthetic fixture only."}],
    "actual_frame_verification": {"status": "unverified", "reason_code": "FRAME_NOT_REQUESTED"},
    "input_authorization": {"granted_by_checker": False},
}

def payload(size):
    unit = canary.encode("ascii")
    return (unit * (size // len(unit) + 1))[:size]

def write_all(fd, data):
    while data:
        count = os.write(fd, data[:4096])
        data = data[count:]

if mode == "old-gate":
    # This tests wrapper forwarding of the ACTUAL bootstrap guard on simulated
    # old version metadata. It is not a real older-interpreter validation.
    sys.version_info = (3, 10, 14)
    source = (root / "bootstrap_source.py").read_text(encoding="utf-8")
    exec(compile(source, "bootstrap_source.py", "exec"),
         {"__name__": "__main__", "__file__": str(root / "bootstrap_source.py")})
elif mode == "argv":
    if sys.argv[1:] != options["expected_argv"]:
        # Never print the supplied arguments, which include a synthetic secret.
        report["exit_code"] = code = 64
        report["checks"][0]["reason_code"] = "FIXTURE_ARGUMENT_MISMATCH"
    print(json.dumps(report))
    raise SystemExit(code)
elif mode in ("stdout", "stderr", "combined", "utf8-stdout"):
    jobs = []
    if mode == "combined":
        # Each stream is BELOW its individual cap; aggregate exceeds 65,536 bytes.
        jobs = [threading.Thread(target=write_all, args=(1, payload(60000))),
                threading.Thread(target=write_all, args=(2, payload(8000)))]
    elif mode == "utf8-stdout":
        jobs = [threading.Thread(target=write_all, args=(1, b"\xf0\x9f\x98\x80" * 20000))]
    else:
        jobs = [threading.Thread(target=write_all, args=(1 if mode == "stdout" else 2, payload(262144)))]
    for job in jobs:
        job.start()
    for job in jobs:
        job.join()
    time.sleep(120)
elif mode == "hung":
    write_all(1, b'{"partial":"' + canary.encode("ascii"))
    time.sleep(120)
elif mode == "closed-pipes-hung":
    os.close(1)
    os.close(2)
    time.sleep(120)
elif mode == "stderr-small":
    write_all(2, canary.encode("ascii"))
    print(json.dumps(report))
elif mode == "malformed":
    write_all(1, canary.encode("ascii") + b'{"incomplete":')
elif mode == "invalid-utf8":
    write_all(1, b"\xff" + canary.encode("ascii"))
elif mode == "exception":
    raise RuntimeError(canary)
else:
    print(json.dumps(report))
    raise SystemExit(code)
'''


@unittest.skipUnless(os.name == "nt", "Wrapper runtime tests require Windows; do not install or elevate")
class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not self.powershell:
            self.skipTest("No existing PowerShell; no installation or execution-policy bypass")
        self.temp = tempfile.TemporaryDirectory(prefix="gamelens wrapper with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.wrapper = self.tools / "check_setup.ps1"
        shutil.copyfile(WRAPPER, self.wrapper)
        (self.tools / "check_setup.py").write_text(CHILD_SOURCE, encoding="utf-8")
        (self.root / "bootstrap_source.py").write_bytes((WRAPPER.parent / "check_setup.py").read_bytes())
        self.local_app = self.root / "Local App Data"
        (self.local_app / "GameLens").mkdir(parents=True)
        for name in ("agent-8777.token", "agent-8778.token", "operator-8777.token"):
            (self.local_app / "GameLens" / name).write_bytes(CANARY.encode("ascii"))
        (self.root / ".mcp.json").write_text(CANARY, encoding="utf-8")
        (self.root / "config with spaces.toml").write_text(CANARY, encoding="utf-8")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def scenario(self, mode="json", code=0, **kwargs):
        (self.root / "scenario.json").write_text(json.dumps(dict(mode=mode, code=code, **kwargs)), encoding="utf-8")
        (self.root / "child.pid").unlink(missing_ok=True)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and path.name != "child.pid"}

    def execute(self, args, *, expect_child=True, path=None, timeout=12):
        before = self.snapshot()
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(self.local_app)
        if path is not None:
            environment["PATH"] = path
        command = [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File"] + list(args)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        handle = None
        saw_pid = False
        already_exited = False
        try:
            until = time.monotonic() + 5
            while expect_child and time.monotonic() < until:
                pid_file = self.root / "child.pid"
                if pid_file.exists():
                    try:
                        pid = int(pid_file.read_text(encoding="ascii"))
                    except ValueError:
                        time.sleep(0.01)
                        continue
                    saw_pid = True
                    handle = self.kernel.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | TERMINATE
                    if not handle:
                        self.assertEqual(ctypes.get_last_error(), 87, "Cannot verify fixture process exit")
                        already_exited = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.fail("Wrapper fixture exceeded its outer test deadline")
            self.assertNotIn(CANARY.encode("ascii"), stdout + stderr, "Secret canary escaped wrapper boundary")
            self.assertEqual(stderr, b"", "Wrapper emitted raw stderr")
            self.assertNotIn(b"Traceback", stdout)
            try:
                result = json.loads(stdout.decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                self.fail("Wrapper did not emit exactly one valid JSON document")
            self.assertEqual(before, self.snapshot(), "Configuration, credential or fixture content was mutated")
            if expect_child:
                self.assertTrue(saw_pid, "Synthetic checker did not start; this is not cleanup evidence")
                if handle:
                    self.assertEqual(self.kernel.WaitForSingleObject(handle, 2000), 0,
                                     "Checker remains alive after wrapper returned")
                else:
                    self.assertTrue(already_exited)
            else:
                # Actual-checker smoke cases do not contain the PID-writing shim;
                # this assertion is not process-cleanup evidence for those cases.
                self.assertFalse((self.root / "child.pid").exists(), "Unexpected synthetic checker launch")
            return process.returncode, result
        finally:
            # Clean up only processes owned by this disposable fixture. Holding
            # the child handle avoids terminating an unrelated recycled PID.
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            if handle:
                try:
                    if self.kernel.WaitForSingleObject(handle, 0) != 0:
                        self.kernel.TerminateProcess(handle, 70)
                        self.kernel.WaitForSingleObject(handle, 5000)
                finally:
                    self.kernel.CloseHandle(handle)

    def wrapper_args(self, python=None, extra=()):
        args = [str(self.wrapper), "-Format", "json"]
        if python is not None:
            args.extend(("-PythonPath", str(python)))
        return args + list(extra)

    def path_probe(self, case):
        code, report = self.execute([str(PROBE), "-WrapperPath", str(self.wrapper), "-Case", case], expect_child=False)
        self.assertEqual(code, 0)
        self.assertNotIn("fixture_error", report)
        self.assertEqual(report["launches"], 0)
        return report

    def test_successful_json_and_exact_exit_code_forwarding(self):
        for code in (0, 1, 2, 64, 70, 130):
            with self.subTest(code=code):
                self.scenario(code=code)
                actual, report = self.execute(self.wrapper_args(sys.executable))
                self.assertEqual(actual, code)
                self.assertEqual(report["exit_code"], code)
                self.assertEqual(report["checks"][0]["reason_code"], "FIXTURE_COMPLETE")
                self.assertFalse(report["input_authorization"]["granted_by_checker"])

    def test_actual_checker_offline_json_in_disposable_project(self):
        for name in ("check_setup.py", "setup_check_http.py", "setup_check_session.py"):
            shutil.copyfile(WRAPPER.parent / name, self.tools / name)
        shutil.copyfile(HERE.parent / "requirements.txt", self.root / "requirements.txt")
        self.scenario()
        actual, report = self.execute(self.wrapper_args(sys.executable), expect_child=False)
        self.assertEqual(actual, 2)  # Deliberately missing project .venv, no repair.
        self.assertEqual(report["mode"], "offline")
        reasons = {row["reason_code"] for row in report["checks"]}
        self.assertIn("PROJECT_ENVIRONMENT_MISSING_OR_UNREADABLE", reasons)
        self.assertIn("CONNECT_NOT_REQUESTED", reasons)
        self.assertFalse(report["input_authorization"]["granted_by_checker"])

    def test_real_interpreter_and_project_with_spaces_and_argv_quoting(self):
        # Disposable stdlib venv, copies rather than symlinks; no pip/install/download.
        environment = self.root / "Interpreter With Spaces"
        venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
        python = environment / "Scripts" / "python.exe"
        self.assertTrue(python.is_file())
        quote_value = 'C:\\config path\\quoted "value"\\' + CANARY
        trailing = 'C:\\clients path\\trailing\\'
        self.scenario("argv", expected_argv=["--format", "json", "--scope", "desktop",
                                             "--mcp-host", "codex", "--mcp-config", quote_value,
                                             "--clients-file", trailing])
        actual, report = self.execute(self.wrapper_args(python, ["-Scope", "desktop", "-McpHost", "codex",
                                                               "-McpConfig", quote_value, "-ClientsFile", trailing]))
        self.assertEqual(actual, 0)
        self.assertEqual(report["checks"][0]["reason_code"], "FIXTURE_COMPLETE")

    def test_missing_project_python_uses_valid_system_fallback(self):
        self.scenario()
        self.assertFalse((self.root / ".venv").exists())
        actual, _ = self.execute(self.wrapper_args(), path=str(Path(sys.executable).parent))
        self.assertEqual(actual, 0)

    def test_missing_or_empty_explicit_python_never_uses_valid_fallback(self):
        for value in (str(self.root / "missing" / "python.exe"), ""):
            self.scenario()
            actual, report = self.execute(self.wrapper_args(value), expect_child=False,
                                          path=str(Path(sys.executable).parent))
            self.assertEqual(actual, 2)
            self.assertEqual(report["checks"][0]["reason_code"], "PYTHON_NOT_FOUND")

    def test_explicit_unsupported_version_guard_is_forwarded_without_traceback(self):
        self.scenario("old-gate")
        actual, report = self.execute(self.wrapper_args(sys.executable))
        self.assertEqual(actual, 2)
        self.assertEqual(report["checks"][0]["reason_code"], "PYTHON_UNSUPPORTED")

    def test_real_older_interpreter_when_owner_explicitly_provides_one(self):
        old = os.environ.get("GAMELENS_TEST_OLD_PYTHON")
        if not old:
            self.skipTest("No owner-selected older Python; simulated gate is NOT real old-runtime validation")
        (self.tools / "check_setup.py").write_bytes((WRAPPER.parent / "check_setup.py").read_bytes())
        self.scenario()
        actual, report = self.execute(self.wrapper_args(old), expect_child=False)
        self.assertEqual(actual, 2)
        self.assertEqual(report["checks"][0]["reason_code"], "PYTHON_UNSUPPORTED")

    def test_network_unknown_and_failed_drive_checks_precede_all_path_probes(self):
        for case in ("drive-network", "drive-unknown", "drive-no-root", "drive-cdrom", "drive-error"):
            with self.subTest(case=case):
                report = self.path_probe(case)
                self.assertFalse(report["approved"])
                self.assertEqual(report["drive_roots"], ["Q:\\"])
                self.assertEqual(report["attribute_paths"], [])

    def test_literal_unc_relative_traversal_and_store_alias_are_not_probed(self):
        for case in ("literal-unc", "relative-drive", "traversal", "store-alias"):
            report = self.path_probe(case)
            self.assertFalse(report["approved"])
            self.assertEqual(report["drive_roots"], [])
            self.assertEqual(report["attribute_paths"], [])

    def test_full_chain_is_checked_root_to_leaf_and_stops_at_reparse_point(self):
        chain = ["Q:\\", "Q:\\Gate", "Q:\\Gate\\Python With Spaces", "Q:\\Gate\\Python With Spaces\\python.exe"]
        for case, count in (("root-junction", 1), ("parent-junction", 3), ("leaf-reparse", 4), ("attribute-error", 1)):
            report = self.path_probe(case)
            self.assertFalse(report["approved"])
            self.assertEqual(report["attribute_paths"], chain[:count])
        report = self.path_probe("local")
        self.assertTrue(report["approved"])
        self.assertEqual(report["attribute_paths"], chain)

    def test_resolver_gates_explicit_project_and_every_system_candidate(self):
        explicit = self.path_probe("resolve-explicit-missing")
        self.assertFalse(explicit["approved"])
        self.assertFalse(any("System Python" in item for item in explicit["attribute_paths"]))
        project = self.path_probe("resolve-project")
        self.assertEqual(project["path"], "Q:\\Project With Spaces\\.venv\\Scripts\\python.exe")
        self.assertNotIn("N:\\", project["drive_roots"])
        system = self.path_probe("resolve-system")
        self.assertEqual(system["path"], "Q:\\System Python\\python.exe")
        self.assertIn("N:\\", system["drive_roots"])
        self.assertFalse(any(item.startswith("N:\\") for item in system["attribute_paths"]))
        self.assertNotIn("Q:\\Junction\\Python", system["attribute_paths"])

    def test_oversized_stdout_stderr_combined_and_multibyte_output_are_bounded(self):
        for mode in ("stdout", "stderr", "combined", "utf8-stdout"):
            with self.subTest(mode=mode):
                self.scenario(mode)
                actual, report = self.execute([str(PROBE), "-WrapperPath", str(self.wrapper), "-Case", "collect",
                                               "-PythonPath", sys.executable, "-CheckerFile", str(self.tools / "check_setup.py"),
                                               "-TimeoutMilliseconds", "2500"])
                self.assertEqual(actual, 0)
                self.assertFalse(report["ok"])
                self.assertEqual(report["reason"], "CHECKER_OUTPUT_LIMIT")
                self.assertFalse(report["has_child_output"])

    def test_hung_checker_and_closed_pipes_without_process_exit_are_terminated(self):
        for mode in ("hung", "closed-pipes-hung"):
            self.scenario(mode)
            actual, report = self.execute([str(PROBE), "-WrapperPath", str(self.wrapper), "-Case", "collect",
                                           "-PythonPath", sys.executable, "-CheckerFile", str(self.tools / "check_setup.py"),
                                           "-TimeoutMilliseconds", "1500"])
            self.assertEqual(actual, 0)
            self.assertFalse(report["ok"])
            self.assertEqual(report["reason"], "CHECKER_PROCESS_DEADLINE")
            self.assertFalse(report["has_child_output"])

    def test_venv_timeout_checks_actual_checker_handle_not_just_launcher_exit(self):
        environment = self.root / "Interpreter With Spaces"
        venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
        self.scenario("hung")
        actual, report = self.execute([str(PROBE), "-WrapperPath", str(self.wrapper), "-Case", "collect",
                                       "-PythonPath", str(environment / "Scripts" / "python.exe"),
                                       "-CheckerFile", str(self.tools / "check_setup.py"),
                                       "-TimeoutMilliseconds", "2000"])
        self.assertEqual(actual, 0)
        self.assertEqual(report["reason"], "CHECKER_PROCESS_DEADLINE")
        self.assertFalse(report["has_child_output"])
        # execute() holds the PID-written checker's process handle, including
        # when CPython's venv redirector is a DIFFERENT owning launcher process.

    def test_entrypoint_never_leaks_partial_output_errors_or_canaries(self):
        for mode in ("stdout", "stderr", "combined", "stderr-small", "malformed", "invalid-utf8", "exception"):
            with self.subTest(mode=mode):
                self.scenario(mode)
                actual, report = self.execute(self.wrapper_args(sys.executable))
                self.assertEqual(actual, 70)
                self.assertEqual(report["exit_code"], 70)
                self.assertEqual(report["checks"][0]["safe_evidence"], {})
                self.assertIn(report["checks"][0]["reason_code"], {
                    "CHECKER_OUTPUT_LIMIT", "PYTHON_LAUNCH_FAILED", "CHECKER_BOOTSTRAP_FAILED", "CHECKER_CHILD_IO_FAILED"})


class WrapperSourceContractTests(unittest.TestCase):
    def test_no_unbounded_reads_or_pre_validation_command_discovery(self):
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertNotIn(".ReadToEndAsync(", source)
        self.assertNotIn(".ReadToEnd(", source)
        self.assertNotIn("Get-Command $", source)
        self.assertIn("$combinedCap = 65536", source)
        self.assertIn("$stderrCap = 16384", source)
        self.assertIn("$stdoutCap = 65536", source)
        self.assertIn("$TimeoutMilliseconds = 30000", source)
        self.assertIn("$Process.WaitForExit(2000)", source)
        self.assertIn("$start.UseShellExecute = $false", source)
