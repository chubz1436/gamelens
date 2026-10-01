"""Opt-in portal/math overlap; the caller alone verifies the exchange receipt.

Use a fresh helper for one supported exchange. No math, Start, navigation,
focus, arming, or retries happen here; actions use the existing guarded act.
"""
from __future__ import annotations

import time

from gamelens.marathon import UnknownScreen


class PortalExchangeOverlap:
    SUPPORTED = {(8, 1), (14, 8)}
    POST_ARRIVAL_GUARD = 2.3

    def __init__(self):
        self.controller = None
        self.chapter = self.trainer = None
        self.phase = "idle"
        self.portal_sent = False
        self.math_receipt_confirmed = False
        self.cast_at = self.arrival_at = self.confirmed_at = self.mounted_at = None
        self.additional_guard_seconds = 0.0

    def snapshot(self):
        return {"chapter": self.chapter, "trainer": self.trainer, "phase": self.phase,
                "portal_sent": self.portal_sent,
                "math_receipt_confirmed": self.math_receipt_confirmed,
                "cast_at": self.cast_at, "arrival_observed_at": self.arrival_at,
                "receipt_confirmation_observed_at": self.confirmed_at,
                "mounted_at": self.mounted_at,
                "additional_guard_seconds": self.additional_guard_seconds,
                "cast_to_confirmation_seconds": None if self.confirmed_at is None else self.confirmed_at-self.cast_at,
                "cast_to_mounted_seconds": None if self.mounted_at is None else self.mounted_at-self.cast_at}

    def before_exchange(self, controller, chapter, trainer):
        if self.phase != "idle" or (chapter, trainer) not in self.SUPPORTED:
            raise UnknownScreen("Portal overlap is unsupported or already attempted; no retry")
        self.controller, self.chapter, self.trainer = controller, chapter, trainer
        self.phase = "preparing"
        try:
            controller.read()
            if (controller.progress.chapter != chapter or
                    controller.area() not in ("city", "suburb") or
                    not controller.portrait_matches(trainer) or
                    controller.mount_state() != "mounted"):
                raise UnknownScreen("Portal overlap requires the active chapter and selected mounted Trainer")
            if not controller.athens_portal_ready():
                raise UnknownScreen("Athens portal is not verified ready")
            controller.act([{"do": "tap", "key": "7", "ms": 50}], "Dismount for exchange portal overlap")
            controller.wait_mount("unmounted", 10)
            controller.wait_for(controller.athens_portal_ready, 10, "Portal not ready after overlap dismount")
            # Timestamp dispatch, not an assumed warp or exchange success.
            self.cast_at = time.monotonic()
            controller.act([{"do": "tap", "key": "2", "ms": 50}], "Cast Athens portal during exchange")
            self.portal_sent = True
            self.phase = "awaiting_receipt"
            return self.snapshot()
        except Exception:
            self.phase = "stopped"
            raise

    def observe_arrival(self, controller, refresh=True):
        """Optional observation during math; does not wait, act or confirm math.

        A verified arrival timestamp allows math to consume the loading guard.
        Cast time alone never proves arrival or an expired post-warp cooldown.
        Set refresh=False only from a read hook after its fresh frame is read.
        """
        if controller is not self.controller or self.phase != "awaiting_receipt":
            raise UnknownScreen("Portal overlap has no matching pending cast")
        if refresh:
            controller.read()
        return self._arrival_matches(controller)

    def _arrival_matches(self, controller):
        try:
            arrived = controller.at_athens_spawn() and controller.mount_state() == "unmounted"
        except UnknownScreen:
            # Transitional loading pixels are not proof; only bounded waits.
            return False
        if arrived and self.arrival_at is None:
            self.arrival_at = time.monotonic()
        return arrived

    def after_confirmed_exchange(self, controller, chapter, trainer):
        if (controller is not self.controller or self.phase != "awaiting_receipt" or
                (chapter, trainer) != (self.chapter, self.trainer) or not self.portal_sent):
            raise UnknownScreen("No matching armed portal overlap; no retry")
        try:
            # The caller must verify the actual receipt, advance progress and
            # close it before invoking this hook. A sent portal is not a receipt.
            if controller.progress.chapter != chapter + 1:
                raise UnknownScreen("Actual exchange receipt has not advanced the chapter")
            self.math_receipt_confirmed = True
            self.confirmed_at = time.monotonic()
            controller.wait_for(lambda: self._arrival_matches(controller), 15,
                                "Overlap portal did not reach verified Athens spawn on foot")
            remaining = max(0.0, self.POST_ARRIVAL_GUARD-(time.monotonic()-self.arrival_at))
            self.additional_guard_seconds = remaining
            if remaining:
                time.sleep(remaining)
            controller.read()
            if not self._arrival_matches(controller):
                raise UnknownScreen("Athens overlap spawn did not settle")
            controller.wait_for(controller.riding_ready, 8, "Riding not ready after overlap portal")
            controller.read()
            if not self._arrival_matches(controller) or not controller.riding_ready():
                raise UnknownScreen("Overlap Riding origin or ready icon changed")
            self.phase = "remounting"
            controller.act([{"do": "tap", "key": "7", "ms": 50}], "Restore Riding after exchange portal")
            controller.wait_mount("mounted")
            self.mounted_at = time.monotonic()
            self.phase = "completed"
            return self.snapshot()
        except Exception:
            # Never change controller.progress or the original server Start.
            self.phase = "stopped"
            raise
