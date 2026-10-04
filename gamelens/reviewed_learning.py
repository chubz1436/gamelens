"""Owner-reviewed advisory learning; never executes input or edits safety policy."""
from __future__ import annotations
import hashlib
import json
import os
import threading
import secrets
import stat
from pathlib import Path

class LearningDenied(ValueError):
    pass

def snapshot(value):
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(text.encode()) > 262144:
            raise LearningDenied('payload exceeds 256 KiB')
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise LearningDenied('bounded JSON required') from exc

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

class ReviewedLearningStore:
    """The adapter must derive owner identity from authenticated operator context.

    Durable state is trusted local storage. Restart requires fresh owner review.
    """
    def __init__(self, path=None, max_proposals=100):
        if type(max_proposals) is not int or not 1 <= max_proposals <= 1000:
            raise LearningDenied('max_proposals must be 1..1000')
        self.path = Path(path) if path is not None else None
        self.max_proposals = max_proposals
        self._lock = threading.RLock()
        self._records = {}
        if self.path:
            self._refuse_links(self.path.absolute())
        if self.path and self.path.exists():
            if self.path.stat().st_size > max_proposals * (262144 + 128) + 2:
                raise LearningDenied('store exceeds size bound')
            try:
                records = json.loads(self.path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise LearningDenied('invalid store JSON') from exc
            if not isinstance(records, dict) or len(records) > max_proposals:
                raise LearningDenied('invalid store')
            for key, record in records.items():
                self._validate_record(key, record)
                record['active'] = False
                record['review'] = None
            self._records = records

    @classmethod
    def _validate_record(cls, key, record):
        snapshot(record)
        if not isinstance(record, dict) or set(record) != {'id', 'candidate_hash', 'proposal', 'review', 'active', 'history'}:
            raise LearningDenied('invalid record schema')
        cls._validate(record['proposal'])
        if key != digest(record['proposal']) or record['candidate_hash'] != key or record['id'] != key:
            raise LearningDenied('proposal integrity mismatch')
        if type(record['active']) is not bool or not isinstance(record['history'], list) or len(record['history']) > 100:
            raise LearningDenied('invalid record state')
        if record['review'] is not None:
            cls._validate_review(key, record['review'])
        for entry in record['history']:
            if not isinstance(entry, dict):
                raise LearningDenied('invalid history entry')
            event = entry.get('event')
            if event == 'review':
                cls._validate_review(key, {k: v for k, v in entry.items() if k != 'event'})
            elif event == 'activate':
                if set(entry) != {'event', 'candidate_hash', 'owner'} or entry['candidate_hash'] != key:
                    raise LearningDenied('invalid activation history')
                cls._validate_owner(entry['owner'])
            else:
                raise LearningDenied('invalid history event')

    @staticmethod
    def _validate_owner(owner):
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
            raise LearningDenied('authenticated owner identity required')

    @classmethod
    def _validate_review(cls, key, review):
        if not isinstance(review, dict) or set(review) != {'candidate_hash', 'decision', 'owner', 'notes'}:
            raise LearningDenied('invalid review schema')
        if review['candidate_hash'] != key or not isinstance(review['decision'], str) or review['decision'] not in {'approve', 'reject'}:
            raise LearningDenied('invalid review decision/hash')
        cls._validate_owner(review['owner'])
        if not isinstance(review['notes'], str) or len(review['notes']) > 4096:
            raise LearningDenied('invalid review notes')

    @staticmethod
    def _validate(p):
        if not isinstance(p, dict) or set(p) != {'skill_id', 'failure_evidence', 'candidate', 'test_evidence'}:
            raise LearningDenied('invalid proposal schema')
        if not isinstance(p['skill_id'], str) or not p['skill_id'].strip() or len(p['skill_id']) > 128:
            raise LearningDenied('skill_id required')
        f, t, c = p['failure_evidence'], p['test_evidence'], p['candidate']
        if not isinstance(f, dict) or f.get('outcome') != 'failed' or not f.get('evidence'):
            raise LearningDenied('failure evidence required')
        if not isinstance(t, dict) or t.get('passed') is not True or not t.get('evidence'):
            raise LearningDenied('passing test evidence required')
        if not isinstance(c, dict) or not c or set(c) - {'description', 'observation_hint', 'verification_hint'}:
            raise LearningDenied('only advisory adaptations allowed; code/input/safety changes denied')
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in c.values()):
            raise LearningDenied('bounded advisory text required')

    @staticmethod
    def _refuse_links(path):
        # Windows junctions and other reparse points may not report is_symlink.
        for node in (path, *path.parents):
            try:
                info = node.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise LearningDenied('linked/reparse store paths are forbidden')

    def _commit(self, records):
        if self.path:
            self._refuse_links(self.path.absolute())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._refuse_links(self.path.absolute())
            tmp = self.path.with_name(self.path.name + '.' + secrets.token_hex(16) + '.tmp')
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(tmp, flags, 0o600)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    fd = None
                    json.dump(records, stream, sort_keys=True, separators=(',', ':'), allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._refuse_links(self.path.absolute())
                self._refuse_links(tmp.absolute())
                os.replace(tmp, self.path)
            finally:
                if fd is not None:
                    os.close(fd)
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        self._records = records

    def propose(self, skill_id, failure_evidence, candidate, test_evidence):
        p = snapshot(dict(skill_id=skill_id, failure_evidence=failure_evidence, candidate=candidate, test_evidence=test_evidence))
        self._validate(p)
        key = digest(p)
        with self._lock:
            if key not in self._records:
                if len(self._records) >= self.max_proposals:
                    raise LearningDenied('store full; immutable evidence cannot be evicted')
                records = dict(self._records)
                records[key] = dict(id=key, candidate_hash=key, proposal=p, review=None, active=False, history=[])
                self._validate_record(key, records[key])
                self._commit(records)
            return self.get(key)

    def get(self, proposal_id):
        with self._lock:
            if proposal_id not in self._records:
                raise LearningDenied('unknown proposal')
            # Snapshot each bounded record rather than imposing a total-store limit.
            return snapshot(self._records[proposal_id])

    def list_proposals(self):
        with self._lock:
            return [self.get(key) for key in self._records]

    def _checked(self, proposal_id, candidate_hash, owner):
        r = self.get(proposal_id)
        if candidate_hash != r['candidate_hash'] or candidate_hash != digest(r['proposal']):
            raise LearningDenied('exact candidate/evidence hash required')
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
            raise LearningDenied('authenticated owner identity required')
        return r

    def _replace(self, key, record):
        if len(record['history']) > 100:
            raise LearningDenied('review history full')
        snapshot(record)
        records = dict(self._records)
        records[key] = record
        self._commit(records)

    def review(self, proposal_id, candidate_hash, decision, owner, notes=''):
        with self._lock:
            r = self._checked(proposal_id, candidate_hash, owner)
            if not isinstance(decision, str) or decision not in {'approve', 'reject'} or not isinstance(notes, str) or len(notes) > 4096:
                raise LearningDenied('explicit approve/reject required')
            r['review'] = dict(candidate_hash=candidate_hash, decision=decision, owner=owner, notes=notes)
            r['active'] = False
            r['history'].append(dict(event='review', **r['review']))
            self._replace(proposal_id, r)
            return self.get(proposal_id)

    def activate(self, proposal_id, candidate_hash, owner):
        with self._lock:
            r = self._checked(proposal_id, candidate_hash, owner)
            v = r['review']
            if not v or v['decision'] != 'approve' or v['candidate_hash'] != candidate_hash or v['owner'] != owner:
                raise LearningDenied('explicit approval by activating owner required')
            r['active'] = True
            r['history'].append(dict(event='activate', candidate_hash=candidate_hash, owner=owner))
            self._replace(proposal_id, r)
            return self.get(proposal_id)

    def active_candidates(self, skill_id=None):
        return [r for r in self.list_proposals() if r['active'] and (skill_id is None or r['proposal']['skill_id'] == skill_id)]
