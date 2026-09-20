"""The desktop sidebar mirror and its health sidecar: C-23.28.

Every test names the clause it proves (C-20.5). The desktop store lives under
`tmp_path` through `SUBFLEET_SESSION_STORE`, and the transcripts under
`SUBFLEET_CLAUDE_DIR`; nothing here reads or writes the operator's own.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

ONE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
TWO = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
ACCOUNT_A, ORG_A = "acct-aaaa", "org-aaaa"
ACCOUNT_B, ORG_B = "acct-bbbb", "org-bbbb"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A state root, a `~/.claude`, and a two-account desktop store."""
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)):
        (store / account / org).mkdir(parents=True, exist_ok=True)
    root = tmp_path / "state"
    root.mkdir()
    return home, store, root


def engine(world, **policy_overrides) -> mirror.Mirror:
    _home, _store, root = world
    return mirror.Mirror(root, fx.policy(**policy_overrides), now=lambda: fx.NOW)


def openable(home, store, session_id: str, account: str, org: str, **kwargs):
    """An index entry whose transcript exists, so the session is openable."""
    fx.transcript(home, session_id, fx.completed())
    return fx.index_entry(store, account, org, session_id, **kwargs)


def copies(store: Path, session_id: str) -> dict[Path, dict]:
    found = {}
    for path in store.glob("*/*/local_*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cliSessionId") == session_id:
            found[path] = data
    return found


def count_entry_reads(monkeypatch):
    read = mirror._load
    seen = []

    def counted(path, **kwargs):
        if path.name.startswith("local_"):
            seen.append(path)
        return read(path, **kwargs)

    monkeypatch.setattr(mirror, "_load", counted)
    return seen


def test_large_mirror_reuses_unchanged_entries_and_propagates_one_edit(world, monkeypatch):
    """C-23.28: account copies share payloads; a warm pass reads only changed entries."""
    home, store, _root = world
    folders = [(ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)] + [
        (f"account-{n}", f"org-{n}") for n in range(6)]
    identities = [f"session-{n}" for n in range(80)]
    for identity in identities:
        fx.transcript(home, identity, fx.completed())
        for account, org in folders:
            fx.index_entry(store, account, org, identity, settings={"ultracode": True},
                           metadata={"preview": "retained conversation metadata " * 400})
    reads = count_entry_reads(monkeypatch)
    running = engine(world)
    assert running.run_once().state == "ok"
    assert len(reads) == 640
    assert len(running._entries) == 640
    assert len(running._payloads) == 80, "equal account copies must not multiply retained payloads"
    assert running.run_once().entries_scanned == 640
    assert len(reads) == 640, "a warm pass must not reopen unchanged sidebar JSON"

    path = store / ACCOUNT_A / ORG_A / "local_session-0.json"
    old_snapshot = running._entry(path)
    data = json.loads(path.read_text())
    data.update(isArchived=True, title="Operator title", titleSource="manual")
    path.write_text(json.dumps(data))
    result = running.run_once()
    assert result.flag_synced == result.retitled == 1
    assert reads[640:] == [path]
    updated = copies(store, identities[0])
    assert len(updated) == 8
    assert all(row["isArchived"] and row["title"] == "Operator title" for row in updated.values())
    assert old_snapshot["isArchived"] is False, "sync must not mutate interned snapshots"
    assert old_snapshot["title"] == "a session"


def test_atomic_replacement_invalidates_cache_even_with_same_mtime_and_size(world, monkeypatch):
    """C-23.28: an app's atomic replacement is an edit even if its timestamp is preserved."""
    home, store, _root = world
    path = openable(home, store, ONE, ACCOUNT_A, ORG_A,
                    title="old title", title_source="manual", settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    running.run_once()  # also cache the new account copy
    before = path.stat()
    replacement = path.with_suffix(".replacement")
    replacement.write_text(path.read_text().replace("old title", "new title"))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    reads = count_entry_reads(monkeypatch)
    assert running.run_once().retitled == 1
    assert reads == [path]
    assert {row["title"] for row in copies(store, ONE).values()} == {"new title"}


def test_deleted_entries_are_evicted_and_not_resurrected_from_cache(world):
    """C-23.28: a cached historical copy is never a source after the app deletes it."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    running.run_once()
    for path in copies(store, ONE):
        path.unlink()
    assert running.run_once().sessions == 0
    assert not copies(store, ONE)
    assert not running._entries and not running._payloads
    assert running._payload_bytes == 0


def test_transient_read_failure_is_retried_without_a_metadata_change(world, monkeypatch):
    """C-23.28: an I/O failure must not become a cached, apparently empty entry."""
    home, store, _root = world
    path = openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    read = Path.read_text
    failed = False

    def fail_once(target, *args, **kwargs):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError("transient read failure")
        return read(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_once)
    running = engine(world)
    signature = running._signature(path)
    assert running.run_once().added == 0
    assert path not in running._entries
    assert running._signature(path) == signature
    assert running.run_once().added == 1
    assert len(copies(store, ONE)) == 2


@pytest.mark.parametrize("limit,value", [("ENTRY_CACHE_LIMIT", 1), ("PAYLOAD_CACHE_BYTES", 1)])
def test_cache_limits_do_not_limit_mirrored_sessions(world, monkeypatch, limit, value):
    """C-23.28: bounded retention may cost reads but cannot omit any session from a pass."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    openable(home, store, TWO, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    monkeypatch.setattr(mirror, limit, value)
    running = engine(world)
    assert running.run_once().added == 2
    assert running.run_once().sessions == 2
    assert len(copies(store, ONE)) == len(copies(store, TWO)) == 2
    assert len(running._entries) <= mirror.ENTRY_CACHE_LIMIT
    assert running._payload_bytes <= mirror.PAYLOAD_CACHE_BYTES


def test_cancelled_inventory_reports_progress_without_advancing_last_success(world, monkeypatch):
    """C-23.28: interruption is observable and a cancelled inventory writes no sidebar data."""
    home, store, root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    previous_ok = running.sidecar()["last_ok_at"]
    before = {p: p.read_bytes() for p in store.glob("*/*/local_*.json")}
    cancel = running.cancel = threading.Event()
    running.now = lambda: fx.NOW + timedelta(minutes=1)
    read = running._entry
    recorded = []
    record = running._record

    def observe(current, **kwargs):
        recorded.append(current.to_dict())
        return record(current, **kwargs)

    def cancel_after_read(path):
        value = read(path)
        cancel.set()
        return value

    monkeypatch.setattr(running, "_record", observe)
    monkeypatch.setattr(running, "_entry", cancel_after_read)
    result = running.run_once()
    assert result.state == "cancelled" and result.entries_scanned == 1
    assert result.stage == "reading entries"
    assert any(row["stage"] == "reading entries" and row["state"] == "running" for row in recorded)
    assert running.sidecar()["last_ok_at"] == previous_ok
    assert running.health()["status"] == "stalled"
    assert {p: p.read_bytes() for p in before} == before
    assert result.finished_at is not None
    cancel.clear()
    monkeypatch.setattr(running, "_entry", read)
    assert running.run_once().state == "ok", "the interrupted pass released its lock"


def test_cancellation_during_flag_publication_finishes_the_matching_merge_base(world, monkeypatch):
    """C-23.28: shutdown cannot interrupt the dirty-copy batch before its merge base is saved."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, settings={"ultracode": True})
    running = engine(world)
    running.run_once()
    running.run_once()
    path = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    data = json.loads(path.read_text())
    data["isArchived"] = True
    path.write_text(json.dumps(data))
    cancel = running.cancel = threading.Event()
    write = mirror._write_json

    def cancel_after_first_write(target, value, **kwargs):
        write(target, value, **kwargs)
        if target.name.startswith("local_"):
            cancel.set()

    monkeypatch.setattr(mirror, "_write_json", cancel_after_first_write)
    result = running.run_once()
    assert result.state == "cancelled"
    assert all(row["isArchived"] for row in copies(store, ONE).values())
    assert json.loads(running.flags_path.read_text())[ONE]["isArchived"] is True
    cancel.clear()
    assert running.run_once().state == "ok"
    assert all(row["isArchived"] for row in copies(store, ONE).values())


# --- v1's saved per-user options ---------------------------------------------

def test_saved_config_restores_an_archive_without_timer_arguments(world, tmp_path):
    """C-17.1 and plan decision 8: the timer preserves v1's archive settings."""
    home, store, _root = world
    fx.index_entry(store, ACCOUNT_A, ORG_A, ONE)
    archive = tmp_path / "archive"
    archive.mkdir()
    archived = archive / f"{ONE}.jsonl"
    archived.write_text("archived transcript\n", encoding="utf-8")
    config = home / mirror.CONFIG_NAME
    saved = json.dumps({"archive": str(archive / "*.jsonl"), "dead_home": ORG_B})
    config.write_text(saved, encoding="utf-8")

    options = mirror.options_from(fx.policy())
    result = engine(world).run_once(options)
    assert result.revived == 1 and len(copies(store, ONE)) == 2
    assert options.dead_home == ORG_B
    assert config.read_text(encoding="utf-8") == saved, "v1's config remains read-only"


def test_cli_exclusions_add_to_configured_exclusions(world):
    """C-17.1: v1's --exclude adds exclusions without dropping saved ones."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    (home / mirror.CONFIG_NAME).write_text(json.dumps({"exclude": [ORG_A]}))
    options = mirror.options_from(fx.policy(), exclude=(ORG_B,))

    assert set(options.exclude) == {ORG_A, ORG_B}
    assert engine(world).run_once(options).accounts == 0


def test_explicit_empty_archive_and_dead_home_override_saved_defaults(world):
    """C-17.1: explicit v1 flags may clear saved archive and dead-home values."""
    home, _store, _root = world
    (home / mirror.CONFIG_NAME).write_text(json.dumps({
        "archive": "/fixture/archive/*.jsonl", "dead_home": ORG_B,
        "exclude": [ORG_A]}))
    options = mirror.options_from(fx.policy(), archive="", dead_home="", exclude=())
    assert options.archive == "" and options.dead_home == ""
    assert options.exclude == (ORG_A,)


@pytest.mark.parametrize("excluded", [False, 42, "org-aaaa", {"org-aaaa": True}])
def test_malformed_saved_exclusions_do_not_break_the_mirror(world, excluded):
    """C-23.28: malformed saved exclusions do not prevent a pass from running."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    (home / mirror.CONFIG_NAME).write_text(json.dumps({"exclude": excluded}))
    options = mirror.options_from(fx.policy())
    assert options.exclude == ()
    assert engine(world).run_once(options).added == 1


# --- the copy (plan decision 8) -----------------------------------------------

def test_an_openable_session_is_copied_into_every_account(world):
    """Plan decision 8: the sidebar shows only the logged-in account's folder,
    so a session vanishes on a switch until its index file exists everywhere."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    result = engine(world).run_once(mirror.Options())
    assert result.added == 1
    assert len(copies(store, ONE)) == 2


def test_a_dead_session_is_never_spread(world):
    """v1's rule, kept: a session whose transcript Claude Code pruned shows
    "no messages" in every account, so copying it spreads a dead row."""
    _home, store, _root = world
    fx.index_entry(store, ACCOUNT_A, ORG_A, ONE)        # no transcript written
    result = engine(world).run_once(mirror.Options())
    assert result.added == 0
    assert len(copies(store, ONE)) == 1


def test_a_stale_empty_copy_is_repaired_in_place(world):
    """v1's identity rule: the FILENAME is the stable identity, not the
    cliSessionId, which is filled in later — so an early copy can freeze empty
    and render as "no messages"."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    stale = store / ACCOUNT_B / ORG_B / f"local_{ONE}.json"
    stale.write_text(json.dumps({"sessionId": f"local_{ONE}", "cliSessionId": ""}),
                     encoding="utf-8")
    result = engine(world).run_once(mirror.Options())
    assert (result.added, result.repaired) == (0, 1)
    assert json.loads(stale.read_text())["cliSessionId"] == ONE


def test_a_filename_collision_falls_back_instead_of_clobbering(world):
    """v1's rule: `local_<id>` filenames are not unique across accounts, so a
    collision must not overwrite a different session."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, name="local_shared.json")
    openable(home, store, TWO, ACCOUNT_B, ORG_B, name="local_shared.json")
    engine(world).run_once(mirror.Options())
    survivors = {path.name: json.loads(path.read_text())["cliSessionId"]
                 for path in (store / ACCOUNT_B / ORG_B).glob("local_*.json")}
    assert survivors["local_shared.json"] == TWO, "the resident session is untouched"
    assert survivors[f"local_{ONE}.json"] == ONE, "the newcomer gets its own name"


def test_the_most_recently_active_copy_is_the_one_that_is_spread(world):
    """v1's rule: the canonical copy is the resolvable one with the newest activity."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, title="old", last_activity=10)
    fx.index_entry(store, ACCOUNT_B, ORG_B, ONE, title="new", last_activity=99,
                   name="local_other.json")
    engine(world).run_once(mirror.Options())
    spread = [data["title"] for path, data in copies(store, ONE).items()
              if path.name == "local_other.json" or path.parent.name == ORG_A]
    assert "new" in spread


def test_a_dry_run_changes_nothing_and_still_reports(world):
    """C-17.4: a preview that writes is not a preview."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    result = engine(world).run_once(mirror.Options(dry_run=True))
    assert result.added == 1
    assert len(copies(store, ONE)) == 1
    assert not (engine(world).sidecar_path).exists(), "a dry run leaves no sidecar"


def test_the_mirror_never_calls_a_provider(world, monkeypatch):
    """C-23.28 and plan decision 8: the mirror is a file copy, nothing more."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(
        "the mirror ran a subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail(
        "the mirror spawned a process"))
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    assert engine(world).run_once(mirror.Options()).state == "ok"


# --- flag sync ----------------------------------------------------------------

def test_archiving_in_one_account_propagates_to_every_copy(world):
    """v1's merge base, kept: a copy that differs from the last synced value is
    a user action, so the CHANGE wins — archive and un-archive both work."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    engine(world).run_once(mirror.Options())            # bootstrap: both false
    a_copy = store / ACCOUNT_A / ORG_A / f"local_{ONE}.json"
    data = json.loads(a_copy.read_text())
    data["isArchived"] = True
    a_copy.write_text(json.dumps(data), encoding="utf-8")

    result = engine(world).run_once(mirror.Options())
    assert result.flag_synced == 1
    assert all(item["isArchived"] for item in copies(store, ONE).values())

    # And back again: the change wins in both directions.
    data = json.loads(a_copy.read_text())
    data["isArchived"] = False
    a_copy.write_text(json.dumps(data), encoding="utf-8")
    engine(world).run_once(mirror.Options())
    assert not any(item["isArchived"] for item in copies(store, ONE).values())


def test_with_no_merge_base_archived_anywhere_wins(world):
    """v1's bootstrap, kept: the historical backlog has no base, and archiving
    is an affirmative act while not-archiving is the default."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, archived=True)
    fx.index_entry(store, ACCOUNT_B, ORG_B, ONE, archived=False,
                   name="local_other.json")
    engine(world).run_once(mirror.Options())
    assert all(item["isArchived"] for item in copies(store, ONE).values())


def test_the_merge_base_lives_in_the_state_root_not_in_claude_dir(world):
    """v2 never edits v1's `~/.claude/cc-mirror-state.json`, so the two mirrors
    can run side by side during the shadow period."""
    home, store, root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    engine(world).run_once(mirror.Options())
    assert (root / "sessions" / mirror.FLAGS_NAME).is_file()
    assert not (home / "cc-mirror-state.json").exists()


def test_a_divergent_title_prefers_a_manual_rename(world):
    """v1's rule: mtimes are noise (the app rewrites on focus), so a manual
    rename beats an automatic one and the most recently active breaks ties."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A, title="auto name",
             title_source="auto", last_activity=99)
    fx.index_entry(store, ACCOUNT_B, ORG_B, ONE, title="what Max called it",
                   title_source="manual", last_activity=1, name="local_other.json")
    result = engine(world).run_once(mirror.Options())
    assert result.retitled == 1
    assert {item["title"] for item in copies(store, ONE).values()} == {"what Max called it"}


def test_an_entry_without_the_ultracode_key_gets_the_default(world):
    """2026-08-26: the app's spawn path never passes sessionSettings, so a
    spawned session silently missed the ultracode ruling."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    engine(world).run_once(mirror.Options())
    assert all(item["sessionSettings"]["ultracode"] is True
               for item in copies(store, ONE).values())


def test_an_explicit_ultracode_false_is_respected(world):
    """The same rule's other half: it is a default, not a sync."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A,
             settings={"ultracode": False})
    engine(world).run_once(mirror.Options())
    assert all(item["sessionSettings"]["ultracode"] is False
               for item in copies(store, ONE).values())


def test_flag_sync_can_be_switched_off(world):
    """v1's `--no-flag-sync`, kept."""
    home, store, _root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    engine(world).run_once(mirror.Options(flag_sync=False))
    for item in copies(store, ONE).values():
        assert "sessionSettings" not in item


# --- prune --------------------------------------------------------------------

def test_prune_keeps_one_dead_copy_in_the_dead_home(world):
    """v1's `--prune`, kept and still off by default: the Claude app prunes
    dead copies itself on load."""
    _home, store, _root = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)):
        fx.index_entry(store, account, org, ONE)        # no transcript: dead
    engine(world).run_once(mirror.Options(prune=True, dead_home=ORG_A))
    assert (store / ACCOUNT_A / ORG_A / f"local_{ONE}.json").exists()
    assert not (store / ACCOUNT_B / ORG_B / f"local_{ONE}.json").exists()


def test_prune_is_off_by_default(world):
    """The default pass removes nothing."""
    _home, store, _root = world
    for account, org in ((ACCOUNT_A, ORG_A), (ACCOUNT_B, ORG_B)):
        fx.index_entry(store, account, org, ONE)
    assert engine(world).run_once(mirror.Options()).pruned == 0
    assert len(copies(store, ONE)) == 2


# --- health: the per-pass sidecar (C-23.28) -----------------------------------

def test_mirror_heartbeat_reads_per_pass_sidecar_with_thirty_minute_cutoff(world):
    """C-23.28: mirror health is judged from the per-pass state sidecar, never
    from log recency: a pass the sidecar records as in flight is healthy until
    thirty minutes after its recorded start, and only then is the mirror
    `stalled`.

    Ledger row 159. A quiet log produced a false "stalled" on 2026-08-19 07:08,
    and an 8.5-minute pass was observed on 2026-08-18 during app churn.
    """
    home, store, root = world
    running = engine(world)
    assert running.health()["status"] == "absent", "a fresh root has never run"

    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    running.run_once(mirror.Options())
    assert running.health()["status"] == "healthy"

    # A pass that started and has not finished: in flight, not stalled.
    started = fx.NOW - timedelta(minutes=8.5)
    (root / "sessions" / mirror.SIDECAR_NAME).write_text(json.dumps({
        "pass": {"started_at": fx.iso(started), "finished_at": None,
                 "state": "running"},
        "updated_at": fx.iso(started)}), encoding="utf-8")
    in_flight = running.health()
    assert in_flight["status"] == "running"
    assert in_flight["run_min"] == 8.5

    # The same pass, forty-five minutes in: hung.
    hung = fx.NOW - timedelta(minutes=45)
    (root / "sessions" / mirror.SIDECAR_NAME).write_text(json.dumps({
        "pass": {"started_at": fx.iso(hung), "finished_at": None, "state": "running"},
        "updated_at": fx.iso(hung)}), encoding="utf-8")
    assert running.health()["status"] == "stalled"
    assert "run hung for 45.0 min" in running.health()["detail"]


def test_a_finished_pass_goes_stale_after_the_stall_window(world):
    """C-23.28: the sidecar's own timestamp is the heartbeat, not a log's mtime."""
    home, store, root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    engine(world).run_once(mirror.Options())
    sidecar = root / "sessions" / mirror.SIDECAR_NAME
    data = json.loads(sidecar.read_text())
    data["updated_at"] = fx.iso(fx.NOW - timedelta(minutes=42))
    data["pass"]["finished_at"] = data["updated_at"]
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    health = engine(world).health()
    assert health["status"] == "stalled"
    assert "sidecar idle 42.0 min, no pass in flight" in health["detail"]


def test_a_no_op_pass_still_refreshes_the_heartbeat(world):
    """C-23.28's whole point: `--quiet` silences the log on a no-op pass, so the
    sidecar is written unconditionally and the log is never consulted."""
    _home, _store, root = world
    result = engine(world).run_once(mirror.Options())
    assert (result.added, result.repaired) == (0, 0)
    assert (root / "sessions" / mirror.SIDECAR_NAME).is_file()
    assert engine(world).health()["status"] == "healthy"


def test_the_sidecar_records_the_pass_before_the_work_starts(world):
    """C-23.28: a pass that hangs must be visible as in flight, not as silence."""
    home, store, root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    seen: list[dict] = []
    engine_under_test = engine(world)
    original = engine_under_test._pass                   # noqa: SLF001 - the seam

    def observe(current, options):
        seen.append(json.loads((root / "sessions" / mirror.SIDECAR_NAME).read_text()))
        return original(current, options)

    engine_under_test._pass = observe                    # noqa: SLF001
    engine_under_test.run_once(mirror.Options())
    assert seen and seen[0]["pass"]["state"] == "running"
    assert seen[0]["pass"]["finished_at"] is None


def test_a_failed_pass_is_stalled_and_says_why(world, monkeypatch):
    """C-23.28: an error is a health signal, not a silent no-op."""
    engine_under_test = engine(world)
    monkeypatch.setattr(engine_under_test, "_pass",
                        lambda *a: (_ for _ in ()).throw(OSError("disk gone")))
    result = engine_under_test.run_once(mirror.Options())
    assert result.state == "error" and "disk gone" in result.error
    health = engine_under_test.health()
    assert health["status"] == "stalled" and "last pass failed" in health["detail"]


@pytest.mark.parametrize("run_min, status", [(10, "running"), (45, "stalled")])
def test_a_second_pass_that_finds_the_lock_held_touches_nothing(world, run_min, status):
    """C-23.28: a 60 s timer over a pass that is still running fires constantly,
    and the loser must not overwrite the running pass's record.

    If it did, a pass hung for an hour would read `healthy`, which is the exact
    reading the clause exists to prevent.
    """
    import fcntl
    home, store, root = world
    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    engine_under_test = engine(world)

    # A pass in flight, within the grace period or already stalled.
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    in_flight = json.dumps({
        "pass": {"started_at": fx.iso(fx.NOW - timedelta(minutes=run_min)),
                 "finished_at": None, "state": "running"},
        "updated_at": fx.iso(fx.NOW - timedelta(minutes=run_min))})
    (root / "sessions" / mirror.SIDECAR_NAME).write_text(in_flight, encoding="utf-8")

    held = engine_under_test._lock()                     # noqa: SLF001 - the seam
    assert held is not None
    try:
        result = engine(world).run_once(mirror.Options())
        assert result.state == "ok", "a contended pass is normal, not an error"
        assert "another pass holds the lock" in result.error
        assert (root / "sessions" / mirror.SIDECAR_NAME).read_text() == in_flight
        assert engine(world).health()["status"] == status, \
            "the pass that is actually in flight is still the one reported"
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


# --- doctor reads the same file ------------------------------------------------

def test_doctor_reports_the_mirror_from_the_sidecar_alone(world):
    """C-23.28: `doctor`'s row is the sidecar's verdict, with a fix attached.

    `doctor` has no injected clock — it reports what is true now — so this pass
    runs on the real one rather than on the fixture instant.
    """
    from subfleet import doctor
    home, store, root = world

    def row():
        return [item for item in doctor.checks(root)
                if item["check"] == "desktop sidebar mirror"][0]

    assert row()["status"] == "unknown", "a root that never ran a pass"

    openable(home, store, ONE, ACCOUNT_A, ORG_A)
    mirror.Mirror(root, fx.policy()).run_once(mirror.Options())
    assert row()["status"] == "pass"
    assert "sidecar" in row()["fix"]


# --- the timer ----------------------------------------------------------------

def test_the_mirror_is_a_sixty_second_daemon_timer(tmp_path):
    """C-23.28 and plan decision 8: a 60 s file-copy timer in the daemon.

    It runs on its own worker: an 8.5-minute pass sharing the two-slot cycle
    pool would hold a probe or a keepalive behind it for minutes.
    """
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(tmp_path / "state.sqlite3")
    timers = Timers(store, tmp_path, fx.policy())
    try:
        assert timers.intervals["mirror"] == 60
        assert "mirror" in timers.status()
        assert timers._mirror is not timers._cycles      # noqa: SLF001 - the point
        assert hasattr(timers, "mirror_cycle")
    finally:
        timers.stop()
        store.close()


def test_the_timer_can_be_switched_off_without_removing_the_verb(tmp_path):
    """C-6.4: `sessions.mirror_interval_s: 0` is a policy change."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(tmp_path / "state.sqlite3")
    timers = Timers(store, tmp_path, fx.policy(mirror_interval_s=0))
    try:
        assert "mirror" not in timers.intervals
    finally:
        timers.stop()
        store.close()
