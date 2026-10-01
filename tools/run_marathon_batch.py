"""Bounded, explicitly invoked Marathon batch with one video per new race.

Receipt times/rewards remain pending independent visual review. Uncertainty
halts the batch with the current recording and original race timer preserved.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gamelens.marathon import UnknownScreen
from gamelens.marathon_race import RaceController
from gamelens.mcp import GameLensClient

DEFAULT_PROFILE = ROOT / "profiles/godsarena/characters/atong69"


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def recording(client, action):
    args = {"action": action}
    if action == "start":
        args["fps"] = 30
    items, error = client.tool_recording(args)
    text = next((item["text"] for item in items if item.get("type") == "text"), "")
    if error:
        raise UnknownScreen(f"Recording {action} failed; no retry: {text}")
    try:
        state = json.loads(text)
    except ValueError:
        raise UnknownScreen(f"Recording {action} response unreadable; no retry") from None
    if not isinstance(state, dict) or not isinstance(state.get("active"), bool):
        raise UnknownScreen(f"Recording {action} state missing; no retry")
    return state


class RecordingRaceController(RaceController):
    def start_from_confirmation(self):
        self.read()
        if not self.portrait_matches(1) or self.area() != "suburb":
            raise UnknownScreen("Recording start requires actual Fitness1 in suburb")
        if not self.label_present(self.assets["start"], 408, 277):
            raise UnknownScreen("Recording start requires actual Start confirmation")
        if recording(self.client, "status")["active"]:
            raise UnknownScreen("Recording already active; batch will not take it over")
        state = recording(self.client, "start")
        save_json(self.evidence / "recording-start.json", {
            "recording": state, "saved_at": time.time(), "race_started_at": self.started_at,
        })
        if not state["active"]:
            raise UnknownScreen("Recording did not become active; no race Start sent")
        # Base rechecks current NPC/area/Start and retains its guarded input path.
        super().start_from_confirmation()


def run_context(controller):
    return {
        "started_at": controller.started_at,
        "chapter": controller.progress.chapter,
        "events": controller.events,
        "last_frame": str((controller.evidence / f"frame-{controller.count:04}.jpg").resolve()),
    }


def run_batch(client, profile, evidence, count=5, overlap_portals=False,
              controller_factory=RecordingRaceController):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
        raise ValueError("count must be an integer from 1 to 5")
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=False)
    manifest = {"requested_count": count, "profile": str(Path(profile).resolve()),
                "overlap_portals": bool(overlap_portals), "status": "running", "runs": []}
    save_json(evidence / "batch.json", manifest)
    for number in range(1, count + 1):
        run_dir = evidence / f"race-{number:02}"
        run_dir.mkdir()
        controller = None
        try:
            controller = controller_factory(client, Path(profile), run_dir, chapter=1,
                                            overlap_portals=overlap_portals)
            controller.begin_race()
            # Never infer completion from returning normally or elapsed wall time.
            progress = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
            if progress.get("phase") != "finish_receipt_needs_review":
                raise UnknownScreen("Final progress phase is not a finish receipt")
            if [event.get("chapter") for event in controller.events] != list(range(1, 18)):
                raise UnknownScreen("Seventeen verified chapter events are required")
            controller.read()
            if not controller.finish_visible() or not controller.portrait_matches(1):
                raise UnknownScreen("Actual finish and Fitness1 portrait are not verified")
            controller.checkpoint("finish_receipt_needs_review")
            receipt = run_context(controller)["last_frame"]
            stopped = recording(client, "stop")
            save_json(run_dir / "recording-stop.json", stopped)
            if stopped["active"]:
                raise UnknownScreen("Recording stop not confirmed; no next race")
            result = {"number": number, "status": "needs_review", "final_receipt": receipt,
                      "progress": str((run_dir / "progress.json").resolve()),
                      "recording_stop": stopped, "context": run_context(controller)}
            save_json(run_dir / "result.json", result)
            manifest["runs"].append(result)
            save_json(evidence / "batch.json", manifest)
        except Exception as exc:
            # Do not stop/restart the video or replay input: root can repair this race.
            halted = {"number": number, "status": "halted", "error": str(exc),
                      "evidence": str(run_dir.resolve())}
            if controller is not None:
                halted["context"] = run_context(controller)
            save_json(run_dir / "halted.json", halted)
            manifest["runs"].append(halted)
            manifest["status"] = "halted"
            save_json(evidence / "batch.json", manifest)
            return manifest
    manifest["status"] = "needs_review"
    save_json(evidence / "batch.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--evidence-dir", type=Path, required=True,
                        help="New directory; existing directories are refused")
    parser.add_argument("--overlap-portals", action="store_true",
                        help="Opt into the controller's learned portal/exchange overlap")
    args = parser.parse_args(argv)
    for asset in ("screens.npz", "math-glyphs.json"):
        if not (args.profile / asset).is_file():
            parser.error(f"Profile missing required asset: {asset}")
    result = run_batch(GameLensClient("http://127.0.0.1:8777", None), args.profile,
                       args.evidence_dir, args.count, args.overlap_portals)
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "halted" else 0


if __name__ == "__main__":
    raise SystemExit(main())
