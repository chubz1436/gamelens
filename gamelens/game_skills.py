"""Versioned game skills. Listing/importing never launches a workflow or sends input.

Only a trusted host can supply observations and an existing guarded GameLens
instance. No scripts, raw-input callbacks, launchers or session controls exist.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

SCHEMA_VERSION = 1
MAX_STEPS = 32
MAX_OBSERVATIONS = 64
MAX_TIMEOUT_SECONDS = 3600


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f"{name} must be a nonempty bounded string")
    return value


def _keys(data: dict, allowed: set[str]) -> None:
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("unknown or invalid definition fields")


@dataclass(frozen=True)
class SkillCheck:
    fact: str
    expected: str | bool | int | float
    description: str

    def __post_init__(self):
        _text(self.fact, "fact")
        _text(self.description, "description")
        if type(self.expected) not in (str, bool, int, float):
            raise ValueError("expected must be a JSON scalar")
        _json(self.expected)

    def matches(self, facts: dict) -> bool:
        # Missing facts and truthy values of the wrong type cannot pass.
        return (self.fact in facts and type(facts[self.fact]) is type(self.expected)
                and facts[self.fact] == self.expected)

    def to_dict(self):
        return {"fact": self.fact, "expected": self.expected,
                "description": self.description}

    @classmethod
    def from_dict(cls, data):
        _keys(data, {"fact", "expected", "description"})
        return cls(**data)


@dataclass(frozen=True)
class WorkflowReference:
    source: str
    description: str

    def __post_init__(self):
        _text(self.source, "source")
        _text(self.description, "description")

    def to_dict(self):
        return {"source": self.source, "description": self.description}


@dataclass(frozen=True)
class SkillStep:
    """One inspected decision, copied as immutable JSON."""
    name: str
    sequence_json: str
    prerequisites: tuple[SkillCheck, ...]
    expected_outcome: tuple[SkillCheck, ...]

    def __post_init__(self):
        _text(self.name, "step name")
        sequence = json.loads(self.sequence_json)
        if not isinstance(sequence, list) or not sequence or len(sequence) > 64:
            raise ValueError("step requires a bounded nonempty sequence")
        # Primitive, hold and coordinate validation remains with parse_sequence.
        object.__setattr__(self, "sequence_json", _json(sequence))
        for name in ("prerequisites", "expected_outcome"):
            checks = tuple(getattr(self, name))
            if not checks or any(not isinstance(c, SkillCheck) for c in checks):
                raise ValueError("step requires valid prerequisites and expected outcome")
            object.__setattr__(self, name, checks)

    def to_dict(self):
        return {"name": self.name, "sequence": json.loads(self.sequence_json),
                "prerequisites": [c.to_dict() for c in self.prerequisites],
                "expected_outcome": [c.to_dict() for c in self.expected_outcome]}

    @classmethod
    def from_dict(cls, data):
        _keys(data, {"name", "sequence", "prerequisites", "expected_outcome"})
        return cls(data["name"], _json(data["sequence"]),
                   tuple(SkillCheck.from_dict(c) for c in data["prerequisites"]),
                   tuple(SkillCheck.from_dict(c) for c in data["expected_outcome"]))


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    version: str
    title: str
    game_id: str
    compatible_profiles: tuple[str, ...]
    prerequisites: tuple[SkillCheck, ...]
    expected_outcome: tuple[SkillCheck, ...]
    references: tuple[WorkflowReference, ...]
    steps: tuple[SkillStep, ...] = ()
    timeout_seconds: float = 30.0
    max_observations: int = MAX_OBSERVATIONS
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported skill schema_version")
        for name in ("skill_id", "title", "game_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            raise ValueError("version must be major.minor.patch")
        for name in ("compatible_profiles", "prerequisites", "expected_outcome", "references", "steps"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not self.compatible_profiles:
            raise ValueError("explicit profile compatibility is required")
        for profile in self.compatible_profiles:
            _text(profile, "profile")
        if not self.prerequisites or not self.expected_outcome or not self.references:
            raise ValueError("prerequisites, expected outcome and source references are required")
        if any(not isinstance(c, SkillCheck) for c in self.prerequisites + self.expected_outcome):
            raise ValueError("invalid skill checks")
        if any(not isinstance(r, WorkflowReference) for r in self.references):
            raise ValueError("invalid workflow reference")
        if len(self.steps) > MAX_STEPS or any(not isinstance(s, SkillStep) for s in self.steps):
            raise ValueError("invalid or excessive steps")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise ValueError("timeout_seconds must be finite and bounded")
        if type(self.max_observations) is not int or not 2 <= self.max_observations <= MAX_OBSERVATIONS:
            raise ValueError("max_observations must be between 2 and 64")

    @property
    def definition_hash(self):
        return hashlib.sha256(_json(self.to_dict()).encode()).hexdigest()

    def to_dict(self):
        return {"schema_version": self.schema_version, "skill_id": self.skill_id,
                "version": self.version, "title": self.title, "game_id": self.game_id,
                "compatible_profiles": list(self.compatible_profiles),
                "prerequisites": [c.to_dict() for c in self.prerequisites],
                "expected_outcome": [c.to_dict() for c in self.expected_outcome],
                "references": [r.to_dict() for r in self.references],
                "steps": [s.to_dict() for s in self.steps],
                "timeout_seconds": self.timeout_seconds,
                "max_observations": self.max_observations}

    @classmethod
    def from_dict(cls, data):
        _keys(data, {"schema_version", "skill_id", "version", "title", "game_id",
                    "compatible_profiles", "prerequisites", "expected_outcome",
                    "references", "steps", "timeout_seconds", "max_observations"})
        copied = json.loads(_json(data))
        if "schema_version" not in copied:
            raise ValueError("schema_version is required")
        copied["compatible_profiles"] = tuple(copied["compatible_profiles"])
        for name in ("prerequisites", "expected_outcome"):
            copied[name] = tuple(SkillCheck.from_dict(c) for c in copied[name])
        refs = []
        for ref in copied["references"]:
            _keys(ref, {"source", "description"})
            refs.append(WorkflowReference(**ref))
        copied["references"] = tuple(refs)
        copied["steps"] = tuple(SkillStep.from_dict(s) for s in copied.get("steps", []))
        return cls(**copied)


class SkillCatalog:
    def __init__(self, definitions=()):
        self._definitions = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SkillDefinition):
        if not isinstance(definition, SkillDefinition):
            raise ValueError("validated SkillDefinition required")
        key = (definition.skill_id, definition.version)
        if key in self._definitions:
            raise ValueError("skill version already exists")
        self._definitions[key] = definition

    def get(self, skill_id: str, version: str) -> SkillDefinition:
        return self._definitions[(skill_id, version)]

    def list_skills(self):
        return [{**definition.to_dict(), "definition_hash": definition.definition_hash,
                 "reference_only": not bool(definition.steps)}
                for _, definition in sorted(self._definitions.items())]

    @classmethod
    def reference_catalog(cls):
        """Metadata only. Reuse sources; never import or duplicate game routes."""
        ready = SkillCheck("owner_authorized", True, "Current requested workflow is authorized")
        workflows = [
            ("godsarena.marathon", "GodsArena Marathon", ("godsarena-marathon",),
             (SkillCheck("daily_eligible", True, "Fresh eligibility receipt checked"),
              SkillCheck("mounted", True, "Observed mounted Fitness1 start")),
             (SkillCheck("finish_receipt_verified", True, "Actual finish time and reward reviewed"),),
             (WorkflowReference("plugins/gamelens-godsarena/skills/marathon/SKILL.md", "Saved supervised workflow"),
              WorkflowReference("profiles/godsarena/marathon-guide.json", "Existing calibrated route/profile"))),
            ("godsarena.open-clients", "Open requested GodsArena clients", ("godsarena-client-lifecycle",),
             (SkillCheck("requested_clients_identified", True, "Fresh identities prevent duplicate launches"),),
             (SkillCheck("requested_huds_verified", True, "Requested character HUDs and server verified"),),
             (WorkflowReference("skill://codex-home-skills/C:/Users/CHUBZ SERVER/.codex/skills/godsarena-open-clients/SKILL.md", "Existing owner launcher/login workflow"),)),
            ("godsarena.close-clients", "Normally close requested GodsArena clients", ("godsarena-client-lifecycle",),
             (SkillCheck("requested_clients_identified", True, "Fresh exact requested process/window identities"),),
             (SkillCheck("requested_processes_exited", True, "Original requested processes and windows absent"),),
             (WorkflowReference("skill://codex-home-skills/C:/Users/CHUBZ SERVER/.codex/skills/godsarena-close-clients/SKILL.md", "Existing owner normal close-and-verify workflow"),)),
        ]
        return cls(SkillDefinition(skill_id, "1.0.0", title, "godsarena", profiles,
                                   (ready,) + prerequisites, outcomes, references)
                   for skill_id, title, profiles, prerequisites, outcomes, references in workflows)


@dataclass(frozen=True)
class SkillObservation:
    client: str
    game_id: str
    profile_id: str
    identity: str  # trusted target/capture-session/geometry fingerprint
    observation_id: str
    frame_id: int
    facts_json: str

    def __post_init__(self):
        for name in ("client", "game_id", "profile_id", "identity", "observation_id"):
            _text(getattr(self, name), name)
        if type(self.frame_id) is not int or self.frame_id < 0:
            raise ValueError("frame_id must be a nonnegative integer")
        facts = json.loads(self.facts_json)
        if not isinstance(facts, dict):
            raise ValueError("observation facts must be an object")
        object.__setattr__(self, "facts_json", _json(facts))

    @classmethod
    def from_facts(cls, *, facts: dict, **provenance):
        return cls(**provenance, facts_json=_json(facts))

    @property
    def facts(self):
        return json.loads(self.facts_json)


class SkillCancelled(RuntimeError):
    pass


class SkillTimedOut(RuntimeError):
    pass


class RunBudget:
    """Trusted callbacks must honor this cooperative deadline/cancellation token."""
    def __init__(self, timeout, cancel=None, clock=time.monotonic):
        self._clock = clock
        self.deadline = clock() + timeout
        self.cancel = cancel if cancel is not None else threading.Event()

    def check(self):
        if self.cancel.is_set():
            raise SkillCancelled("skill cancelled")
        if self._clock() >= self.deadline:
            raise SkillTimedOut("skill deadline exceeded")

    @property
    def remaining(self):
        self.check()
        return max(0.0, self.deadline - self._clock())


class GuardedActionExecutor:
    """Existing guarded delegate plus fail-closed cancellation of its session.

    An active dispatch runs on one daemon callback thread. Cancellation, deadline,
    pending result or callback failure latches the selected GameLens supervisor
    off through its existing kill cleanup. The adapter is then permanently closed.
    It never resets a latch or performs raw input.
    """
    def __init__(self, gamelens):
        if not callable(getattr(gamelens, "submit_sequence", None)) or not callable(
                getattr(gamelens, "sequence_capacity", None)) or not callable(
                getattr(getattr(gamelens, "safety", None), "kill", None)):
            raise ValueError("existing guarded GameLens delegate and safety.kill are required")
        self._gamelens = gamelens
        self._admission = threading.Lock()
        self._closed = False
        self._active = None

    def _halt(self, reason):
        self._closed = True
        self._gamelens.safety.kill(f"game skill stopped: {reason}")

    def execute(self, step: SkillStep, observation: SkillObservation, budget: RunBudget):
        from gamelens.arbiter import parse_sequence
        from gamelens.input import Dwell

        budget.check()
        if not self._admission.acquire(blocking=False):
            raise ValueError("skill delegate has an outstanding callback")
        try:
            if self._closed or (self._active is not None and self._active.is_alive()):
                raise ValueError("skill delegate stopped; explicit session recovery required")
            parsed = parse_sequence(json.loads(step.sequence_json),
                                    capacity=self._gamelens.sequence_capacity())
            budget.check()
            duration = sum(s.seconds for s in parsed if isinstance(s, Dwell))
            if duration + 0.25 > budget.remaining:
                raise SkillTimedOut("remaining deadline cannot contain the guarded sequence")
            completed = threading.Event()
            answer = {}
            wait = min(0.5, budget.remaining)

            def submit():
                try:
                    answer["receipt"] = self._gamelens.submit_sequence(
                        observation_id=observation.observation_id, steps=parsed,
                        label=f"skill: {step.name}", source="agent", rebind=False,
                        wait=wait).to_dict()
                except BaseException as exc:
                    answer["error"] = exc
                finally:
                    completed.set()

            worker = threading.Thread(target=submit, name="gamelens-skill-dispatch", daemon=True)
            self._active = worker
            callback_deadline = time.monotonic() + duration + wait + 0.25
            worker.start()
            while True:
                try:
                    budget.check()
                except (SkillCancelled, SkillTimedOut) as exc:
                    self._halt(str(exc))
                    # Existing kill synchronously releases held inputs. Never wait
                    # indefinitely for an uncooperative callback to return.
                    completed.wait(0.1)
                    receipt = answer.get("receipt")
                    return receipt if receipt is not None else {
                        "verdict": "STOPPED", "outcome": "cancelled",
                        "detail": str(exc), "partial": True, "after_frame": None,
                    }
                if time.monotonic() >= callback_deadline:
                    self._halt('guarded callback exceeded bounded wait')
                    return {"verdict": "STOPPED", "outcome": "pending",
                            "detail": "guarded callback exceeded bounded wait",
                            "partial": True, "after_frame": None}
                if completed.wait(0.01):
                    break
            # Completion is signalled in finally just before thread exit.
            # Reap that bounded tail before admitting the next verified step.
            worker.join(timeout=0.05)
            if worker.is_alive():
                self._halt('guarded callback did not quiesce')
                return {"verdict": "STOPPED", "outcome": "pending",
                        "detail": "guarded callback did not quiesce",
                        "partial": True, "after_frame": None}
            if "error" in answer:
                self._halt("guarded callback failed")
                raise ValueError("guarded callback failed") from answer["error"]
            receipt = answer["receipt"]
            try:
                budget.check()
            except (SkillCancelled, SkillTimedOut) as exc:
                self._halt(str(exc))
                return receipt
            if receipt.get("outcome") == "pending":
                self._halt("guarded callback pending; no confirmed terminal outcome")
            return receipt
        finally:
            self._admission.release()

class SkillRunner:
    def __init__(self, observe: Callable[[RunBudget], SkillObservation],
                 executor: GuardedActionExecutor, *, clock=time.monotonic):
        if not isinstance(executor, GuardedActionExecutor):
            raise ValueError("a guarded GameLens executor is required")
        self._observe, self._executor, self._clock = observe, executor, clock

    def run(self, definition: SkillDefinition, *, client: str, game_id: str,
            profile_id: str, owner_authorized: bool = False, cancel=None):
        """Explicit trusted-host call. A queued/uncertain action is never replayed."""
        result = {"skill_id": definition.skill_id, "version": definition.version,
                  "definition_hash": definition.definition_hash, "client": client,
                  "status": "blocked", "detail": "", "completed_steps": 0,
                  "receipts": [], "observation_ids": []}

        def stop(status, detail):
            result.update(status=status, detail=detail)
            return result

        if owner_authorized is not True:
            return stop("blocked", "explicit current owner authorization required")
        if not client or game_id != definition.game_id or profile_id not in definition.compatible_profiles:
            return stop("blocked", "client/game/profile incompatible")
        if not definition.steps:
            return stop("reference_only", "consult source workflow; no executable steps registered")
        budget = RunBudget(definition.timeout_seconds, cancel, self._clock)
        identity = None

        def read():
            nonlocal identity
            budget.check()
            if len(result["observation_ids"]) >= definition.max_observations:
                raise SkillTimedOut("observation budget exhausted")
            observation = self._observe(budget)
            budget.check()
            if not isinstance(observation, SkillObservation):
                raise ValueError("trusted observation required")
            if (observation.client, observation.game_id, observation.profile_id) != (client, game_id, profile_id):
                raise ValueError("observation client/game/profile changed")
            if identity is not None and observation.identity != identity:
                raise ValueError("target/capture/geometry identity changed")
            identity = observation.identity
            result["observation_ids"].append(observation.observation_id)
            return observation

        def matches(checks, observation):
            return all(check.matches(observation.facts) for check in checks)

        try:
            current = read()
            if not matches(definition.prerequisites, current):
                return stop("blocked", "skill prerequisites not verified")
            for step in definition.steps:
                budget.check()
                if not matches(step.prerequisites, current):
                    return stop("blocked", f"step prerequisites not verified: {step.name}")
                dispatch = self._executor.execute(step, current, budget)
                result["receipts"].append(json.loads(_json(dispatch)))
                budget.check()
                if dispatch.get("outcome") != "sent" or dispatch.get("partial") is True or str(dispatch.get("verdict", "")).lower() != "ok":
                    return stop("uncertain" if dispatch.get("outcome") == "pending" else "denied",
                                "guarded dispatch did not confirm sent; inspect without retry")
                after_frame = dispatch.get("after_frame")
                if type(after_frame) is not int or after_frame < 0:
                    return stop("uncertain", "post-injection frame provenance missing; inspect without retry")
                floor = max(current.frame_id, after_frame)
                previous_id = current.observation_id
                while True:
                    next_observation = read()
                    if (next_observation.frame_id > floor
                            and next_observation.observation_id != previous_id
                            and matches(step.expected_outcome, next_observation)):
                        current = next_observation
                        break
                result["completed_steps"] += 1
            if not matches(definition.expected_outcome, current):
                return stop("outcome_unverified", "final expected outcome not verified")
            return stop("succeeded", "fresh expected outcome verified")
        except SkillCancelled as exc:
            return stop("cancelled", str(exc))
        except SkillTimedOut as exc:
            return stop("timed_out", str(exc))
        except Exception as exc:
            return stop("blocked", f"callback or validation failed: {type(exc).__name__}")
