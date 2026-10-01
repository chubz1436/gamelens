"""Offline dispatch checks for portal cooldown fallback; no HTTP or game inputs."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gamelens.marathon_control import FastExchange
from gamelens.marathon_race import RaceController
import gamelens.marathon_overlap as overlap_module


def seeded_controller(chapter, ready, enabled=True):
    controller = RaceController.__new__(RaceController)
    controller.progress = SimpleNamespace(chapter=chapter)
    controller.overlap_portals = enabled
    controller._portal_overlap = None
    controller.read = Mock()
    controller.act = Mock()

    def readiness():
        # The branch must decide from a fresh observation.
        controller.read.assert_called_once_with()
        return ready

    controller.athens_portal_ready = Mock(side_effect=readiness)
    return controller


@pytest.mark.parametrize("chapter,trainer", [(8, 1), (14, 8)])
def test_cooldown_delegates_normal_exchange_without_portal_input(monkeypatch, chapter, trainer):
    controller = seeded_controller(chapter, ready=False)
    ordinary = Mock(return_value={"exchange": "ordinary"})
    monkeypatch.setattr(FastExchange, "run", ordinary)
    overlap_factory = Mock(side_effect=AssertionError("Cooldown must not enter overlap"))
    monkeypatch.setattr(overlap_module, "PortalExchangeOverlap", overlap_factory)

    assert controller.run(trainer) == {"exchange": "ordinary"}
    ordinary.assert_called_once_with(trainer)
    controller.athens_portal_ready.assert_called_once_with()
    controller.act.assert_not_called()
    overlap_factory.assert_not_called()
    assert controller._portal_overlap is None


@pytest.mark.parametrize("chapter,trainer", [(8, 1), (14, 8)])
def test_supported_ready_branch_enters_overlap(monkeypatch, chapter, trainer):
    controller = seeded_controller(chapter, ready=True)
    ordinary = Mock()
    monkeypatch.setattr(FastExchange, "run", ordinary)
    entered = Mock()
    entered.before_exchange.side_effect = RuntimeError("offline overlap entry sentinel")
    factory = Mock(return_value=entered)
    monkeypatch.setattr(overlap_module, "PortalExchangeOverlap", factory)

    with pytest.raises(RuntimeError, match="offline overlap entry sentinel"):
        controller.run(trainer)
    factory.assert_called_once_with()
    entered.before_exchange.assert_called_once_with(controller, chapter, trainer)
    controller.athens_portal_ready.assert_called_once_with()
    assert controller._portal_overlap is entered
    ordinary.assert_not_called()
    controller.act.assert_not_called()


@pytest.mark.parametrize("chapter,trainer,enabled", [(7, 1, True), (14, 9, True), (8, 1, False)])
def test_unsupported_or_disabled_overlap_skips_portal_query(monkeypatch, chapter, trainer, enabled):
    controller = seeded_controller(chapter, ready=True, enabled=enabled)
    ordinary = Mock(return_value="normal")
    monkeypatch.setattr(FastExchange, "run", ordinary)
    factory = Mock(side_effect=AssertionError("Unsupported overlap must not be created"))
    monkeypatch.setattr(overlap_module, "PortalExchangeOverlap", factory)

    assert controller.run(trainer) == "normal"
    ordinary.assert_called_once_with(trainer)
    controller.read.assert_called_once_with()
    controller.athens_portal_ready.assert_not_called()
    controller.act.assert_not_called()
    factory.assert_not_called()
