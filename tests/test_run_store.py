"""Local persistence tests use only pytest-owned temporary directories."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

from gamelens.run_metrics import RunMetrics
from gamelens.run_store import LocalRunStore


def make_snapshot():
    metrics = RunMetrics()
    run = metrics.begin_run()
    metrics.record_observation(run, 'obs', {'frame_id': 3, 'frame_captured_at': 1}, freshness_limit=1)
    metrics.record_action(run, 1, observation_id='obs')
    metrics.record_outcome(run, 1, 'sent', partial=True)
    metrics.record_objective(run, 'goal', False, evidence=['obs', '1'])
    metrics.link_video(run, 'clip', 'clip.mp4')
    return metrics.snapshot(run)


def test_default_disabled_has_no_filesystem_side_effects(tmp_path):
    base = tmp_path / 'does-not-exist'
    store = LocalRunStore(base)
    assert store.save(make_snapshot()) == {'saved': False, 'reason': 'disabled'}
    assert store.snapshots() == []
    assert not base.exists()
    assert store.status()['enabled'] is False


def test_atomic_roundtrip_and_no_payload_media_or_credentials(tmp_path):
    snapshot = make_snapshot()
    store = LocalRunStore(tmp_path, enabled=True)
    result = store.save(snapshot)
    assert result['saved'] is True and result['file'].startswith('run-')
    assert store.snapshots() == [snapshot]
    assert [p.suffix for p in store.directory.iterdir()] == ['.json']
    stored = json.loads((store.directory / result['file']).read_text())
    assert set(stored) == {'format', 'schema_version', 'run'}
    assert stored['format'] == 'gamelens-run-metrics'
    snapshot['events'].append({'token': 'must-not-persist'})
    assert store.save(snapshot)['reason'] == 'invalid_metadata'
    assert len(store.snapshots()[0]['events']) == 5


def test_reject_nested_unknown_fields_and_nonfinite_metadata(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True)
    snapshot = make_snapshot()
    snapshot['events'][0]['provenance']['password'] = 'secret'
    assert store.save(snapshot)['saved'] is False
    assert not store.directory.exists()
    snapshot = make_snapshot()
    snapshot['elapsed_ms'] = float('inf')
    assert store.save(snapshot)['reason'] == 'invalid_metadata'


def test_file_and_event_limits(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True, max_file_bytes=100, max_bytes=100)
    assert store.save(make_snapshot())['reason'] == 'file_limit'
    assert not store.directory.exists()
    store = LocalRunStore(tmp_path, enabled=True, max_events=1)
    assert store.save(make_snapshot())['reason'] == 'invalid_metadata'


def test_file_count_retention_only_deletes_owned_metadata(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True, max_files=2)
    first = make_snapshot()
    first_file = store.save(first)['file']
    store.save(make_snapshot())
    store.save(make_snapshot())
    assert len(list(store.directory.iterdir())) == 2
    assert not (store.directory / first_file).exists()
    foreign = store.directory / 'owner-notes.txt'
    foreign.write_text('keep me')
    assert store.save(make_snapshot())['saved'] is True
    assert foreign.read_text() == 'keep me'
    assert len(list(store.directory.iterdir())) == 2


def test_total_bytes_retention(tmp_path):
    probe = LocalRunStore(tmp_path / 'probe', enabled=True)
    size = probe.save(make_snapshot())['bytes']
    store = LocalRunStore(tmp_path / 'actual', enabled=True, max_bytes=size+64, max_file_bytes=size+64)
    assert store.save(make_snapshot())['saved'] is True
    assert store.save(make_snapshot())['saved'] is True
    files = list(store.directory.iterdir())
    assert len(files) == 1 and sum(p.stat().st_size for p in files) <= size+64


def test_age_retention_and_read_filter(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True, max_age_seconds=10)
    old = store.save(make_snapshot())['file']
    path = store.directory / old
    os.utime(path, (1, 1))
    assert store.snapshots() == []
    assert store.save(make_snapshot())['saved'] is True
    assert not path.exists()


def test_foreign_and_malformed_files_are_never_removed(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True, max_files=1)
    store.directory.mkdir()
    foreign = store.directory / ('run-' + '0'*64 + '.json')
    foreign.write_text('{"token":"foreign"}')
    result = store.save(make_snapshot())
    assert result == {'saved': False, 'reason': 'storage_unavailable'}
    assert foreign.read_text() == '{"token":"foreign"}'
    assert store.snapshots() == []


def test_refuse_replacing_an_unowned_matching_filename(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True)
    snapshot = make_snapshot()
    store.directory.mkdir()
    foreign = store.directory / store._filename(snapshot['run_id'])
    foreign.write_text('owner data')
    assert store.save(snapshot)['saved'] is False
    assert foreign.read_text() == 'owner data'


def test_storage_error_is_fixed_and_temporary_file_cleaned(tmp_path, monkeypatch):
    store = LocalRunStore(tmp_path, enabled=True)
    def fail(*args):
        raise OSError('private pathname or credential-like details')
    monkeypatch.setattr(os, 'replace', fail)
    assert store.save(make_snapshot()) == {'saved': False, 'reason': 'storage_unavailable'}
    assert list(store.directory.iterdir()) == []


def test_atomic_replace_failure_preserves_existing_snapshot(tmp_path, monkeypatch):
    store = LocalRunStore(tmp_path, enabled=True)
    snapshot = make_snapshot()
    assert store.save(snapshot)['saved'] is True
    original = store.snapshots()
    snapshot['status'] = 'completed'
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(os, 'replace', fail)
    assert store.save(snapshot)['saved'] is False
    assert store.snapshots() == original
    assert len(list(store.directory.iterdir())) == 1


def test_directory_symlink_is_refused_where_available(tmp_path):
    import pytest
    destination = tmp_path / 'outside'
    destination.mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(destination, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('Host does not permit symlinks')
    store = LocalRunStore(link, enabled=True)
    assert store.save(make_snapshot())['saved'] is False
    assert list(destination.iterdir()) == []


def test_concurrent_saves_leave_valid_bounded_snapshots(tmp_path):
    store = LocalRunStore(tmp_path, enabled=True, max_files=4)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(store.save, [make_snapshot() for _ in range(12)]))
    assert all(result['saved'] for result in results)
    assert len(store.snapshots()) == 4
    assert len(list(store.directory.iterdir())) == 4
