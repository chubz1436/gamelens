"""Authored, NOT RUN. Fake sockets plus one bounded loopback-only fixture."""
import errno
import json
import os
import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

from helpers import CANARY, FakeSocket, http, response, state_fixture


class HttpProbeTests(unittest.TestCase):
    def assert_closed_once(self, sock):
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(len(sock.factory_calls), 1)
        self.assertEqual(len(sock.connect_calls), 1)
        self.assertFalse(sock.blocking)
        self.assertLessEqual(bytes(sock.sent).count(b"GET /state HTTP/1.1"), 1)

    def test_success_projects_only_allowlisted_fields(self):
        sock = FakeSocket()
        result = sock.probe()
        self.assertTrue(result["ok"])
        self.assertEqual(result["reported_state"]["reported_capture"]["source"], "server_reported_only")
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn("fixture-agent-token", json.dumps(result))
        self.assertNotIn("title", result["reported_state"]["reported_target"])
        self.assert_closed_once(sock)

    def test_partial_request_writes_do_not_duplicate_request(self):
        sock = FakeSocket(partial_write=3)
        sock.send_results = [1, BlockingIOError(), 2, InterruptedError(), 1]
        result = sock.probe()
        self.assertTrue(result["ok"])
        sent = bytes(sock.sent)
        self.assertEqual(sent.count(b"GET /state HTTP/1.1\r\n"), 1)
        self.assertEqual(sent.count(b"X-GameLens-Token: fixture-agent-token\r\n"), 1)
        self.assertTrue(sent.endswith(b"\r\n\r\n"))
        self.assert_closed_once(sock)

    def test_zero_partial_write_and_write_error_close_socket(self):
        for final, code in ((0, "HTTP_REQUEST_WRITE_FAILED"), (OSError(CANARY), "HTTP_IO_FAILED")):
            with self.subTest(code=code):
                sock = FakeSocket()
                sock.send_results = [2, final]
                result = sock.probe()
                self.assertEqual(result["reason_code"], code)
                self.assertNotIn(CANARY, json.dumps(result))
                self.assert_closed_once(sock)

    def test_stalled_request_write_uses_total_deadline(self):
        sock = FakeSocket()
        sock.write_stall = True
        self.assertEqual(sock.probe()["reason_code"], "HTTP_DEADLINE_EXCEEDED")
        self.assert_closed_once(sock)

    def test_pending_connect_and_connect_failure(self):
        success = FakeSocket()
        success.connect_code = errno.EINPROGRESS
        self.assertTrue(success.probe()["ok"])
        self.assert_closed_once(success)
        for pending in (False, True):
            sock = FakeSocket()
            sock.connect_code = errno.EINPROGRESS if pending else errno.ECONNREFUSED
            sock.socket_error = errno.ECONNREFUSED
            self.assertEqual(sock.probe()["reason_code"], "HTTP_CONNECT_FAILED")
            self.assert_closed_once(sock)

    def test_stalled_headers_and_body(self):
        chunks_cases = ([None], [b"HTTP/1.1 200 OK\r\n", None],
                        [b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{", None])
        for chunks in chunks_cases:
            with self.subTest(chunks=len(chunks)):
                sock = FakeSocket(chunks)
                result = sock.probe()
                self.assertEqual(result["reason_code"], "HTTP_DEADLINE_EXCEEDED")
                self.assertLessEqual(sock.clock.value, 3.001)
                self.assert_closed_once(sock)

    def test_slow_trickle_never_resets_deadline(self):
        message = response()
        header, body = message.split(b"\r\n\r\n", 1)
        for chunks in ([bytes([value]) for value in message],
                       [header + b"\r\n\r\n"] + [bytes([value]) for value in body]):
            sock = FakeSocket(chunks)
            sock.tick = 0.07
            result = sock.probe()
            self.assertEqual(result["reason_code"], "HTTP_DEADLINE_EXCEEDED")
            self.assertLess(sock.clock.value, 3.1)
            self.assert_closed_once(sock)

    def test_header_and_body_limits(self):
        cases = [([b"HTTP/1.1 200 OK\r\nX-Long: " + b"x" * http.MAX_HEADERS], "HTTP_HEADERS_TOO_LARGE"),
                 ([response(b"x" * (http.MAX_BODY + 1))], "HTTP_BODY_TOO_LARGE")]
        for chunks, expected in cases:
            sock = FakeSocket(chunks)
            self.assertEqual(sock.probe()["reason_code"], expected)
            self.assert_closed_once(sock)

    def test_duplicate_content_length_including_identical_is_refused(self):
        raw = b'{}'
        for extra in (b"Content-Length: 2\r\n", b"content-length: 3\r\n"):
            sock = FakeSocket([response(raw, extra_headers=extra)])
            self.assertEqual(sock.probe()["reason_code"], "HTTP_FRAMING_UNSUPPORTED")
            self.assert_closed_once(sock)

    def test_premature_eof_at_each_response_stage(self):
        for chunks in ([], [b"HTTP/1.1 200"], [response(b"{}", declared_length=30)]):
            sock = FakeSocket(chunks)
            self.assertEqual(sock.probe()["reason_code"], "HTTP_PREMATURE_EOF")
            self.assert_closed_once(sock)

    def test_unsupported_framing(self):
        cases = [b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}",
                 response(b"{}", extra_headers=b"Transfer-Encoding: chunked\r\n"),
                 response(b"{}", extra_headers=b"Content-Encoding: gzip\r\n"),
                 response(b"{}", extra_headers=b" folded: value\r\n"),
                 response(b"{}", declared_length=-1),
                 response(b"{}", declared_length=1)]
        for message in cases:
            sock = FakeSocket([message])
            self.assertEqual(sock.probe()["reason_code"], "HTTP_FRAMING_UNSUPPORTED")
            self.assert_closed_once(sock)

    def test_malformed_json_duplicate_keys_depth_and_non_json_numbers(self):
        for raw in (CANARY.encode(), b'{"a":1,"a":2}', b'{"age":NaN}', b'\xff',
                    b'[' * 33 + b']' * 33, b'{"huge":' + b'9' * 100 + b'}'):
            sock = FakeSocket([response(raw)])
            result = sock.probe()
            self.assertEqual(result["reason_code"], "STATE_JSON_INVALID")
            self.assertNotIn(CANARY, json.dumps(result))
            self.assert_closed_once(sock)

    def test_valid_json_with_bad_state_schema_is_not_healthy(self):
        for value in ({}, [], {"capture": {"healthy": True}}, {"capture": CANARY}):
            sock = FakeSocket([response(json.dumps(value).encode())])
            result = sock.probe()
            self.assertEqual(result["reason_code"], "STATE_SCHEMA_UNSUPPORTED")
            self.assertNotIn("reported_state", result)
            self.assert_closed_once(sock)

    def test_failed_status_never_returns_body_headers_or_redirect_destination(self):
        for status, expected in ((301, "HTTP_REDIRECT_REFUSED"), (307, "HTTP_REDIRECT_REFUSED"),
                                 (401, "HTTP_AUTH_REJECTED"), (403, "HTTP_FORBIDDEN"),
                                 (500, "HTTP_STATUS_REJECTED")):
            sock = FakeSocket([response(CANARY.encode(), status=status,
                                        extra_headers=("Location: http://127.0.0.1:8778/" + CANARY + "\r\n").encode())])
            result = sock.probe()
            self.assertEqual(result, {"ok": False, "reason_code": expected, "http_status": status})
            self.assertNotIn(CANARY, json.dumps(result))
            self.assert_closed_once(sock)

    def test_proxies_dns_and_other_address_families_not_used(self):
        sock = FakeSocket()
        env = {"HTTP_PROXY": "http://" + CANARY, "HTTPS_PROXY": "http://" + CANARY,
               "ALL_PROXY": "http://" + CANARY, "GAMELENS_AGENT_TOKEN": CANARY}
        with mock.patch.dict(os.environ, env), mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            self.assertTrue(sock.probe()["ok"])
        self.assertEqual(sock.connect_calls, [("127.0.0.1", 8777)])
        self.assertNotIn(CANARY.encode(), sock.sent)
        self.assert_closed_once(sock)
        ipv6 = FakeSocket()
        result = http.probe_state("http://[::1]:8778", "test", socket_factory=ipv6.factory,
                                  clock=ipv6.clock, waiter=ipv6.wait)
        self.assertTrue(result["ok"])
        self.assertEqual(ipv6.connect_calls, [("::1", 8778, 0, 0)])
        self.assertEqual(ipv6.factory_calls, [(socket.AF_INET6, socket.SOCK_STREAM)])
        self.assert_closed_once(ipv6)

    def test_invalid_origin_or_token_never_creates_socket(self):
        factory = mock.Mock(side_effect=AssertionError("socket must not exist"))
        for origin in ("http://localhost:8777", "https://127.0.0.1:8777", "http://127.0.0.1:8777/" + CANARY,
                       "http://127.0.0.1:08777", "http://127.0.0.1:8777?token=" + CANARY):
            result = http.probe_state(origin, "token", socket_factory=factory)
            self.assertEqual(result["reason_code"], "ORIGIN_INVALID")
        for token in ("", CANARY + "\r\nX: bad", "x" * 4097):
            result = http.probe_state("http://127.0.0.1:8777", token, socket_factory=factory)
            self.assertEqual(result["reason_code"], "SESSION_TOKEN_INVALID")
        factory.assert_not_called()

    def test_interrupted_recv_recovers_but_keyboard_cancel_closes(self):
        sock = FakeSocket([BlockingIOError(), InterruptedError(), response()])
        self.assertTrue(sock.probe()["ok"])
        self.assert_closed_once(sock)
        sock = FakeSocket([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            sock.probe()
        self.assert_closed_once(sock)

    def test_unexpected_socket_error_is_sanitized_and_closed(self):
        sock = FakeSocket([RuntimeError(CANARY)])
        result = sock.probe()
        self.assertEqual(result["reason_code"], "HTTP_IO_FAILED")
        self.assertNotIn(CANARY, json.dumps(result))
        self.assert_closed_once(sock)

    def test_real_loopback_stall_deadline_and_peer_eof(self):
        # Future local fixture only: fake token, ephemeral loopback port, no
        # GameLens process. Timing tolerance accounts for scheduler variability.
        seen, closed = [], threading.Event()
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(1)
                data = bytearray()
                while b"\r\n\r\n" not in data and len(data) < 8192:
                    chunk = self.request.recv(1024)
                    if not chunk:
                        return
                    data.extend(chunk)
                seen.append(bytes(data))
                self.request.sendall(b"HTTP/1.1 200 OK\r\n")
                time.sleep(0.15)
                if self.request.recv(1) == b"":
                    closed.set()
        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            start = time.monotonic()
            result = http.probe_state("http://127.0.0.1:%d" % server.server_address[1],
                                      "fixture-only", http.Limits(deadline_seconds=0.08))
            elapsed = time.monotonic() - start
            self.assertEqual(result["reason_code"], "HTTP_DEADLINE_EXCEEDED")
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertLess(elapsed, 1.0)
            self.assertTrue(closed.wait(2), "client socket was not observed closed")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].count(b"GET /state HTTP/1.1"), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
