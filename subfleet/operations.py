"""Native operator commands over the daemon's existing maintenance components.

Read commands never run a provider. Maintenance is queued on the timer workers,
which retain their lane leases, cadence, action history, and shutdown ownership.
"""
from __future__ import annotations

import argparse
import json
import math
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import protocol, render
from .client import DaemonError, DaemonUnavailable
from .contracts import Exit
from .offline import Offline, OfflineUnavailable


def error_report(query, hours: float, *, now=None) -> dict:
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0:
        raise protocol.ProtocolError('errors: --hours must be a finite positive number')
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    attempts = query("SELECT attempt_id,job_id,lane_id,state,outcome_class,outcome_detail,rc,finished_at "
                     "FROM attempts WHERE COALESCE(finished_at,started_at,reserved_at)>=? "
                     "AND (state IN ('failed','lost','quarantined') OR outcome_class IN ('limited','auth-dead')) "
                     "ORDER BY COALESCE(finished_at,started_at,reserved_at) DESC", (since,))
    closures = query("SELECT lane_id,scope,reason,until_at,clock_source,source_event,created_at,released_at "
                     "FROM closures WHERE created_at>=? ORDER BY created_at DESC", (since,))
    probes = []
    for row in query("SELECT lane_id,ts,data_json FROM events WHERE kind='timer.verdict' AND ts>=? "
                     "AND data_json!='{}' ORDER BY event_id DESC", (since,)):
        data = json.loads(row['data_json'])
        status = data.get('probe_status') or data.get('status') or data.get('verdict')
        if status and status not in ('ok', 'admission-observed'):
            probes.append({'lane_id': row['lane_id'], 'observed_at': row['ts'], 'status': status,
                           **{key: data[key] for key in ('error_type', 'retry_after_until') if key in data}})
    return {'since': since, 'hours': hours, 'attempts': attempts, 'closures': closures, 'probes': probes}


def brief(view: dict) -> str:
    """The morning-brief section, using evidence labels instead of quota guesses."""
    lines = ['## AI capacity', '', f"Snapshot: {view.get('now', 'cached store')}" +
             ('; daemon offline, current availability unverified.' if view.get('offline') else '.')]
    readings = view.get('readings', ())
    for provider in ('codex', 'claude'):
        lanes = [row for row in view.get('lanes', ()) if row['provider'] == provider]
        count = sum(bool(row.get('dispatchable')) for row in lanes)
        availability = 'availability unverified' if view.get('offline') else f'{count}/{len(lanes)} lanes dispatchable'
        lines.append(f'- {provider}: {availability}.')
        for lane in lanes:
            evidence = lane.get('readings', [row for row in readings if row['lane_id'] == lane['lane_id']])
            windows = [row for row in evidence if row.get('scope') == 'account' and
                       row.get('window') in ('five_hour', 'seven_day')]
            text = '; '.join(render.reading_text(row) for row in windows) or 'usage unknown'
            conditions = []
            if not lane.get('enabled', True):
                conditions.append('disabled')
            if lane.get('desktop'):
                conditions.append('desktop')
            if lane.get('owner') != 'v2':
                conditions.append('owner ' + str(lane.get('owner', 'unknown')))
            if lane.get('identity_status') in ('mismatch', 'unverified'):
                conditions.append('identity ' + lane['identity_status'])
            closures = lane.get('closures', [row for row in view.get('closures', ()) if row['lane_id'] == lane['lane_id']])
            conditions.extend(render.closure_text(row) for row in closures)
            lines.append(f"  - {lane['lane_id']}: {text}" + ('; ' + '; '.join(conditions) if conditions else ''))
    return '\n'.join(lines)


def _target(service, target: str | None, provider='codex'):
    from .scheduler import resolve_lane
    if target is None or target == 'all':
        return None
    if not isinstance(target, str) or not target.strip():
        raise protocol.ProtocolError('name a lane or all')
    value = f'{provider}-{target}' if target.isdigit() else target
    row = resolve_lane(service.store.query('SELECT * FROM lanes'), value)
    if row is None or row['provider'] != provider:
        raise protocol.ProtocolError(f'unknown {provider} lane {target!r}', fix='subfleet lanes list')
    return row


def dispatch(service, args: protocol.OperationsArgs) -> dict:
    if not isinstance(args.dry_run, bool):
        raise protocol.ProtocolError('dry_run must be a boolean')
    if args.command == 'errors':
        return error_report(service.store.query, args.hours)
    if args.command == 'canonical-model':
        from .policy import resolve_model
        short = resolve_model(service.policy, args.target or '')
        return {'model': service.policy['models'][short]['id']}
    if args.command == 'login':
        if args.target in ('app', 'desktop'):
            raise protocol.ProtocolError('desktop login is never a Subfleet lane operation', Exit.REFUSED)
        lane = _target(service, args.target)
        if lane is None:
            raise protocol.ProtocolError('login codex: name one existing lane')
        if lane['desktop']:
            raise protocol.ProtocolError('desktop login is never a Subfleet lane operation', Exit.REFUSED)
        if lane['owner'] != 'v2':
            raise protocol.ProtocolError('transfer this lane to v2 before re-enrollment', Exit.REFUSED)
        home = shlex.quote(lane['home'] or lane['credential_ref'])
        return {'status': 'manual-login-required', 'lane_id': lane['lane_id'],
                'detail': 'Subfleet never performs provider login. In your terminal, authenticate this lane home, '
                          'then explicitly re-enroll its disabled binding.',
                'login_command': f'CODEX_HOME={home} codex login',
                'enroll_command': f'subfleet lanes enroll {home}'}
    if args.command not in ('brief', 'watch', 'keepalive', 'reset'):
        raise protocol.ProtocolError('unknown operator command')
    view = service._capacity_view(service._desktop_identity())
    if args.command == 'brief':
        return {'text': brief(view), 'generated_at': view.get('now'), 'offline': False}
    if args.command == 'reset':
        lane = _target(service, args.target)
        lane_id = lane['lane_id'] if lane else None
        if args.dry_run:
            return service.timers.actions.evaluate(view, now=service.timers.now(),
                                                   target_lane_id=lane_id, dry_run=True)
        return service.timers.request('reset_credits', target=lane_id)
    if args.dry_run:
        if args.command == 'watch':
            from .alerts import evaluate_conditions
            return {'status': 'preview', 'dry_run': True, 'cached': True,
                    'conditions': evaluate_conditions(view, now=service.timers.now()),
                    'detail': 'Cached conditions only; no probes, alerts, or reset actions were run.'}
        candidates = []
        for lane in service.store.list_lanes():
            if lane.provider != 'claude':
                continue
            recent = service.timers.latest_request(lane.lane_id)
            open_now = recent and (service.timers.now() - recent).total_seconds() < 18000
            busy = service.store.one("SELECT holder FROM leases WHERE lease_key LIKE ?", (f'lane:{lane.lane_id}:slot:%',))
            state = ('skipped-unavailable' if lane.owner != 'v2' or not lane.enabled or lane.desktop else
                     'skipped-busy' if busy else 'skipped-open' if open_now else 'would-check')
            candidates.append({'lane_id': lane.lane_id, 'status': state})
        return {'status': 'preview', 'dry_run': True, 'results': candidates,
                'detail': 'Execution rechecks lane ownership, closures, and the last provider request.'}
    return service.timers.request('probe' if args.command == 'watch' else 'keepalive')


def cmd(args) -> int:
    from . import cli
    command = args.operation
    if command == 'reset' and bool(args.policy) == bool(args.target):
        return cli.fail(Exit.INVALID_INPUT, 'reset codex: provide a lane/all or --policy, not both')
    payload = {'command': command, 'dry_run': bool(getattr(args, 'dry_run', False))}
    if hasattr(args, 'hours'):
        payload['hours'] = args.hours
    if hasattr(args, 'target'):
        payload['target'] = args.target
    try:
        result = cli._client(args).call('operations', payload)
    except DaemonUnavailable as exc:
        if command not in ('brief', 'errors'):
            return cli._daemon_down(exc)
        try:
            offline = Offline(cli._root(args))
            if command == 'brief':
                result = {'text': brief(offline.status()), 'offline': True}
            else:
                with offline.reading() as conn:
                    result = error_report(lambda sql, params=(): [dict(r) for r in conn.execute(sql, params)], args.hours)
                result['offline'] = True
        except (OfflineUnavailable, protocol.ProtocolError) as error:
            return cli.fail(getattr(error, 'code', Exit.OPERATIONAL), str(error))
    except (DaemonError, protocol.ProtocolError) as exc:
        return cli.fail(exc.code, str(exc), getattr(exc, 'fix', None))
    if getattr(args, 'json', False):
        cli.emit(result)
    elif command == 'brief':
        cli.out(result['text'])
    elif command == 'canonical-model':
        cli.out(result['model'])
    elif command == 'login':
        cli.out(result['detail'])
        cli.out(result['login_command'])
        cli.out(result['enroll_command'])
    elif command == 'errors':
        entries = result['attempts'] + result['closures'] + result['probes']
        for row in entries:
            cli.out(json.dumps(row, sort_keys=True))
        if not entries:
            cli.out(f"No recorded errors in the last {args.hours:g}h.")
    else:
        cli.emit(result)
    return int(Exit.OPERATIONAL) if result.get('status') == 'stopping' else int(Exit.OK)


def cmd_uninstall_hooks(args) -> int:
    from . import cli, hooks
    report = hooks.plan(remove=True)
    if not report.get('ok'):
        return cli.fail(Exit.OPERATIONAL, report.get('error', 'cannot read hook settings'))
    cli.out(report['diff'])
    if args.dry_run:
        cli.note('Dry run: no hook settings were written.')
        return int(Exit.OK)
    result = hooks.apply(remove=True)
    if not result.get('ok'):
        return cli.fail(Exit.OPERATIONAL, result.get('error', 'cannot update hook settings'))
    cli.out('Removed Subfleet v2 hook entries.' if result['written'] else 'No Subfleet v2 hook entries installed.')
    return int(Exit.OK)


def add_verbs(sub) -> None:
    for name in ('errors', 'brief', 'watch', 'keepalive'):
        parser = sub.add_parser(name, help=f'native {name} from the v2 daemon')
        parser.set_defaults(handler=cmd, operation=name)
        parser.add_argument('--json', action='store_true')
        if name == 'errors':
            parser.add_argument('--hours', type=float, default=24)
        if name in ('watch', 'keepalive'):
            parser.add_argument('--dry-run', action='store_true')
        if name == 'keepalive':
            parser.add_argument('--family', choices=['claude'], default='claude')
    reset = sub.add_parser('reset', help='request the daemon’s guarded gifted-credit evaluation')
    families = reset.add_subparsers(dest='reset_family', required=True)
    codex = families.add_parser('codex')
    codex.set_defaults(handler=cmd, operation='reset')
    codex.add_argument('target', nargs='?')
    codex.add_argument('--policy', action='store_true')
    codex.add_argument('--dry-run', action='store_true')
    codex.add_argument('--json', action='store_true')
    login = sub.add_parser('login', help='show manual lane authentication and native re-enrollment commands')
    login.set_defaults(handler=cmd, operation='login')
    login.add_argument('family', choices=['codex'])
    login.add_argument('target')
    login.add_argument('--no-watch', action='store_true', help='accepted; v2 never launches a login watcher')
    login.add_argument('--no-open', action='store_true', help='accepted; v2 never opens provider login')
    login.add_argument('--json', action='store_true')
    canonical = sub.add_parser('_canonical-model', help=argparse.SUPPRESS)
    canonical.set_defaults(handler=cmd, operation='canonical-model')
    canonical.add_argument('target')
    uninstall = sub.add_parser('_hooks-uninstall', help=argparse.SUPPRESS)
    uninstall.set_defaults(handler=cmd_uninstall_hooks)
    uninstall.add_argument('--dry-run', action='store_true')
