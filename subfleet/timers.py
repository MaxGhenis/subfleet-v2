"""Bounded daemon maintenance: a single post-heal monitoring verdict (C-18.1).

Network calls and guardian turns happen in a fixed worker pool. Transactions
only reserve lanes or publish facts. Timer requests never create jobs (C-8.4).
A probe cycle also starts idle Codex weekly clocks (C-18.3).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4

from . import capacity
from .adapters.base import AdapterError
from .adapters.registry import get_adapter
from .contracts import (CLOCK_TOUCH_SPACING_S, CLOCK_TOUCH_TIMEOUT_S, CLOCK_UNSTARTED_TOLERANCE_S,
                        ClockSource, Closure, ClosureReason, Outcome, OutcomeClass, Reading, ReadingLabel)
from .credentials import resolve_credential
from .policy import touch_model

#: C-18.3: the probe statuses under which a lane's credential cannot run a turn.
LATCHED_STATUSES = ('auth-dead', 'revoked', 'auth-revoked', 'expired-token', 'no-auth')
#: C-18.3: what a touch that never reached the provider, or never answered, records.
TOUCH_FAILED = ('refused', 'failed', 'timed-out', 'unknown', 'transient', 'limited',
                'auth-dead', 'cli-too-old', 'content-filter', 'quarantined')
#: C-18.3: a touch the daemon's stop cut short, or a crash interrupted: no verdict.
TOUCH_UNSETTLED = ('cancelled', 'interrupted')
#: C-18.3: a touch with no verdict yet or ever; the lane's standing is its last settled touch.
TOUCH_PENDING = ('touching',) + TOUCH_UNSETTLED
#: C-18.3: operator touch results kept in memory for `lanes touch` to collect.
TOUCH_RESULTS_KEPT = 32


def instant(value=None):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value or datetime.now(timezone.utc)


def iso(value):
    return instant(value).astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class Timers:
    def __init__(self, store, root, policy, *, turn=None, adapter_factory=get_adapter,
                 deliver=None, now=None, log=None):
        from .actions import ResetCredits
        from .alerts import Alerts
        self.store, self.root, self.policy = store, Path(root), policy
        self.turn, self.adapter_factory = turn, adapter_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        # C-18.3: one daemon.log line per touch. The daemon passes its own log.
        self.log = log or logging.getLogger('subfleet.timers')
        self.cancel = threading.Event()
        self._lock = threading.RLock()
        self._cycles = ThreadPoolExecutor(max_workers=2, thread_name_prefix='subfleet-timer')
        # The mirror gets its own worker. It is a file-copy pass over the whole
        # desktop session store and an 8.5-minute one was observed on 2026-08-18
        # during app churn; sharing the two-slot cycle pool would let it hold a
        # probe or a keepalive behind it for minutes (C-23.28).
        self._mirror = ThreadPoolExecutor(max_workers=1, thread_name_prefix='subfleet-mirror')
        self._session_mirror = None
        self.lane_workers = min(4, policy.get('caps', {}).get('keepalive_workers', 4))
        self._lanes = ThreadPoolExecutor(max_workers=self.lane_workers, thread_name_prefix='subfleet-timer-lane')
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
        self.actions = ResetCredits(store, policy, adapter_factory=lambda lane: self.adapter_factory(lane.provider))
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
        self._status = {name: {'last_run': None, 'next_due': None, 'last_error_type': None}
                        for name in ('probe', 'keepalive', 'reset_credits', 'alerts',
                                     'retention', 'mirror', 'mirror_hot', 'touch')}
        self.metadata = self._latest('timer.verdict')
        # C-18.3: each Codex lane's latest touch; its `at` starts the spacing.
        self.touches = self._latest('timer.touch')
        self._touch_cv = threading.Condition()
        self._touch_pending = set()
        self._touch_results = {}
        self.balances = self._latest('reset-credit.balance')
        self._cycle_error = None
        for row in store.query("SELECT data_json FROM events WHERE kind='timer.run' ORDER BY event_id"):
            data = json.loads(row['data_json'])
            if data.get('timer') in self._status and data.get('last_run'):
                self._status[data['timer']].update({k: data.get(k) for k in ('last_run', 'next_due', 'last_error_type')})

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
                self._status[name]['next_due'] = iso(self.now() + timedelta(seconds=interval))
                self._due[name] = time.monotonic() + interval
            self._status['retention']['next_due'] = iso(self.now() + timedelta(hours=1))

    def status(self):
        with self._lock:
            return {name: dict(value) for name, value in self._status.items()}

    def mark(self, name, *, error=None, next_due=None, persist=True):
        with self._lock:
            self._status[name].update(last_run=iso(self.now()), last_error_type=error)
            if next_due is not None:
                self._status[name]['next_due'] = next_due
            if persist:
                self.store.add_event('timer.run', data={'timer': name, **self._status[name]})

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
                pool = self._mirror if name in ('mirror', 'mirror_hot') else self._cycles
                pool.submit(self._run, name)

    def request(self, name, *, target=None, request_id=None):
        """Queue operator maintenance on the same workers and overlap guard."""
        if name not in ('probe', 'keepalive', 'reset_credits', 'touch'):
            raise ValueError('unknown maintenance timer')
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
            self.store.add_event('timer.requested', data={'timer': name, 'target': target,
                                                          **({'request_id': request_id} if request_id else {})})
            callback = None
            if name == 'reset_credits':
                callback = lambda: self.reset_credits_cycle(target=target)
            elif name == 'touch':
                # C-18.3: an operator's touch runs on the timer workers, never in
                # the request handler (C-16.4); `touch_status` collects it.
                with self._touch_cv:
                    self._touch_pending.add(request_id)
                callback = lambda: self.touch_request(target=target, request_id=request_id)
            self._cycles.submit(self._run, name, callback)
        return {'status': 'scheduled', 'timer': name, 'target': target,
                **({'request_id': request_id} if request_id else {})}

    def _run(self, name, callback=None):
        error = None
        result = None
        try:
            result = (callback or getattr(self, name + '_cycle'))()
            if name == 'probe':
                error = self._cycle_error
        except Exception as exc:
            error = type(exc).__name__
            self.store.add_event('timer.error', data={'timer': name, 'error_type': error})
        finally:
            if name == 'probe':
                # Each lane's durable debounce starts when its probe finishes.
                # Scheduling from cycle start could therefore skip the entire
                # next cycle whenever a request took nonzero time.
                with self._lock:
                    interval = self.intervals[name]
                    self._due[name] = time.monotonic() + interval
                    next_due = iso(self.now() + timedelta(seconds=interval))
                    self._status[name]['next_due'] = next_due
                    # These passes run inside probe_cycle, so their displayed
                    # deadlines must follow its completion-based schedule too.
                    # Preserve last_run/error if the cycle failed before them.
                    for companion in ('reset_credits', 'alerts'):
                        self._status[companion]['next_due'] = next_due
                        self.store.add_event('timer.run', data={
                            'timer': companion, **self._status[companion]})
            # A hot mirror pass runs every few seconds; the store keeps its
            # timer.run events forever and replays them at start, so only a
            # pass that changed something or failed is recorded there.
            self.mark(name, error=error,
                      persist=name != 'mirror_hot' or error is not None or bool(result))
            with self._lock:
                self._running.discard(name)

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
        """One hot sidebar pass (C-23.28): spread new sessions within seconds.

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

    def reset_credits_cycle(self, *, target=None):
        snapshot = self.snapshot()
        result = self.actions.evaluate(snapshot, now=self.now(), cancel=self.cancel,
                                       deadline=time.monotonic() + 60, target_lane_id=target)
        if result.get('status') == 'confirmed':
            lane_id = result['lane_id']
            row = next(row for row in snapshot['lanes'] if row['lane_id'] == lane_id)
            count = row.get('reset_credits_remaining')
            balance = {'action_id': result['action_id'],
                       'remaining': max(0, count - 1) if isinstance(count, int) else None}
            self.store.add_event('reset-credit.balance', lane_id=lane_id, data=balance)
            self.balances[lane_id] = balance
        self.store.add_event('timer.reset-credit', data=result)
        from .status_json import write_status
        write_status(self.root, self.snapshot(), now=self.now())
        return result

    def stop(self):
        with self._lock:
            self.cancel.set()
        self._cycles.shutdown(wait=True, cancel_futures=True)
        self._lanes.shutdown(wait=True, cancel_futures=True)
        self._mirror.shutdown(wait=True, cancel_futures=True)

    def record_auth_dead(self, lane_id):
        meta = {'verdict': 'auth-dead', 'probe_status': 'auth-dead'}
        self.store.add_event('timer.verdict', lane_id=lane_id, data=meta)
        self.metadata[lane_id] = meta

    def _epoch(self, lane):
        try:
            raw = json.loads((Path(lane.home or lane.credential.ref).expanduser() / 'auth.json').read_bytes())
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
                self.metadata[row['lane_id']] = data
            if not row['enabled']:
                continue
            first = seen.setdefault(row['account_key'], row)
            if first['lane_id'] != row['lane_id']:
                with self.store.transaction('lane.noncanonical', lane_id=row['lane_id']):
                    self.store.update_lane(row['lane_id'], enabled=0)
                    data = {'identity_status': 'non-canonical', 'duplicate_of': first['home'] or first['lane_id'],
                            'verdict': 'duplicate', 'home': row['home']}
                    self.store.add_event('timer.verdict', lane_id=row['lane_id'], data=data)
                self.metadata[row['lane_id']] = data
        # Desktop Codex identity is observed read-only; it is never a lane.
        try:
            from .adapters.codex import _identity, _read_auth
            identity = _identity(_read_auth(Path.home() / '.codex'))
            self._app_account = 'codex:' + str(identity.get('account_id') or identity.get('email') or '')
        except (OSError, ValueError):
            self._app_account = None

    def _reserve(self, lane, purpose):
        holder = 'probe:timer:' + str(uuid4())
        with self.store.transaction('timer.reserved', lane_id=lane.lane_id):
            current = self.store.get_lane(lane.lane_id)
            if not current or not current.enabled or current.owner != 'v2' or current.desktop:
                return None
            if self.store.one("SELECT 1 FROM attempts WHERE lane_id=? AND state IN ('reserved','starting','running','finalizing')", (lane.lane_id,)):
                return None
            if self.store.one('SELECT 1 FROM leases WHERE lease_key LIKE ?', (f'lane:{lane.lane_id}:%',)):
                return None
            if self.store.one("SELECT 1 FROM closures WHERE lane_id=? AND reason IN ('auth-dead','operator-hold') AND released_at IS NULL AND until_at>?", (lane.lane_id, iso(self.now()))):
                return None
            self.store.acquire_lease(f'lane:{lane.lane_id}:slot:0', holder)
            self.store.add_event('timer.reservation', lane_id=lane.lane_id,
                                 data={'holder': holder, 'purpose': purpose})
        with self._lock:
            self.active_holders.add(holder)
        return holder

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
        last = previous.get('probed_at')
        if last and (self.now() - instant(last)).total_seconds() < self.intervals['probe']:
            return None
        until = previous.get('retry_after_until')
        if until and self.now() < instant(until):
            return None     # C-9.9: the usage endpoint asked us to wait
        holder = self._reserve(lane, 'probe')
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
            return lane, {**probe, 'probed_at': iso(self.now())}
        except (TimeoutError, OSError) as exc:
            return lane, {'status': 'network-error', 'readings': (), 'probed_at': iso(self.now()), 'error_type': type(exc).__name__}
        except Exception as exc:
            return lane, {'status': 'unknown', 'readings': (), 'probed_at': iso(self.now()), 'error_type': type(exc).__name__}
        finally:
            self._probe_holders[lane.lane_id] = (holder, quarantined)

    def _pace_usage(self):
        """C-9.9: one usage read at a time, `reserve.usage_spacing_s` apart."""
        spacing = float((self.policy.get('reserve') or {}).get('usage_spacing_s', 3))
        with self._usage_lock:
            now = time.monotonic()
            wait = max(0.0, self._usage_next - now)
            self._usage_next = max(now, self._usage_next) + spacing
        if wait > 0:
            self.cancel.wait(wait)

    def _persist(self, lane, probe):
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
        if status != 'identity-mismatch':
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
            elif status == 'ok' and probe.get('limit_reached') is False:
                # Release only after real server evidence of a reset, not absence of numbers.
                if any(r.label == ReadingLabel.PROVIDER and r.utilization is not None and r.utilization < 1 for r in readings):
                    with self.store.transaction('closure.reset') as tx:
                        tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND scope='account' AND reason='provider-limit' AND released_at IS NULL", (at, lane.lane_id))
            self.store.add_event('timer.verdict', lane_id=lane.lane_id, data=meta)
        self.metadata[lane.lane_id] = meta

    def snapshot(self, *, held=False):
        with self.store.transaction('timer.snapshot'):
            view = capacity.from_store(self.store, now=self.now(), reading_ttl_s=self.policy.get('caps', {}).get('reading_ttl_s', 120))
            if held:
                # C-18.3: a lane a probe reservation holds (a quarantined one keeps
                # its lease) cannot be touched; `touch_plan` says so as `held`.
                view['unavailable_lanes'] = {row['lease_key'].split(':')[1]: row['holder'] for row in self.store.query(
                    "SELECT lease_key,holder FROM leases WHERE holder LIKE 'probe:%' AND lease_key LIKE 'lane:%'")}
        return self.enrich_view(view)

    def enrich_view(self, view):
        # Re-enrolment creates a new lane id. The previous binding stays in the
        # ledger but no longer supplies the home's active credential condition.
        last_work = self._last_work(view.get('attempts'))              # C-18.3
        bindings = {}
        for lane in self.store.query('SELECT * FROM lanes WHERE enabled=1 ORDER BY created_at,rowid'):
            bindings[(lane['provider'], lane['home'] or lane['credential_ref'])] = lane['lane_id']
        for row in view['lanes']:
            row.update(self.metadata.get(row['lane_id'], {}))
            bound = bindings.get((row['provider'], row['home'] or row['credential_ref']))
            if not row['enabled'] and bound and bound != row['lane_id']:
                row['superseded_by'] = bound
            row['app_shadowed'] = row.get('app_shadowed', False) or row['account_key'] == getattr(self, '_app_account', None)
            row['probe'] = dict(self.metadata.get(row['lane_id'], {}), status=row.get('probe_status', 'unknown'))
            row['probe']['readings'] = row['readings']
            if row.get('revoked_epoch') is not None or row.get('probe_status') in ('revoked', 'auth-revoked', 'expired-token', 'no-auth'):
                view.setdefault('unavailable_lanes', {})[row['lane_id']] = 'credential-latched'
            row['reset_credits_remaining'] = (row.get('reset_credits') or {}).get('available')
            override = self.actions.confirmed_override(row['lane_id'], now=self.now())
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
            slot_cap = caps.get('max_in_flight_per_lane', 2) if measured and not override else min(caps.get('max_in_flight_per_lane', 2), caps.get('max_in_flight_unmeasured', 1))
            row['dispatchable'] = bool(headroom_ok and row['enabled'] and row['owner'] == 'v2' and not row['desktop'] and
                                       not row['closures'] and row.get('revoked_epoch') is None and row.get('probe_status') not in ('auth-dead', 'revoked', 'expired-token', 'no-auth') and
                                       row['lane_id'] not in view.get('unavailable_lanes', {}) and
                                       row['in_flight'] < slot_cap and
                                       sum(view.get('in_flight', {}).values()) + view.get('reserved_probes', 0) < caps.get('max_active_attempts', 4))
            if row['provider'] == 'codex':
                self._clock_state(row, last_work.get(row['lane_id']))
        return view

    # --- C-18.3: weekly clocks start on first use -----------------------------

    def _touch_settings(self):
        settings = self.policy.get('timers') or {}
        return (bool(settings.get('touch_unstarted', True)),
                float(settings.get('touch_spacing_s', CLOCK_TOUCH_SPACING_S)),
                float(settings.get('touch_timeout_s', CLOCK_TOUCH_TIMEOUT_S)))

    @staticmethod
    def _last_work(attempts):
        """C-18.3: when work last reached each lane: the latest attempt start that
        sent a request (C-23.19: rc 5 without a provider session did not, nor did
        a spawn failure)."""
        latest = {}
        for attempt in attempts or ():
            started = attempt.get('started_at')
            if not started or (attempt.get('rc') in (5, 127) and not attempt.get('native_session_id')):
                continue
            lane_id = attempt.get('lane_id')
            if lane_id and (lane_id not in latest or started > latest[lane_id]):
                latest[lane_id] = started
        return latest

    def _touch_status(self, touch):
        """A recorded touch's status, with a turn that can no longer be running read as interrupted."""
        status = touch.get('status') if touch else None
        if status == 'touching' and touch.get('at'):
            _, _, timeout = self._touch_settings()
            if (self.now() - instant(touch['at'])).total_seconds() > timeout + 300:
                return 'interrupted'
        return status

    def _settled(self, touch):
        """C-18.3: the lane's last touch that reached a verdict.

        A touch still running, cut short, or interrupted says nothing about the
        clock, so the lane keeps standing on the one before it, which each
        record carries as `previous`.
        """
        if not touch:
            return None
        return touch.get('previous') if self._touch_status(touch) in TOUCH_PENDING else touch

    def _spaced_until(self, touch):
        """C-18.3: when the spacing a lane's last touch imposes ends, or None.

        A touch the daemon's own stop cut short, or one a crash interrupted,
        reached no verdict and imposes none: the next cycle may touch again.
        """
        if not touch or not touch.get('at') or self._touch_status(touch) in TOUCH_UNSETTLED:
            return None
        _, spacing, _ = self._touch_settings()
        until = instant(touch['at']) + timedelta(seconds=spacing)
        return until if until > self.now() else None

    def _clock_state(self, row, last_work=None):
        """C-18.3: derive a Codex lane's weekly clock from its own readings.

        `weekly_clock` is `not-started` when the readings show a window that has
        not started (`capacity.clock_unstarted`); `touched` when they still do
        but a request reached the lane within the tolerance before they were
        read: a touch that succeeded (unless the one before it already failed to
        start this clock) or a work attempt (`last_work`), because the usage
        endpoint shows a just-started window sliding for a few minutes; None
        otherwise. `clock_alert` says why a lane that is not started was not
        started by this daemon: its last touch failed, a touch that succeeded
        did not start it, the touch model is closed on it, or automatic
        touching is off.
        """
        auto, spacing, _ = self._touch_settings()
        now = self.now()
        evidence = capacity.clock_unstarted(row['readings'], now=now,
                                            reading_ttl_s=self.policy.get('caps', {}).get('reading_ttl_s', 120))
        touch = self.touches.get(row['lane_id'])
        status = self._touch_status(touch)
        # A touch in flight (an operator's, while a cycle reads the lane) must
        # not clear a standing warning for a cycle and re-raise it the next.
        settled = self._settled(touch)
        verdict = settled.get('status') if settled else None
        state = alert = request = None
        if verdict == 'ok' and not settled.get('ineffective') and settled.get('requested_at'):
            request = {'at': settled['requested_at'], 'source': 'touch'}
        if last_work and (request is None or instant(last_work) > instant(request['at'])):
            request = {'at': last_work, 'source': 'attempt'}
        if evidence:
            state = 'not-started'
            if request and (instant(evidence['observed_at']) - instant(request['at'])).total_seconds() <= CLOCK_UNSTARTED_TOLERANCE_S:
                state = 'touched'
        block = None
        if state == 'not-started':
            recent = settled and settled.get('at') and (now - instant(settled['at'])).total_seconds() <= 2 * spacing
            block = self.touch_block(row)
            if recent and verdict in TOUCH_FAILED:
                alert = 'touch-failed'
            elif recent and verdict == 'ok':
                alert = 'touch-ineffective'
            elif block and block.startswith(f"closed:{touch_model(self.policy)['id']}:"):
                alert = 'touch-blocked'
            elif not auto:
                alert = 'auto-touch-off'
        row['weekly_clock'] = state
        row['clock_evidence'] = evidence
        row['clock_request'] = request if state == 'touched' else None
        row['clock_touch'] = {**touch, 'status': status} if touch else None
        # The warning names the touch it is about: the last one with a verdict.
        row['clock_alert_touch'] = dict(settled) if alert in ('touch-failed', 'touch-ineffective') else None
        row['clock_block'] = block if alert == 'touch-blocked' else None
        row['clock_alert'] = alert

    def touch_block(self, row):
        """C-18.3: why this lane may not be touched at all, or None.

        Detection and spacing are not here: an operator naming one lane skips
        both. These are the lanes no touch may use: another owner's, a
        superseded or disabled binding, the desktop login, a credential that
        proved to hold another account or cannot run a turn, and a lane closed
        or limited for the account or the touch model. A lane with a live
        attempt needs no touch: that attempt is the first request.
        """
        if row.get('provider') != 'codex':
            return 'not-codex'
        if row.get('owner') != 'v2':
            return 'owner-v1'
        if row.get('superseded_by'):
            return 'superseded'
        if not row.get('enabled', True):
            return 'disabled'
        if row.get('desktop'):
            return 'desktop'
        if capacity.identity_blocked(row):
            return 'identity-mismatch'
        if row.get('revoked_epoch') is not None or row.get('probe_status') in LATCHED_STATUSES:
            return 'credential-latched'
        if row.get('probe_status') == 'limited' or row.get('limit_reached') is True or row.get('allowed') is False:
            return 'limited'
        model = touch_model(self.policy)['id']
        for closure in row.get('closures', ()):
            if closure.get('scope') in ('account', model):
                return f"closed:{closure['scope']}:{closure.get('until_at')}"
        if row.get('in_flight'):
            return 'busy'
        return None

    def touch_plan(self, view, *, target=None, only=None, auto=False):
        """C-18.3: what a touch pass would do with each Codex lane, and why.

        Reads the view only: no probe, turn, event, or lease. `target` names one
        lane to touch whatever its readings and spacing say (an operator's
        explicit lane); otherwise a lane is touched when its weekly clock is
        `not-started`, nothing blocks it, and its last touch's spacing
        (`timers.touch_spacing_s`) has passed. `auto` is an automatic pass: it
        honours `timers.touch_unstarted`, is restricted by `only` to the lanes
        its cycle measured, and skips a lane a probe reservation holds
        (`unavailable_lanes`: a quarantined probe keeps its lease) as `held`
        rather than wait; an operator's pass waits for such a lane instead.
        """
        enabled, _, _ = self._touch_settings()
        now = self.now()
        ttl = self.policy.get('caps', {}).get('reading_ttl_s', 120)
        held = {lane_id for lane_id, holder in (view.get('unavailable_lanes') or {}).items()
                if auto and str(holder).startswith('probe:')}
        plan = []
        for row in view.get('lanes', ()):
            if row.get('provider') != 'codex' or target and row['lane_id'] != target:
                continue
            if row.get('superseded_by') and not target or only is not None and row['lane_id'] not in only:
                continue
            if 'weekly_clock' not in row:
                self._clock_state(row, self._last_work(view.get('attempts')).get(row['lane_id']))
            touch = row.get('clock_touch')
            weekly = capacity.long_windows(row.get('readings', ()))
            spaced = self._spaced_until(touch)
            entry = {'lane_id': row['lane_id'], 'home': row.get('home') or row.get('credential_ref'),
                     'weekly_clock': row.get('weekly_clock'),
                     'resets_at': min((r['resets_at'] for r in weekly if r.get('resets_at')), default=None),
                     'last_touch': {key: touch.get(key) for key in ('at', 'status', 'mode', 'requested_at')} if touch else None,
                     'next_touch_at': iso(spaced) if spaced else None}
            block = self.touch_block(row)
            if block:
                entry.update(action='skip', reason=block)
            elif target:
                entry.update(action='touch', reason='forced')
            elif row['lane_id'] in held:
                entry.update(action='skip', reason='held')
            elif row.get('weekly_clock') == 'touched':
                entry.update(action='skip', reason='touched')
            elif row.get('weekly_clock') != 'not-started':
                measured = any(capacity.fresh_provider(r, now=now, reading_ttl_s=ttl) for r in weekly)
                entry.update(action='skip', reason='started' if measured else 'unmeasured')
            elif auto and not enabled:
                entry.update(action='skip', reason='auto-touch-off')
            elif spaced:
                entry.update(action='skip', reason='spaced')
            else:
                entry.update(action='touch', reason='not-started')
            plan.append(entry)
        return plan

    def _record_touch(self, lane_id, record):
        self.store.add_event('timer.touch', lane_id=lane_id, data=record)
        with self._lock:
            self.touches[lane_id] = dict(record)

    def _touch_lane(self, lane, entry, *, mode, request_id, wait_s):
        """C-18.3: one supervised touch: reserve, one tiny turn, re-probe.

        The reservation is the same lane lease a probe takes (`_reserve`), so
        admission sees the lane as held and counts it toward the fleet cap. The
        turn is the daemon's guardian path (`turn`): API-key refusal, guard
        preflight, containment. The attempt is recorded before the turn starts,
        so a crash mid-turn still spaces the next one. Everything after the
        reservation is inside `try`: the result always carries the holder, and
        the caller (`_publish_touch`) always releases it.
        """
        def skipped(status):
            return {'lane': lane, 'holder': None, 'quarantined': False, 'probe': None,
                    'record': {'lane_id': lane.lane_id, 'status': status, 'mode': mode}}
        if self.cancel.is_set():
            return skipped('skipped-cancelled')
        deadline = time.monotonic() + wait_s
        holder = self._reserve(lane, 'touch')
        while not holder and time.monotonic() < deadline and not self.cancel.is_set():
            self.cancel.wait(.25)
            holder = self._reserve(lane, 'touch')
        if not holder:
            return skipped('skipped-busy')
        item = {'lane': lane, 'holder': holder, 'quarantined': False, 'probe': None,
                'record': {'lane_id': lane.lane_id, 'status': 'skipped-failed', 'mode': mode}}
        try:
            _, spacing, timeout = self._touch_settings()
            previous = self.touches.get(lane.lane_id) or {}
            if entry.get('reason') != 'forced' and self._spaced_until(previous):
                # Another touch of this lane finished while this one waited.
                item['record']['status'] = 'skipped-spaced'
                return item
            model = touch_model(self.policy)
            record = {'lane_id': lane.lane_id, 'at': iso(self.now()), 'mode': mode, 'model': model['id'],
                      'reason': entry.get('reason'), 'status': 'touching',
                      'before': {'weekly_clock': entry.get('weekly_clock'), 'resets_at': entry.get('resets_at')},
                      **({'request_id': request_id} if request_id else {})}
            base = self._settled(previous) or {}
            if base:
                record['previous'] = {key: base.get(key) for key in
                                      ('at', 'status', 'requested_at', 'ineffective', 'detail') if base.get(key) is not None}
                # Whether that touch was of a clock that had not started: only
                # such a touch can have failed to start this one.
                record['previous']['unstarted'] = base.get(
                    'unstarted', (base.get('before') or {}).get('weekly_clock') == 'not-started')
            recent = base.get('at') and (self.now() - instant(base['at'])).total_seconds() <= 2 * spacing
            if entry.get('weekly_clock') == 'not-started' and recent:
                # Count the touches in this idle stretch that reached the provider
                # and left the clock unstarted, across any failed ones between, so
                # the lane reads `not-started` (not `touched`), no "started" notice
                # goes out, and its warning stays up instead of clearing for ten
                # minutes an hour. A touch of a clock that was running (an
                # operator's, forced), or from an earlier week, started nothing
                # that was waiting and says nothing about this one.
                effective_base = base.get('status') == 'ok' and record['previous']['unstarted']
                count = int(base.get('ineffective') or 0) + (1 if effective_base else 0)
                if count:
                    record['ineffective'] = count
            item['record'] = record
            self._record_touch(lane.lane_id, record)
            outcome = self._turn(lane, 'touch', holder, timeout)
            item['quarantined'] = quarantined = bool(outcome.evidence.get('probe_quarantined'))
            if self.cancel.is_set() and outcome.cls != OutcomeClass.OK and not quarantined:
                # The daemon is stopping and cut the turn short; that is no verdict
                # on the lane, so it neither spaces the next touch nor warns.
                record.update(status='cancelled', detail=str(outcome.detail or '')[:300])
                return item
            status = ('quarantined' if quarantined else 'timed-out' if outcome.evidence.get('timed_out')
                      else outcome.cls.value)
            record.update(status=status, detail=str(outcome.detail or '')[:300],
                          requested_at=outcome.evidence.get('requested_at'), rc=outcome.evidence.get('rc'))
            with self.store.transaction('timer.touched', lane_id=lane.lane_id):
                if outcome.cls == OutcomeClass.AUTH_DEAD:
                    self.store.update_lane(lane.lane_id, enabled=0)
                    self.record_auth_dead(lane.lane_id)
                elif (outcome.cls == OutcomeClass.LIMITED and outcome.closure
                      and not self.actions.confirmed_override(lane.lane_id, now=self.now())):
                    self.store.add_closure(outcome.closure)       # C-9.4
            if not quarantined and outcome.cls != OutcomeClass.AUTH_DEAD:
                adapter = self.adapter_factory('codex')
                if hasattr(adapter, 'timeout'):
                    adapter.timeout = min(15, self.policy.get('caps', {}).get('probe_timeout_s', 60))
                try:
                    probe = self._read_probe(adapter, lane, resolve_credential(lane.credential))
                except (TimeoutError, OSError) as exc:
                    probe = {'status': 'network-error', 'readings': (), 'error_type': type(exc).__name__}
                item['probe'] = {**probe, 'probed_at': iso(self.now())}
        except AdapterError as exc:
            # Refused before any provider launch: the guard preflight (C-14.2)
            # or an API-key home (C-6.5). Nothing reached the provider.
            item['record'].update(status='refused', detail=str(exc)[:300], code=int(getattr(exc, 'code', 1) or 1),
                                  fix=getattr(exc, 'fix', None))
        except TimeoutError:
            item['record'].update(status='timed-out')
        except Exception as exc:
            item['record'].update(status='failed', error_type=type(exc).__name__)
        return item

    def _publish_touch(self, item, mode):
        """C-18.3: persist a touch's re-probe, record it, log it, then release its lane.

        Runs as each touch finishes, so a lane is held for its own turn only.
        A failure here is the lane's, recorded as its `error_type`; the lease is
        released whatever happens.
        """
        lane, record, probe = item['lane'], item['record'], item['probe']
        try:
            if probe is not None and not self.cancel.is_set():
                self._persist(lane, probe)
                record['probe_status'] = probe.get('status')
                record['resets_at'] = min((r['resets_at'] for r in capacity.long_windows(probe.get('readings', ()))
                                           if r.get('resets_at')), default=None)
        except Exception as exc:
            record['error_type'] = type(exc).__name__
        try:
            if record.get('at'):
                self._record_touch(lane.lane_id, record)
                self.log.info('lane touch %s lane=%s mode=%s model=%s status=%s requested_at=%s '
                              'weekly_reset=%s%s', iso(self.now()), lane.lane_id, mode, record.get('model'),
                              record['status'], record.get('requested_at') or '-', record.get('resets_at') or '-',
                              f" error_type={record['error_type']}" if record.get('error_type') else '')
        finally:
            if item['holder']:
                self._release(item['holder'], quarantined=item['quarantined'])
        return record

    def touch(self, view=None, *, target=None, only=None, mode='auto', request_id=None):
        """C-18.3: touch every lane the plan chooses, publishing each as it finishes.

        Turns run on the lane workers. An automatic pass touches at most as many
        lanes as there are lane workers, so a cycle waits on one round of turns
        at most; the rest are touched on the next cycle. An operator's pass
        takes every lane it planned and waits up to 30 s for a lane a probe is
        reading. An automatic pass tells the operator how many clocks it started.
        """
        view = view if view is not None else self.snapshot()
        plan = self.touch_plan(view, target=target, only=only, auto=mode == 'auto')
        lanes = {lane.lane_id: lane for lane in self.store.list_lanes()}
        chosen = [entry for entry in plan if entry['action'] == 'touch' and entry['lane_id'] in lanes]
        if mode == 'auto':
            for entry in chosen[self.lane_workers:]:
                entry.update(action='skip', reason='next-cycle')
            chosen = chosen[:self.lane_workers]
        wait_s = 30 if mode == 'operator' else 0
        futures = {self._lanes.submit(self._touch_lane, lanes[entry['lane_id']], entry, mode=mode,
                                      request_id=request_id, wait_s=wait_s): entry for entry in chosen}
        results = []
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception as exc:
                # `_touch_lane` settles everything after its reservation, so only a
                # worker cancelled at shutdown lands here, holding nothing.
                entry = futures[future]
                item = {'lane': lanes[entry['lane_id']], 'holder': None, 'quarantined': False, 'probe': None,
                        'record': {'lane_id': entry['lane_id'], 'mode': mode,
                                   'status': 'skipped-' + type(exc).__name__}}
            results.append(self._publish_touch(item, mode))
        results.sort(key=lambda record: record['lane_id'])
        if any(record.get('at') for record in results):
            self.mark('touch', error=next((r.get('error_type') for r in results if r.get('error_type')), None))
        # A repeat touch of a clock the last one did not start is not news: the
        # `codex-clock` warning (alerts.py) already says so.
        started = [record for record in results if record.get('status') == 'ok' and not record.get('ineffective')]
        if mode == 'auto' and started:
            lines = [f"{record['lane_id']} (weekly reset now {record.get('resets_at') or 'unknown'})"
                     for record in started]
            self.alerts.deliver({
                'key': 'codex-clock-started', 'severity': 'info', 'home': 'fleet:codex',
                'subject': f'codex: weekly clock started on {len(started)} lane(s)',
                'body': ('A Codex weekly window starts at its first request, not at the reset, so each '
                         'idle lane was touched with one tiny ' + (started[0].get('model') or 'Luna') +
                         ' turn: ' + '; '.join(lines) + '.')})
        return {'mode': mode, 'request_id': request_id, 'target': target,
                'plan': plan, 'results': results}

    def touch_request(self, *, target=None, request_id=None):
        """C-18.3: an operator's `lanes touch`, run on a timer worker."""
        result = {'mode': 'operator', 'request_id': request_id, 'target': target, 'results': [],
                  'error_type': 'Interrupted'}
        try:
            result = self.touch(target=target, mode='operator', request_id=request_id)
        except Exception as exc:
            result = {**result, 'error_type': type(exc).__name__}
            raise
        finally:
            with self._touch_cv:
                self._touch_pending.discard(request_id)
                self._touch_results[request_id] = result
                while len(self._touch_results) > TOUCH_RESULTS_KEPT:
                    self._touch_results.pop(next(iter(self._touch_results)))
                self._touch_cv.notify_all()
        return result

    def touch_status(self, request_id, *, wait_s=0):
        """C-18.3: an operator touch's result, long-polled for at most 30 s (C-16.4)."""
        deadline = time.monotonic() + max(0.0, min(float(wait_s or 0), 30.0))
        with self._touch_cv:
            while (request_id not in self._touch_results and request_id in self._touch_pending
                   and not self.cancel.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._touch_cv.wait(min(remaining, 1.0))
            if request_id in self._touch_results:
                return {'status': 'done', **self._touch_results[request_id]}
            if request_id in self._touch_pending:
                return {'status': 'running', 'request_id': request_id}
        # The daemon restarted (or the result aged out of memory): answer from the
        # events, one record per lane, the last one written.
        records = {}
        for row in self.store.query("SELECT data_json FROM events WHERE kind='timer.touch' ORDER BY event_id"):
            record = json.loads(row['data_json'])
            if record.get('request_id') == request_id:
                records[record.get('lane_id')] = record
        return {'status': 'unknown', 'request_id': request_id, 'results': list(records.values())}

    def probe_cycle(self):
        if self.cancel.is_set():
            return
        self._cycle_error = None
        self._identities()
        futures = [self._lanes.submit(self._probe_lane, lane) for lane in self.store.list_lanes()
                   if lane.enabled and lane.owner == 'v2']
        results = [value for future in as_completed(futures) if (value := future.result())]
        if self.cancel.is_set():
            for holder, quarantined in self._probe_holders.values():
                self._release(holder, quarantined=quarantined)
            self._probe_holders.clear()
            return
        # All homes heal before any cycle reading/verdict is published.
        try:
            for lane, probe in results:
                self._persist(lane, probe)
        finally:
            for holder, quarantined in self._probe_holders.values():
                self._release(holder, quarantined=quarantined)
            self._probe_holders.clear()
        self._cycle_error = next((p['error_type'] for _, p in results if p.get('error_type')), None)
        codex = [p for lane, p in results if lane.provider == 'codex']
        offline = bool(codex) and all(p.get('status') == 'network-error' for p in codex)
        if not offline:
            try:
                self._auto_touch(results)
            except Exception as exc:
                # C-18.3: every lease is released inside the pass; what remains is
                # a cycle to publish, so the error is kept and publishing goes on.
                self._cycle_error = self._cycle_error or type(exc).__name__
                self.store.add_event('timer.error', data={'timer': 'touch', 'error_type': type(exc).__name__})
        snapshot = self.snapshot()
        if not offline:
            result = self.actions.evaluate(snapshot, now=self.now(), cancel=self.cancel,
                                           deadline=time.monotonic() + 60)
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
        self.alerts.evaluate(snapshot, now=self.now(), offline=offline)
        self.mark('alerts', next_due=self.status()['probe']['next_due'])
        from .status_json import attach_batches, write_status
        attach_batches(self.store, snapshot)
        write_status(self.root, snapshot, now=self.now())
        self.store.add_event('timer.cycle', data={'offline': offline, 'at': iso(self.now()),
                             'lanes': [lane.lane_id for lane, _ in results]})
        return snapshot

    def _auto_touch(self, results):
        """C-18.3: touch the lanes this cycle measured with a clock that has not started.

        Only lanes whose fresh probe in this cycle shows the evidence are
        considered, so an idle fleet costs no extra snapshot. The pass runs
        before the cycle publishes, so status.json, alerts, and history see the
        started clocks.
        """
        enabled, _, _ = self._touch_settings()
        if not enabled or self.cancel.is_set():
            return None
        ttl = self.policy.get('caps', {}).get('reading_ttl_s', 120)
        now = self.now()
        measured = {lane.lane_id for lane, probe in results if lane.provider == 'codex'
                    and probe.get('status') == 'ok'
                    and capacity.clock_unstarted(probe.get('readings', ()), now=now, reading_ttl_s=ttl)}
        if not measured:
            return None
        return self.touch(self.snapshot(held=True), only=measured, mode='auto')

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
