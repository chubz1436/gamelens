"""Fixture-only skills tests; the delegate never invokes native input."""
import json
import threading
import time
from types import SimpleNamespace
from dataclasses import replace

import pytest

from gamelens.game_skills import (
    GuardedActionExecutor, RunBudget, SkillCatalog, SkillCheck, SkillDefinition,
    SkillObservation, SkillRunner, SkillStep, WorkflowReference,
)


@pytest.fixture
def skill_definition():
    ready = SkillCheck('ready', True, 'Fresh expected menu visible')
    changed = SkillCheck('receipt', 'complete', 'Actual completion receipt visible')
    return SkillDefinition(
        'fixture.complete', '1.0.0', 'Fixture completion', 'fixture-game',
        ('fixture-profile',), (ready,), (changed,),
        (WorkflowReference('tests/test_game_skills.py', 'Synthetic fixture only'),),
        (SkillStep('one decision', json.dumps([{'do': 'tap', 'key': 'w', 'ms': 20}]),
                   (ready,), (changed,)),), max_observations=4,
    )


def observation(frame=1, **changes):
    data = dict(client='fixture-client', game_id='fixture-game',
                profile_id='fixture-profile', identity='target/capture/geometry-A',
                observation_id=f'obs-{frame}', frame_id=frame,
                facts={'ready': True, 'receipt': 'complete' if frame > 1 else 'pending'})
    data.update(changes)
    return SkillObservation.from_facts(**data)


class DispatchFixture:
    def __init__(self, **receipt):
        self.receipt = dict(verdict='ok', outcome='sent', after_frame=2, partial=False)
        self.receipt.update(receipt)

    def to_dict(self):
        return dict(self.receipt)


class GameLensFixture:
    """The host's trusted guarded delegate is stubbed, never native input."""
    def __init__(self, **receipt):
        self.dispatch = DispatchFixture(**receipt)
        self.calls = []
        self.kills = []
        self.safety = SimpleNamespace(kill=self.kills.append)

    def sequence_capacity(self):
        return 10

    def submit_sequence(self, **call):
        self.calls.append(call)
        return self.dispatch


def runner(frames, delegate=None, clock=None):
    observations = iter(frames)
    gl = delegate or GameLensFixture()
    options = {'clock': clock} if clock else {}
    return SkillRunner(lambda budget: next(observations), GuardedActionExecutor(gl), **options), gl


def run(subject, definition, **options):
    defaults = dict(client='fixture-client', game_id='fixture-game',
                    profile_id='fixture-profile', owner_authorized=True)
    defaults.update(options)
    return subject.run(definition, **defaults)


def test_roundtrip_hash_and_immutable_definition(skill_definition):
    raw = skill_definition.to_dict()
    copied = SkillDefinition.from_dict(raw)
    assert copied.definition_hash == skill_definition.definition_hash
    raw['steps'][0]['sequence'][0]['key'] = 'a'
    assert copied.to_dict()['steps'][0]['sequence'][0]['key'] == 'w'
    assert replace(copied, version='1.0.1').definition_hash != copied.definition_hash
    facts = {'ready': True}
    obs = observation(facts=facts)
    facts['ready'] = False
    obs.facts['ready'] = False
    assert obs.facts['ready'] is True


@pytest.mark.parametrize('changes', [
    {'schema_version': 2}, {'schema_version': True}, {'version': 'latest'},
    {'timeout_seconds': 0}, {'timeout_seconds': float('inf')},
    {'timeout_seconds': 3601}, {'max_observations': 100},
    {'compatible_profiles': []}, {'expected_outcome': []}, {'prerequisites': []},
    {'references': []}, {'automatic_run': True},
])
def test_invalid_definitions_fail_closed(skill_definition, changes):
    data = skill_definition.to_dict()
    data.update(changes)
    with pytest.raises((ValueError, TypeError)):
        SkillDefinition.from_dict(data)


def test_schema_version_required(skill_definition):
    data = skill_definition.to_dict()
    data.pop('schema_version')
    with pytest.raises(ValueError):
        SkillDefinition.from_dict(data)


def test_checks_require_exact_type_and_present_fact():
    check = SkillCheck('ready', True, 'Verified ready')
    assert not check.matches({})
    assert not check.matches({'ready': 1})
    assert not check.matches({'ready': 'true'})
    assert check.matches({'ready': True})


def test_reference_catalog_reuses_sources_and_never_dispatches():
    catalog = SkillCatalog.reference_catalog()
    entries = catalog.list_skills()
    assert len(entries) == 3
    assert all(e['reference_only'] and not e['steps'] for e in entries)
    assert any('marathon-guide.json' in r['source'] for e in entries for r in e['references'])
    subject, gl = runner([])
    definition = catalog.get('godsarena.marathon', '1.0.0')
    assert run(subject, definition, game_id='godsarena', profile_id='godsarena-marathon')['status'] == 'reference_only'
    assert gl.calls == []
    entries[0]['title'] = 'mutated'
    assert catalog.list_skills()[0]['title'] != 'mutated'
    with pytest.raises(ValueError):
        catalog.register(definition)


@pytest.mark.parametrize('options', [
    {'owner_authorized': False}, {'owner_authorized': 'true'},
    {'client': ''}, {'game_id': 'other-game'}, {'profile_id': 'other-profile'},
])
def test_authority_and_compatibility_before_any_callback(skill_definition, options):
    subject, gl = runner([])
    assert run(subject, skill_definition, **options)['status'] == 'blocked'
    assert not gl.calls


def test_guarded_callback_and_fresh_outcome_required(skill_definition):
    # Frame2 has the expected result but is not newer than the injection stamp.
    subject, gl = runner([observation(), observation(2), observation(3)])
    result = run(subject, skill_definition)
    assert result['status'] == 'succeeded'
    assert result['completed_steps'] == 1
    assert result['observation_ids'] == ['obs-1', 'obs-2', 'obs-3']
    assert len(gl.calls) == 1
    assert gl.calls[0]['observation_id'] == 'obs-1'
    assert gl.calls[0]['source'] == 'agent'
    assert gl.calls[0]['rebind'] is False
    assert gl.calls[0]['wait'] <= 0.5
    # Uses the existing parser, not JSON instructions handed to raw input.
    assert all(not isinstance(step, dict) for step in gl.calls[0]['steps'])


@pytest.mark.parametrize('receipt,status', [
    ({'verdict': 'NOT_FOREGROUND', 'outcome': 'denied'}, 'denied'),
    ({'outcome': 'pending'}, 'uncertain'), ({'outcome': 'dry'}, 'denied'),
    ({'outcome': 'cancelled', 'partial': True}, 'denied'),
    ({'after_frame': None}, 'uncertain'), ({'after_frame': True}, 'uncertain'),
])
def test_refused_uncertain_partial_and_unstamped_actions_never_retry(skill_definition, receipt, status):
    subject, gl = runner([observation()], GameLensFixture(**receipt))
    result = run(subject, skill_definition)
    assert result['status'] == status
    assert result['completed_steps'] == 0
    assert len(gl.calls) == 1
    assert len(result['receipts']) == 1


@pytest.mark.parametrize('changes', [
    {'client': 'other-client'}, {'profile_id': 'other-profile'},
    {'game_id': 'other-game'}, {'identity': 'changed-capture'},
])
def test_changed_provenance_stops_after_dispatch(skill_definition, changes):
    subject, gl = runner([observation(), observation(3, **changes)])
    assert run(subject, skill_definition)['status'] == 'blocked'
    assert len(gl.calls) == 1


def test_unverified_prerequisites_never_dispatch(skill_definition):
    subject, gl = runner([observation(facts={'ready': False})])
    assert run(subject, skill_definition)['status'] == 'blocked'
    assert not gl.calls


def test_sent_does_not_prove_outcome_or_freshness(skill_definition):
    subject, gl = runner([observation()] * 4)
    result = run(subject, skill_definition)
    assert result['status'] == 'timed_out'
    assert result['completed_steps'] == 0
    assert len(gl.calls) == 1


def test_step_receipt_does_not_override_final_outcome(skill_definition):
    definition = replace(skill_definition, expected_outcome=(SkillCheck('reward_verified', True, 'Reward receipt reviewed'),))
    subject, gl = runner([observation(), observation(3)])
    result = run(subject, definition)
    assert result['status'] == 'outcome_unverified'
    assert result['completed_steps'] == 1
    assert len(gl.calls) == 1


def test_cancellation_before_dispatch_and_after_observation(skill_definition):
    cancel = threading.Event()
    cancel.set()
    subject, gl = runner([])
    assert run(subject, skill_definition, cancel=cancel)['status'] == 'cancelled'
    assert not gl.calls
    cancel.clear()
    def observe(budget):
        cancel.set()
        return observation()
    subject = SkillRunner(observe, GuardedActionExecutor(gl))
    assert run(subject, skill_definition, cancel=cancel)['status'] == 'cancelled'
    assert not gl.calls


def test_observer_deadline_is_checked_before_dispatch(skill_definition):
    now = [0.0]
    gl = GameLensFixture()
    def observe(budget):
        now[0] = budget.deadline
        return observation()
    subject = SkillRunner(observe, GuardedActionExecutor(gl), clock=lambda: now[0])
    assert run(subject, skill_definition)['status'] == 'timed_out'
    assert not gl.calls


def test_remaining_deadline_cannot_queue_a_longer_action(skill_definition):
    subject, gl = runner([observation()])
    assert run(subject, replace(skill_definition, timeout_seconds=0.1))['status'] == 'timed_out'
    assert not gl.calls


def test_malformed_sequence_rejected_by_existing_parser_before_dispatch(skill_definition):
    step = replace(skill_definition.steps[0], sequence_json=json.dumps([{'do': 'tap', 'key': 'alt'}]))
    subject, gl = runner([observation()])
    assert run(subject, replace(skill_definition, steps=(step,)))['status'] == 'blocked'
    assert not gl.calls


def test_raw_callback_cannot_replace_guarded_delegate():
    with pytest.raises(ValueError):
        GuardedActionExecutor(lambda *args: None)
    with pytest.raises(ValueError):
        SkillRunner(lambda budget: observation(), lambda *args: None)


def test_callback_exception_fails_closed(skill_definition):
    gl = GameLensFixture()
    def observe(budget):
        raise RuntimeError('fixture capture failure')
    subject = SkillRunner(observe, GuardedActionExecutor(gl))
    assert run(subject, skill_definition)['status'] == 'blocked'
    assert not gl.calls


def test_cancel_during_dispatch_retains_receipt_and_prevents_next_decision(skill_definition):
    cancel = threading.Event()
    class CancellingGameLens(GameLensFixture):
        def submit_sequence(self, **call):
            receipt = super().submit_sequence(**call)
            cancel.set()
            return receipt
    subject, gl = runner([observation()], CancellingGameLens())
    result = run(subject, skill_definition, cancel=cancel)
    assert result['status'] == 'cancelled'
    assert len(result['receipts']) == 1
    assert result['receipts'][0]['outcome'] == 'sent'
    assert len(gl.calls) == 1



@pytest.mark.parametrize('reason', ['cancel', 'timeout'])
def test_running_sequence_is_killed_released_and_cannot_press_later(skill_definition, monkeypatch, reason):
    """Real supervisor cleanup/InputExecutor; SendInput is replaced entirely."""
    from gamelens import input as gl_input
    from gamelens.input import InputExecutor, Sequence, KEYEVENTF_KEYUP
    from gamelens.safety import SafetySupervisor, Denial, NotPermitted

    cancel = threading.Event()
    now = [0.0]
    events = []
    pressed = threading.Event()
    finished = threading.Event()
    supervisor = SafetySupervisor(1, dry_run=False)
    # Synthetic safety approval only; no watchdog, Arm/Live or real window.
    monkeypatch.setattr(supervisor, 'check', lambda **kw: Denial.KILLED if supervisor.killed else Denial.OK)
    executor = InputExecutor(supervisor)

    def send_fixture(inputs):
        for event in inputs:
            events.append((event.ki.wVk, event.ki.dwFlags))
            if not event.ki.dwFlags & KEYEVENTF_KEYUP:
                pressed.set()
    monkeypatch.setattr(gl_input, '_send', send_fixture)

    class ExecutingGameLens(GameLensFixture):
        def __init__(self):
            super().__init__()
            self.safety = supervisor
        def submit_sequence(self, **call):
            self.calls.append(call)
            seq = Sequence(call['steps'])
            try:
                status = executor._execute(seq)
            except NotPermitted:
                status = 'denied'
            finally:
                finished.set()
            return DispatchFixture(outcome=status, partial=seq.injected_steps > 0 and status != 'sent')

    def interrupt():
        if pressed.wait(1):
            # Trigger inside the dwell, after the first synthetic key-down.
            time.sleep(0.03)
            if reason == 'cancel':
                cancel.set()
            else:
                now[0] = 1.0
    interrupter = threading.Thread(target=interrupt, daemon=True)
    interrupter.start()
    step = replace(skill_definition.steps[0], sequence_json=json.dumps([
        {'do': 'key_down', 'key': 'w'}, {'do': 'wait', 'ms': 300},
        {'do': 'tap', 'key': 'a', 'ms': 20}, {'do': 'key_up', 'key': 'w'},
    ]))
    definition = replace(skill_definition, steps=(step,), timeout_seconds=1.0)
    subject, gl = runner([observation(), observation()], ExecutingGameLens(), clock=lambda: now[0])
    result = run(subject, definition, cancel=cancel)
    interrupter.join(1)
    assert finished.wait(1)
    assert result['status'] == ('cancelled' if reason == 'cancel' else 'timed_out')
    assert supervisor.killed
    assert not supervisor.armed
    assert len(events) == 2  # W down, existing cleanup W up; no A down.
    assert not events[0][1] & KEYEVENTF_KEYUP
    assert events[1][1] & KEYEVENTF_KEYUP
    assert executor.snapshot()['pressed_keys'] == []
    assert executor.snapshot()['unreleased'] == []
    cancel.clear()
    now[0] = 0.0
    # Even after the callback settles, this stopped adapter cannot dispatch again.
    assert run(subject, definition)['status'] == 'blocked'
    assert len(gl.calls) == 1


def test_pending_kills_selected_delegate_and_cannot_run_again(skill_definition):
    subject, gl = runner([observation(), observation()], GameLensFixture(outcome='pending'))
    assert run(subject, skill_definition)['status'] == 'uncertain'
    assert len(gl.kills) == 1
    assert run(subject, skill_definition)['status'] == 'blocked'
    assert len(gl.calls) == 1


def test_uncooperative_callback_has_bounded_wait_and_closed_adapter(skill_definition):
    release = threading.Event()
    finished = threading.Event()
    class StuckGameLens(GameLensFixture):
        def submit_sequence(self, **call):
            self.calls.append(call)
            release.wait(2)
            finished.set()
            return self.dispatch
    subject, gl = runner([observation(), observation()], StuckGameLens())
    started = time.monotonic()
    try:
        result = run(subject, skill_definition)
        assert result['status'] == 'uncertain'
        assert time.monotonic() - started < 1.5
        assert len(gl.kills) == 1
        assert run(subject, skill_definition)['status'] == 'blocked'
        assert len(gl.calls) == 1
    finally:
        release.set()
        assert finished.wait(1)




def test_valid_long_guarded_sequence_uses_duration_in_callback_bound(skill_definition):
    class SlowGuardedGameLens(GameLensFixture):
        def submit_sequence(self, **call):
            self.calls.append(call)
            # The real GameLens dispatcher waits expected duration + wait.
            time.sleep(1.0)
            return self.dispatch
    step = replace(skill_definition.steps[0], sequence_json=json.dumps([
        {'do': 'tap', 'key': 'w', 'ms': 1000},
    ]))
    definition = replace(skill_definition, steps=(step,), timeout_seconds=3.0)
    subject, gl = runner([observation(), observation(3)], SlowGuardedGameLens())
    assert run(subject, definition)['status'] == 'succeeded'
    assert len(gl.calls) == 1
    assert gl.kills == []


def test_next_decision_only_after_verified_outcome_and_callback_quiescence(skill_definition):
    definition = replace(skill_definition, steps=skill_definition.steps * 2)
    subject, gl = runner([observation(), observation(3), observation(5)])
    result = run(subject, definition)
    assert result['status'] == 'succeeded'
    assert result['completed_steps'] == 2
    assert [call['observation_id'] for call in gl.calls] == ['obs-1', 'obs-3']
    assert gl.kills == []
