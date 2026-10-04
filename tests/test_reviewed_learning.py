import json
import pytest
from gamelens.reviewed_learning import LearningDenied, ReviewedLearningStore

def propose(store, hint='Check resulting screen'):
    return store.propose('demo', {'outcome': 'failed', 'evidence': {'receipt': 'no effect'}}, {'verification_hint': hint}, {'passed': True, 'evidence': {'test': 'fixture verified'}})

def test_review_exact_hash_before_activation():
    s = ReviewedLearningStore()
    p = propose(s)
    with pytest.raises(LearningDenied):
        s.activate(p['id'], p['candidate_hash'], 'owner')
    with pytest.raises(LearningDenied):
        s.review(p['id'], 'wrong', 'approve', 'owner')
    s.review(p['id'], p['candidate_hash'], 'approve', 'owner')
    with pytest.raises(LearningDenied):
        s.activate(p['id'], p['candidate_hash'], 'other')
    assert s.activate(p['id'], p['candidate_hash'], 'owner')['active']
    s.review(p['id'], p['candidate_hash'], 'reject', 'owner')
    assert not s.active_candidates()
    with pytest.raises(LearningDenied):
        s.activate(p['id'], p['candidate_hash'], 'owner')

def test_evidence_and_candidate_are_immutable_snapshots():
    s = ReviewedLearningStore()
    failure = {'outcome': 'failed', 'evidence': {'receipt': 'failure'}}
    p = s.propose('demo', failure, {'description': 'Observe'}, {'passed': True, 'evidence': 'fixture'})
    failure['evidence']['receipt'] = 'changed'
    p['proposal']['candidate']['description'] = 'changed'
    assert s.get(p['id'])['proposal']['candidate']['description'] == 'Observe'
    assert s.get(p['id'])['proposal']['failure_evidence']['evidence']['receipt'] == 'failure'
    assert propose(s)['candidate_hash'] != propose(s, 'Different hint')['candidate_hash']

@pytest.mark.parametrize('candidate', [{'safety_policy': 'disable'}, {'code': 'exec'}, {'actions': []}, {}, {'description': 1}])
def test_non_advisory_changes_denied(candidate):
    with pytest.raises(LearningDenied):
        ReviewedLearningStore().propose('demo', {'outcome': 'failed', 'evidence': 'receipt'}, candidate, {'passed': True, 'evidence': 'fixture'})

@pytest.mark.parametrize('failure,tests', [({'outcome': 'sent', 'evidence': 'receipt'}, {'passed': True, 'evidence': 'fixture'}), ({'outcome': 'failed'}, {'passed': True, 'evidence': 'fixture'}), ({'outcome': 'failed', 'evidence': 'receipt'}, {'passed': False, 'evidence': 'fixture'})])
def test_failure_and_passing_test_evidence_required(failure, tests):
    with pytest.raises(LearningDenied):
        ReviewedLearningStore().propose('demo', failure, {'description': 'Observe'}, tests)

def test_bounded_store_deduplicates_without_eviction():
    s = ReviewedLearningStore(max_proposals=1)
    p = propose(s)
    assert propose(s) == p
    with pytest.raises(LearningDenied):
        propose(s, 'different')
    assert len(s.list_proposals()) == 1

def test_restart_requires_fresh_review_and_detects_tampering(tmp_path):
    path = tmp_path / 'proposals.json'
    s = ReviewedLearningStore(path)
    p = propose(s)
    s.review(p['id'], p['candidate_hash'], 'approve', 'owner')
    s.activate(p['id'], p['candidate_hash'], 'owner')
    loaded = ReviewedLearningStore(path)
    assert not loaded.active_candidates()
    assert loaded.get(p['id'])['history'][-1]['event'] == 'activate'
    with pytest.raises(LearningDenied):
        loaded.activate(p['id'], p['candidate_hash'], 'owner')
    records = json.loads(path.read_text())
    records[p['id']]['proposal']['candidate']['verification_hint'] = 'tampered'
    path.write_text(json.dumps(records))
    with pytest.raises(LearningDenied):
        ReviewedLearningStore(path)

def test_failed_persistence_does_not_commit(tmp_path, monkeypatch):
    s = ReviewedLearningStore(tmp_path / 'store.json')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('gamelens.reviewed_learning.os.replace', fail)
    with pytest.raises(OSError):
        propose(s)
    assert s.list_proposals() == []

def test_record_envelope_limit_is_checked_before_commit(tmp_path):
    from gamelens.reviewed_learning import snapshot
    path = tmp_path / 'store.json'
    s = ReviewedLearningStore(path)
    failure = {'outcome': 'failed', 'evidence': 'x' * (262144 - 220)}
    # The proposal itself fits; wrapping it in the review record does not.
    snapshot(dict(skill_id='demo', failure_evidence=failure, candidate={'description': 'Observe'}, test_evidence={'passed': True, 'evidence': 'fixture'}))
    with pytest.raises(LearningDenied):
        s.propose('demo', failure, {'description': 'Observe'}, {'passed': True, 'evidence': 'fixture'})
    assert s.list_proposals() == []
    assert not path.exists()

@pytest.mark.parametrize('contents', ['{', 'null', '[]', '{"bad":null}', '{"bad":{}}'])
def test_malformed_store_fails_learning_denied(tmp_path, contents):
    path = tmp_path / 'store.json'
    path.write_text(contents)
    with pytest.raises(LearningDenied):
        ReviewedLearningStore(path)

@pytest.mark.parametrize('field,value', [('history', [None]), ('history', [{'event': 'review'}]), ('history', [{'event': 'activate', 'candidate_hash': 'bad', 'owner': 'owner'}]), ('active', 'yes'), ('review', []), ('review', {'candidate_hash': 'bad'})])
def test_invalid_record_fields_fail_learning_denied(tmp_path, field, value):
    path = tmp_path / 'store.json'
    s = ReviewedLearningStore(path)
    p = propose(s)
    records = json.loads(path.read_text())
    records[p['id']][field] = value
    path.write_text(json.dumps(records))
    with pytest.raises(LearningDenied):
        ReviewedLearningStore(path)

def make_link_or_skip(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'symlink creation unavailable: {exc}')

def test_predictable_tmp_link_is_never_opened(tmp_path):
    foreign = tmp_path / 'foreign.txt'
    foreign.write_text('preserve')
    path = tmp_path / 'store.json'
    make_link_or_skip(tmp_path / 'store.json.tmp', foreign)
    s = ReviewedLearningStore(path)
    propose(s)
    assert foreign.read_text() == 'preserve'
    assert path.exists()

@pytest.mark.parametrize('when', ['load', 'write'])
def test_linked_destination_is_refused_without_touching_target(tmp_path, when):
    path = tmp_path / 'store.json'
    foreign = tmp_path / 'foreign.json'
    foreign.write_text('{}')
    if when == 'load':
        make_link_or_skip(path, foreign)
        with pytest.raises(LearningDenied):
            ReviewedLearningStore(path)
    else:
        s = ReviewedLearningStore(path)
        make_link_or_skip(path, foreign)
        with pytest.raises(LearningDenied):
            propose(s)
        assert s.list_proposals() == []
    assert foreign.read_text() == '{}'

def test_linked_parent_is_refused(tmp_path):
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    linked = tmp_path / 'linked'
    make_link_or_skip(linked, foreign, directory=True)
    with pytest.raises(LearningDenied):
        ReviewedLearningStore(linked / 'store.json')
    assert list(foreign.iterdir()) == []

def test_temporary_creation_is_exclusive_and_collision_preserves_foreign_file(tmp_path, monkeypatch):
    path = tmp_path / 'store.json'
    foreign = tmp_path / 'store.json.fixed.tmp'
    foreign.write_text('preserve')
    monkeypatch.setattr('gamelens.reviewed_learning.secrets.token_hex', lambda n: 'fixed')
    s = ReviewedLearningStore(path)
    with pytest.raises(FileExistsError):
        propose(s)
    assert foreign.read_text() == 'preserve'
    assert s.list_proposals() == []

def test_repeated_activation_is_idempotent_and_rejection_always_works(tmp_path):
    path = tmp_path / 'store.json'
    s = ReviewedLearningStore(path)
    p = propose(s)
    s.review(p['id'], p['candidate_hash'], 'approve', 'owner')
    first = s.activate(p['id'], p['candidate_hash'], 'owner')
    for _ in range(150):
        assert s.activate(p['id'], p['candidate_hash'], 'owner') == first
    assert len(s.get(p['id'])['history']) == 2
    rejected = s.review(p['id'], p['candidate_hash'], 'reject', 'owner')
    assert not rejected['active']
    assert s.active_candidates() == []
    persisted = json.loads(path.read_text())[p['id']]
    assert persisted['review']['decision'] == 'reject'
    assert persisted['active'] is False
    assert ReviewedLearningStore(path).get(p['id'])['history'][-1]['decision'] == 'reject'

def test_full_transition_history_retains_latest_rejection(tmp_path):
    path = tmp_path / 'store.json'
    s = ReviewedLearningStore(path)
    p = propose(s)
    for _ in range(100):
        s.review(p['id'], p['candidate_hash'], 'approve', 'owner')
    assert len(s.get(p['id'])['history']) == 100
    s.activate(p['id'], p['candidate_hash'], 'owner')
    assert len(s.get(p['id'])['history']) == 100
    rejected = s.review(p['id'], p['candidate_hash'], 'reject', 'owner', 'Revoke at capacity')
    assert len(rejected['history']) == 100
    assert rejected['history'][-1]['decision'] == 'reject'
    assert rejected['history'][-1]['notes'] == 'Revoke at capacity'
    assert s.active_candidates() == []
    persisted = json.loads(path.read_text())[p['id']]
    assert persisted['active'] is False
    assert persisted['review']['decision'] == 'reject'
    loaded = ReviewedLearningStore(path)
    assert loaded.active_candidates() == []
    assert loaded.get(p['id'])['history'][-1]['decision'] == 'reject'
    assert loaded.get(p['id'])['proposal'] == p['proposal']
