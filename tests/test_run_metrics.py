"""Pure evidence tests: no live window or input operations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gamelens.run_metrics import RunMetrics


@pytest.fixture
def clock():
    return [100.0]


@pytest.fixture
def metrics(clock):
    return RunMetrics(monotonic=lambda: clock[0], utcnow=lambda: datetime(2026, 10, 4, tzinfo=timezone.utc))


def test_observation_provenance_freshness_and_detached_copy(metrics, clock):
    run = metrics.begin_run()
    observation = SimpleNamespace(frame_id=8, frame_captured_at=99.5, backend_session_id=3,
                                  frame_width=1280, scale=0.5, jpeg=b'not retained', token='secret')
    event = metrics.record_observation(run, 'observation-8', observation, freshness_limit=0.8)
    assert event['frame_age_ms'] == 500
    assert event['fresh'] is True
    assert event['provenance']['scale'] == 0.5
    assert 'jpeg' not in event['provenance'] and 'token' not in event['provenance']
    observation.frame_id = 99
    event['provenance']['frame_id'] = 77
    assert metrics.snapshot(run)['events'][0]['provenance']['frame_id'] == 8
    clock[0] = 102
    stale = metrics.record_observation(run, 'stale', observation, freshness_limit=0.8)
    assert stale['fresh'] is False


def test_unknown_freshness_is_explicit(metrics):
    run = metrics.begin_run()
    event = metrics.record_observation(run, 'empty', {})
    assert event['frame_age_ms'] is None and event['fresh'] is None


def test_pending_terminal_latency_and_objective_are_separate(metrics, clock):
    run = metrics.begin_run()
    metrics.record_action(run, 17, observation_id='obs', kind='click')
    clock[0] += 0.25
    pending = metrics.record_outcome(run, 17, 'pending', verdict='ok')
    assert pending['terminal'] is False and pending['latency_ms'] == 250
    clock[0] += 0.25
    sent = metrics.record_outcome(run, 17, 'sent', injected_steps=1)
    assert sent['terminal'] is True and sent['objective_success'] is None
    assert sent['latency_ms'] == 500 and sent['action_id'] == '17'
    report = metrics.record_objective(run, 'reached-target', False, evidence=['17', 'obs'])
    assert report['objective_success'] is False
    metrics.finish_run(run, status='completed', objective_success=False)
    snapshot = metrics.snapshot(run)
    assert snapshot['status'] == 'completed' and snapshot['objective_success'] is False
    assert snapshot['ended_at_utc'].endswith('Z')
    assert metrics.finish_run(run) is None


def test_retry_is_an_explicit_reference(metrics):
    run = metrics.begin_run()
    metrics.record_action(run, 1)
    retry = metrics.record_action(run, 2, attempt=2, retry_of=1)
    assert retry['attempt'] == 2 and retry['retry_of'] == '1'
    for fields in ({'attempt': 2}, {'attempt': 1, 'retry_of': 1}, {'attempt': 2, 'retry_of': 2}):
        with pytest.raises(ValueError):
            metrics.record_action(run, 2, **fields)
    assert metrics.snapshot(run)['event_count'] == 2


def test_evicted_action_has_unknown_latency(clock):
    metrics = RunMetrics(max_events=1, monotonic=lambda: clock[0])
    run = metrics.begin_run()
    metrics.record_action(run, 1)
    metrics.record_action(run, 2)
    assert metrics.record_outcome(run, 1, 'sent')['latency_ms'] is None
    snapshot = metrics.snapshot(run)
    assert len(snapshot['events']) == 1 and snapshot['dropped_events'] == 2
    assert snapshot['counts'] == {'action': 2, 'outcome': 1}


def test_run_capacity_and_idle_age(clock):
    metrics = RunMetrics(max_runs=2, max_age_seconds=5, monotonic=lambda: clock[0])
    old = metrics.begin_run()
    metrics.begin_run()
    latest = metrics.begin_run()
    assert metrics.snapshot(old) is None and len(metrics.runs()) == 2
    clock[0] += 6
    assert metrics.snapshot(latest) is None and metrics.runs() == []
    assert metrics.record_action(latest, 1) is None


def test_monotonic_latency_does_not_depend_on_wall_clock(clock):
    wall = [datetime(2026, 10, 4, tzinfo=timezone.utc)]
    metrics = RunMetrics(monotonic=lambda: clock[0], utcnow=lambda: wall[0])
    run = metrics.begin_run()
    metrics.record_action(run, 1)
    wall[0] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    clock[0] += 0.1
    assert metrics.record_outcome(run, 1, 'dry')['latency_ms'] == 100


def test_video_reference_drops_directory_and_never_opens_media(metrics):
    run = metrics.begin_run()
    event = metrics.link_video(run, 'clip', r'C:\Users\private\GameLens-clip.mp4', start_seconds=1, end_seconds=3)
    assert event['reference'] == 'GameLens-clip.mp4'
    with pytest.raises(ValueError):
        metrics.link_video(run, 'bad', 'image.png')
    with pytest.raises(ValueError):
        metrics.link_video(run, 'bad', 'clip.mp4', start_seconds=3, end_seconds=1)


def test_sink_failure_cannot_change_evidence_or_escape(metrics):
    class BrokenSink:
        def save(self, snapshot):
            raise OSError('must not be exposed')
    metrics._sink = BrokenSink()
    run = metrics.safe_call('begin_run')
    metrics.safe_call('record_action', run, 1)
    metrics.safe_call('record_outcome', run, 1, 'dry')
    assert metrics.snapshot(run)['events'][-1]['outcome'] == 'dry'
    assert metrics.snapshot()['recording_errors'] == 3
    assert metrics.safe_call('record_objective', run, 'goal', None) is None
    assert metrics.snapshot()['recording_errors'] == 4


def test_concurrent_writers_have_unique_ordered_sequence_numbers():
    metrics = RunMetrics(max_events=80)
    run = metrics.begin_run()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: metrics.record_action(run, i), range(80)))
    snapshot = metrics.snapshot(run)
    assert [event['sequence'] for event in snapshot['events']] == list(range(1, 81))
    assert snapshot['event_count'] == snapshot['counts']['action'] == 80


def test_reject_unbounded_or_invalid_values(metrics):
    run = metrics.begin_run()
    with pytest.raises(ValueError):
        metrics.record_action(run, 'x'*129)
    with pytest.raises(ValueError):
        metrics.record_outcome(run, 1, 'success')
    with pytest.raises(ValueError):
        metrics.record_observation(run, 'bad', {'frame_captured_at': float('nan')})
    with pytest.raises(ValueError):
        metrics.record_objective(run, 'goal', True, evidence=['x']*33)
    for kwargs in ({'max_runs': 0}, {'max_events': True}, {'max_age_seconds': -1}):
        with pytest.raises(ValueError):
            RunMetrics(**kwargs)


def test_urlsafe_observation_and_view_identifiers(metrics):
    run = metrics.begin_run(run_id='run-Ab_c-9')
    metrics.record_observation(run, 'view-Ab_c-9', {})
    event = metrics.record_action(run, 'request-Ab_c-9', observation_id='view-Ab_c-9', log_id=41)
    assert event['observation_id'] == 'view-Ab_c-9' and event['log_id'] == '41'


def test_late_pending_does_not_override_observed_completion(metrics):
    run = metrics.begin_run()
    metrics.record_action(run, 1)
    metrics.record_outcome(run, 1, 'sent')
    assert metrics.record_outcome(run, 1, 'pending') is None
    assert metrics.snapshot(run)['events'][-1]['outcome'] == 'sent'


def test_future_frame_timestamp_never_reports_fresh(metrics, clock):
    run = metrics.begin_run()
    for limit in (None, 1.0):
        event = metrics.record_observation(run, 'future', {'frame_captured_at': clock[0]+1}, freshness_limit=limit)
        assert event['frame_age_ms'] == -1000 and event['fresh'] is False


def test_action_preserves_original_and_executed_observation_ids(metrics):
    run = metrics.begin_run()
    original = metrics.record_observation(run, 'original', {'frame_id': 1})
    bound = metrics.record_observation(run, 'action-source-41', {'frame_id': 2}, source='action')
    event = metrics.record_action(run, 41, observation_id=original['observation_id'],
                                  bound_observation_id=bound['observation_id'], log_id=7)
    assert event['observation_id'] == 'original'
    assert event['bound_observation_id'] == 'action-source-41' and event['log_id'] == '7'


def test_sub_microsecond_future_timestamp_is_not_hidden_by_rounding(metrics, clock):
    run = metrics.begin_run()
    event = metrics.record_observation(run, 'slightly-future', {'frame_captured_at': clock[0]+0.0000001}, freshness_limit=1)
    assert event['fresh'] is False
