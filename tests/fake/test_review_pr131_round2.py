"""Independent round-two probes; preserved as a patch in the review report."""
import json

import pytest

from subfleet import procs
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.fake.test_review_pr131_probes import BOOT, confirm_dead, ident, script_table
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_workspace_contract import repository


def quarantine(daemon, harness, *, owned=None, held=None):
    repository(daemon, harness)
    job, a, adir = reserve(daemon, harness, sandbox='workspace-write', in_place=True,
                          out_path=str(harness.root / 'held-output.md'))
    daemon.store.update_attempt(a['attempt_id'], state='running', guardian_pid=100, pgid=100,
        boot_id=BOOT, proc_start='guardian-start',
        evidence_json=json.dumps({'owned_identities': owned or {}}))
    a = daemon.store.get_attempt(a['attempt_id'])
    census = procs.Containment(marker_pids=frozenset(int(p) for p in held or {}),
        identities={int(p): procs.ProcessIdentity(**i) for p, i in (held or {}).items()},
        unverifiable=not held,
        errors=('group enumeration unavailable',) if not held else ())
    daemon._quarantine(a, census, 'termination could not verify containment')
    return daemon.store.get_attempt(a['attempt_id'])


def resolve(daemon, a, operator):
    if operator:
        confirm_dead(daemon, a)
    else:
        daemon._recheck_quarantines()
    actual = daemon.store.get_attempt(a['attempt_id'])
    leases = [l for l in daemon.store.list_leases()
              if l['holder'] in {a['job_id'], a['attempt_id']}]
    return actual, leases


def test_census_walks_descendants_of_a_recycled_orphan_group_member(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, owned={'200': ident(200, 'old')})
    script_table(monkeypatch, {200: (1, 100, 'S', 'new'),
                               300: (200, 300, 'Ss', 'escaped-child')})
    census = daemon._contain(a)
    assert 300 in census.live_pids, census.to_dict()


@pytest.mark.parametrize('operator', [False, True])
def test_recycled_group_members_escaped_child_keeps_leases_after_parent_exits(
        state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, owned={'200': ident(200, 'old')})
    script_table(monkeypatch, {200: (1, 100, 'S', 'new'),
                               300: (200, 300, 'Ss', 'escaped-child')})
    clock.advance()
    daemon._recheck_quarantines()
    a = daemon.store.get_attempt(a['attempt_id'])
    assert a['state'] == 'quarantined'
    first_reason = json.loads(a['quarantine_reason'])
    script_table(monkeypatch, {300: (1, 300, 'Ss', 'escaped-child')})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'quarantined' and leases, (actual['state'], leases, first_reason)


@pytest.mark.parametrize('new_receipt', [False, True])
@pytest.mark.parametrize('operator', [False, True])
def test_real_legacy_start_format_holds_unobserved_child_after_guardian_dies(
        state_daemon, monkeypatch, new_receipt, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    start = {'guardian_pid': 100, 'pgid': 100, 'boot_id': BOOT,
             'proc_start': 'guardian-start', 'started_at': '2026-10-05T00:00:00Z'}
    if new_receipt:
        start.update(child_pid=500, child_identity=ident(500, 'provider-start'))
    adir = daemon.root / 'jobs' / a['job_id'] / 'a1'
    (adir / 'start.json').write_text(json.dumps(start))
    assert a['child_pid'] is None and not (adir / 'exit.json').exists()
    # The old daemon's group-only owned census never recorded this provider,
    # which escaped its group and scrubbed markers before its first inspection.
    script_table(monkeypatch, {100: (1, 100, 'Ss', 'guardian-start'),
                               500: (100, 500, 'Ss', 'provider-start')})
    # Before the new daemon's first full census, the legacy guardian crashes
    # without reaping its child or writing exit.json. The provider is reparented.
    script_table(monkeypatch, {500: (1, 500, 'Ss', 'provider-start')})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'quarantined' and leases, (new_receipt, actual['state'], leases)


@pytest.mark.parametrize('operator', [False, True])
def test_new_guardian_crash_before_child_receipt_keeps_the_live_child(
        state_daemon, monkeypatch, operator):
    from subfleet import guardian
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    adir = daemon.root / 'jobs' / a['job_id'] / 'a1'
    spawned = []
    class Child:
        pid = 500
        def wait(self):
            # The gated launcher now sees EOF and is reaped without exec.
            return 127
    def spawn(*args, **kwargs):
        spawned.append(500)
        return Child()
    def start(pid):
        if pid == 500:
            raise RuntimeError('guardian died during child identity read')
        return 'guardian-start'
    previous_umask = guardian.os.umask(0o077)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(guardian.os, 'setsid', lambda: None)
            mp.setattr(guardian.os, 'getpid', lambda: 100)
            mp.setattr(guardian.os, 'getpgrp', lambda: 100)
            mp.setattr(guardian.signal, 'signal', lambda *args: None)
            mp.setattr(guardian, 'boot_id', lambda: BOOT)
            mp.setattr(guardian, 'proc_start', start)
            mp.setattr(guardian.subprocess, 'Popen', spawn)
            with pytest.raises(RuntimeError, match='guardian died during child identity read'):
                guardian.run_guardian(['provider'], attempt_dir=adir, cwd=str(harness.workdir),
                    stdin_path=None, stdout_path=str(adir / 'stdout'), stderr_path=str(adir / 'stderr'))
    finally:
        guardian.os.umask(previous_umask)
    assert spawned == [500] and not (adir / 'exit.json').exists()
    receipt = json.loads((adir / 'start.json').read_text())
    assert 'child_pid' not in receipt and 'child_identity' not in receipt
    # No provider executed: publication failed before the gate opened. Missing
    # publication still holds conservatively, including legacy receipt formats.
    script_table(monkeypatch, {500: (1, 500, 'Ss', 'provider-start')})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'quarantined' and leases, (actual['state'], leases, receipt)


@pytest.mark.parametrize('root_kind', ['child', 'group'])
@pytest.mark.parametrize('operator', [False, True])
def test_pre_reboot_roots_do_not_own_new_boot_processes(state_daemon, monkeypatch, operator, root_kind):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    old_boot = '6F1C0F2E-1111-4222-8333-944455556666'
    new_boot = '7F1C0F2E-1111-4222-8333-944455556666'
    daemon.store.update_attempt(a['attempt_id'], child_pid=500 if root_kind == 'child' else None,
                                boot_id=old_boot)
    a = daemon.store.get_attempt(a['attempt_id'])
    # A legacy exit receipt supplied the PID, but not the child's launch identity.
    adir = daemon.root / 'jobs' / a['job_id'] / 'a1'
    (adir / 'start.json').write_text(json.dumps({'guardian_pid': 100, 'pgid': 100,
        'boot_id': old_boot, 'proc_start': 'guardian-start'}))
    (adir / 'exit.json').write_text(json.dumps({'rc': 0, 'child_pid': 500}))
    rows = ({500: (1, 500, 'Ss', 'system-service')} if root_kind == 'child' else
            {200: (1, 100, 'S', 'system-service')})
    script_table(monkeypatch, rows, boot=new_boot)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'lost' and not leases, (actual['state'], leases)


@pytest.mark.parametrize('operator', [False, True])
def test_a_writer_forking_after_the_table_read_does_not_lose_its_child(
        state_daemon, monkeypatch, operator):
    """C-5.7 residual: a process never observed by lineage/group/cwd, outside
    retained groups and descendant walks, which runs a platform binary with
    invisible markers (or removes both markers from ps-visible output, including
    by rewriting its title), can escape both paths. No new
    session is required. Here it scrubs both and never has cwd in the workdir.
    """
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, held={'300': ident(300, 'writer-start')})
    world = {300: (1, 300, 'Ss', 'writer-start')}
    monkeypatch.setattr(procs, 'snapshot', lambda: procs.ProcessTable(dict(world), boot_id=BOOT))
    def marker_read(argv, **kwargs):
        assert 'pid=,command=' in argv
        # Recorded writer forks after the table snapshot; child escapes its
        # group and scrubs markers before the later environment scan.
        world[301] = (300, 301, 'Ss', 'late-child')
        return ''
    monkeypatch.setattr(procs, '_read', marker_read)
    from tests.fake.test_review_pr131_probes import ORIGINAL_CENSUS
    monkeypatch.setattr(procs, 'containment', ORIGINAL_CENSUS)
    clock.advance()
    daemon._recheck_quarantines()
    a = daemon.store.get_attempt(a['attempt_id'])
    assert a['state'] == 'quarantined' and 301 in world
    # Parent exits between passes. Its child is now an unmarked orphan.
    script_table(monkeypatch, {301: (1, 301, 'Ss', 'late-child')})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'lost' and not leases, (actual['state'], leases)
    assert daemon._contain(actual).verified_empty  # live row 301 is outside lineage, group, cwd and marker sources


@pytest.mark.parametrize('operator', [False, True])
def test_forked_child_keeping_markers_holds_across_many_paces_after_parent_exits(
        state_daemon, monkeypatch, operator):
    """A non-platform child whose title exposes either marker remains visible."""
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, held={'300': ident(300, 'writer-start')})
    before = daemon.store.list_leases()
    world = {300: (1, 300, 'Ss', 'writer-start')}
    marker = f'SUBFLEET_ATTEMPT={a["attempt_id"]} SUBFLEET_ROOT={daemon.root}'
    monkeypatch.setattr(procs, 'snapshot', lambda: procs.ProcessTable(dict(world), boot_id=BOOT))
    def marker_read(argv, **kwargs):
        assert 'pid=,command=' in argv
        # Fork after the table read, detach into a new session, keep markers.
        world[301] = (300, 301, 'Ss', 'late-child')
        return f'301 writer {marker}\n'
    monkeypatch.setattr(procs, '_read', marker_read)
    monkeypatch.setattr(procs, '_stat', lambda pid: world[pid][2])
    monkeypatch.setattr(procs, 'identity', lambda pid: procs.ProcessIdentity(pid, BOOT, world[pid][3]))
    from tests.fake.test_review_pr131_probes import ORIGINAL_CENSUS
    monkeypatch.setattr(procs, 'containment', ORIGINAL_CENSUS)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'quarantined' and leases
    reason = json.loads(actual['quarantine_reason'])
    assert reason['marker_pids'] == [301] and reason['identities']['301'] == ident(301, 'late-child')
    # Parent exits; the detached child still carries both environment markers.
    script_table(monkeypatch, {301: (1, 301, 'Ss', 'late-child')}, markers=f'301 writer {marker}\n')
    for _ in range(20):
        clock.advance()
        actual, leases = resolve(daemon, actual, operator)
        assert actual['state'] == 'quarantined' and daemon.store.list_leases() == before
        assert json.loads(actual['quarantine_reason'])['marker_pids'] == [301]
    assert not daemon.store.one("SELECT 1 FROM events WHERE kind IN ('quarantine.self_resolved','quarantine.confirmed_dead')")
    script_table(monkeypatch, {})
    clock.advance()
    actual, leases = resolve(daemon, actual, operator)
    assert actual['state'] == 'lost' and not leases


@pytest.mark.parametrize('operator', [False, True])
def test_empty_same_boot_census_releases_within_one_pace_after_restart(state_daemon, monkeypatch, operator):
    from subfleet.daemon import Daemon
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, held={'300': ident(300, 'writer-start')})
    # Previously saved boots, including unknown evidence, must not gate release.
    daemon.store.update_attempt(a['attempt_id'], evidence_json=json.dumps({'lineage_boot_ids': [BOOT, '']}))
    before = daemon.store.list_leases()
    script_table(monkeypatch, {})
    clock.advance(9)
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a['attempt_id'])['state'] == 'quarantined'
    assert daemon.store.list_leases() == before
    daemon.close()
    restarted = Daemon(harness.root)
    try:
        restarted.policy['quarantine_recheck_s'] = 10
        # The durable pace survives restart, but release needs no boot change.
        restarted._recheck_quarantines()
        assert restarted.store.get_attempt(a['attempt_id'])['state'] == 'quarantined'
        clock.advance(1)
        actual, leases = resolve(restarted, a, operator)
        assert actual['state'] == 'lost' and not leases
        events = [e for e in restarted.store.list_events(a['job_id'])
                  if e['kind'] in {'quarantine.self_resolved', 'quarantine.confirmed_dead'}]
        assert len(events) == 1
        assert json.loads(events[0]['data_json'])['containment']['unverifiable'] is False
    finally:
        restarted.close()


@pytest.mark.parametrize('operator', [False, True])
def test_current_boot_marked_writer_survives_old_reboot_proof(state_daemon, monkeypatch, operator):
    from subfleet.daemon import Daemon
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    current = '7F1C0F2E-1111-4222-8333-944455556666'
    marker = f'SUBFLEET_ATTEMPT={a["attempt_id"]} SUBFLEET_ROOT={daemon.root}'
    script_table(monkeypatch, {600: (1, 600, 'Ss', 'new-writer')},
                 boot=current, markers=f'600 writer {marker}\n')
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == 'quarantined' and leases
    daemon.close()
    restarted = Daemon(harness.root)
    try:
        restarted.policy['quarantine_recheck_s'] = 10
        # Identity retained across restart still owns the escaped live writer,
        # even if it now scrubs its markers. The old boot cannot exclude it.
        script_table(monkeypatch, {600: (1, 600, 'Ss', 'new-writer')}, boot=current)
        clock.advance()
        actual, leases = resolve(restarted, actual, operator)
        assert actual['state'] == 'quarantined' and leases
        script_table(monkeypatch, {}, boot=current)
        clock.advance()
        actual, leases = resolve(restarted, actual, operator)
        assert actual['state'] == 'lost' and not leases
    finally:
        restarted.close()


@pytest.mark.parametrize('operator', [False, True])
@pytest.mark.parametrize('child_published', [False, True])
def test_launch_publication_hold_ends_when_child_is_known_or_guardian_waited(
        state_daemon, monkeypatch, operator, child_published):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    adir = daemon.root / 'jobs' / a['job_id'] / 'a1'
    start = {'guardian_pid': 100, 'pgid': 100, 'boot_id': BOOT, 'proc_start': 'guardian-start'}
    if child_published:
        start.update(child_pid=500, child_identity=ident(500, 'provider-start'))
    (adir / 'start.json').write_text(json.dumps(start))
    script_table(monkeypatch, {})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    if not child_published:
        assert actual['state'] == 'quarantined' and leases
        assert 'guardian child publication unavailable' in actual['quarantine_reason']
        # A legacy guardian's exit receipt attests that it waited for its child.
        (adir / 'exit.json').write_text(json.dumps({'rc': 0, 'child_pid': 500}))
        clock.advance()
        actual, leases = resolve(daemon, actual, operator)
    assert actual['state'] == 'lost' and not leases


@pytest.mark.parametrize('operator', [False, True])
@pytest.mark.parametrize('capture', ['gone', 'uninspectable'])
def test_marker_identity_race_retries_inspection_errors_without_requiring_reboot(
        state_daemon, monkeypatch, operator, capture):
    """A gone marked PID is excluded; a failed identity inspection still holds."""
    from subfleet.daemon import Daemon
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    marker = f'SUBFLEET_ATTEMPT={a["attempt_id"]} SUBFLEET_ROOT={daemon.root}'
    script_table(monkeypatch, {}, markers=f'600 writer {marker}\n')
    monkeypatch.setattr(procs, '_stat', lambda pid: 'S')
    def identify(pid):
        if capture == 'uninspectable':
            raise procs.InspectionError('identity unavailable')
        return None
    monkeypatch.setattr(procs, 'identity', identify)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual['state'] == ('lost' if capture == 'gone' else 'quarantined')
    if capture == 'gone':
        assert not leases
        return
    assert leases and '600' not in json.loads(actual['quarantine_reason'])['identities']
    daemon.close()
    restarted = Daemon(harness.root)
    try:
        restarted.policy['quarantine_recheck_s'] = 10
        script_table(monkeypatch, {})
        clock.advance()
        actual, leases = resolve(restarted, actual, operator)
        assert actual['state'] == 'lost' and not leases
    finally:
        restarted.close()


@pytest.mark.parametrize('reason', ['held', 'null'])
def test_plain_reason_keeps_diagnostic_boot_evidence_without_pinning_a_gone_marker(state_daemon, monkeypatch, reason):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    marker = f'SUBFLEET_ATTEMPT={a["attempt_id"]} SUBFLEET_ROOT={daemon.root}'
    script_table(monkeypatch, {}, markers=f'600 writer {marker}\n')
    monkeypatch.setattr(procs, '_stat', lambda pid: 'S')
    def identify(pid):
        raise procs.InspectionError('identity unavailable')
    monkeypatch.setattr(procs, 'identity', identify)
    clock.advance()
    actual, leases = resolve(daemon, a, False)
    assert actual['state'] == 'quarantined' and leases
    assert BOOT in json.loads(actual['evidence_json'])['lineage_boot_ids']
    daemon.store.update_attempt(a['attempt_id'], quarantine_reason=reason)
    script_table(monkeypatch, {})
    clock.advance()
    actual, leases = resolve(daemon, actual, False)
    assert actual['state'] == 'lost' and not leases
