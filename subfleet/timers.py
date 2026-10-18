"""Bounded daemon maintenance: a single post-heal monitoring verdict (C-18.1).

Network calls and guardian turns happen in a fixed worker pool. Transactions
only reserve lanes or publish facts. Timer requests never create jobs (C-8.4).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import traceback
from uuid import uuid4

from . import capacity, claude_cards
from .policy import cap as policy_cap, lane_slot_cap
from .adapters.registry import get_adapter
from .contracts import ClockSource, Closure, ClosureReason, Outcome, OutcomeClass, Reading, ReadingLabel
from .credentials import resolve_credential
from .sessions.transcripts import read_regular
from .store import Store

#: `Timers._claim`'s answer for a lane with work on it: an attempt in flight or a
#: lane lease (C-18.3).
BUSY = 'busy'


def instant(value=None):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value or datetime.now(timezone.utc)


def iso(value):
    return instant(value).astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


#: The timers `Timers.status()` reports (C-18.1).
TIMERS = ('probe', 'keepalive', 'reset_credits', 'alerts', 'retention', 'mirror', 'mirror_hot', 'claude_cards')
#: C-18.5: a failure stays in `status` and `doctor` this long after the timer's
#: next run that no exception ended.
FAILURE_VISIBLE_S = 86400
#: C-18.5: a timer that keeps failing writes a `timer.error` event at least this
#: often, by the daemon's monotonic clock.
FAILURE_HEARTBEAT_S = 3600
#: C-18.5: each timer's failure record, exactly, in the state root. Rewritten
#: whenever a record changes, read back at start and by `doctor`, so a restart
#: or a reader loses nothing the bounded event stream leaves out.
RECORDS_FILE = 'timers.json'
#: C-18.5: the most of RECORDS_FILE a reader takes.
RECORDS_MAX_BYTES = 1 << 20
#: C-18.5: the `timer.error` events a refused store write leaves for the timer's
#: next run to write: the first not written, and the latest.
UNSENT_MAX = 2
#: C-18.5: the most kinds of one failure, by type and place, that each get a
#: `timer.error` event of their own.
FAILURE_KINDS_MAX = 8
#: C-18.5: the most of an exception's message that is kept, after the scrub.
FAILURE_MESSAGE_CHARS = 240
#: C-18.5: a longer message is not scrubbed on a timer's worker, and is left out.
FAILURE_SCRUB_MAX_CHARS = 1_000_000
#: C-18.5: the innermost frames under this package that a `timer.error` event names.
FAILURE_TRACE_FRAMES = 6
#: C-18.5: a timer whose clean runs are not recorded (`mirror_hot`) records a
#: change between failing and not at most this often, by the monotonic clock.
FAILURE_CHANGE_S = 60
#: C-18.5: what a failure message may not carry, past the handoff scrub (C-23.14):
#: three dot-separated base64url segments holding a digit (a JWT whose header is
#: not `eyJ`); a run of 20 or more token characters with an upper-case letter, a
#: lower-case letter and a digit; and 32 or more hexadecimal digits. A path's
#: components are judged one by one; a run never starts at a `/`.
_JWT_SHAPE = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])')
_OPAQUE_RUN = re.compile(r'(?<![A-Za-z0-9_+=-])[A-Za-z0-9_+=-]{20,}(?![A-Za-z0-9_+=-])')
_HEX_RUN = re.compile(r'(?<![A-Za-z0-9])[0-9A-Fa-f]{32,}(?![A-Za-z0-9])')
_REDACTED = '[REDACTED]'
#: C-18.5: what `doctor` and an offline `status` read, newest first, through
#: `events_kind`. `Store.add_event` writes an empty event of the same kind beside
#: each one, which is skipped. At five runs a minute this reaches back about a week.
RECORDED_QUERY = ("SELECT data_json FROM events WHERE kind='timer.run' AND data_json<>'{}' "
                  "ORDER BY event_id DESC LIMIT 50000")
_PACKAGE = os.path.dirname(os.path.abspath(__file__)) + os.sep
_FAILURE_TIMES = ('first_at', 'since', 'last_at')


def _utf8(text):
    """C-18.5: `text` as valid UTF-8: a lone surrogate (a path decoded with
    `surrogateescape`, say) becomes its `\\udcxx` escape. One in a record could be
    neither stored nor sent: every write for the timer raised, and `daemon.status`
    could not be encoded (review of #171, round 2)."""
    return text.encode('utf-8', 'backslashreplace').decode('utf-8') if isinstance(text, str) else text


def raised_from(exc):
    """C-18.5: where `exc` was raised, as `(place, trace)`.

    `place` is the innermost frame under this package that `exc` came through
    (`sessions/mirror.py:1535 in _spread`), or the innermost of all when it came
    through none. `trace` is the last FAILURE_TRACE_FRAMES frames under this
    package, outermost first: a `sqlite3` error's place is the store's own
    method, and only its callers say which step of a cycle it was. Read from
    the traceback's frames: no source file is opened, and no value is kept.
    """
    own, last = [], None
    for frame, line in traceback.walk_tb(exc.__traceback__):
        path = os.path.abspath(frame.f_code.co_filename)
        inside = path.startswith(_PACKAGE)
        last = _utf8(f"{path[len(_PACKAGE):] if inside else os.path.basename(path)}:{line} in {frame.f_code.co_name}")
        if inside:
            own.append(last)
    return (own[-1] if own else last), own[-FAILURE_TRACE_FRAMES:]


def redact_opaque(text):
    """C-18.5: `text` with every token-shaped run the handoff list may miss replaced.

    The handoff list (C-23.14) knows credentials by their names and prefixes; a
    message can carry one with neither, such as a JWT whose header does not
    encode to `eyJ` (review of #171). A failure message is for finding a
    defect, so anything shaped like a token goes, a hash or an id with it.
    """
    def opaque(match):
        run = match.group(0)
        mixed = (any(ch.isupper() for ch in run) and any(ch.islower() for ch in run)
                 and any(ch.isdigit() for ch in run))
        return _REDACTED if mixed else run
    text = _JWT_SHAPE.sub(lambda match: _REDACTED if any(ch.isdigit() for ch in match.group(0))
                          else match.group(0), text)
    return _HEX_RUN.sub(_REDACTED, _OPAQUE_RUN.sub(opaque, text))


def failure_message(exc):
    """C-18.5: an exception's message as one bounded line, credentials scrubbed.

    A provider or keychain error can carry a credential in its message, so the
    text goes through the handoff scrub list (C-23.14) whole, then through
    `redact_opaque`, and only then is cut: a cut made first can part a
    credential from the name that marks it. Never raises; a message that cannot
    be rendered is None.
    """
    try:
        text = str(exc)
        if len(text) > FAILURE_SCRUB_MAX_CHARS:
            return f"(a message of {len(text):,} characters, left out)"
        from .sessions.handoff import scrub_bounded
        text = _utf8(' '.join(redact_opaque(scrub_bounded(text, None)[0]).split()))
    except Exception:
        return None
    if len(text) > FAILURE_MESSAGE_CHARS:
        text = text[:FAILURE_MESSAGE_CHARS - 1].rstrip() + '…'
    return text or None


def failure_record(value):
    """C-18.5: `value` if it has the shape of a failure record, else None."""
    if not isinstance(value, dict):
        return None
    times = {}
    try:
        for key in (*_FAILURE_TIMES, *(('recovered_at',) if value.get('recovered_at') is not None else ())):
            if not isinstance(value[key], str) or instant(value[key]).utcoffset() is None:
                return None
            times[key] = iso(value[key])            # canonical: `fromisoformat` takes any separator
        whole = all(isinstance(value[key], int) and not isinstance(value[key], bool) and value[key] >= 1
                    for key in ('runs', 'failed_runs'))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not whole or not isinstance(value.get('error_type'), str):
        return None
    # Strings read back are made valid UTF-8 as new ones are: a record written
    # before that rule could otherwise break every write and reply again.
    return {**{key: value[key] for key in ('runs', 'failed_runs')}, **times,
            'error_type': _utf8(value['error_type']),
            **{key: _utf8(value.get(key)) if isinstance(value.get(key), str) else None
               for key in ('message', 'raised_at')},
            'recovered_at': times.get('recovered_at')}


def failure_shown(failure, now):
    """C-18.5: is `failure` still to be shown at `now`? While the timer fails,
    always; after a clean run, for FAILURE_VISIBLE_S."""
    if failure is None:
        return False
    try:
        return (failure['recovered_at'] is None
                or (instant(now) - instant(failure['recovered_at'])).total_seconds() < FAILURE_VISIBLE_S)
    except (KeyError, TypeError, ValueError, AttributeError):
        return True                         # unreadable: shown, never a reader's crash


def advance(failure, at, raised=None):
    """C-18.5: a timer's failure record after one more run.

    `failure` is the record before the run, None when the timer is clear; `at`
    is the run's time; `raised` is None when no exception ended the run, else
    its `error_type`, `message` and `raised_at`. The record that comes back is
    a new one. `runs` counts the failed runs in a row ending at `last_at`, from
    `since`; `failed_runs` counts every failed run from `first_at`, across the
    clean runs between them, until the record is a day past a clean run and the
    next failure starts another.
    """
    if not failure_shown(failure, at):
        failure = None
    if raised is None:
        if failure is None or failure['recovered_at'] is not None:
            return failure
        return {**failure, 'recovered_at': at}
    if failure is None:
        return {'first_at': at, 'since': at, 'last_at': at, 'runs': 1, 'failed_runs': 1,
                **raised, 'recovered_at': None}
    resumed = failure['recovered_at'] is not None
    return {**failure, **raised, 'since': at if resumed else failure['since'], 'last_at': at,
            'runs': 1 if resumed else failure['runs'] + 1, 'failed_runs': failure['failed_runs'] + 1,
            'recovered_at': None}


def _rev(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def write_records_file(path, data):
    """C-18.5: publish `data` at `path` as `guardian.atomic_publish` does (a new
    file, fsync, rename, directory fsync) and return the identity of the file this
    wrote (inode, size, modification time), read from its own descriptor before
    the rename: a file anything else puts there afterwards, or its absence, is
    never taken for it (review of #171, round 4)."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
        os.rename(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return info.st_ino, info.st_size, info.st_mtime_ns


def file_identity(path):
    """The identity of the file at `path` as `write_records_file` returns it; None
    when there is none."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns


def load_records(root):
    """C-18.5: RECORDS_FILE's entry for each timer it names: `failure` (None: no
    record), `rev`, and the `last_run` and `last_error_type` it was written with;
    {} when there is no such file or it cannot be read."""
    try:
        data = json.loads(read_regular(Path(root) / RECORDS_FILE, RECORDS_MAX_BYTES))
    except (OSError, ValueError):
        return {}
    timers = data.get('timers') if isinstance(data, dict) else None
    if not isinstance(timers, dict):
        return {}
    return {name: {'failure': failure_record(value.get('failure')), 'rev': _rev(value.get('rev')),
                   **{key: _utf8(value.get(key)) if isinstance(value.get(key), str) else None
                      for key in ('last_run', 'last_error_type')}}
            for name, value in timers.items() if name in TIMERS and isinstance(value, dict)}


def recorded(rows, now=None, records=None):
    """C-18.5: each timer's status as the daemon last recorded it.

    `rows` are `timer.run` events newest first (RECORDED_QUERY); `records` are
    RECORDS_FILE's (`load_records`). Each carries the record's revision, which
    goes up whenever the record changes; the newer revision wins. The file is
    written before a run's events and is exact while its writes succeed (a hot
    timer's events are bounded and lag); an event newer than the file means a
    write of it failed, and the timer is marked `file_behind`. A timer with
    neither is left out. For readers that have no daemon to ask: `doctor`, which
    reads files only (C-17.5), and an offline `status`.
    """
    found, revs, carried = {}, {}, set()
    for row in rows:
        try:
            data = json.loads(row['data_json'])
        except (TypeError, ValueError):
            continue
        name = data.get('timer') if isinstance(data, dict) else None
        if name not in TIMERS or name in found or not data.get('last_run'):
            continue
        failure = failure_record(data.get('failure'))
        revs[name] = _rev(data.get('rev'))
        if 'rev' in data:
            carried.add(name)
        found[name] = {**{key: _utf8(data.get(key)) for key in ('last_run', 'next_due', 'last_error_type')},
                       'failure': failure if failure_shown(failure, now) else None, 'rev': revs[name]}
        if len(found) == len(TIMERS):
            break
    if records is not None:
        # An event with a revision means the daemon meant the file to hold that
        # timer: no file, or no entry for it, is a file behind too.
        for name in carried:
            if name not in records:
                found[name]['file_behind'] = True
    for name, entry in (records or {}).items():
        if name in revs and revs[name] > entry['rev']:
            found[name]['file_behind'] = True
            continue
        shown = entry['failure'] if failure_shown(entry['failure'], now) else None
        if name in revs and revs[name] == entry['rev']:
            found[name]['failure'] = shown          # the same revision; the event's run is the later
        elif shown or entry['last_run']:
            found[name] = {'last_run': entry['last_run'] or shown['recovered_at'] or shown['last_at'],
                           'next_due': None, 'last_error_type': entry['last_error_type'], 'failure': shown,
                           'rev': entry['rev']}
    return found


class Timers:
    def __init__(self, store, root, policy, *, turn=None, adapter_factory=get_adapter,
                 deliver=None, now=None, cards_sensor=None, demand=None):
        from .actions import ResetCredits, inhibit_path
        from .alerts import Alerts
        self.store, self.root, self.policy = store, Path(root), policy
        self.turn, self.adapter_factory = turn, adapter_factory
        # C-23.16 (a): the daemon's admission view of the jobs waiting on capacity
        # (`Daemon._reset_demand`). Without one no credit is ever spent, by the
        # timer or for an operator's `reset codex <lane>`.
        self.demand = demand
        # C-10.3: whether Claude Code is using the desktop login now, a callable the
        # daemon supplies (`Daemon._desktop_in_use`); None judges the desktop lane
        # in use, as a view without the signal does.
        self.desktop_in_use = None
        # C-18.1: a probe holder's newest record (the daemon's), for the state
        # status.json shows beside a lane that probe holds; None reads none.
        self.probe_record = None
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.cancel = threading.Event()
        self._lock = threading.RLock()
        self._cycles = ThreadPoolExecutor(max_workers=2, thread_name_prefix='subfleet-timer')
        # The mirror gets its own worker. It is a file-copy pass over the whole
        # desktop session store and an 8.5-minute one was observed on 2026-08-18
        # during app churn; sharing the two-slot cycle pool would let it hold a
        # probe or a keepalive behind it for minutes (C-23.28).
        # Both mirror timers stay serial. While the full timer owns this
        # worker, Mirror checkpoints service due flag-only hot passes under
        # its flock; another worker would just lose that lock and skip.
        self._mirror = ThreadPoolExecutor(max_workers=1, thread_name_prefix='subfleet-mirror')
        self._session_mirror = None
        self._lanes = ThreadPoolExecutor(max_workers=min(4, policy.get('caps', {}).get('keepalive_workers', 4)),
                                         thread_name_prefix='subfleet-timer-lane')
        # C-9.10: the card sensor reads every login in turn and may spend a heal
        # turn on some; minutes in the worst case, so it never takes a cycle
        # worker a probe or a keepalive is waiting for.
        self._cards = ThreadPoolExecutor(max_workers=1, thread_name_prefix='subfleet-cards')
        self._cards_sensor = cards_sensor
        self._running = set()
        self.active_holders = set()
        self._probe_holders = {}
        self._due = {}
        self._io_busy = set()
        self._io_slots = threading.BoundedSemaphore(4)
        # C-9.9: the usage endpoint penalises bursts; Claude usage reads are spaced.
        self._usage_lock = threading.Lock()
        self._usage_next = 0.0
        self.started = False
        self.actions = ResetCredits(store, policy, adapter_factory=lambda lane: self.adapter_factory(lane.provider),
                                    inhibit=inhibit_path(self.root))
        self.alerts = Alerts(store, policy, deliver or (lambda notice: False))
        settings = policy.get('timers', {})
        self.intervals = {'probe': settings.get('probe_interval_s', 60),
                          'keepalive': settings.get('keepalive_interval_s', 18300)}
        # C-23.28 and plan decision 8: the desktop sidebar mirror is a 60 s
        # file-copy timer. Its interval lives under `sessions`, not `timers`,
        # because it is the sessions kit's cadence; `mirror_interval_s: 0`
        # switches it off without removing the verb.
        mirror_interval = policy.get('sessions', {}).get('mirror_interval_s', 60)
        if mirror_interval:
            self.intervals['mirror'] = mirror_interval
            # The app lists a session folder only when it loads it, so a new
            # session must reach every folder before the next account switch:
            # the hot pass spreads new sessions within seconds. It shares the
            # mirror's worker and lock, and is off whenever the mirror is.
            hot_interval = policy.get('sessions', {}).get('mirror_hot_interval_s', 2)
            if hot_interval:
                self.intervals['mirror_hot'] = hot_interval
        cards = claude_cards.settings(policy)
        if cards['enabled']:
            self.intervals['claude_cards'] = float(cards['interval_min']) * 60
        self._status = {name: {'last_run': None, 'next_due': None, 'last_error_type': None, 'failure': None}
                        for name in TIMERS}
        # C-18.5: what this process has written of each timer's failure, which
        # paces its `timer.error` events: the failure it belongs to (`first_at`),
        # the kinds told and when (monotonic) the last event was written.
        self._told = {}
        # C-18.5: `timer.error` events a store write refused, for the timer's next
        # run to write; one timer's are written by one thread at a time.
        self._unsent = {name: [] for name in TIMERS}
        self._sending = {name: threading.Lock() for name in TIMERS}
        # C-18.5: each record's revision, up by one whenever it changes, in
        # RECORDS_FILE and in every `timer.run`: the newer copy wins.
        self._rev = {name: 0 for name in TIMERS}
        # C-18.5: where each revision is durable, so that none is in neither: the
        # highest each timer's events hold (they are never taken back), and the
        # revisions RECORDS_FILE holds, counted only while the file there is the
        # one this process wrote (`_records_identity`). Why its last write failed
        # (None: it did not).
        self._records_lock = threading.Lock()
        self._event_rev = {name: 0 for name in TIMERS}
        self._file_rev = {name: 0 for name in TIMERS}
        self._records_identity = None
        self._records_failure = None
        self.metadata = self._latest('timer.verdict')
        # Failure pacing is sensor state, not a credential/admission verdict.
        # Busy failures are withheld from metadata but still need a durable
        # cooldown so requested cycles and restarts cannot hammer a missing sensor.
        self._usage_backoff = self._latest('timer.usage-backoff')
        # Each lane's verdict is replaced whole, under this lock, so that putting
        # one back after a failed publication can check it is still the one in
        # place and replace it as one step (C-18.3). Only dictionary work is done
        # under it; the store's lock, when both are held, is taken first.
        self._verdicts = threading.RLock()
        self.balances = self._latest('reset-credit.balance')
        self._cycle_error = None
        for row in store.query("SELECT data_json FROM events WHERE kind='timer.run' ORDER BY event_id"):
            data = json.loads(row['data_json'])
            if data.get('timer') in self._status and data.get('last_run'):
                self._status[data['timer']].update({k: _utf8(data.get(k)) for k in ('last_run', 'next_due', 'last_error_type')},
                                                   failure=failure_record(data.get('failure')))
                self._rev[data['timer']] = _rev(data.get('rev'))
                self._event_rev[data['timer']] = max(self._event_rev[data['timer']], _rev(data.get('rev')))
        # C-18.5: whether the last `timer.run` written for each timer says it is
        # failing, and when (monotonic) a change of that was last written (None:
        # before this process).
        self._kept = {name: (self._failing(value['failure']), None) for name, value in self._status.items()}
        # C-18.5: the exact records, where the events' copies lag (`mirror_hot`);
        # an event newer than the file (a write of it failed) wins.
        for name, entry in load_records(self.root).items():
            if entry['rev'] >= self._rev[name]:
                self._status[name]['failure'] = entry['failure']
            if entry['rev'] > self._rev[name]:
                self._status[name].update(last_error_type=entry['last_error_type'],
                                          **({'last_run': entry['last_run']} if entry['last_run'] else {}))
                self._rev[name] = entry['rev']

    def _latest(self, kind):
        result = {}
        for row in self.store.query('SELECT lane_id,data_json FROM events WHERE kind=? ORDER BY event_id', (kind,)):
            data = json.loads(row['data_json'])
            if row['lane_id'] and data:
                result[row['lane_id']] = data
        return result

    def start(self):
        with self._lock:
            if self.started:
                return
            self.started = True
            for name, interval in self.intervals.items():
                # C-9.10: the first card read comes a minute after start, not a
                # whole interval later; a restart must not hide a card for hours.
                interval = min(interval, 60) if name == 'claude_cards' else interval
                self._status[name]['next_due'] = iso(self.now() + timedelta(seconds=interval))
                self._due[name] = time.monotonic() + interval
            self._status['retention']['next_due'] = iso(self.now() + timedelta(hours=1))

    def status(self):
        """C-18.1, C-18.5: each timer's last run, next due time, failure record and its revision."""
        now = self.now()
        with self._lock:
            return {name: {**value, 'failure': value['failure'] if failure_shown(value['failure'], now) else None,
                           'rev': self._rev[name]}
                    for name, value in self._status.items()}

    @staticmethod
    def _failing(failure):
        return failure is not None and failure['recovered_at'] is None

    def _error_due(self, name, failure, mono):
        """C-18.5: is this failed run one the store is told of? Under `_lock`.

        The first of a failure always is, and so is the first that this process
        has written of one a restart carried over. After that: a kind (type and
        place) not told yet, up to FAILURE_KINDS_MAX of them; each time the
        failed runs reach a power of two; and whenever FAILURE_HEARTBEAT_S have
        passed since the last event written, so a failure that goes on is in
        every hour's events. Time here is the monotonic clock `mono`: a wall
        clock set back adds no event (review of #171).
        """
        told = self._told.get(name)
        if told is None or told['first_at'] != failure['first_at']:
            return True
        runs = failure['failed_runs']
        return ((failure['error_type'], failure['raised_at']) not in told['kinds']
                and len(told['kinds']) < FAILURE_KINDS_MAX
                or runs & (runs - 1) == 0
                or mono - told['mono'] >= FAILURE_HEARTBEAT_S)

    def mark(self, name, *, error=None, next_due=None, persist=True):
        """One run of `name` has ended (C-18.1, C-18.5).

        `error` is None, the exception that ended the run, or the name of an
        error type a run reported without raising (a probe cycle one of whose
        reads failed, a retention pass its deadline stopped). Only an exception
        opens or extends the timer's failure record. `persist=False` keeps a
        run out of the store unless C-18.5 has something of it to record.
        """
        raised = trace = None
        if isinstance(error, BaseException):
            place, trace = raised_from(error)
            raised = {'error_type': _utf8(type(error).__name__), 'message': failure_message(error), 'raised_at': place}
            error = raised['error_type']
        at, mono = iso(self.now()), time.monotonic()
        with self._lock:
            status = self._status[name]
            failure = advance(status['failure'], at, raised)
            if failure != status['failure']:
                self._rev[name] += 1
            status.update(last_run=at, last_error_type=error, failure=failure)
            if next_due is not None:
                status['next_due'] = next_due
            data = {'timer': name, **status, 'rev': self._rev[name]}
            event = None
            if raised and self._error_due(name, failure, mono):
                event = {'timer': name, **{key: value for key, value in failure.items() if key != 'recovered_at'},
                         'trace': trace}
                unsent = self._unsent[name]
                if len(unsent) < UNSENT_MAX:
                    unsent.append(event)
                else:
                    unsent[-1] = event          # the first not written stays
            failing, kept = self._failing(failure), self._kept[name]
            changed = failing != kept[0]
            persist = bool(persist or event or changed and (
                kept[1] is None or mono - kept[1] >= FAILURE_CHANGE_S))
        # C-3.7: written after `_lock` is let go. The control loop takes that
        # lock every tick (`tick`), so a store write waiting for the store lock
        # while holding it kept the loop from scheduling anything. C-18.5: the
        # records file first, so it is never older than the events; then the
        # events, of which one the store refuses stays to be written later.
        self._publish_records()
        with self._lock:
            # C-18.5: a revision the file does not hold is written with this run,
            # paced or not: it must be in one of the two.
            persist = persist or data['rev'] > max(self._event_rev[name], self._file_rev[name])
        self._send_errors(name)
        if persist:
            self.store.add_event('timer.run', data=data)
            with self._lock:
                self._kept[name] = (failing, mono if changed else kept[1])
                self._event_rev[name] = max(self._event_rev[name], data['rev'])

    def _send_errors(self, name):
        """C-18.5: write `name`'s unsent `timer.error` events, oldest first, one
        thread at a time. One the store refuses (`OSError`, `sqlite3.Error`) stays,
        with those after it, and the refusal is raised; one it can never take
        (it cannot be encoded) is dropped, so it holds back none after it."""
        with self._sending[name]:
            while True:
                with self._lock:
                    if not self._unsent[name]:
                        return
                    event = self._unsent[name][0]
                try:
                    self.store.add_event('timer.error', data=event)
                except (OSError, sqlite3.Error):
                    raise
                except Exception:                   # noqa: BLE001 - never writable: dropped
                    with self._lock:
                        if self._unsent[name] and self._unsent[name][0] is event:
                            self._unsent[name].pop(0)
                    continue
                with self._lock:
                    if self._unsent[name] and self._unsent[name][0] is event:
                        self._unsent[name].pop(0)
                    told = self._told.get(name)
                    if told is None or told['first_at'] != event['first_at']:
                        told = self._told[name] = {'first_at': event['first_at'], 'kinds': set()}
                    told['mono'] = time.monotonic()
                    if len(told['kinds']) < FAILURE_KINDS_MAX:
                        told['kinds'].add((event['error_type'], event['raised_at']))

    def _publish_records(self):
        """C-18.5: rewrite RECORDS_FILE when a revision has changed since this
        process last wrote it, or when the file there is not the one it wrote
        (deleted, truncated or replaced; until it is written again, no revision
        counts as held by it). Records and revisions are copied in one critical
        section, so no entry pairs a record with another revision. Never raises:
        a write that fails is kept for `records_status()` (`daemon.status`,
        `status`, `doctor --live`), and the next run tries again."""
        path = self.root / RECORDS_FILE
        with self._records_lock:
            on_disk = file_identity(path)           # a stat, outside `_lock`
            with self._lock:
                revs = dict(self._rev)
                entries = {name: {'failure': value['failure'], 'rev': revs[name], 'last_run': value['last_run'],
                                  'last_error_type': value['last_error_type']}
                           for name, value in self._status.items()}
                at = iso(self.now())
                current = self._records_identity is not None and on_disk == self._records_identity
                if current and revs == self._file_rev:
                    return
            # Not current: the write below sets what the file holds, and a failed
            # one holds nothing. Both readers of `_file_rev` (`mark`, `stop`) run
            # this method first, which waits for this one, so neither reads it between.
            try:
                text = json.dumps({'version': 1, 'timers': entries}, sort_keys=True, allow_nan=False) + '\n'
                identity = write_records_file(path, text.encode())
            except Exception as exc:                # noqa: BLE001 - kept and shown, never raised
                with self._lock:
                    self._records_identity = None
                    self._file_rev = {name: 0 for name in TIMERS}
                    before = self._records_failure or {'since': at, 'tries': 0}
                    self._records_failure = {'since': before['since'], 'last_at': at, 'tries': before['tries'] + 1,
                                             'error_type': _utf8(type(exc).__name__), 'message': failure_message(exc)}
                return
            with self._lock:
                self._records_failure = None
                self._records_identity = identity
                self._file_rev = revs

    def records_status(self):
        """C-18.5: why RECORDS_FILE is behind (`since`, `last_at`, `tries`, the
        last error's type and message), or None while its writes succeed."""
        with self._lock:
            return dict(self._records_failure) if self._records_failure else None

    def tick(self):
        with self._lock:
            if not self.started or self.cancel.is_set():
                return
            for name, interval in self.intervals.items():
                if (name in self._running or self._due[name] > time.monotonic()
                        or name == 'probe' and 'reset_credits' in self._running):
                    continue
                self._running.add(name)
                self._due[name] = time.monotonic() + interval
                # Timestamp precision is seconds in the store; monotonic deadlines
                # below still bound fractional test intervals and per-lane work.
                self._status[name]['next_due'] = iso(self.now() + timedelta(seconds=interval))
                pool = (self._mirror if name in ('mirror', 'mirror_hot')
                        else self._cards if name == 'claude_cards' else self._cycles)
                pool.submit(self._run, name)

    def request(self, name, *, target=None):
        """Queue operator maintenance on the same workers and overlap guard."""
        if name not in ('probe', 'keepalive', 'reset_credits', 'claude_cards'):
            raise ValueError('unknown maintenance timer')
        if name == 'claude_cards' and 'claude_cards' not in self.intervals:
            return {'status': 'disabled', 'timer': name,
                    'detail': 'claude_cards.enabled is false in the policy; nothing was read.'}
        with self._lock:
            if self.cancel.is_set():
                return {'status': 'stopping', 'timer': name}
            if not self.started:
                return {'status': 'recovering', 'timer': name,
                        'detail': 'Daemon recovery has not finished; no maintenance was queued.',
                        'fix': 'Retry this command after startup recovery finishes; inspect subfleet daemon logs if it persists.'}
            if (name in self._running or name == 'reset_credits' and 'probe' in self._running
                    or name == 'probe' and 'reset_credits' in self._running):
                return {'status': 'already-running', 'timer': name}
            self._running.add(name)
        # C-3.7: the store write happens outside `_lock` (see `mark`). The name
        # is already running, so no tick starts it meanwhile; a failed write
        # gives it back rather than leaving it "already-running" for good.
        try:
            self.store.add_event('timer.requested', data={'timer': name, 'target': target})
        except BaseException:
            with self._lock:
                self._running.discard(name)
            raise
        callback = (lambda: self.reset_credits_cycle(target=target)) if name == 'reset_credits' else None
        (self._cards if name == 'claude_cards' else self._cycles).submit(self._run, name, callback)
        return {'status': 'scheduled', 'timer': name, 'target': target}

    def _run(self, name, callback=None):
        error = None
        result = None
        try:
            result = (callback or getattr(self, name + '_cycle'))()
            if name == 'probe':
                error = self._cycle_error
        except Exception as exc:
            # C-18.5: `mark` keeps its type, its scrubbed message and where it
            # was raised, and decides which failed runs the store is told of.
            error = exc
        finally:
            try:
                self._settle(name, error, result)
            finally:
                # C-18.5: whatever the record's own writes raise, the timer is
                # no longer running. A `timer.run` or `timer.error` write that
                # raised here used to skip this, and the timer was "already
                # running" until the daemon restarted: no tick started it again.
                with self._lock:
                    self._running.discard(name)

    def _settle(self, name, error, result):
        """Record one finished run: the probe's companions' deadlines, then the run."""
        companions = []
        if name == 'probe':
            # The automatic cadence starts at cycle completion. Every
            # subsequent cycle reads each usable lane; operator requests
            # may start another cycle before the automatic deadline.
            with self._lock:
                interval = self.intervals[name]
                self._due[name] = time.monotonic() + interval
                next_due = iso(self.now() + timedelta(seconds=interval))
                self._status[name]['next_due'] = next_due
                # These passes run inside probe_cycle, so their displayed
                # deadlines must follow its completion-based schedule too.
                # Preserve last_run/error if the cycle failed before them.
                for companion in ('reset_credits', 'alerts'):
                    status = self._status[companion]
                    status['next_due'] = next_due
                    companions.append({'timer': companion, **status, 'rev': self._rev[companion]})
        # A hot mirror pass runs every few seconds; the store keeps its
        # timer.run events forever and replays them at start, so only a pass
        # that changed something is recorded there for its own sake. `mark`
        # adds the passes C-18.5 records: a failed one the store is told of,
        # and a change between failing and not.
        try:
            for data in companions:             # outside `_lock` (C-3.7, see `mark`)
                self.store.add_event('timer.run', data=data)
                with self._lock:
                    self._event_rev[data['timer']] = max(self._event_rev[data['timer']], data['rev'])
        finally:
            self.mark(name, error=error, persist=name != 'mirror_hot' or bool(result))

    def mirror_cycle(self):
        """One desktop sidebar pass (C-23.28). Never calls a provider.

        The pass records itself in its own sidecar as it starts and again as it
        ends, so `doctor` judges the mirror from that file rather than from this
        timer's `last_run` — a pass that hangs must read as in flight for thirty
        minutes, not as a timer that merely has not reported yet.
        """
        from .sessions.mirror import options_from
        self._mirror_engine().run_once(options_from(self.policy))

    def mirror_hot_cycle(self):
        """One hot sidebar pass (C-23.28): spread sessions and sync changed flags.

        Returns whether it changed anything, which is what decides whether this
        run is worth a `timer.run` event.
        """
        from .sessions.mirror import options_from
        result = self._mirror_engine().run_hot(options_from(self.policy))
        return result.changed or result.state not in ('ok',)

    def _mirror_engine(self):
        from .sessions.mirror import Mirror
        if self._session_mirror is None:
            self._session_mirror = Mirror(self.root, self.policy, now=self.now, cancel=self.cancel)
        return self._session_mirror

    def current_demand(self, *, record=True, errors=None):
        """C-23.16 (a): the waiting jobs, as admission judges them, or None if unknown.

        A reader that raises is no demand: an unreadable queue never spends a
        credit. The failure is an event unless `record` is false (a preview
        writes nothing, C-19.1), and its type is appended to `errors` if given.
        """
        if self.demand is None:
            return None
        try:
            return list(self.demand())
        except Exception as exc:
            if errors is not None:
                errors.append(type(exc).__name__)
            if record:
                self.store.add_event('timer.error', data={'timer': 'reset_credits', 'stage': 'demand',
                                                          'error_type': type(exc).__name__})
            return None

    def evaluate_resets(self, snapshot, *, target=None):
        """C-23.16: one reset-credit evaluation, demand read only if it is needed.

        The timer and an operator's `reset codex <lane>` (`target`) both judge
        the same waiting demand; the operator's names the one lane a credit may
        go on. A demand reader that raised spends nothing, and says so: the
        result is `demand-error` with its `error_type`, so the timer's status
        shows it rather than a quiet `no-demand`. The error travels with this
        call, not on the component, so a preview on another thread cannot
        relabel it. The daemon's clock goes with it: the lanes are judged again
        at the spend, at the time it then reads (C-23.16 (c), (d)).
        """
        errors = []
        result = self.actions.evaluate(snapshot, now=self.now(), cancel=self.cancel,
                                       deadline=time.monotonic() + 60, target_lane_id=target,
                                       demand=lambda: self.current_demand(errors=errors), clock=self.now)
        if errors and result.get('status') in ('no-demand', 'demand-changed'):
            result = {**result, 'status': 'demand-error', 'error_type': errors[-1]}
        return result

    def reset_credits_cycle(self, *, target=None):
        snapshot = self.snapshot()
        result = self.evaluate_resets(snapshot, target=target)
        if result.get('status') == 'confirmed':
            lane_id = result['lane_id']
            row = next(row for row in snapshot['lanes'] if row['lane_id'] == lane_id)
            count = row.get('reset_credits_remaining')
            balance = {'action_id': result['action_id'],
                       'remaining': max(0, count - 1) if isinstance(count, int) else None}
            self.store.add_event('reset-credit.balance', lane_id=lane_id, data=balance)
            self.balances[lane_id] = balance
        self.store.add_event('timer.reset-credit', data=result)
        self.publish_status(self.snapshot())
        return result

    def publish_status(self, snapshot):
        """C-18.1, C-18.2, C-29.6: the one way `status.json` is written.

        Runs on a timer worker, never the control loop or a request thread. It
        adds what the snapshot lacks: batch labels for the displayed jobs, the
        conversation summary (read through its own read-only connection,
        `conversations.store.status_summary`), and the policy's short name for
        each Claude model id, which labels model-scoped windows. Last, and for
        the menu alone, it lays the probe leases admission honours
        (`fence_probes`): the reset-credit policy and the alerts judge the
        snapshot as it was built, so the slot an admission probe or a keepalive
        holds for its seconds neither triggers a credit nor raises an alert.

        The conversation reader never stops the file being written: it answers
        a store it cannot read with `available: false`, and anything it raises
        instead is published the same way, under the exception's name (C-29.6:
        unknown, never zeros), so lanes and jobs still reach the menu.
        """
        from .conversations.store import status_summary
        from .status_json import attach_batches, write_status
        attach_batches(self.store, snapshot)
        try:
            snapshot['conversations'] = status_summary(self.root)
        except Exception as exc:   # noqa: BLE001 - the file must still be written
            snapshot['conversations'] = {'available': False, 'error': type(exc).__name__}
        snapshot['model_names'] = {entry['id']: short for short, entry in self.policy.get('models', {}).items()
                                   if isinstance(entry, dict) and entry.get('id')}
        self.fence_probes(snapshot)
        snapshot['alerts'] = self.alerts.active()          # C-18.4: every publication
        if 'claude_cards' not in snapshot:
            snapshot['claude_cards'] = self.cards_view()   # C-9.10: every publication
        return write_status(self.root, snapshot, now=self.now())

    def probe_rows(self):
        """C-3.7, C-18.1: the probe leases and each holder's newest record, as
        `snapshot` reads them inside the store snapshot its rows come from, and
        as `Daemon._capacity_rows` reads them for admission."""
        leases = self.store.query(capacity.PROBE_LEASES)
        return {'leases': leases, 'records': {row['holder']: self.probe_record(row['holder'])
                                              if self.probe_record is not None else None for row in leases}}

    def fence_probes(self, view):
        """C-18.1: lay the probe leases admission honours over a view `enrich_view` judged.

        A lane whose slot a probe holds (a quarantined probe's for hours, C-5.7a)
        otherwise reads as dispatchable in status.json while admission refuses
        it. The leases are those `snapshot` read with the view's rows, in one
        committed state as admission reads them (C-3.7; a view without them
        reads them now), and are laid as `Daemon._capacity_view` lays them
        before `enrich_view`. They can only add a lane to `unavailable_lanes`
        and probes to `reserved_probes`, and `enrich_view` finds a lane
        dispatchable only while it is outside the one and the fleet is below its
        cap, if it has one, with the other, so clearing the verdict of each lane
        they reach is `enrich_view` judging again.
        """
        rows = view.pop('probe_rows', None) or self.probe_rows()
        capacity.mark_probe_leases(view, rows['leases'], rows['records'].get)
        full = self.fleet_full(view)
        for row in view['lanes']:
            if full or row['lane_id'] in view['unavailable_lanes']:
                row['dispatchable'] = False
        return view

    def fleet_full(self, view):
        """C-6.4: every active attempt and every probe's reservation counts toward
        `max_active_attempts`, when the policy sets one."""
        fleet_cap = policy_cap(self.policy.get('caps', {}), 'max_active_attempts')
        return (fleet_cap is not None and
                sum(view.get('in_flight', {}).values()) + view.get('reserved_probes', 0) >= fleet_cap)

    # --- Claude limit-reset cards and promotional credits (C-9.10) -----------

    def cards_view(self):
        """The card snapshot as status, `status.json` and alerts read it. A
        file that cannot be read is an empty view, never a failed publication;
        a sensor switched off shows nothing, so no alert outlives its reads."""
        if not claude_cards.settings(self.policy)['enabled']:
            return {'read_at': None, 'accounts': [], 'warnings': [], 'disabled': True}
        try:
            return claude_cards.load_view(self.root, self.policy, self.now())
        except Exception as exc:                        # noqa: BLE001 - see docstring
            return {'read_at': None, 'accounts': [], 'warnings': [], 'error_type': type(exc).__name__}

    def _card_lanes(self):
        """C-9.10: the Claude lanes, each marked `held` when an operator hold is in force.

        A hold is read from the closures and from its own `lane.held` and
        `lane.released` events. `Store.put_closure` now keeps a hold's row apart
        from any limit (C-9.6, D-HOLD1), but in a store written before that, a
        hold that ended before an open provider limit on the account scope left
        no `operator-hold` row, though it is in force. Releasing such a hold
        changes no row and so writes no `lane.released` event; the hold then
        lasts until its own end, which errs toward spending nothing.
        """
        now = iso(self.now())
        held = {row['lane_id'] for row in self.store.query(
            "SELECT DISTINCT lane_id FROM closures WHERE reason='operator-hold' AND released_at IS NULL AND until_at>?",
            (now,))}
        until = {}
        for row in self.store.query("SELECT lane_id,kind,data_json FROM events WHERE kind IN ('lane.held','lane.released') "
                                    "ORDER BY event_id"):
            if row['kind'] == 'lane.released':
                until.pop(row['lane_id'], None)
                continue
            try:
                value = json.loads(row['data_json'] or '{}').get('until')
            except ValueError:
                continue
            if isinstance(value, str) and value > until.get(row['lane_id'], ''):
                until[row['lane_id']] = value
        held |= {lane_id for lane_id, value in until.items() if value > now}
        return [{'lane_id': row['lane_id'], 'identity': row['identity'], 'label': row['label'],
                 'account_key': row['account_key'], 'enabled': bool(row['enabled']),
                 'held': row['lane_id'] in held}
                for row in self.store.query("SELECT lane_id,identity,label,account_key,enabled FROM lanes "
                                            "WHERE provider='claude' ORDER BY created_at,rowid")]

    def _card_sensor(self):
        if self._cards_sensor is None:
            from .adapters.claude import ENROLL_MODEL, ENROLL_PROMPT, home_login
            claude_bin = getattr(self.adapter_factory('claude'), 'claude_bin', 'claude')
            # C-23.47: the CLI renews its own login when it runs; one minimal
            # turn under the login's folder is the heal, and the sensor only
            # reads the store the CLI wrote.
            self._cards_sensor = claude_cards.Sensor(
                login_reader=home_login,
                heal=lambda home: claude_cards.heal_turn(home, claude_bin=claude_bin, model=ENROLL_MODEL,
                                                         prompt=ENROLL_PROMPT, cancel=self.cancel),
                version=lambda: claude_cards.cli_version(claude_bin),
                now=self.now)
        return self._cards_sensor

    def claude_cards_cycle(self):
        """C-9.10: read every login's cards, credits and plan; publish the snapshot.

        Its requests are GETs, never a claim: no code path here or in
        `claude_cards` can redeem a card or claim a credit. It is not free of
        cost: it may spend one heal turn (C-23.47) on each expired login that
        backs a lane, unless an operator hold covers one of its lanes, at most
        once per `claude_cards.heal_interval_min` per login. Usage reads are
        paced with the probe's (C-9.9).
        """
        if self.cancel.is_set():
            return None
        config = claude_cards.settings(self.policy)
        path = self.root / claude_cards.SNAPSHOT_FILE
        previous = claude_cards.read_snapshot(path)
        snapshot = claude_cards.refresh(
            self._card_sensor(),
            logins=claude_cards.discover_logins(claude_cards.logins_folder(self.root, self.policy)),
            lanes=self._card_lanes, previous=previous, heal=bool(config['heal']),
            heal_after_s=float(config['heal_interval_min']) * 60, pace=self._pace_usage,
            stop=self.cancel.is_set, checkpoint=lambda partial: claude_cards.write_snapshot(path, partial))
        claude_cards.write_snapshot(path, snapshot)
        summary = {'read_at': snapshot['read_at'],
                   'accounts': {row['login']: {'status': row.get('status'), 'lanes': row.get('lanes', []),
                                               'unused': sum(grant['resets_left'] for grant in
                                                             claude_cards.unused_cards(row, self.now()))}
                                for row in snapshot['accounts']},
                   'heals': sorted(row['login'] for row in snapshot['accounts'] if row.get('healed'))}
        self.store.add_event('timer.claude-cards', data=summary)
        return summary

    def stop(self):
        with self._lock:
            self.cancel.set()
        self._cycles.shutdown(wait=True, cancel_futures=True)
        self._lanes.shutdown(wait=True, cancel_futures=True)
        self._mirror.shutdown(wait=True, cancel_futures=True)
        self._cards.shutdown(wait=True, cancel_futures=True)
        # C-18.5: a last try at what refused writes left: the records file, then
        # for each timer on its own (one refusal stops no other), its events and a
        # `timer.run` for a revision in neither.
        self._publish_records()
        for name in TIMERS:
            try:
                self._send_errors(name)
            except Exception:                   # noqa: BLE001 - the store may refuse or be gone
                pass
            try:
                with self._lock:
                    pending = (self._rev[name] > max(self._event_rev[name], self._file_rev[name])
                               and {'timer': name, **self._status[name], 'rev': self._rev[name]})
                if pending:
                    self.store.add_event('timer.run', data=pending)
                    with self._lock:
                        self._event_rev[name] = max(self._event_rev[name], pending['rev'])
            except Exception:                   # noqa: BLE001
                pass

    def record_auth_dead(self, lane_id):
        meta = {'verdict': 'auth-dead', 'probe_status': 'auth-dead'}
        self.store.add_event('timer.verdict', lane_id=lane_id, data=meta)
        self._set_verdict(lane_id, meta)

    def _set_verdict(self, lane_id, verdict):
        """Replace a lane's verdict whole; None removes it."""
        with self._verdicts:
            if verdict is None:
                self.metadata.pop(lane_id, None)
            else:
                self.metadata[lane_id] = verdict

    def _epoch(self, lane):
        try:
            # Only a regular file, never waiting in open(): Timers.stop() waits for this worker.
            raw = json.loads(read_regular(Path(lane.home or lane.credential.ref).expanduser() / 'auth.json'))
            return raw.get('last_refresh', lane.credential.epoch)
        except (OSError, ValueError, TypeError):
            return lane.credential.epoch

    def _identities(self):
        # Binding order, not lane-number lexicography, defines canonical identity.
        rows = self.store.query('SELECT * FROM lanes ORDER BY created_at,rowid')
        seen = {}
        for row in rows:
            if row['enabled'] and self.store.one("SELECT 1 FROM closures WHERE lane_id=? AND reason='auth-dead' AND released_at IS NULL", (row['lane_id'],)):
                self.store.update_lane(row['lane_id'], enabled=0)
                row['enabled'] = False
                data = {'verdict': 'auth-dead', 'probe_status': 'auth-dead'}
                self.store.add_event('timer.verdict', lane_id=row['lane_id'], data=data)
                self._set_verdict(row['lane_id'], data)
            if not row['enabled']:
                continue
            first = seen.setdefault(row['account_key'], row)
            if first['lane_id'] != row['lane_id']:
                with self.store.transaction('lane.noncanonical', lane_id=row['lane_id']):
                    self.store.update_lane(row['lane_id'], enabled=0)
                    data = {'identity_status': 'non-canonical', 'duplicate_of': first['home'] or first['lane_id'],
                            'verdict': 'duplicate', 'home': row['home']}
                    self.store.add_event('timer.verdict', lane_id=row['lane_id'], data=data)
                self._set_verdict(row['lane_id'], data)
        # Desktop Codex identity is observed read-only; it is never a lane.
        try:
            from .adapters.codex import _identity, _read_auth
            from .home_paths import codex_app_home
            identity = _identity(_read_auth(codex_app_home()))
            self._app_account = 'codex:' + str(identity.get('account_id') or identity.get('email') or '')
        except (OSError, ValueError):
            self._app_account = None

    def _never_read(self, lane_id):
        """C-18.1: a lane no timer reads, busy or idle: disabled, not v2's, the desktop's, held or `auth-dead`."""
        current = self.store.get_lane(lane_id)
        if not current or not current.enabled or current.owner != 'v2' or current.desktop:
            return True
        return bool(self.store.one("SELECT 1 FROM closures WHERE lane_id=? AND reason IN ('auth-dead','operator-hold') AND released_at IS NULL AND until_at>?", (lane_id, iso(self.now()))))

    def _claim(self, lane, purpose):
        """C-18.1, C-18.3: `slot:0` for an idle lane, `BUSY` for one at work, None for one never read.

        One transaction judges the lane, so idle and busy are decided against one
        state. Whether the lane is read at all is asked before its occupancy.
        """
        holder = 'probe:timer:' + str(uuid4())
        with self.store.transaction('timer.reserved', lane_id=lane.lane_id):
            if self._never_read(lane.lane_id):
                return None
            if (self.store.one("SELECT 1 FROM attempts WHERE lane_id=? AND state IN ('reserved','starting','running','finalizing')", (lane.lane_id,))
                    or self.store.one('SELECT 1 FROM leases WHERE lease_key LIKE ?', (f'lane:{lane.lane_id}:%',))):
                return BUSY
            self.store.acquire_lease(f'lane:{lane.lane_id}:slot:0', holder)
            self.store.add_event('timer.reservation', lane_id=lane.lane_id,
                                 data={'holder': holder, 'purpose': purpose})
        with self._lock:
            self.active_holders.add(holder)
        return holder

    def _reserve(self, lane, purpose):
        """A timer turn's hold (a keepalive): `slot:0` on an idle lane, else None."""
        claim = self._claim(lane, purpose)
        return None if claim is BUSY else claim

    def _release(self, holder, *, quarantined=False):
        if not quarantined:
            self.store.release_leases(holder)
        with self._lock:
            self.active_holders.discard(holder)

    def _turn(self, lane, purpose, holder, timeout):
        if not self.turn:
            return Outcome(OutcomeClass.UNKNOWN, 'guardian turn unavailable')
        return self.turn(lane, purpose, holder, cancel=self.cancel, deadline=time.monotonic() + timeout)

    def _read_probe(self, adapter, lane, env):
        # urllib has socket deadlines. An injected/misbehaving opener is fenced
        # too: four outstanding reads maximum, one per home, no late DB writes.
        key = lane.lane_id
        with self._lock:
            if key in self._io_busy or not self._io_slots.acquire(blocking=False):
                raise TimeoutError("probe reader still occupied")
            self._io_busy.add(key)
        done, values = threading.Event(), []
        def read():
            try:
                if hasattr(adapter, 'probe_status'):
                    values.append(adapter.probe_status(lane, env))
                else:
                    readings = adapter.probe(lane, env)
                    values.append({'status': 'ok' if readings else 'unknown', 'readings': readings})
            except BaseException as exc:
                values.append(exc)
            finally:
                with self._lock:
                    self._io_busy.discard(key)
                self._io_slots.release()
                done.set()
        threading.Thread(target=read, daemon=True, name='subfleet-usage-read').start()
        deadline = time.monotonic() + min(15, self.policy.get('caps', {}).get('probe_timeout_s', 60))
        while not done.wait(.02):
            if self.cancel.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("usage deadline")
        if isinstance(values[0], BaseException):
            raise values[0]
        return values[0]

    def _probe_lane(self, lane):
        if self.cancel.is_set() or lane.lane_id in self._io_busy:
            return None
        previous = self.metadata.get(lane.lane_id, {})
        epoch = self._epoch(lane) if lane.provider == 'codex' else lane.credential.epoch
        if previous.get('revoked_epoch') == epoch:
            return None
        # C-18.1: a cycle reads every usable lane, even when an operator requests
        # another cycle immediately. The cycle's completion-based cadence owns
        # scheduling; an attempt's reading or the preceding probe never skips it.
        until = previous.get('retry_after_until')
        if until and self.now() < instant(until):
            return None     # C-9.9: the usage endpoint asked us to wait
        if self._usage_wait(lane):
            return None
        holder = self._claim(lane, 'probe')
        if holder is BUSY:
            # C-18.3: both providers have usage sensors that spend no turn. Read
            # beside the attempts, without holding a lease or healing a token.
            # Defer all busy reads until the idle homes have healed, published
            # and released their slots. Busy pacing/network calls must not
            # extend any idle lane's unavailability (C-18.3).
            return lane, None, BUSY
        if not holder:
            return None
        quarantined = False
        try:
            if lane.provider == 'codex':
                adapter = self.adapter_factory('codex')
                if hasattr(adapter, 'timeout'):
                    adapter.timeout = min(15, self.policy.get('caps', {}).get('probe_timeout_s', 60))
                env = resolve_credential(lane.credential)
                probe = self._read_probe(adapter, lane, env)
                if probe.get('status') == 'expired-token':
                    heals = self._latest('timer.heal')
                    prior = heals.get(lane.lane_id, {})
                    if prior.get('epoch') != epoch and (not prior.get('at') or (self.now() - instant(prior['at'])).total_seconds() >= 1200):
                        # Commit the single allowance before launching the CLI.
                        self.store.add_event('timer.heal', lane_id=lane.lane_id, data={'epoch': epoch, 'at': iso(self.now())})
                        outcome = self._turn(lane, 'heal', holder, 60)
                        quarantined = outcome.evidence.get('probe_quarantined', False)
                        if 'revoked' in outcome.detail.lower():
                            probe = {'status': 'revoked', 'readings': (), 'revoked_epoch': epoch}
                        elif not quarantined:
                            probe = self._read_probe(adapter, lane, env)
                if probe.get('status') in ('revoked', 'auth-revoked'):
                    probe['revoked_epoch'] = epoch
                if probe.get('status') == 'expired-token' and \
                        self._latest('timer.heal').get(lane.lane_id, {}).get('epoch') == epoch:
                    # C-11.8: the one heal this credential epoch allows has run and left
                    # the token expired; only a new login (a new epoch) ends it now.
                    probe['heal_spent'] = True
            else:
                # C-9.9: the periodic Claude probe is the usage endpoint, never a
                # model turn (a turn on a Fable-bearing account spends the shared
                # week). The pre-launch probe (C-11.4) and keepalive still spend one.
                adapter = self.adapter_factory('claude')
                env = resolve_credential(lane.credential)
                self._pace_usage()
                probe = self._read_probe(adapter, lane, env)
                if probe.get('status') == 'expired-token' and lane.credential.kind == 'home':
                    # C-23.47 for Claude homes: the CLI refreshes its own keychain
                    # login when it runs, so one minimal turn under this home heals
                    # it; at most one such turn per 20 minutes per credential epoch.
                    heals = self._latest('timer.heal')
                    prior = heals.get(lane.lane_id, {})
                    if prior.get('epoch') != epoch or (not prior.get('at') or (self.now() - instant(prior['at'])).total_seconds() >= 1200):
                        self.store.add_event('timer.heal', lane_id=lane.lane_id, data={'epoch': epoch, 'at': iso(self.now())})
                        outcome = self._turn(lane, 'heal', holder, 60)
                        quarantined = outcome.evidence.get('probe_quarantined', False)
                        if not quarantined:
                            self._pace_usage()
                            probe = self._read_probe(adapter, lane, env)
                if probe.get('retry_after_s'):
                    probe['retry_after_until'] = iso(self.now() + timedelta(seconds=int(probe['retry_after_s'])))
            return self._probe_result(lane, probe, None)
        except (TimeoutError, OSError) as exc:
            return self._probe_result(lane, {'status': 'network-error', 'readings': (), 'error_type': type(exc).__name__}, None)
        except Exception as exc:
            return self._probe_result(lane, {'status': 'unknown', 'readings': (), 'error_type': type(exc).__name__}, None)
        finally:
            self._probe_holders[lane.lane_id] = (holder, quarantined)

    def _usage_wait(self, lane):
        until = self._usage_backoff.get(lane.lane_id, {}).get('next_read_at')
        return bool(until and self.now() < instant(until))

    def _probe_result(self, lane, probe, opened):
        if lane.provider == 'claude' and (probe.get('status') not in ('ok', 'limited') or not probe.get('readings')):
            # Reuse the existing completion-based probe cadence. Retry-After
            # may lengthen it; no invented provider reading or auth latch.
            delay = max(self.intervals['probe'], int(probe.get('retry_after_s') or 0))
            # Policy intervals may be fractional. Whole-second truncation
            # would let a requested repeat beat both cadence and Retry-After.
            until = (self.now() + timedelta(seconds=delay)).astimezone(timezone.utc)
            data = {'next_read_at': until.isoformat().replace('+00:00', 'Z'),
                    'status': probe.get('status', 'unknown')}
            self.store.add_event('timer.usage-backoff', lane_id=lane.lane_id, data=data)
            self._usage_backoff[lane.lane_id] = data
        return lane, {**probe, 'probed_at': iso(self.now())}, opened

    def _busy_read(self, lane):
        """C-18.3: a busy Claude or Codex lane's usage read beside its attempts.

        The usage GET alone. It takes no lease and writes no reservation, so it
        never holds a slot a job could take, and it runs no heal turn: the lane's
        attempts renew its token. It goes through the per-lane read fence an idle
        read does (`_read_probe`). The newest event as it starts travels with the
        result, so that publishing it never releases a limit reported after it
        (`_mark`, `_publishable`). Whatever the mark's query, the adapter or the
        request raises is a read that failed, never a cycle that failed.
        """
        if self.cancel.is_set() or self._never_read(lane.lane_id) or self._usage_wait(lane):
            return None
        opened = 0
        try:
            opened = self._mark()
            adapter = self.adapter_factory(lane.provider)
            if hasattr(adapter, 'timeout'):
                adapter.timeout = min(15, self.policy.get('caps', {}).get('probe_timeout_s', 60))
            if lane.provider == 'claude':
                self._pace_usage()
            probe = {**self._read_probe(adapter, lane, resolve_credential(lane.credential))}
            if lane.provider == 'claude' and probe.get('retry_after_s'):
                probe['retry_after_until'] = iso(self.now() + timedelta(seconds=int(probe['retry_after_s'])))
        except (TimeoutError, OSError) as exc:
            probe = {'status': 'network-error', 'readings': (), 'error_type': type(exc).__name__}
        except Exception as exc:
            probe = {'status': 'unknown', 'readings': (), 'error_type': type(exc).__name__}
        return self._probe_result(lane, probe, opened)

    def _mark(self):
        """The newest event as a busy read starts: a mark no later limit report slips past.

        `put_closure` keeps one open closure per lane, scope and kind (a hold
        or a limit). It extends that row in place, and a limit reported again
        that ends no later changes nothing in it. Every report leaves a `closure.recorded` event all the
        same (`Store.put_closure`), whoever made it: an attempt, a conversation
        turn, an admission probe, an operator's hold. Retention prunes jobs and
        their attempts, never events, so it cannot take the trace away.
        """
        return (self.store.one('SELECT MAX(event_id) AS mark FROM events') or {}).get('mark') or 0

    def _publishable(self, lane, probe, opened):
        """C-18.3: what a busy read may publish, judged as it is published.

        None publishes nothing. A Claude 429 publishes its Retry-After cooldown
        only; existing provider readings keep their original observation times.
        A credential verdict (`auth-dead`, `revoked`,
        `expired-token`, `no-auth`) or a failed read is never published from a
        busy lane: its attempts are renewing its token as it is read, and they
        report a dead credential themselves (C-23.44). A credential naming
        another account is published as an idle read publishes it (no provider
        readings, the lane disabled). Nothing is published for a lane that
        `_claim` would no longer read: disabled, transferred, made the desktop's
        or held since the read. (A lane's binding never changes; `Store.put_lane`.)

        False publishes the readings and the verdict but releases no closure and
        settles no reset credit: a limit was reported on the lane after the read
        began (a closure recorded, extended, or reported again), and an older
        answer must not undo a newer limit. The next cycle reads again. True
        publishes everything an idle read would.
        """
        account = probe.get('account_key')
        mismatch = bool(account) and account != lane.account_key
        cooldown = (lane.provider == 'claude' and probe.get('status') == 'rate-limited'
                    and bool(probe.get('retry_after_until')))
        if not mismatch and probe.get('status') not in ('ok', 'limited') and not cooldown:
            return None
        if self._never_read(lane.lane_id):
            return None
        return not self.store.one("SELECT 1 FROM events WHERE kind='closure.recorded' AND event_id>? AND lane_id=? LIMIT 1",
                                  (opened, lane.lane_id))

    def _pace_usage(self):
        """C-9.9: one usage read at a time, `reserve.usage_spacing_s` apart."""
        spacing = float((self.policy.get('reserve') or {}).get('usage_spacing_s', 3))
        with self._usage_lock:
            now = time.monotonic()
            wait = max(0.0, self._usage_next - now)
            self._usage_next = max(now, self._usage_next) + spacing
        if wait > 0:
            self.cancel.wait(wait)

    def _publish_busy(self, lane, probe, opened):
        """C-18.3: judge a busy read and publish it in one transaction; what `_publishable` judged.

        None was not published, False was published with nothing released (a
        newer limit fenced it), True was published whole.

        No slot keeps the lane's attempts away, so the store's write lock does:
        nothing can report a limit, disable the lane or hold it between the
        judgement and the commit. A publication that raises rolls back whole,
        the settlement with it, and the verdict the lane had is put back while
        the lock is still held. When it is the commit that fails, the lock is
        already gone, and the verdict is put back only if nothing has replaced
        this publication's since.
        """
        before = wrote = missing = object()
        try:
            with self.store.transaction('timer.busy-read', lane_id=lane.lane_id):
                before = self.metadata.get(lane.lane_id)       # under the lock an attempt's verdict needs
                try:
                    releases = self._publishable(lane, probe, opened)
                    if releases is not None:
                        self._persist(lane, probe, releases=releases)
                except BaseException:
                    self._set_verdict(lane.lane_id, before)
                    raise
                wrote = self.metadata.get(lane.lane_id)
        except BaseException:
            # The commit itself failed, after the store lock was let go. Put the
            # old verdict back only if this publication's is still the one in
            # place, checked and replaced as one step: an attempt's verdict
            # recorded since stays.
            with self._verdicts:
                if wrote is not missing and wrote is not before and self.metadata.get(lane.lane_id) is wrote:
                    self._set_verdict(lane.lane_id, before)
            raise
        return releases

    def _persist(self, lane, probe, *, releases=True):
        """Publish one read: readings, closures, settlement, then the verdict.

        `releases=False` (a busy read that a newer closure contradicts, C-18.3)
        keeps every closure and reset credit as it stands.
        """
        at = iso(self.now())
        status = probe.get('status', 'unknown')
        outcome = probe.get('outcome')
        readings = tuple(Reading(**r) if isinstance(r, dict) else r for r in probe.get('readings', ()))
        meta = {k: v for k, v in probe.items() if k not in ('readings', 'outcome', 'account_key', 'detail')}
        actual_account = probe.get('account_key')
        if actual_account and actual_account != lane.account_key:
            readings = ()
            meta['identity_status'] = 'mismatch'
            meta['observed_account_key'] = actual_account
            # C-11.2: the email the probe read is the other account's; the lane
            # must not answer to it as a pin name.
            if 'email' in meta:
                meta['observed_email'] = meta.pop('email')
            status = 'identity-mismatch'
            meta['reset_credits'] = {'available': None, 'applicable': None}
        previous = self.metadata.get(lane.lane_id, {})
        if 'email' not in meta and previous.get('email') and status != 'identity-mismatch':
            # C-11.2: a probe that could not read the account (a network error)
            # does not unname it; the pin roster and the view keep the email the
            # last answering probe reported for this credential.
            meta['email'] = previous['email']
        meta['probe_status'] = status
        meta['verdict'] = {'ok': 'ok', 'auth-dead': 'auth-dead', 'revoked': 'auth-revoked',
                           'expired-token': 'auth-suspect'}.get(status, status)
        if status != 'identity-mismatch' and releases:
            self.actions.settle_by_usage(lane.lane_id, probe, now=self.now())
        with self.store.transaction('timer.probe', lane_id=lane.lane_id):
            if status in ('auth-dead', 'identity-mismatch'):
                self.store.update_lane(lane.lane_id, enabled=0)
            for reading in readings:
                self.store.add_reading(replace(reading, attempt_id=None))
            if not readings:
                self.store.add_reading(Reading(lane.lane_id, 'account', 'admission', None, None,
                                              ReadingLabel.UNKNOWN, 'probe', at))
            override = self.actions.confirmed_override(lane.lane_id, now=self.now())
            if status != 'identity-mismatch' and outcome and outcome.closure and not override:
                self.store.add_closure(outcome.closure)
            if status != 'identity-mismatch' and (probe.get('limit_reached') is True or status == 'limited') and not override:
                reset = max((r.resets_at for r in readings if r.resets_at), default=None)
                self.store.add_closure(Closure(lane.lane_id, 'account', reset or iso(self.now() + timedelta(hours=1)),
                    ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED if reset else ClockSource.GUESSED, 'wham'))
            elif status == 'ok' and probe.get('limit_reached') is False and releases:
                # Release only after real server evidence of a reset, not absence of numbers.
                if any(r.label == ReadingLabel.PROVIDER and r.utilization is not None and r.utilization < 1 for r in readings):
                    with self.store.transaction('closure.reset') as tx:
                        tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND scope='account' AND reason='provider-limit' AND released_at IS NULL", (at, lane.lane_id))
            self.store.add_event('timer.verdict', lane_id=lane.lane_id, data=meta)
        self._set_verdict(lane.lane_id, meta)

    def snapshot(self):
        at = self.now()
        # One committed state for the whole view. C-3.7: a read snapshot, not a
        # write transaction, and only for the reads: the view (every lane,
        # reading, closure, attempt and job) is built after it, holding nothing.
        with self.store.snapshot():
            rows = capacity.store_rows(self.store, now=at)
            extra = self.view_rows(rows['lanes'])
            # C-18.1: read with the rows; laid only for status.json, after the
            # reset-credit policy and the alerts judged the view (`publish_status`).
            probes = self.probe_rows()
        # C-10.3: the published capacity (status.json) judges the desktop lane as
        # admission does (review of PR #72's plan: without the signal it read the
        # lane excluded while admission placed work there).
        in_use = self.desktop_in_use() if self.desktop_in_use is not None else None
        view = capacity.build_view(**rows, now=at, reading_ttl_s=self.policy.get('caps', {}).get('reading_ttl_s', 120),
                                   desktop_in_use=in_use)
        view = self.enrich_view(view, extra)
        view['probe_rows'] = probes
        return view

    def view_rows(self, lanes=()):
        """What `enrich_view` reads from the store, to be read inside the snapshot
        the view's own rows come from (C-3.7). `lanes` are that snapshot's lane
        rows: the override context's lane lookups are answered from them, so the
        build reads nothing."""
        overrides = self.actions.override_context()      # read once for every lane (C-3.7)
        overrides['lanes'].update({row['lane_id']: Store.lane_from_row(row) for row in lanes})
        return {'enabled': self.store.query('SELECT * FROM lanes WHERE enabled=1 ORDER BY created_at,rowid'),
                'overrides': overrides}

    def merge_lane(self, row):
        """What the last probe found of a lane (`metadata`), laid over its row, as a view has it.

        C-6.3's check inside a reservation merges the lane rows it reads the same
        way. `metadata` is replaced whole per lane, never changed in place, so
        another thread reads one probe's finding or the next's."""
        row.update(self.metadata.get(row['lane_id'], {}))
        return row

    def enrich_view(self, view, rows=None):
        rows = rows or self.view_rows()
        # Re-enrolment creates a new lane id. The previous binding stays in the
        # ledger but no longer supplies the home's active credential condition.
        bindings = {}
        for lane in rows['enabled']:
            bindings[(lane['provider'], lane['home'] or lane['credential_ref'])] = lane['lane_id']
        overrides = rows['overrides']
        full = self.fleet_full(view)
        for row in view['lanes']:
            self.merge_lane(row)
            bound = bindings.get((row['provider'], row['home'] or row['credential_ref']))
            if not row['enabled'] and bound and bound != row['lane_id']:
                row['superseded_by'] = bound
            row['app_shadowed'] = row.get('app_shadowed', False) or row['account_key'] == getattr(self, '_app_account', None)
            row['probe'] = dict(self.metadata.get(row['lane_id'], {}), status=row.get('probe_status', 'unknown'))
            row['probe']['readings'] = row['readings']
            if capacity.credential_latched(row):
                view.setdefault('unavailable_lanes', {})[row['lane_id']] = 'credential-latched'
            row['reset_credits_remaining'] = (row.get('reset_credits') or {}).get('available')
            # The view's own clock, as `Daemon._pick` asks it: one view decides an
            # override once, so its readings are not relabelled here on one side of
            # the override's end and kept there on the other (C-6.3).
            override = self.actions.confirmed_override(row['lane_id'], now=view.get('now') or self.now(),
                                                       context=overrides)
            if override:
                balance = self.balances.get(row['lane_id'], {})
                if balance.get('action_id') == override['action_id']:
                    current, remaining = row['reset_credits_remaining'], balance.get('remaining')
                    row['reset_credits_remaining'] = min(current, remaining) if isinstance(current, int) and isinstance(remaining, int) else None
                row['reset_override'] = override
                row['verdict'] = 'admission-observed'
                for reading in row['readings']:
                    if reading['label'] == 'provider':
                        reading['label'] = 'stale-provider'
            measured = [r for r in row['readings'] if capacity.fresh_provider(r, now=self.now(),
                        reading_ttl_s=self.policy.get('caps', {}).get('reading_ttl_s', 120))]
            headroom_ok = override or not any(r['utilization'] >= 1 - self.policy.get('headroom_floor', .15) for r in measured if r['scope'] == 'account')
            caps = self.policy.get('caps', {})
            # C-6.4: no count caps unless the policy sets them; C-10.3: the desktop
            # login's lane is dispatchable while Claude Code is not using it.
            slot_cap = lane_slot_cap(caps, bool(measured) and not override)
            row['dispatchable'] = bool(headroom_ok and row['enabled'] and row['owner'] == 'v2' and not capacity.desktop_excluded(row) and
                                       not row['closures'] and row.get('revoked_epoch') is None and row.get('probe_status') not in ('auth-dead', 'revoked', 'expired-token', 'no-auth') and
                                       row['lane_id'] not in view.get('unavailable_lanes', {}) and
                                       (slot_cap is None or row['in_flight'] < slot_cap) and
                                       not full)
        return view

    def probe_cycle(self):
        if self.cancel.is_set():
            return
        self._cycle_error = None
        self._identities()
        # An immediately requested next cycle may inherit spacing from its
        # predecessor's last busy Claude read. Pay that wait before taking
        # ANY idle lease, including Codex's; it cannot extend an idle hold.
        with self._usage_lock:
            wait = max(0.0, self._usage_next - time.monotonic())
        if wait > 0:
            self.cancel.wait(wait)
        if self.cancel.is_set():
            return
        futures = [self._lanes.submit(self._probe_lane, lane) for lane in self.store.list_lanes()
                   if lane.enabled and lane.owner == 'v2']
        # Every holder this cycle took is released, whatever raised: one lane's
        # failure must not leave another lane's `slot:0` held until a restart
        # (review of PR #72). All homes heal before any cycle reading or verdict
        # is published. A busy lane's read is judged as it is published (C-18.3);
        # what it may not publish, and what a newer limit fenced, is named on the
        # cycle's event.
        results, failure, deferred, fenced, busy = [], None, {}, [], []
        try:
            for future in as_completed(futures):
                try:
                    if (value := future.result()):
                        if value[2] is BUSY:
                            busy.append(value[0])
                        else:
                            results.append(value)
                except Exception as exc:           # re-raised once every holder is released
                    failure = failure or exc
            if self.cancel.is_set():
                return
            for lane, probe, opened in results:
                if opened is None:
                    self._persist(lane, probe)
                elif (published := self._publish_busy(lane, probe, opened)) is None:
                    deferred[lane.lane_id] = probe.get('status', 'unknown')
                elif not published:
                    fenced.append(lane.lane_id)
        finally:
            for holder, quarantined in self._probe_holders.values():
                self._release(holder, quarantined=quarantined)
            self._probe_holders.clear()
        if failure is not None:
            raise failure
        # The same four-worker/read fences apply, but no idle probe lease is
        # held across busy reads, their Claude pacing, or their publication.
        futures = [self._lanes.submit(self._busy_read, lane) for lane in busy]
        busy_results = [value for future in as_completed(futures) if (value := future.result())]
        results.extend(busy_results)
        for lane, probe, opened in busy_results:
            if (published := self._publish_busy(lane, probe, opened)) is None:
                deferred[lane.lane_id] = probe.get('status', 'unknown')
            elif not published:
                fenced.append(lane.lane_id)
        if self.cancel.is_set():
            return
        # Every read counts here, published or not: a busy lane's network error
        # is as much evidence of being offline as an idle one's.
        self._cycle_error = next((p['error_type'] for _, p, _ in results if p.get('error_type')), None)
        codex = [p for lane, p, _ in results if lane.provider == 'codex']
        offline = bool(codex) and all(p.get('status') == 'network-error' for p in codex)
        snapshot = self.snapshot()
        if not offline:
            try:
                result = self.evaluate_resets(snapshot)
            except Exception as exc:
                # C-18.5: the pass is reset_credits' run, and an exception ended
                # it; the cycle ends with it, as before, and with its error, not
                # one a write of its record raised.
                try:
                    self.mark('reset_credits', error=exc, next_due=self.status()['probe']['next_due'])
                except Exception:                   # noqa: BLE001 - `exc` is the one to raise
                    pass
                raise
            self.mark('reset_credits', error=result.get('error_type'), next_due=self.status()['probe']['next_due'])
            if result.get('status') == 'confirmed':
                row = next(row for row in snapshot['lanes'] if row['lane_id'] == result['lane_id'])
                count = row.get('reset_credits_remaining')
                balance = {'action_id': result['action_id'], 'remaining': max(0, count - 1) if isinstance(count, int) else None}
                self.store.add_event('reset-credit.balance', lane_id=row['lane_id'], data=balance)
                self.balances[row['lane_id']] = balance
            snapshot = self.snapshot()
            snapshot['reset_policy'] = result
        snapshot['offline'] = offline
        snapshot['claude_cards'] = self.cards_view()       # C-9.10: alerts and status.json read it
        try:
            self.alerts.evaluate(snapshot, now=self.now(), offline=offline)
        except Exception as exc:
            try:                                    # C-18.5, as above
                self.mark('alerts', error=exc, next_due=self.status()['probe']['next_due'])
            except Exception:                       # noqa: BLE001
                pass
            raise
        self.mark('alerts', next_due=self.status()['probe']['next_due'])
        self.publish_status(snapshot)
        self.store.add_event('timer.cycle', data={'offline': offline, 'at': iso(self.now()),
                             'lanes': [lane.lane_id for lane, _, _ in results],
                             'busy': [lane.lane_id for lane, _, opened in results if opened is not None],
                             'deferred': deferred, 'fenced': fenced})
        return snapshot

    def latest_request(self, lane_id):
        timestamps = []
        for row in self.store.list_attempts():
            if row['lane_id'] != lane_id or (row.get('rc') == 5 and not row.get('native_session_id')):
                continue
            value = row.get('started_at')
            if value:
                timestamps.append(instant(value))
        for row in self.store.list_readings(lane_id):
            if row['source'] == 'keepalive' or row['label'] == 'admission-observed':
                timestamps.append(instant(row['observed_at']))
        for row in self.store.query("SELECT data_json FROM events WHERE kind='timer.request' AND lane_id=?", (lane_id,)):
            data = json.loads(row['data_json'])
            if data.get('requested_at') and not (data.get('rc') == 5 and not data.get('native_session_id')):
                timestamps.append(instant(data['requested_at']))
        return max((value for value in timestamps if value <= self.now()), default=None)

    def _keepalive_lane(self, lane):
        if self.cancel.is_set():
            return
        recent = self.latest_request(lane.lane_id)
        if recent and (self.now() - recent).total_seconds() < 18000:
            self.store.add_event('timer.keepalive', lane_id=lane.lane_id, data={'status': 'skipped-open'})
            return 'skipped-open'
        holder = self._reserve(lane, 'keepalive')
        if not holder:
            return 'skipped-busy'
        quarantined = False
        try:
            timeout = min(60, self.policy.get('caps', {}).get('keepalive_timeout_s', 60))
            outcome = self._turn(lane, 'keepalive', holder, timeout)
            quarantined = outcome.evidence.get('probe_quarantined', False)
            sent = outcome.evidence.get('requested_at')
            state = 'timed-out' if outcome.evidence.get('timed_out') else outcome.cls.value
            with self.store.transaction('timer.keepalive', lane_id=lane.lane_id):
                if outcome.cls == OutcomeClass.AUTH_DEAD:
                    self.store.update_lane(lane.lane_id, enabled=0)
                    self.record_auth_dead(lane.lane_id)
                if sent and (outcome.cls == OutcomeClass.OK or outcome.native_session_id):
                    self.store.add_reading(Reading(lane.lane_id, self.policy['models']['haiku']['id'], 'admission',
                        None, None, ReadingLabel.ADMISSION_OBSERVED, 'keepalive', sent))
                self.store.add_event('timer.keepalive', lane_id=lane.lane_id,
                                     data={'status': state, 'requested_at': sent})
            return state
        except TimeoutError:
            self.store.add_event('timer.keepalive', lane_id=lane.lane_id, data={'status': 'timed-out'})
            return 'timed-out'
        finally:
            self._release(holder, quarantined=quarantined)

    def keepalive_cycle(self):
        futures = [self._lanes.submit(self._keepalive_lane, lane) for lane in self.store.list_lanes()
                   if lane.provider == 'claude' and lane.enabled and lane.owner == 'v2' and not lane.desktop]
        return [future.result() for future in as_completed(futures)]
