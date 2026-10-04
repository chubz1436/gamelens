"""Fixture-only skills tests; the delegate never invokes native input."""
import json
import threading
import time
from types import SimpleNamespace
from dataclasses import replace

import pytest
from tests.test_rebind import lens  # fixture: no capture worker or real input

from gamelens.game_skills import (
    GuardedActionExecutor, RunBudget, SkillCatalog, SkillCheck, SkillDefinition,
    SkillObservation, SkillRunner, SkillStep, WorkflowReference, ProfilePin, GameLensSkillHost, profile_content_hash,
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
        profile_pins=(ProfilePin('fixture-profile', '1.0.0', 'a' * 64),),
    )


def observation(frame=1, **changes):
    data = dict(client='fixture-client', game_id='fixture-game', captured_at=time.monotonic(),
                profile_version='1.0.0', profile_hash='a' * 64,
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
    # Synthetic read publishes its fixture timestamp at callback time.
    return SkillRunner(lambda budget: replace(next(observations), captured_at=time.monotonic()), GuardedActionExecutor(gl), **options), gl


def run(subject, definition, **options):
    defaults = dict(client='fixture-client', game_id='fixture-game',
                    profile_id='fixture-profile', profile_version='1.0.0',
                    profile_hash='a' * 64, owner_authorized=True)
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




@pytest.mark.parametrize('options', [
    {'profile_version': '2.0.0'}, {'profile_hash': 'b' * 64},
    {'profile_version': ''}, {'profile_hash': ''},
])
def test_profile_pins_reject_same_id_changed_binding_before_observation(skill_definition, options):
    subject, gl = runner([])
    result = run(subject, skill_definition, **options)
    assert result['status'] == 'blocked'
    assert not gl.calls


@pytest.mark.parametrize('changes', [
    {'profile_version': '2.0.0'}, {'profile_hash': 'b' * 64},
])
def test_profile_pins_reject_same_id_changed_outcome_binding(skill_definition, changes):
    subject, gl = runner([observation(), observation(3, **changes)])
    result = run(subject, skill_definition)
    assert result['status'] == 'blocked'
    assert result['completed_steps'] == 0
    assert len(gl.calls) == 1


def test_unpinned_legacy_executable_definition_is_denied(skill_definition):
    raw = skill_definition.to_dict()
    raw.pop('profile_pins')
    with pytest.raises(ValueError, match='exact profile'):
        SkillDefinition.from_dict(raw)
    with pytest.raises(ValueError, match='exact profile'):
        replace(skill_definition, profile_pins=())
    raw['steps'] = []
    assert not SkillDefinition.from_dict(raw).profile_pins  # inert metadata only


def test_profile_pin_hash_changes_definition_and_default_references_are_portable(skill_definition):
    pin = ProfilePin('fixture-profile', '1.0.0', 'b' * 64)
    assert replace(skill_definition, profile_pins=(pin,)).definition_hash != skill_definition.definition_hash
    assert profile_content_hash({'a': 1, 'b': 2}) == profile_content_hash({'b': 2, 'a': 1})
    references = [r['source'] for e in SkillCatalog.reference_catalog().list_skills() for r in e['references']]
    assert 'installed-skill:godsarena-open-clients' in references
    assert 'installed-skill:godsarena-close-clients' in references
    assert all('C:/Users/' not in ref and 'CHUBZ SERVER' not in ref for ref in references)


def test_original_capture_timestamp_is_required():
    with pytest.raises(TypeError):
        SkillObservation.from_facts(client='fixture-client', game_id='fixture-game',
            profile_id='fixture-profile', identity='source', observation_id='original',
            frame_id=1, facts={'ready': True})


@pytest.mark.parametrize('age', [2.0, -1.0])
def test_stale_or_future_outcome_facts_cannot_be_restamped(skill_definition, age):
    gl = GameLensFixture()
    observations = iter([observation(), observation(3, captured_at=time.monotonic() - age)])
    subject = SkillRunner(lambda budget: next(observations), GuardedActionExecutor(gl))
    result = run(subject, skill_definition)
    assert result['status'] == 'blocked'
    assert result['completed_steps'] == 0
    assert len(gl.calls) == 1


@pytest.fixture
def skill_host_runtime(lens):
    from gamelens.app import GameLens
    from tests.test_rebind import ArrFrame
    lens.calls = []
    lens.kills = []
    lens.safety = SimpleNamespace(kill=lens.kills.append)
    lens.sequence_capacity = lambda: 10
    lens.capture.frames.latest_id = lambda: lens.capture.frames.frame.frame_id
    lens.arbiter._observation_deadline = 1.0  # synthetic fixture uses production defaults
    lens.arbiter._action_ttl = 0.8
    fake_executor = lens.arbiter._executor
    def accept_synthetic(seq):
        fake_executor.submitted.append(seq)
        seq.on_outcome(SimpleNamespace(status='sent', detail='', completed_steps=len(seq.steps),
            injected_steps=1, last_completed_step=len(seq.steps)-1, partial=False))
        return True
    fake_executor.submit = accept_synthetic
    def guarded_sequence(**call):
        lens.calls.append(call)
        result = GameLens.submit_sequence(lens, **call)  # real original guard path
        old = lens.capture.frames.frame
        lens.capture.frames.frame = ArrFrame(old.frame_id + 1, old.array.copy(), old.session_id)
        return result
    lens.submit_sequence = guarded_sequence
    return lens


def make_host(runtime, perceive=None, profile_provider=None):
    if perceive is None:
        perceive = lambda jpeg, original: {'ready': True, 'receipt': 'complete' if original.frame_id > 1 else 'pending'}
    return GameLensSkillHost(runtime, client='fixture-client', game_id='fixture-game',
        profile=ProfilePin('fixture-profile', '1.0.0', 'a' * 64), perceive=perceive,
        profile_provider=profile_provider)


def test_trusted_host_observation_preserves_real_full_frame_provenance(skill_host_runtime):
    runtime = skill_host_runtime
    observed = []
    def perceive(jpeg, original):
        assert jpeg.startswith(b'\xff\xd8')
        observed.append(original)
        return {'ready': True}
    host = make_host(runtime, perceive)
    result = host.observe(RunBudget(1))
    original = runtime.observations.resolve(result.observation_id)
    assert original is observed[0]
    assert result.frame_id == original.frame_id
    assert result.captured_at == original.frame_captured_at
    identity = json.loads(result.identity)
    assert identity['target_hwnd'] == runtime.capture.binding.hwnd
    assert identity['capture_session'] == runtime.capture.backend.session_id
    assert identity['geometry_generation'] == runtime.geometry.generation
    assert result.profile_hash == 'a' * 64 and result.profile_version == '1.0.0'
    assert not runtime.calls


def test_trusted_host_executes_user_defined_skill_via_real_guarded_runtime(skill_host_runtime, skill_definition):
    runtime = skill_host_runtime
    host = make_host(runtime)
    assert host.run(skill_definition)['status'] == 'blocked'
    assert runtime.calls == []
    result = host.run(skill_definition, owner_authorized=True)
    assert result['status'] == 'succeeded'
    assert result['completed_steps'] == 1
    assert len(runtime.calls) == 1
    assert runtime.calls[0]['observation_id'] == result['observation_ids'][0]
    assert runtime.calls[0]['rebind'] is False
    assert len(runtime.arbiter._executor.submitted) == 1
    assert not runtime.kills


def test_trusted_host_same_id_changed_loaded_profile_is_denied(skill_host_runtime, skill_definition):
    host = make_host(skill_host_runtime, profile_provider=lambda: ProfilePin('fixture-profile', '2.0.0', 'b' * 64))
    assert host.run(skill_definition, owner_authorized=True)['status'] == 'blocked'
    assert skill_host_runtime.calls == []


@pytest.mark.parametrize('change', ['profile', 'geometry', 'capture', 'future', 'stale'])
def test_trusted_host_rechecks_binding_after_perception(skill_host_runtime, skill_definition, change):
    runtime = skill_host_runtime
    active = [ProfilePin('fixture-profile', '1.0.0', 'a' * 64)]
    if change in {'future', 'stale'}:
        runtime.capture.frames.frame.captured_at = time.monotonic() + (1 if change == 'future' else -2)
    def perceive(jpeg, original):
        if change == 'profile':
            active[0] = ProfilePin('fixture-profile', '1.0.0', 'b' * 64)
        elif change == 'geometry':
            runtime.geometry.move()
        elif change == 'capture':
            runtime.capture.backend.retired = True
        return {'ready': True}
    host = make_host(runtime, perceive, profile_provider=lambda: active[0])
    assert host.run(skill_definition, owner_authorized=True)['status'] == 'blocked'
    assert runtime.calls == []


def test_trusted_host_rejects_same_id_profile_change_after_guarded_action(skill_host_runtime, skill_definition):
    runtime = skill_host_runtime
    active = [ProfilePin('fixture-profile', '1.0.0', 'a' * 64)]
    original_submit = runtime.submit_sequence
    def changed_profile_after_action(**call):
        receipt = original_submit(**call)
        active[0] = ProfilePin('fixture-profile', '1.0.0', 'b' * 64)
        return receipt
    runtime.submit_sequence = changed_profile_after_action
    host = make_host(runtime, profile_provider=lambda: active[0])
    result = host.run(skill_definition, owner_authorized=True)
    assert result['status'] == 'blocked'
    assert result['completed_steps'] == 0
    assert len(runtime.calls) == 1


def test_trusted_host_missing_frame_observation_honors_cancel(skill_host_runtime):
    from gamelens.game_skills import SkillCancelled
    from gamelens.server import NO_FRAME
    cancel = threading.Event()
    def no_frame(**options):
        cancel.set()
        return NO_FRAME
    skill_host_runtime.encode_frame = no_frame
    host = make_host(skill_host_runtime)
    with pytest.raises(SkillCancelled):
        host.observe(RunBudget(1, cancel))
    assert skill_host_runtime.calls == []
