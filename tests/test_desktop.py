"""Desktop ownership and credential boundaries; no game input or native GUI."""
from types import SimpleNamespace, ModuleType
import sys
from unittest.mock import Mock

import pytest

from gamelens import desktop


def test_bootstrap_never_authenticates_a_different_origin():
    window = Mock()
    window.get_current_url.return_value = "https://example.com/"
    desktop.bootstrap(window, "http://127.0.0.1:8777/", "private-token")
    window.evaluate_js.assert_not_called()


def test_bootstrap_encodes_token_as_data_only():
    window = Mock()
    window.get_current_url.return_value = "http://127.0.0.1:8777/"
    desktop.bootstrap(window, window.get_current_url(), 'a";boom()')
    script = window.evaluate_js.call_args.args[0]
    assert '"a\\\";boom()"' in script
    assert "location" not in script and "pywebview.api" not in script


def test_owned_close_gates_inputs_and_clears_only_its_credential(monkeypatch):
    session = desktop.OwnedSession(8877)
    session.lens = Mock(tokens=SimpleNamespace(agent="owned-agent", operator="owned-operator"))
    session.server = SimpleNamespace(should_exit=False)
    session.thread = Mock()
    session.listener = Mock()
    clear = Mock()
    monkeypatch.setattr(desktop, "clear_agent_token", clear)
    operator_clear = Mock()
    monkeypatch.setattr(desktop, "clear_operator_token", operator_clear)
    session.close()
    session.close()
    session.lens.safety.kill.assert_called_once_with("desktop window closed")
    session.lens.stop.assert_called_once()
    assert session.server.should_exit
    clear.assert_called_once_with(desktop.agent_token_path(8877), "owned-agent")
    operator_clear.assert_called_once_with(desktop.operator_token_path(8877), "owned-operator")
    session.listener.close.assert_called_once()


def test_redirect_is_refused_before_following_credentials():
    with pytest.raises(desktop.DesktopError):
        desktop.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")


def fake_webview(monkeypatch, start_error=None):
    webview = ModuleType("webview")
    window = Mock()
    window.events = SimpleNamespace(loaded=FakeEvent())
    webview.create_window = Mock(return_value=window)
    start = Mock(side_effect=start_error)
    webview.start = start
    monkeypatch.setitem(sys.modules, "webview", webview)
    return webview, window, start


class FakeEvent:
    def __iadd__(self, handler):
        self.handler = handler
        return self


def test_attached_session_never_creates_or_closes_runtime(monkeypatch):
    view, window, start = fake_webview(monkeypatch)
    monkeypatch.setattr(desktop, "existing_session", lambda port: "agent-token")
    owned = Mock()
    monkeypatch.setattr(desktop, "OwnedSession", owned)
    assert desktop.run() == 0
    owned.assert_not_called()
    assert view.create_window.call_args.args[1] == "http://127.0.0.1:8777/"
    assert "js_api" not in view.create_window.call_args.kwargs
    start.assert_called_once_with(gui="edgechromium", private_mode=True, debug=False)


def test_cancelled_picker_starts_no_server(monkeypatch):
    view, window, start = fake_webview(monkeypatch)
    monkeypatch.setattr(desktop, "existing_session", lambda port: None)
    monkeypatch.setattr(desktop, "choose_target", lambda: None)
    owned = Mock()
    monkeypatch.setattr(desktop, "OwnedSession", owned)
    assert desktop.run() == 0
    owned.assert_not_called()
    start.assert_not_called()


def test_gui_failure_still_closes_owned_runtime(monkeypatch):
    fake_webview(monkeypatch, RuntimeError("GUI unavailable"))
    monkeypatch.setattr(desktop, "existing_session", lambda port: None)
    monkeypatch.setattr(desktop, "choose_target", lambda: "selected-window")
    session = Mock()
    session.start.return_value = "operator-token"
    monkeypatch.setattr(desktop, "OwnedSession", lambda port: session)
    with pytest.raises(RuntimeError, match="GUI unavailable"):
        desktop.run()
    session.start.assert_called_once_with("selected-window")
    session.close.assert_called_once()
