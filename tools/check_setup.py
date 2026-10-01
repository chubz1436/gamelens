#!/usr/bin/env python
"""Read-only GameLens setup diagnostics. Python 3.11+; no GameLens imports.

Keep this entire bootstrap parseable by Python 2.7/older Python 3 so its version
failure runs BEFORE importing pathlib, tomllib or any other modern dependency.
"""
import sys
sys.dont_write_bytecode = True

if sys.version_info[:2] < (3, 11):
    if "json" in sys.argv or "--format=json" in sys.argv:
        sys.stdout.write('{"schema_version":1,"mode":"offline","checks":[{"id":"python","scope":"environment","status":"blocked","affects_exit":true,"reason_code":"PYTHON_UNSUPPORTED","safe_evidence":{},"manual_next_step":"Select an existing Python 3.11 or newer interpreter manually."}],"actual_frame_verification":{"status":"unverified","reason_code":"FRAME_NOT_REQUESTED"},"input_authorization":{"granted_by_checker":false},"exit_code":2}\n')
    else:
        sys.stdout.write("blocked: PYTHON_UNSUPPORTED. Select an existing Python 3.11+ interpreter. No checks were run.\n")
    raise SystemExit(2)

import argparse
import json
import os
from pathlib import Path
import re
import struct
import tomllib

if __package__:
    from . import setup_check_http as transport
    from . import setup_check_session as session
else:
    import setup_check_http as transport
    import setup_check_session as session

PROJECT_ROOT = Path(__file__).absolute().parents[1]
MAX_CLIENTS = 16
MAX_CONFIG_BYTES = 65536
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
MAX_METADATA_ENTRIES = 4096
KNOWN_DEPENDENCIES = frozenset(("windows-capture", "mss", "pywin32", "numpy",
                              "opencv-python", "fastapi", "uvicorn", "anthropic",
                              "pywebview"))
NEXT_STEPS = {
    "environment": "Inspect the selected project environment manually; nothing was installed or repaired.",
    "mcp": "Inspect only the explicitly selected host configuration; no host command was executed.",
    "selection": "Supply one explicit configured client or canonical loopback origin.",
    "connection": "Inspect the selected existing session manually; no retry or setup repair was attempted.",
    "capture": "Treat this as server-reported state only; a valid game frame was not inspected.",
    "input": "Review session controls manually; this checker never authorizes or injects input.",
    "checker": "Review the checker invocation; raw arguments and errors are intentionally omitted.",
}


class ArgumentFailure(Exception):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error includes supplied values, possibly secrets.
        raise ArgumentFailure("CLI_INVALID")


def _parser():
    parser = SafeParser(description="Standalone read-only setup checker (offline by default).",
                        allow_abbrev=False)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--scope", choices=("core", "desktop"), default="core")
    parser.add_argument("--mcp-host", choices=("codex", "claude-code"))
    parser.add_argument("--mcp-config")
    parser.add_argument("--client")
    parser.add_argument("--clients-file")
    parser.add_argument("--url")
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--expected-target-hwnd", type=int)
    return parser


def _arguments(argv):
    args = _parser().parse_args(argv)
    if bool(args.mcp_host) != bool(args.mcp_config):
        raise ArgumentFailure("MCP_SELECTOR_REQUIRED")
    if bool(args.client) != bool(args.clients_file):
        raise ArgumentFailure("CLIENT_SELECTOR_REQUIRED")
    if args.client and (args.url or NAME.fullmatch(args.client) is None):
        raise ArgumentFailure("CLIENT_SELECTOR_INVALID")
    if args.connect and not (args.client or args.url):
        raise ArgumentFailure("SESSION_SELECTOR_REQUIRED")
    if args.expected_target_hwnd is not None:
        if not args.connect or not 0 < args.expected_target_hwnd <= 2 ** 64 - 1:
            raise ArgumentFailure("TARGET_SELECTOR_INVALID")
    if args.url:
        try:
            transport.validate_origin(args.url)
        except ValueError:
            raise ArgumentFailure("ORIGIN_INVALID")
    for value in (args.mcp_config, args.clients_file):
        if value:
            try:
                session.local_path(value)
            except session.FileBoundaryError:
                raise ArgumentFailure("CONFIG_PATH_INVALID")
    return args


def new_report(mode):
    return {
        "schema_version": 1, "mode": mode, "checks": [],
        "reported_capture": {"status": "unverified", "reason_code": "STATE_NOT_REQUESTED"},
        "actual_frame_verification": {"status": "unverified", "reason_code": "FRAME_NOT_REQUESTED"},
        "input_authorization": {"granted_by_checker": False},
        "target_match": {"status": "unverified", "reason_code": "EXPECTED_TARGET_NOT_SUPPLIED"},
    }


def add(report, check_id, scope, status, reason, evidence=None, affects_exit=True):
    report["checks"].append({
        "id": check_id, "scope": scope, "status": status,
        "affects_exit": bool(affects_exit), "reason_code": reason,
        "safe_evidence": evidence or {}, "manual_next_step": NEXT_STEPS[scope],
    })


def exit_code(report):
    statuses = {item["status"] for item in report["checks"] if item["affects_exit"]}
    if "blocked" in statuses:
        return 2
    if statuses.intersection(("warning", "unverified")):
        return 1
    return 0


def _same_path(left, right):
    try:
        left = session.local_path(left)
        right = session.local_path(right)
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))
    except session.FileBoundaryError:
        return False


def _normal_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def load_clients(path):
    raw = session.read_local_bytes(path)
    data = transport.strict_json(raw)
    if not isinstance(data, dict) or set(data) != {"clients"}:
        raise ValueError("CLIENTS_SCHEMA_INVALID")
    entries = data["clients"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CLIENTS:
        raise ValueError("CLIENTS_SCHEMA_INVALID")
    result, names, ports = [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "url"}:
            raise ValueError("CLIENTS_SCHEMA_INVALID")
        name = entry["name"]
        if not isinstance(name, str) or NAME.fullmatch(name) is None:
            raise ValueError("CLIENTS_SCHEMA_INVALID")
        origin, host, port = transport.validate_origin(entry["url"])
        if name in names or port in ports:
            raise ValueError("CLIENTS_SCHEMA_INVALID")
        names.add(name)
        ports.add(port)
        result.append({"name": name, "url": origin, "port": port})
    return result


def select_session(args, report):
    if args.url:
        origin, host, port = transport.validate_origin(args.url)
        selected = {"url": origin, "port": port}
    elif args.client:
        try:
            clients = load_clients(args.clients_file)
            selected = next((item for item in clients if item["name"] == args.client), None)
            if selected is None:
                raise ValueError("CLIENT_NOT_FOUND")
        except (ValueError, UnicodeError, RecursionError, session.FileBoundaryError):
            add(report, "selection", "selection", "blocked", "CLIENT_CONFIG_INVALID")
            return None
    else:
        add(report, "selection", "selection", "skipped", "SESSION_NOT_SELECTED", affects_exit=False)
        return None
    # Names/ports/origins have passed exact-shape validation; no other clients appear.
    report["selected_session"] = selected.copy()
    add(report, "selection", "selection", "pass", "SESSION_SELECTED",
        {"port": selected["port"], "named": bool(args.client)})
    return selected


def _requirements(root, scope):
    pins, extras, unsupported = {}, False, False
    files = ("requirements.txt", "requirements-desktop.txt") if scope == "desktop" else ("requirements.txt",)
    pattern = re.compile(r"([A-Za-z0-9_.-]+)(?:\[([A-Za-z0-9_,.-]+)\])?==([0-9][A-Za-z0-9_.+!-]{0,63})\Z")
    for name in files:
        raw = session.read_local_bytes(root / name).decode("utf-8-sig")
        for line in raw.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if name == "requirements-desktop.txt" and line == "-r requirements.txt":
                continue
            match = pattern.fullmatch(line)
            if match is None or _normal_name(match.group(1)) not in KNOWN_DEPENDENCIES:
                unsupported = True
                continue
            distribution = _normal_name(match.group(1))
            if distribution in pins and pins[distribution] != match.group(3):
                unsupported = True
            pins[distribution] = match.group(3)
            extras = extras or bool(match.group(2))
    return pins, extras, unsupported


def _metadata_versions(site, pins):
    session.safe_directory(site)
    candidates = {name: [] for name in pins}
    count = 0
    with os.scandir(site) as entries:
        for entry in entries:
            count += 1
            if count > MAX_METADATA_ENTRIES:
                raise ValueError("METADATA_SCAN_LIMIT")
            if not entry.name.endswith(".dist-info"):
                continue
            stem = entry.name[:-len(".dist-info")].rsplit("-", 1)[0]
            name = _normal_name(stem)
            if name in candidates:
                candidates[name].append(Path(entry.path) / "METADATA")
    versions = {}
    version_pattern = re.compile(r"[0-9][A-Za-z0-9_.+!-]{0,63}\Z")
    for name, paths in candidates.items():
        if not paths:
            versions[name] = ("missing", None)
            continue
        if len(paths) != 1:
            versions[name] = ("ambiguous", None)
            continue
        try:
            text = session.read_local_bytes(paths[0]).decode("utf-8")
            fields = {"name": [], "version": []}
            for line in text.splitlines():
                if not line:
                    break
                key, separator, value = line.partition(":")
                if separator and key.lower() in fields:
                    fields[key.lower()].append(value.strip())
            if (len(fields["name"]) != 1 or len(fields["version"]) != 1
                    or _normal_name(fields["name"][0]) != name
                    or version_pattern.fullmatch(fields["version"][0]) is None):
                raise ValueError("METADATA_INVALID")
            versions[name] = ("present", fields["version"][0])
        except (session.FileBoundaryError, UnicodeError, ValueError):
            versions[name] = ("unverified", None)
    return versions


def inspect_environment(root, scope, report):
    project_python = root / ".venv" / "Scripts" / "python.exe"
    add(report, "python", "environment", "pass", "PYTHON_SUPPORTED", {
        "version": list(sys.version_info[:3]), "architecture_bits": struct.calcsize("P") * 8,
        "executing_interpreter": "project" if _same_path(sys.executable, project_python) else "external",
        "project_environment": ".venv",
    })
    if os.name != "nt":
        add(report, "windows_runtime", "environment", "unverified", "WINDOWS_RUNTIME_UNVERIFIED")
    add(report, "native_loadability", "environment", "unverified", "NATIVE_IMPORTS_NOT_ATTEMPTED", affects_exit=False)
    try:
        pins, extras, unsupported = _requirements(root, scope)
    except (session.FileBoundaryError, UnicodeError):
        add(report, "requirements", "environment", "blocked", "REQUIREMENTS_UNREADABLE")
        return
    add(report, "requirements", "environment", "unverified" if unsupported else "pass",
        "REQUIREMENTS_UNSUPPORTED" if unsupported else "REQUIREMENTS_READ",
        {"direct_pin_count": len(pins)})
    add(report, "transitive_dependencies", "environment", "unverified",
        "EXTRAS_AND_TRANSITIVES_UNVERIFIED", {"extras_declared": extras}, affects_exit=False)
    try:
        session.safe_directory(root / ".venv")
    except session.FileBoundaryError:
        add(report, "project_environment", "environment", "blocked", "PROJECT_ENVIRONMENT_MISSING_OR_UNREADABLE")
        return
    try:
        session._checked(project_python)
        add(report, "project_interpreter", "environment", "pass", "PROJECT_INTERPRETER_FILE_PRESENT")
    except session.FileBoundaryError:
        add(report, "project_interpreter", "environment", "blocked", "PROJECT_INTERPRETER_MISSING_OR_UNREADABLE")
    # Inventory the known Windows project layout, not the executing system's
    # package search path. Never import metadata providers, entry points or .pth.
    try:
        versions = _metadata_versions(root / ".venv" / "Lib" / "site-packages", pins)
    except (session.FileBoundaryError, OSError, ValueError):
        add(report, "project_metadata", "environment", "unverified", "PROJECT_METADATA_UNREADABLE")
        return
    for name, expected in sorted(pins.items()):
        state, version = versions[name]
        if state == "missing":
            status, reason = "blocked", "DEPENDENCY_MISSING"
        elif state != "present":
            status, reason = "unverified", "DEPENDENCY_METADATA_UNVERIFIED"
        elif version != expected:
            status, reason = "blocked", "DEPENDENCY_VERSION_MISMATCH"
        else:
            status, reason = "pass", "DEPENDENCY_METADATA_MATCH"
        add(report, "dependency_" + name, "environment", status, reason,
            {"distribution": name, "expected_version": expected, "version_matches": version == expected})


def _unresolved(value):
    return isinstance(value, str) and any(marker in value for marker in ("${", "%", "~", "$"))


def inspect_mcp(args, root, selected, report):
    if not args.mcp_host:
        add(report, "mcp_config", "mcp", "skipped", "MCP_NOT_SELECTED", affects_exit=False)
        return
    add(report, "mcp_effective_registration", "mcp", "unverified", "MCP_EFFECTIVE_CONFIG_UNVERIFIED", affects_exit=False)
    add(report, "mcp_handshake", "mcp", "unverified", "MCP_HANDSHAKE_NOT_ATTEMPTED", affects_exit=False)
    try:
        path = session.local_path(args.mcp_config)
        if args.mcp_host == "claude-code" and not _same_path(path, root / ".mcp.json"):
            add(report, "mcp_config", "mcp", "unverified", "MCP_CONFIG_SOURCE_UNSUPPORTED")
            return
        if args.mcp_host == "codex" and path.name != "config.toml":
            add(report, "mcp_config", "mcp", "unverified", "MCP_CONFIG_SOURCE_UNSUPPORTED")
            return
        raw = session.read_local_bytes(path)
        if args.mcp_host == "codex":
            data = tomllib.loads(raw.decode("utf-8-sig"))
            group = data.get("mcp_servers")
        else:
            data = transport.strict_json(raw)
            group = data.get("mcpServers") if isinstance(data, dict) else None
        entry = group.get("gamelens") if isinstance(group, dict) else None
        if not isinstance(entry, dict):
            add(report, "mcp_config", "mcp", "blocked", "MCP_REGISTRATION_MISSING")
            return
    except (session.FileBoundaryError, ValueError, UnicodeError, RecursionError):
        add(report, "mcp_config", "mcp", "blocked", "MCP_CONFIG_UNREADABLE_OR_INVALID")
        return
    allowed = {"command", "args", "env", "cwd", "enabled"} if args.mcp_host == "codex" else {"type", "command", "args", "env"}
    if set(entry) - allowed or entry.get("type", "stdio") != "stdio":
        add(report, "mcp_config", "mcp", "unverified", "MCP_CONFIG_UNSUPPORTED")
        return
    if "enabled" in entry and type(entry["enabled"]) is not bool:
        add(report, "mcp_config", "mcp", "blocked", "MCP_CONFIG_SCHEMA_INVALID")
        return
    if entry.get("enabled") is False:
        add(report, "mcp_config", "mcp", "blocked", "MCP_REGISTRATION_DISABLED")
        return
    command, arguments, env = entry.get("command"), entry.get("args"), entry.get("env", {})
    if (not isinstance(command, str) or not isinstance(arguments, list)
            or not all(isinstance(item, str) for item in arguments)
            or not isinstance(env, dict) or not all(isinstance(value, str) for value in env.values())):
        add(report, "mcp_config", "mcp", "blocked", "MCP_CONFIG_SCHEMA_INVALID")
        return
    permitted_env = {"GAMELENS_PROJECT_DIR", "GAMELENS_CLIENTS_FILE", "GAMELENS_URL"}
    if set(env) - permitted_env:
        add(report, "mcp_config", "mcp", "unverified", "MCP_CONFIG_UNSUPPORTED")
        return
    if any(_unresolved(value) for value in [command] + arguments + list(env.values()) + [entry.get("cwd")]):
        add(report, "mcp_config", "mcp", "unverified", "MCP_CONFIG_UNRESOLVED")
        return
    try:
        command_path, info = session._checked(command)
        if command_path.name.lower() not in ("python", "python3", "python.exe", "python3.exe"):
            add(report, "mcp_config", "mcp", "unverified", "MCP_LAUNCHER_UNSUPPORTED")
            return
    except session.FileBoundaryError:
        add(report, "mcp_config", "mcp", "blocked", "MCP_INTERPRETER_MISSING_OR_UNSUPPORTED")
        return
    # Presence is not proof that a configured executable is Python. Never run it.
    add(report, "mcp_interpreter", "mcp", "pass", "MCP_INTERPRETER_FILE_PRESENT", {
        "matches_executing_interpreter": _same_path(command, sys.executable),
        "matches_project_interpreter": _same_path(command, root / ".venv" / "Scripts" / "python.exe"),
    })
    launcher = root / "plugins" / "gamelens" / "scripts" / "mcp_server.py"
    scripted = len(arguments) == 1 and _same_path(arguments[0], launcher)
    module = arguments == ["-m", "gamelens.mcp"]
    if not scripted and not module:
        add(report, "mcp_config", "mcp", "unverified", "MCP_LAUNCHER_UNSUPPORTED")
        return
    try:
        session._checked(launcher if scripted else root / "gamelens" / "mcp.py")
    except session.FileBoundaryError:
        add(report, "mcp_config", "mcp", "blocked", "MCP_LAUNCHER_MISSING")
        return
    if scripted and not _same_path(env.get("GAMELENS_PROJECT_DIR", ""), root):
        add(report, "mcp_config", "mcp", "blocked", "MCP_PROJECT_PATH_MISMATCH")
        return
    if "cwd" in entry and not _same_path(entry["cwd"], root):
        add(report, "mcp_config", "mcp", "unverified", "MCP_WORKING_DIRECTORY_UNSUPPORTED")
        return
    if module:
        add(report, "mcp_module_resolution", "mcp", "unverified", "MCP_MODULE_RESOLUTION_UNVERIFIED", affects_exit=False)
    if env.get("GAMELENS_CLIENTS_FILE"):
        if not scripted or not args.clients_file or not _same_path(env["GAMELENS_CLIENTS_FILE"], args.clients_file):
            add(report, "mcp_config", "mcp", "unverified", "MCP_CLIENTS_FILE_NOT_EXPLICITLY_SELECTED")
            return
        if "GAMELENS_URL" in env:
            add(report, "mcp_config", "mcp", "unverified", "MCP_ROUTING_AMBIGUOUS")
            return
    elif args.client:
        add(report, "mcp_config", "mcp", "blocked", "MCP_NAMED_ROUTING_MISMATCH")
        return
    elif "GAMELENS_URL" in env:
        try:
            origin, host, port = transport.validate_origin(env["GAMELENS_URL"])
        except ValueError:
            add(report, "mcp_config", "mcp", "blocked", "MCP_ORIGIN_INVALID")
            return
        if selected and origin != selected["url"]:
            add(report, "mcp_config", "mcp", "blocked", "MCP_ORIGIN_MISMATCH")
            return
    elif selected:
        add(report, "mcp_routing", "mcp", "unverified", "MCP_EFFECTIVE_ORIGIN_UNVERIFIED", affects_exit=False)
    add(report, "mcp_config", "mcp", "pass", "MCP_STATIC_CONFIG_VALID", {"host": args.mcp_host, "launch_form": "script" if scripted else "module"})


def connected_check(args, selected, report):
    if not args.connect:
        add(report, "connection", "connection", "skipped", "CONNECT_NOT_REQUESTED", affects_exit=False)
        return
    if selected is None:
        add(report, "connection", "connection", "blocked", "SESSION_SELECTION_UNAVAILABLE")
        return
    credential = None
    try:
        credential = session.read_selected_agent(selected["port"])
        result = transport.probe_state(selected["url"], credential)
    except session.SessionError as exc:
        add(report, "connection", "connection", "blocked", exc.args[0])
        return
    finally:
        credential = None
    if not result["ok"]:
        evidence = {"http_status": result["http_status"]} if result.get("http_status") is not None else {}
        add(report, "connection", "connection", "blocked", result["reason_code"], evidence)
        return
    add(report, "connection", "connection", "pass", "STATE_REPORTED", {"http_status": 200})
    state = result["reported_state"]
    report.update(state)
    capture = state["reported_capture"]
    add(report, "capture_report", "capture", "pass" if capture["healthy"] else "blocked",
        "CAPTURE_REPORTED_HEALTHY" if capture["healthy"] else "CAPTURE_REPORTED_UNHEALTHY")
    input_state = state["reported_input_state"]
    target = state["reported_target"]
    if input_state["killed"]:
        reason = "INPUT_REPORTED_KILLED"
    elif input_state["unreleased_count"]:
        reason = "INPUT_REPORTED_UNRELEASED"
    elif not input_state["armed"]:
        reason = "INPUT_REPORTED_DISARMED"
    elif target is None:
        reason = "INPUT_REPORTED_TARGET_MISSING"
    elif not target["foreground"]:
        reason = "INPUT_REPORTED_NOT_FOREGROUND"
    elif input_state["dry_run"]:
        reason = "INPUT_REPORTED_DRY_RUN"
    else:
        reason = "INPUT_STATE_REPORTED_ONLY"
    add(report, "input_state", "input", "unverified", reason, affects_exit=False)
    if args.expected_target_hwnd is not None:
        matched = target is not None and target["hwnd"] == args.expected_target_hwnd
        report["target_match"] = {"status": "pass" if matched else "blocked",
                                  "reason_code": "TARGET_REPORTED_MATCH" if matched else "TARGET_REPORTED_MISMATCH"}
        add(report, "target_match", "selection", "pass" if matched else "blocked", report["target_match"]["reason_code"])


def build_report(args, root=PROJECT_ROOT):
    report = new_report("connected" if args.connect else "offline")
    selected = select_session(args, report)
    inspect_environment(root, args.scope, report)
    inspect_mcp(args, root, selected, report)
    connected_check(args, selected, report)
    add(report, "actual_frame_verification", "capture", "unverified", "FRAME_NOT_REQUESTED", affects_exit=False)
    add(report, "input_authorization", "input", "unverified", "CHECKER_GRANTS_NO_INPUT_AUTHORIZATION", affects_exit=False)
    report["exit_code"] = exit_code(report)
    return report


def emit(report, output_format, out):
    if output_format == "json":
        out.write(json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
        return
    out.write("GameLens setup diagnostics: %s (read-only)\n" % report["mode"])
    for check in report["checks"]:
        out.write("%s %s: %s\n" % (check["status"], check["id"], check["reason_code"]))
        if check["safe_evidence"]:
            out.write("  %s\n" % json.dumps(check["safe_evidence"], sort_keys=True, ensure_ascii=True))
        if check["status"] in ("blocked", "warning") or (check["status"] == "unverified" and check["affects_exit"]):
            out.write("  %s\n" % check["manual_next_step"])
    if "selected_session" in report:
        out.write("Selected session: %s\n" % json.dumps(report["selected_session"], ensure_ascii=True))
    for key in ("reported_capture", "reported_target", "reported_input_state", "target_match"):
        if key in report:
            out.write("%s: %s\n" % (key, json.dumps(report[key], ensure_ascii=True)))
    out.write("Actual frame: unverified. Input authorization granted by checker: false.\n")
    out.write("Exit code: %d (not permission to control input).\n" % report["exit_code"])


def main(argv=None, out=None, root=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if out is None else out
    output_format = "json" if "json" in argv or "--format=json" in argv else "text"
    try:
        args = _arguments(argv)
        output_format = args.format
        report = build_report(args, PROJECT_ROOT if root is None else Path(root))
    except ArgumentFailure as exc:
        report = new_report("offline")
        add(report, "arguments", "checker", "blocked", exc.args[0])
        report["exit_code"] = 64
    except KeyboardInterrupt:
        report = new_report("offline")
        add(report, "checker", "checker", "unverified", "CHECKER_CANCELLED")
        report["exit_code"] = 130
    except Exception:
        report = new_report("offline")
        add(report, "checker", "checker", "blocked", "CHECKER_INTERNAL_ERROR")
        report["exit_code"] = 70
    emit(report, output_format, out)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
