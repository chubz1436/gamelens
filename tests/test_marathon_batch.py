"""Batch orchestration with seeded fake controllers only; no HTTP or game input."""
import json
from types import SimpleNamespace

import pytest

from gamelens.marathon import UnknownScreen
from gamelens.marathon_race import RaceController
from tools.run_marathon_batch import RecordingRaceController, run_batch, save_json


class FakeClient:
    def __init__(self, active=False):
        self.active = active
        self.calls = []

    def tool_recording(self, args):
        self.calls.append(dict(args))
        if args["action"] == "start":
            self.active = True
        elif args["action"] == "stop":
            self.active = False
        return [{"type": "text", "text": json.dumps({"active": self.active})}], False


class SeededController(RecordingRaceController):
    instances = []

    def __init__(self, client, profile, evidence, chapter=1, overlap_portals=False,
                 fail=False, finish=True, npc=True, start_label=True, chapters=17):
        self.client, self.evidence = client, evidence
        self.started_at, self.events, self.count = None, [], 0
        self.progress = SimpleNamespace(chapter=chapter)
        self.assets = {"start": object()}
        self.overlap_portals = overlap_portals
        self.fail, self.finish, self.npc, self.start_label, self.chapters = fail, finish, npc, start_label, chapters
        self.instances.append(self)

    def read(self):
        self.count += 1
        (self.evidence / f"frame-{self.count:04}.jpg").write_bytes(b"seeded-frame")

    def portrait_matches(self, trainer):
        return self.npc and trainer == 1

    def area(self):
        return "suburb"

    def label_present(self, *args):
        return self.start_label

    def finish_visible(self):
        return self.finish

    def checkpoint(self, phase):
        save_json(self.evidence / "progress.json", {"phase": phase, "started_at": self.started_at,
                                                    "events": self.events})

    def begin_race(self):
        self.start_from_confirmation()
        if self.fail:
            self.events = [{"chapter": 1}]
            self.progress.chapter = 2
            self.checkpoint("stopped: seeded uncertainty")
            raise UnknownScreen("seeded uncertainty")
        self.events = [{"chapter": n} for n in range(1, self.chapters + 1)]
        self.progress.chapter = 17
        self.checkpoint("finish_receipt_needs_review")


@pytest.fixture(autouse=True)
def seeded_start(monkeypatch):
    SeededController.instances = []

    def start(controller):
        # Stand-in for the guarded base input: recording must already be active.
        assert controller.client.active
        assert (controller.evidence / "recording-start.json").is_file()
        controller.started_at = 1234.5

    monkeypatch.setattr(RaceController, "start_from_confirmation", start)


def test_exact_bounded_batch_records_each_race_before_start_and_needs_review(tmp_path):
    client = FakeClient()
    evidence = tmp_path / "new-batch"
    result = run_batch(client, tmp_path / "seeded-profile", evidence,
                       controller_factory=SeededController)
    assert result["status"] == "needs_review"
    assert len(result["runs"]) == len(SeededController.instances) == 5
    assert client.calls == [{"action": action, **({"fps": 30} if action == "start" else {})}
                            for _ in range(5) for action in ("status", "start", "stop")]
    for run in result["runs"]:
        assert run["status"] == "needs_review"
        assert "time" not in run and "reward" not in run
        assert run["context"]["started_at"] == 1234.5
        assert len(run["context"]["events"]) == 17
    assert not client.active
    assert json.loads((evidence / "batch.json").read_text()) == result
    assert all(not controller.overlap_portals for controller in SeededController.instances)


def test_uncertainty_halts_batch_keeps_original_context_and_video(tmp_path):
    client = FakeClient()
    result = run_batch(client, tmp_path, tmp_path / "batch", count=5,
                       controller_factory=lambda *a, **k: SeededController(*a, **k, fail=True))
    assert result["status"] == "halted" and len(result["runs"]) == 1
    assert result["runs"][0]["context"]["started_at"] == 1234.5
    assert result["runs"][0]["context"]["chapter"] == 2
    assert client.active
    assert [call["action"] for call in client.calls] == ["status", "start"]
    assert len(SeededController.instances) == 1
    assert json.loads((tmp_path / "batch/race-01/progress.json").read_text())["phase"].startswith("stopped:")


def test_existing_video_is_not_taken_over_or_stopped(tmp_path):
    client = FakeClient(active=True)
    result = run_batch(client, tmp_path, tmp_path / "batch", controller_factory=SeededController)
    assert result["status"] == "halted"
    assert result["runs"][0]["context"]["started_at"] is None
    assert client.calls == [{"action": "status"}] and client.active


@pytest.mark.parametrize("options", [{"npc": False}, {"start_label": False}])
def test_missing_npc_or_start_label_sends_no_recorder_commands(tmp_path, options):
    client = FakeClient()
    result = run_batch(client, tmp_path, tmp_path / "batch",
                       controller_factory=lambda *a, **k: SeededController(*a, **k, **options))
    assert result["status"] == "halted" and not client.calls
    assert result["runs"][0]["context"]["started_at"] is None


@pytest.mark.parametrize("options", [{"finish": False}, {"chapters": 16}])
def test_unverified_finish_prevents_stop_and_next_race(tmp_path, options):
    client = FakeClient()
    result = run_batch(client, tmp_path, tmp_path / "batch", count=5,
                       controller_factory=lambda *a, **k: SeededController(*a, **k, **options))
    assert result["status"] == "halted" and len(SeededController.instances) == 1
    assert client.active and [call["action"] for call in client.calls] == ["status", "start"]


def test_count_and_new_folder_guards_and_overlap_opt_in(tmp_path):
    for count in (0, 6, True, 1.0):
        with pytest.raises(ValueError, match="count"):
            run_batch(FakeClient(), tmp_path, tmp_path / "unused", count=count,
                      controller_factory=SeededController)
    with pytest.raises(FileExistsError):
        run_batch(FakeClient(), tmp_path, tmp_path, controller_factory=SeededController)
    assert not SeededController.instances
    result = run_batch(FakeClient(), tmp_path, tmp_path / "new", count=1,
                       overlap_portals=True, controller_factory=SeededController)
    assert result["status"] == "needs_review" and len(result["runs"]) == 1
    assert SeededController.instances[0].overlap_portals
