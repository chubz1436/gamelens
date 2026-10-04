"""Bounded passive evidence. No capture, retry, or input authority."""
from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import PureWindowsPath
from uuid import uuid4

SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({'completed', 'failed', 'cancelled', 'aborted'})
OUTCOMES = frozenset({'sent', 'dry', 'denied', 'cancelled', 'error', 'pending', 'rejected'})
PROVENANCE_FIELDS = (
    'target_hwnd', 'backend_session_id', 'frame_id', 'frame_captured_at',
    'frame_width', 'frame_height', 'geometry_generation', 'preemption_counter',
    'scale', 'crop_left', 'crop_top', 'observed_at',
)


def identifier(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_.:\-]{1,128}', value):
        raise ValueError('Expected a bounded metadata identifier')
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected a finite number')
    return value


def positive(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('Expected a positive integer')
    return value


def objective(value):
    if value is not None and not isinstance(value, bool):
        raise ValueError('Objective success must be true, false or unknown')
    return value


def video_reference(reference):
    """Only a clip basename; no directories, URL, images or credential fields."""
    name = PureWindowsPath(str(reference)).name
    if not re.fullmatch(r'[A-Za-z0-9_. -]{1,160}\.(?:mp4|webm|mkv|mov)', name, re.I):
        raise ValueError('Expected a local video filename')
    return name


class RunMetrics:
    """Thread-safe evidence bounded by runs, events per run, and idle age.

    safe_call is the integration boundary: recording errors cannot authorize,
    reject or retry an input. The optional sink receives save(snapshot).
    """

    def __init__(self, *, max_runs=32, max_events=512, max_age_seconds=86400,
                 sink=None, monotonic=None, utcnow=None):
        self.max_runs, self.max_events = positive(max_runs), positive(max_events)
        self.max_age_seconds = number(max_age_seconds)
        if self.max_age_seconds <= 0:
            raise ValueError('Age limit must be positive')
        self._monotonic = monotonic or time.monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._runs = OrderedDict()
        self._sink, self._errors = sink, 0

    def _stamp(self):
        now = self._monotonic()
        utc = self._utcnow().astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        return now, utc

    def _prune(self, now):
        for rid in list(self._runs):
            if now - self._runs[rid]['_updated'] > self.max_age_seconds:
                del self._runs[rid]
        while len(self._runs) > self.max_runs:
            self._runs.popitem(last=False)

    def _persist(self, run_id):
        if self._sink is None:
            return
        # Take the newest snapshot after serializing sink calls, preventing an
        # older concurrent mutation from overwriting newer persisted evidence.
        with self._persist_lock:
            snapshot = self.snapshot(run_id)
            if snapshot is not None:
                try:
                    result = self._sink.save(snapshot)
                    if isinstance(result, dict) and not result.get('saved', False):
                        with self._lock:
                            self._errors += 1
                except Exception:
                    with self._lock:
                        self._errors += 1

    def safe_call(self, method, *args, **kwargs):
        """Best effort recording without exposing raw exception messages."""
        try:
            if method not in {'begin_run', 'finish_run', 'record_observation', 'record_action',
                              'record_outcome', 'record_objective', 'link_video'}:
                raise ValueError('Unknown recording operation')
            return getattr(self, method)(*args, **kwargs)
        except Exception:
            with self._lock:
                self._errors += 1
            return None

    def begin_run(self, *, run_id=None):
        rid = uuid4().hex if run_id is None else identifier(run_id)
        now, utc = self._stamp()
        with self._lock:
            self._prune(now)
            if rid in self._runs:
                raise ValueError('Run already exists')
            self._runs[rid] = {
                'schema_version': SCHEMA_VERSION, 'run_id': rid,
                'started_at_utc': utc, 'updated_at_utc': utc, 'ended_at_utc': None,
                'status': 'running', 'objective_success': None,
                'elapsed_ms': 0.0, 'event_count': 0, 'dropped_events': 0,
                'counts': {}, 'events': [], '_started': now, '_updated': now,
            }
            self._prune(now)
        self._persist(rid)
        return rid

    def _append(self, run, kind, fields, now, utc):
        run['event_count'] += 1
        event = {'sequence': run['event_count'], 'kind': kind,
                 'timestamp_utc': utc, 'elapsed_ms': round(max(0, now-run['_started'])*1000, 3),
                 **fields}
        run['events'].append(event)
        if len(run['events']) > self.max_events:
            del run['events'][0]
            run['dropped_events'] += 1
        run['counts'][kind] = run['counts'].get(kind, 0) + 1
        run['updated_at_utc'], run['_updated'] = utc, now
        run['elapsed_ms'] = event['elapsed_ms']
        return copy.deepcopy(event)

    def _event(self, run_id, kind, fields):
        now, utc = self._stamp()
        with self._lock:
            self._prune(now)
            run = self._runs.get(run_id)
            if run is None:
                return None
            if kind == 'outcome' and fields['outcome'] == 'pending':
                if any(e['kind'] == 'outcome' and e['action_id'] == fields['action_id'] and e['terminal']
                       for e in run['events']):
                    return None
            event = self._append(run, kind, fields, now, utc)
        self._persist(run_id)
        return event

    def finish_run(self, run_id, *, status='completed', objective_success=None):
        if status not in TERMINAL_STATUSES:
            raise ValueError('Expected a terminal run status')
        objective_success = objective(objective_success)
        now, utc = self._stamp()
        with self._lock:
            self._prune(now)
            run = self._runs.get(run_id)
            if run is None or run['status'] != 'running':
                return None
            run['status'], run['objective_success'], run['ended_at_utc'] = status, objective_success, utc
            event = self._append(run, 'terminal', {'status': status, 'objective_success': objective_success}, now, utc)
        self._persist(run_id)
        return event

    def record_observation(self, run_id, observation_id, observation, *, source='snapshot', freshness_limit=None):
        provenance = {}
        for field in PROVENANCE_FIELDS:
            value = observation.get(field) if isinstance(observation, dict) else getattr(observation, field, None)
            if value is not None:
                provenance[field] = number(value)
        captured = provenance.get('frame_captured_at')
        now = self._monotonic()
        future = captured is not None and captured > now
        age_ms = None if captured is None else round((now-captured)*1000, 3)
        limit = None if freshness_limit is None else number(freshness_limit)
        if limit is not None and limit < 0:
            raise ValueError('Freshness limit must be nonnegative')
        return self._event(run_id, 'observation', {
            'observation_id': identifier(observation_id), 'source': identifier(source),
            'provenance': provenance, 'frame_age_ms': age_ms,
            'freshness_limit_ms': None if limit is None else limit*1000,
            'fresh': False if future else (None if age_ms is None or limit is None else age_ms <= limit*1000),
        })

    def record_action(self, run_id, action_id, *, observation_id=None, kind='action', attempt=1, retry_of=None, log_id=None, bound_observation_id=None):
        aid, attempt = identifier(action_id), positive(attempt)
        retry = None if retry_of is None else identifier(retry_of)
        if (attempt == 1 and retry is not None) or (attempt > 1 and (retry is None or retry == aid)):
            raise ValueError('Retries need a distinct explicit prior action reference')
        return self._event(run_id, 'action', {
            'action_id': aid, 'observation_id': None if observation_id is None else identifier(observation_id),
            'action_kind': identifier(kind), 'attempt': attempt, 'retry_of': retry,
            'log_id': None if log_id is None else identifier(log_id),
            'bound_observation_id': None if bound_observation_id is None else identifier(bound_observation_id),
        })

    def record_outcome(self, run_id, action_id, outcome, *, verdict=None, objective_success=None,
                       completed_steps=None, injected_steps=None, last_completed_step=None,
                       partial=False, churn=None, after_frame=None):
        if outcome not in OUTCOMES or not isinstance(partial, bool):
            raise ValueError('Invalid outcome metadata')
        aid = identifier(action_id)
        with self._lock:
            run = self._runs.get(run_id)
            events = [] if run is None else run['events']
            if outcome == 'pending' and any(e['kind'] == 'outcome' and e['action_id'] == aid and e['terminal'] for e in events):
                return None
            action = next((e for e in reversed(events) if e['kind'] == 'action' and e['action_id'] == aid), None)
            latency = None if action is None else round(max(0, (self._monotonic()-run['_started'])*1000-action['elapsed_ms']), 3)
        fields = {'action_id': aid, 'outcome': outcome, 'terminal': outcome != 'pending',
                  'verdict': None if verdict is None else identifier(verdict),
                  'objective_success': objective(objective_success), 'latency_ms': latency,
                  'partial': partial}
        for key, value in (('completed_steps', completed_steps), ('injected_steps', injected_steps),
                           ('last_completed_step', last_completed_step), ('churn', churn), ('after_frame', after_frame)):
            fields[key] = None if value is None else number(value)
        return self._event(run_id, 'outcome', fields)

    def record_objective(self, run_id, objective_id, success, *, evidence=()):
        """Caller-reported objective result, separate from executor completion.

        Evidence contains observation/action/video identifiers only. The method
        does not inspect the game or infer success from sent/dry/pending input.
        """
        if not isinstance(success, bool):
            raise ValueError('Objective result must be an explicit boolean')
        if not isinstance(evidence, (list, tuple)) or len(evidence) > 32:
            raise ValueError('Expected at most 32 evidence identifiers')
        references = [identifier(item) for item in evidence]
        return self._event(run_id, 'objective', {'objective_id': identifier(objective_id),
                                               'objective_success': success, 'evidence': references})
    def link_video(self, run_id, video_id, reference, *, start_seconds=None, end_seconds=None):
        start = None if start_seconds is None else number(start_seconds)
        end = None if end_seconds is None else number(end_seconds)
        if (start is not None and start < 0) or (end is not None and end < 0) or (start is not None and end is not None and end < start):
            raise ValueError('Invalid video span')
        return self._event(run_id, 'video', {'video_id': identifier(video_id), 'reference': video_reference(reference),
                                           'start_seconds': start, 'end_seconds': end})

    def has_run(self, run_id):
        """Cheap live-ID check for hosts resuming after bounded idle retention."""
        with self._lock:
            self._prune(self._monotonic())
            return run_id in self._runs

    def snapshot(self, run_id=None):
        with self._lock:
            self._prune(self._monotonic())
            def public(run):
                return copy.deepcopy({k: v for k, v in run.items() if not k.startswith('_')})
            if run_id is not None:
                run = self._runs.get(run_id)
                return None if run is None else public(run)
            return {'runs': [public(r) for r in self._runs.values()], 'recording_errors': self._errors,
                    'limits': {'max_runs': self.max_runs, 'max_events': self.max_events,
                               'max_age_seconds': self.max_age_seconds}}

    def runs(self):
        return self.snapshot()['runs']
