"""Opt-in local metadata snapshots with confined atomic writes and quotas."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from gamelens.run_metrics import (
    OUTCOMES, PROVENANCE_FIELDS, SCHEMA_VERSION, TERMINAL_STATUSES,
    identifier, number, objective, positive, video_reference,
)

RUN_KEYS = {'schema_version', 'run_id', 'started_at_utc', 'updated_at_utc', 'ended_at_utc',
            'status', 'objective_success', 'elapsed_ms', 'event_count', 'dropped_events', 'counts', 'events'}
BASE_EVENT_KEYS = {'sequence', 'kind', 'timestamp_utc', 'elapsed_ms'}
EVENT_KEYS = {
    'observation': {'observation_id', 'source', 'provenance', 'frame_age_ms', 'freshness_limit_ms', 'fresh'},
    'action': {'action_id', 'observation_id', 'action_kind', 'attempt', 'retry_of', 'log_id', 'bound_observation_id'},
    'outcome': {'action_id', 'outcome', 'terminal', 'verdict', 'objective_success', 'latency_ms',
                'partial', 'completed_steps', 'injected_steps', 'last_completed_step', 'churn', 'after_frame'},
    'terminal': {'status', 'objective_success'},
    'objective': {'objective_id', 'objective_success', 'evidence'},
    'video': {'video_id', 'reference', 'start_seconds', 'end_seconds'},
}
NUMERIC_KEYS = {'sequence', 'elapsed_ms', 'frame_age_ms', 'freshness_limit_ms', 'attempt',
                'latency_ms', 'completed_steps', 'injected_steps', 'last_completed_step', 'churn',
                'after_frame', 'start_seconds', 'end_seconds'}
BOOL_KEYS = {'objective_success', 'terminal', 'partial', 'fresh'}


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Invalid metadata schema')


def _utc(value):
    if not isinstance(value, str) or len(value) > 40 or not value.endswith('Z'):
        raise ValueError('Expected UTC timestamp')
    datetime.fromisoformat(value.replace('Z', '+00:00'))


def validate_snapshot(snapshot, max_events):
    """Closed schema: no arbitrary dictionaries, text, images or credentials."""
    _keys(snapshot, RUN_KEYS)
    if snapshot['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported schema')
    identifier(snapshot['run_id'])
    for key in ('started_at_utc', 'updated_at_utc'):
        _utc(snapshot[key])
    if snapshot['ended_at_utc'] is not None:
        _utc(snapshot['ended_at_utc'])
    if snapshot['status'] not in TERMINAL_STATUSES | {'running'}:
        raise ValueError('Invalid status')
    objective(snapshot['objective_success'])
    for key in ('elapsed_ms', 'event_count', 'dropped_events'):
        if number(snapshot[key]) < 0:
            raise ValueError('Negative metadata counter')
    counts = snapshot['counts']
    if not isinstance(counts, dict) or not set(counts) <= set(EVENT_KEYS):
        raise ValueError('Invalid counts')
    for value in counts.values():
        if number(value) < 0:
            raise ValueError('Negative metadata counter')
    events = snapshot['events']
    if not isinstance(events, list) or len(events) > max_events:
        raise ValueError('Event limit exceeded')
    for event in events:
        if not isinstance(event, dict) or event.get('kind') not in EVENT_KEYS:
            raise ValueError('Invalid event')
        kind = event['kind']
        _keys(event, BASE_EVENT_KEYS | EVENT_KEYS[kind])
        for key, value in event.items():
            if key in NUMERIC_KEYS:
                if value is not None:
                    number(value)
            elif key in BOOL_KEYS:
                objective(value)
            elif key == 'provenance':
                if not isinstance(value, dict) or not set(value) <= set(PROVENANCE_FIELDS):
                    raise ValueError('Invalid provenance')
                for item in value.values():
                    number(item)
            elif key == 'evidence':
                if not isinstance(value, list) or len(value) > 32:
                    raise ValueError('Invalid evidence references')
                for item in value:
                    identifier(item)
            elif key == 'timestamp_utc':
                _utc(value)
            elif key == 'reference':
                if video_reference(value) != value:
                    raise ValueError('Video references must be basenames')
            elif value is not None:
                identifier(value)
        if kind == 'outcome' and event['outcome'] not in OUTCOMES:
            raise ValueError('Invalid outcome')
        if kind == 'terminal' and event['status'] not in TERMINAL_STATUSES:
            raise ValueError('Invalid terminal status')
    return snapshot


class LocalRunStore:
    """Disk is disabled by default; constructing this object never creates files.

    Only the fixed run-metrics child of base_directory is managed. Cleanup is
    nonrecursive and deletes only validated, owned JSON snapshots. Foreign or
    malformed files count toward quotas and are never removed. Limits cover
    committed files; atomic replacement temporarily adds one bounded temp file.
    """

    def __init__(self, base_directory=None, *, enabled=False, max_events=512,
                 max_files=32, max_bytes=8*1024*1024, max_file_bytes=256*1024,
                 max_age_seconds=7*86400, clock=None):
        if not isinstance(enabled, bool):
            raise ValueError('Persistence requires explicit boolean opt-in')
        base = Path(base_directory) if base_directory is not None else Path.home() / '.gamelens'
        self.directory = base.absolute() / 'run-metrics'
        self.enabled = enabled
        self.max_events, self.max_files = positive(max_events), positive(max_files)
        self.max_bytes, self.max_file_bytes = positive(max_bytes), positive(max_file_bytes)
        self.max_age_seconds = number(max_age_seconds)
        if self.max_age_seconds <= 0 or self.max_file_bytes > self.max_bytes:
            raise ValueError('Invalid storage limits')
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._last_result = {'saved': False, 'reason': 'disabled' if not enabled else 'not_saved'}

    def _safe_directory(self, *, create=False):
        # Reject symlinks and Windows junction/reparse points in every existing
        # component before creation or cleanup. No resolve() through links.
        for path in reversed((self.directory, *self.directory.parents)):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise OSError('Unsafe metadata directory')
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.exists() and not self.directory.is_dir():
            raise OSError('Invalid metadata directory')

    @staticmethod
    def _filename(run_id):
        return 'run-' + hashlib.sha256(identifier(run_id).encode('ascii')).hexdigest() + '.json'

    def _read_owned(self, path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return None
        if not re.fullmatch(r'run-[0-9a-f]{64}\.json', path.name) or info.st_size > self.max_file_bytes:
            return None
        try:
            with path.open('rb') as stream:
                raw = stream.read(self.max_file_bytes + 1)
            if len(raw) > self.max_file_bytes:
                return None
            envelope = json.loads(raw)
            _keys(envelope, {'format', 'schema_version', 'run'})
            if envelope['format'] != 'gamelens-run-metrics' or envelope['schema_version'] != SCHEMA_VERSION:
                return None
            run = validate_snapshot(envelope['run'], self.max_events)
            return run if self._filename(run['run_id']) == path.name else None
        except (ValueError, TypeError, KeyError, UnicodeError, OSError):
            return None

    def _scan(self):
        entries = []
        if not self.directory.exists():
            return entries
        with os.scandir(self.directory) as stream:
            for entry in stream:
                if len(entries) >= 4096:
                    raise OSError('Metadata directory entry limit exceeded')
                path = self.directory / entry.name
                info = path.lstat()
                entries.append((path, info.st_size, info.st_mtime, self._read_owned(path)))
        return entries

    def _unlink_owned(self, path):
        self._safe_directory()
        if path.parent != self.directory or self._read_owned(path) is None:
            raise OSError('Refused unowned cleanup')
        path.unlink()

    def _quota(self, entries, target, size):
        # Retain foreign files. Refuse replacing one even if its name matches.
        previous = next((e for e in entries if e[0] == target), None)
        if previous is not None and previous[3] is None:
            raise OSError('Destination is not owned metadata')
        count = len(entries) + (0 if previous else 1)
        total = sum(e[1] for e in entries) + size - (previous[1] if previous else 0)
        for path, old_size, modified, run in sorted(entries, key=lambda e: e[2]):
            if path == target or run is None:
                continue
            expired = self._clock() - modified > self.max_age_seconds
            if expired or count > self.max_files or total > self.max_bytes:
                self._unlink_owned(path)
                count -= 1
                total -= old_size
        if count > self.max_files or total > self.max_bytes:
            raise OSError('Storage quota unavailable')

    def save(self, snapshot):
        """Never raises into an input caller. Returns a fixed, non-secret status."""
        if not self.enabled:
            return {'saved': False, 'reason': 'disabled'}
        with self._lock:
            temporary = None
            try:
                validate_snapshot(snapshot, self.max_events)
                payload = json.dumps({'format': 'gamelens-run-metrics', 'schema_version': SCHEMA_VERSION,
                                      'run': snapshot}, separators=(',', ':'), allow_nan=False).encode('utf-8')
                if len(payload) > self.max_file_bytes:
                    self._last_result = {'saved': False, 'reason': 'file_limit'}
                    return dict(self._last_result)
                self._safe_directory(create=True)
                target = self.directory / self._filename(snapshot['run_id'])
                self._quota(self._scan(), target, len(payload))
                temporary = self.directory / ('.write-' + uuid4().hex + '.tmp')
                with temporary.open('xb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._safe_directory()
                if target.exists() and self._read_owned(target) is None:
                    raise OSError('Destination is not owned metadata')
                os.replace(temporary, target)
                temporary = None
                self._last_result = {'saved': True, 'reason': 'saved', 'file': target.name, 'bytes': len(payload)}
            except (ValueError, TypeError, KeyError):
                self._last_result = {'saved': False, 'reason': 'invalid_metadata'}
            except Exception:
                self._last_result = {'saved': False, 'reason': 'storage_unavailable'}
            finally:
                if temporary is not None:
                    try:
                        self._safe_directory()
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            return dict(self._last_result)

    def snapshots(self):
        """Read validated, retained snapshots. Disabled stores never read disk."""
        if not self.enabled:
            return []
        with self._lock:
            try:
                self._safe_directory()
                entries = sorted(self._scan(), key=lambda e: e[2], reverse=True)
                return [run for _, _, modified, run in entries
                        if run is not None and self._clock()-modified <= self.max_age_seconds][:self.max_files]
            except Exception:
                return []

    def status(self):
        with self._lock:
            return {'enabled': self.enabled, 'limits': {'max_events': self.max_events, 'max_files': self.max_files,
                    'max_bytes': self.max_bytes, 'max_file_bytes': self.max_file_bytes,
                    'max_age_seconds': self.max_age_seconds}, 'last_save': dict(self._last_result)}
