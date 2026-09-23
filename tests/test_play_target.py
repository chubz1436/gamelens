"""play.target_hwnd: the window handle comes from the running server, not a constant.

It runs when play is imported, so every malformed answer has to come back as 0
("no window, take no focus") rather than as an exception that stops every
driver from importing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import play  # noqa: E402


def answer(monkeypatch, status, body):
    monkeypatch.setattr(play, "tokens", lambda: ("op", "ag"))
    monkeypatch.setattr(play, "req", lambda *a, **k: (status, body, {}))


def test_the_handle_the_server_reports_is_used(monkeypatch):
    answer(monkeypatch, 200, json.dumps({"target": {"hwnd": 329882}}).encode())
    assert play.target_hwnd() == 329882


@pytest.mark.parametrize("body", [
    b"null", b"[]", b'"x"', b"7", b"not json",
    b"{}", b'{"target": null}', b'{"target": []}', b'{"target": {}}',
    b'{"target": {"hwnd": "329882"}}', b'{"target": {"hwnd": 1e400}}',
    b'{"target": {"hwnd": 1.5}}', b'{"target": {"hwnd": true}}',
    b'{"target": {"hwnd": -5}}', b'{"target": {"hwnd": 0}}',
    b'{"target": {"hwnd": 18446744073709551616}}',
])
def test_a_malformed_answer_means_no_window(monkeypatch, body):
    answer(monkeypatch, 200, body)
    assert play.target_hwnd() == 0


def test_an_error_status_means_no_window(monkeypatch):
    answer(monkeypatch, 401, b'{"target": {"hwnd": 5}}')
    assert play.target_hwnd() == 0


def test_no_server_means_no_window(monkeypatch):
    def refused(*a, **k):
        raise ConnectionRefusedError
    monkeypatch.setattr(play, "tokens", lambda: ("op", "ag"))
    monkeypatch.setattr(play, "req", refused)
    assert play.target_hwnd() == 0


def test_missing_token_files_mean_no_window(monkeypatch):
    def missing():
        raise FileNotFoundError
    monkeypatch.setattr(play, "tokens", missing)
    assert play.target_hwnd() == 0
