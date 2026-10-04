"""Standalone, read-only /state probe. Never import the GameLens package.

Only fixed reason codes and an allowlisted state projection leave this module.
The dependency injection hooks are for tests; they are not CLI options.
"""
import errno
import json
import math
import re
import select
import socket
import time

DEADLINE_SECONDS = 3.0
MAX_HEADERS = 16 * 1024
MAX_BODY = 128 * 1024
READ_CHUNK = 4096
MAX_DEPTH = 32
MAX_TOKEN = 4096
FAILURE_CODES = frozenset((
    "HTTP_DEADLINE_EXCEEDED", "HTTP_IO_FAILED", "HTTP_PREMATURE_EOF",
    "HTTP_FRAMING_UNSUPPORTED", "HTTP_CONNECT_FAILED", "HTTP_REQUEST_WRITE_FAILED",
    "HTTP_HEADERS_TOO_LARGE", "HTTP_BODY_TOO_LARGE", "HTTP_CONTENT_TYPE_UNSUPPORTED",
    "HTTP_REDIRECT_REFUSED", "HTTP_AUTH_REJECTED", "HTTP_FORBIDDEN", "HTTP_STATUS_REJECTED",
    "STATE_JSON_INVALID", "STATE_SCHEMA_UNSUPPORTED", "SESSION_TOKEN_INVALID",
    "HTTP_PROBE_FAILED", "HTTP_CLOSE_FAILED",
))


class ProbeFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason if reason in FAILURE_CODES else "HTTP_PROBE_FAILED")


class Limits:
    def __init__(self, deadline_seconds=DEADLINE_SECONDS,
                 max_headers=MAX_HEADERS, max_body=MAX_BODY):
        if not (0 < deadline_seconds <= DEADLINE_SECONDS):
            raise ValueError("INVALID_LIMIT")
        if not (0 < max_headers <= MAX_HEADERS and 0 < max_body <= MAX_BODY):
            raise ValueError("INVALID_LIMIT")
        self.deadline_seconds = deadline_seconds
        self.max_headers = max_headers
        self.max_body = max_body


def validate_origin(value):
    """Match the canonical literal-loopback policy in gamelens/transport.py."""
    if not isinstance(value, str):
        raise ValueError("ORIGIN_INVALID")
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})(/?)", value)
    if match is None or not 1 <= int(match.group(2)) <= 65535:
        raise ValueError("ORIGIN_INVALID")
    host, port = match.group(1), int(match.group(2))
    return "http://%s:%d" % (host, port), host.strip("[]"), port


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON_INVALID")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("JSON_INVALID")


def _integer(value):
    if len(value.lstrip("-")) > 20:
        raise ValueError("JSON_INVALID")
    return int(value)


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON_INVALID")
    return result


def check_depth(value):
    """Bound the parsed container depth for JSON and explicitly selected TOML."""
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth > MAX_DEPTH:
                raise ValueError("CONFIG_DEPTH_UNSUPPORTED")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return value


def strict_json(raw):
    """Bound nesting before parsing; reject duplicate keys and non-JSON numbers."""
    text = raw.decode("utf-8-sig")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("JSON_INVALID")
        elif char in "]}":
            depth -= 1
    return check_depth(json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                                  parse_int=_integer, parse_float=_float))


def _int(value, maximum=2 ** 63 - 1):
    return type(value) is int and 0 <= value <= maximum


def project_state(value):
    """Discard titles, logs, labels, messages, paths and all unknown fields."""
    if not isinstance(value, dict):
        raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    capture, safety = value.get("capture"), value.get("safety")
    if not isinstance(capture, dict) or not isinstance(safety, dict):
        raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    if "target" not in value:
        raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    if type(capture.get("healthy")) is not bool:
        raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    for key in ("frame_id", "width", "height"):
        if not _int(capture.get(key)):
            raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    age = capture.get("age_ms")
    if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= 1e12:
        raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    if not isinstance(capture.get("backend"), str):
        raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    for key in ("armed", "dry_run", "killed"):
        if type(safety.get(key)) is not bool:
            raise ValueError("STATE_SCHEMA_UNSUPPORTED")
    target = value["target"]
    safe_target = None
    if target is not None:
        if (not isinstance(target, dict) or not _int(target.get("hwnd"), 2 ** 64 - 1)
                or target["hwnd"] == 0 or type(target.get("foreground")) is not bool
                or not _int(target.get("width")) or not _int(target.get("height"))):
            raise ValueError("STATE_SCHEMA_UNSUPPORTED")
        safe_target = {key: target[key] for key in ("hwnd", "foreground", "width", "height")}
    executor = safety.get("executor")
    unreleased = None
    if isinstance(executor, dict) and isinstance(executor.get("unreleased"), list):
        unreleased = len(executor["unreleased"])
    backend = capture["backend"].lower()
    if backend not in ("wgc", "printwindow", "dxgi", "gdi", "mss", "none"):
        backend = "unknown"
    return {
        "reported_capture": {
            "source": "server_reported_only", "healthy": capture["healthy"],
            "backend": backend, "age_ms": age, "frame_id": capture["frame_id"],
            "width": capture["width"], "height": capture["height"],
        },
        "reported_target": safe_target,
        "reported_input_state": {
            "armed": safety["armed"], "dry_run": safety["dry_run"],
            "killed": safety["killed"], "unreleased_count": unreleased,
        },
    }


def _remaining(deadline, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise ProbeFailure("HTTP_DEADLINE_EXCEEDED")
    return remaining


def _wait(sock, reading, deadline, clock, waiter):
    remaining = _remaining(deadline, clock)
    ready_r, ready_w, exceptional = waiter(
        [sock] if reading else [], [] if reading else [sock], [sock], remaining)
    _remaining(deadline, clock)
    if exceptional:
        raise ProbeFailure("HTTP_IO_FAILED")
    if not (ready_r if reading else ready_w):
        raise ProbeFailure("HTTP_DEADLINE_EXCEEDED")


def _receive(sock, size, deadline, clock, waiter):
    while True:
        _wait(sock, True, deadline, clock, waiter)
        try:
            result = sock.recv(min(READ_CHUNK, size))
        except (BlockingIOError, InterruptedError):
            continue
        _remaining(deadline, clock)
        if not result:
            raise ProbeFailure("HTTP_PREMATURE_EOF")
        return result


def _headers(raw):
    try:
        lines = raw.decode("ascii").split("\r\n")
    except UnicodeError:
        raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED") from None
    if not re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?", lines[0]):
        raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
    status = int(lines[0][9:12])
    fields = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        key, value = line.split(":", 1)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        key = key.lower()
        if key in fields:  # includes identical duplicate Content-Length
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        fields[key] = value.strip()
    return status, fields


def probe_state(selected_origin, credential, limits=None, *, socket_factory=None,
                clock=None, waiter=None):
    """One connection and one request; fixed deadline covers connect/send/read.

    Failed responses expose only a numeric status and safe reason code. A
    successful response exposes a validated projection, never the raw JSON.
    This function does not print, retry, follow redirects, or consult proxies.
    """
    limits = limits or Limits()
    factory = socket_factory or socket.socket
    clock = clock or time.monotonic
    waiter = waiter or select.select
    sock, status = None, None
    result = None
    try:
        origin, host, port = validate_origin(selected_origin)
        if (not isinstance(credential, str) or not 1 <= len(credential) <= MAX_TOKEN
                or any(not 33 <= ord(char) <= 126 for char in credential)):
            raise ProbeFailure("SESSION_TOKEN_INVALID")
        authority = origin[len("http://"):]
        request = ("GET /state HTTP/1.1\r\nHost: %s\r\n"
                   "Accept: application/json\r\nAccept-Encoding: identity\r\n"
                   "Connection: close\r\nX-GameLens-Token: %s\r\n\r\n"
                   % (authority, credential)).encode("ascii")
        deadline = clock() + limits.deadline_seconds
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        sock = factory(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        _remaining(deadline, clock)
        address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
        code = sock.connect_ex(address)
        pending = (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                   errno.EINTR, 10035, 10036, 10037)
        if code not in (0,) + pending:
            raise ProbeFailure("HTTP_CONNECT_FAILED")
        if code:
            _wait(sock, False, deadline, clock, waiter)
            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                raise ProbeFailure("HTTP_CONNECT_FAILED")
        offset = 0
        while offset < len(request):
            _wait(sock, False, deadline, clock, waiter)
            try:
                count = sock.send(memoryview(request)[offset:])
            except (BlockingIOError, InterruptedError):
                continue
            _remaining(deadline, clock)
            if count <= 0:
                raise ProbeFailure("HTTP_REQUEST_WRITE_FAILED")
            offset += count
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            if len(buffer) >= limits.max_headers:
                raise ProbeFailure("HTTP_HEADERS_TOO_LARGE")
            buffer.extend(_receive(sock, limits.max_headers - len(buffer),
                                   deadline, clock, waiter))
        end = buffer.index(b"\r\n\r\n") + 4
        if end > limits.max_headers:
            raise ProbeFailure("HTTP_HEADERS_TOO_LARGE")
        status, headers = _headers(bytes(buffer[:end - 4]))
        if status != 200:
            reason = ("HTTP_REDIRECT_REFUSED" if 300 <= status < 400 else
                      "HTTP_AUTH_REJECTED" if status == 401 else
                      "HTTP_FORBIDDEN" if status == 403 else "HTTP_STATUS_REJECTED")
            raise ProbeFailure(reason)
        if ("transfer-encoding" in headers
                or headers.get("content-encoding", "identity").lower() != "identity"):
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        length = headers.get("content-length", "")
        if not re.fullmatch(r"[0-9]{1,10}", length):
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        length = int(length)
        if length > limits.max_body:
            raise ProbeFailure("HTTP_BODY_TOO_LARGE")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ProbeFailure("HTTP_CONTENT_TYPE_UNSUPPORTED")
        body = bytearray(buffer[end:])
        if len(body) > length:
            raise ProbeFailure("HTTP_FRAMING_UNSUPPORTED")
        while len(body) < length:
            body.extend(_receive(sock, length - len(body), deadline, clock, waiter))
        _remaining(deadline, clock)
        try:
            parsed = strict_json(bytes(body))
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            raise ProbeFailure("STATE_JSON_INVALID") from None
        try:
            state = project_state(parsed)
        except (ValueError, TypeError, OverflowError):
            raise ProbeFailure("STATE_SCHEMA_UNSUPPORTED") from None
        _remaining(deadline, clock)
        result = {"ok": True, "reason_code": "STATE_REPORTED", "http_status": status,
                  "reported_state": state}
    except ProbeFailure as exc:
        result = {"ok": False, "reason_code": exc.args[0], "http_status": status}
    except ValueError:
        result = {"ok": False, "reason_code": "ORIGIN_INVALID", "http_status": status}
    except (OSError, RuntimeError):
        result = {"ok": False, "reason_code": "HTTP_IO_FAILED", "http_status": status}
    except Exception:
        result = {"ok": False, "reason_code": "HTTP_PROBE_FAILED", "http_status": status}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                result = {"ok": False, "reason_code": "HTTP_CLOSE_FAILED", "http_status": status}
    return result
