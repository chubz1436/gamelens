"""Bounded transport views; input still uses the original full observation."""
from __future__ import annotations
import copy
import secrets
import threading
import time
from collections import OrderedDict

class PerceptionViews:
    def __init__(self, capacity=16, ttl=120.0):
        self.capacity, self.ttl = capacity, ttl
        self._records = OrderedDict()
        self._lock = threading.Lock()

    def issue(self, parent_id, view):
        token = "view-" + secrets.token_urlsafe(12)
        with self._lock:
            self._records[token] = (parent_id, view, time.monotonic())
            while len(self._records) > self.capacity:
                self._records.popitem(last=False)
        return token

    def translate(self, body):
        token = body.get("observation_id")
        if not isinstance(token, str) or not token.startswith("view-"):
            return body
        with self._lock:
            record = self._records.get(token)
            if record is None or time.monotonic() - record[2] > self.ttl:
                self._records.pop(token, None)
                raise ValueError("cropped observation is unknown or expired; look again")
            parent_id, view, _ = record
        if "anchor" in body:
            raise ValueError("label anchors require a full-frame observation")
        result = copy.deepcopy(body)
        result["observation_id"] = parent_id
        kind = str(body.get("kind", "click")).lower()
        if kind == "click":
            result["x"], result["y"] = view.to_parent_image_xy(body["x"], body["y"])
        elif kind == "sequence":
            steps = result.get("steps")
            if not isinstance(steps, list) or len(steps) > 64:
                raise ValueError("sequence steps must be a bounded list")
            for step in steps:
                if not isinstance(step, dict):
                    raise ValueError("sequence step must be an object")
                operation = str(step.get("do", "")).strip().lower()
                step["do"] = operation
                if operation == "move" or (operation == "click" and ("x" in step or "y" in step)):
                    step["x"], step["y"] = view.to_parent_image_xy(step["x"], step["y"])
        return result
