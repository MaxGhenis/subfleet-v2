"""Bounded daemon maintenance: a single post-heal monitoring verdict (C-18.1).

Network calls and guardian turns happen in a fixed worker pool. Transactions
only reserve lanes or publish facts. Timer requests never create jobs (C-8.4).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from . import capacity
from .adapters.registry import get_adapter
from .contracts import ClockSource, Closure, ClosureReason, Outcome, OutcomeClass, Reading, ReadingLabel
from .credentials import resolve_credential


def instant(value=None):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value or datetime.now(timezone.utc)


def iso(value):
    return instant(value).astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class Timers:
    def __init__(self, store, root, policy, *, turn=None, adapter_factory=get_adapter,
                 deliver=None, now=None):
        from .actions import ResetCredits
        from .alerts import Alerts
        self.store, self.root, self.policy = store, Path(root), policy
        self.turn, self.adapter_factory = turn, adapter_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.cancel = threading.Event()
        self._lock = threading.RLock()
        self._cycles = ThreadPoolExecutor(max_workers=2, thread_name_prefix='subfleet-timer')
        # The mirror gets its own worker. It is a file-copy pass over the whole
        # desktop session store and an 8.5-minute one was observed on 2026-08-18
        # during app churn; sharing the two-slot cycle pool would let it hold a
        # probe or a keepalive behind it for minutes (C-23.28).
        self._mirror = ThreadPoolExecutor(max_workers=1, thread_name_prefix='subfleet-mirror')
        self._session_mirror = None
        self._lanes = ThreadPoolExecutor(max_workers=min(4, policy.get('caps', {}).get('keepalive_workers', 4)),
                                         thread_name_prefix='subfleet-timer-lane')
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
        self._status = {name: {'last_run': None, 'next_due': None, 'last_error_type': None}
                        for name in ('probe', 'keepalive', 'reset_credits', 'alerts',
                                     'retention', 'mirror')}
        self.metadata = self._latest('timer.verdict')
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

    def mark(self, name, *, error=None, next_due=None):
        with self._lock:
            self._status[name].update(last_run=iso(self.now()), last_error_type=error)
            if next_due is not None:
                self._status[name]['next_due'] = next_due
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
                pool = self._mirror if name == 'mirror' else self._cycles
                pool.submit(self._run, name)

    def request(self, name, *, target=None):
        """Queue operator maintenance on the same workers and overlap guard."""
        if name not in ('probe', 'keepalive', 'reset_credits'):
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
            self.store.add_event('timer.requested', data={'timer': name, 'target': target})
            callback = (lambda: self.reset_credits_cycle(target=target)) if name == 'reset_credits' else None
            self._cycles.submit(self._run, name, callback)
        return {'status': 'scheduled', 'timer': name, 'target': target}

    def _run(self, name, callback=None):
        error = None
        try:
            (callback or getattr(self, name + '_cycle'))()
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
            self.mark(name, error=error)
            with self._lock:
                self._running.discard(name)

    def mirror_cycle(self):
        """One desktop sidebar pass (C-23.28). Never calls a provider.

        The pass records itself in its own sidecar as it starts and again as it
        ends, so `doctor` judges the mirror from that file rather than from this
        timer's `last_run` — a pass that hangs must read as in flight for thirty
        minutes, not as a timer that merely has not reported yet.
        """
        from .sessions.mirror import Mirror, options_from
        if self._session_mirror is None:
            self._session_mirror = Mirror(self.root, self.policy, now=self.now, cancel=self.cancel)
        self._session_mirror.run_once(options_from(self.policy))

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
        self.publish_status(self.snapshot())
        return result

    def publish_status(self, snapshot):
        """C-18.1, C-18.2, C-29.6: the one way `status.json` is written.

        Runs on a timer worker, never the control loop or a request thread. It
        adds what the snapshot lacks: batch labels for the displayed jobs, the
        conversation summary (read through its own read-only connection,
        `conversations.store.status_summary`), and the policy's short name for
        each Claude model id, which labels model-scoped windows.
        """
        from .conversations.store import status_summary
        from .status_json import attach_batches, write_status
        attach_batches(self.store, snapshot)
        snapshot['conversations'] = status_summary(self.root)
        snapshot['model_names'] = {entry['id']: short for short, entry in self.policy.get('models', {}).items()
                                   if isinstance(entry, dict) and entry.get('id')}
        return write_status(self.root, snapshot, now=self.now())

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

    def snapshot(self):
        with self.store.transaction('timer.snapshot'):
            view = capacity.from_store(self.store, now=self.now(), reading_ttl_s=self.policy.get('caps', {}).get('reading_ttl_s', 120))
        return self.enrich_view(view)

    def enrich_view(self, view):
        # Re-enrolment creates a new lane id. The previous binding stays in the
        # ledger but no longer supplies the home's active credential condition.
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
        return view

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
        self.publish_status(snapshot)
        self.store.add_event('timer.cycle', data={'offline': offline, 'at': iso(self.now()),
                             'lanes': [lane.lane_id for lane, _ in results]})
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
