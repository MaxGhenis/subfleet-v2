"""Every copy of a session runs on one model, effort and place: C-23.28.

The desktop app stores one copy of each Code session record per account
folder, and runs a resumed session on the copy's `model` and `effort`, in its
`worktreePath` or `cwd` (bundle 2.9939.2). Before 2026-09-26 the mirror copied
a session only into folders that lacked it and synced only flags and titles,
so a model switch or a worktree move in one account never reached the others:
2,311 copies of 20 open sessions still said Fable under an opus-5-5 newest
copy. These tests hold the mirror to the rule in `decide_setting` (see
`docs/reports/2026-09-26-mirror-settings.md`), on real files under `tmp_path`.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

ONE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
TWO = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
FABLE, OPUS, NEXT = "claude-fable-5-1", "claude-opus-5-5", "claude-opus-6"
T0 = 1_790_000_000_000                          # ms
HOUR = 3_600_000
REPO = "/Users/fixture/repo"
TREE = f"{REPO}/.claude/worktrees/brave-kilby-0c68b4"
WORKTREE = {"cwd": TREE, "originCwd": REPO, "worktreePath": TREE,
            "worktreeName": "brave-kilby-0c68b4", "branch": "claude/brave-kilby-0c68b4",
            "sourceBranch": "main", "gitAnchors": [{"gitRoot": REPO}]}
ROOT = {"cwd": REPO, "originCwd": REPO}


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    for session in (ONE, TWO):
        fx.transcript(home, session, fx.completed())
        fx.transcript(home, session, fx.completed(), cwd=TREE)
    root = tmp_path / "state"
    root.mkdir()
    ticks = itertools.count()
    running = mirror.Mirror(root, fx.policy(),
                            now=lambda: fx.NOW + timedelta(seconds=next(ticks)))
    return running, store


def path(store: Path, index: int, session: str = ONE) -> Path:
    account, org = FOLDERS[index]
    return store / account / org / f"local_{session}.json"


def seed(store: Path, index: int, *, model: str = FABLE, activity: int = T0,
         written: int | None = None, session: str = ONE, place: dict | None = None,
         **extra) -> Path:
    """A copy as the app writes it: `written` (ms) is its mtime, by default a
    second after its last activity."""
    account, org = FOLDERS[index]
    body = dict(place if place is not None else ROOT)
    target = fx.index_entry(store, account, org, session, cwd=body.pop("cwd"), model=model,
                            last_activity=activity, settings={"ultracode": True},
                            **body, **extra)
    stamp = ((written if written is not None else activity) + 1000) / 1000
    os.utime(target, (stamp, stamp))
    return target


def app_writes(target: Path, *, at: int, **fields) -> None:
    """The app's save: the whole record, beside the file, renamed over it."""
    data = {**json.loads(target.read_text()), **fields}
    for key, value in list(fields.items()):
        if value is None:
            data.pop(key)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.utime(temporary, (at / 1000, at / 1000))
    temporary.replace(target)


def read(store: Path, index: int, session: str = ONE) -> dict:
    return json.loads(path(store, index, session).read_text())


def models(store: Path, session: str = ONE) -> list[str]:
    return [read(store, i, session).get("model") for i in range(len(FOLDERS))]


def settled(running, session: str = ONE) -> dict:
    return (mirror._load(running.flags_path).get(session) or {}).get("settings") or {}


def everywhere(store, value: str, session: str = ONE) -> None:
    for index in range(len(FOLDERS)):
        seed(store, index, model=value, session=session)


# --- the first decision ---------------------------------------------------------

def test_the_most_active_copy_decides_a_session_nobody_has_synced(world):
    """The brief's rule: the copy with the greatest lastActivityAt is canonical,
    for the model and the place alike, fields absent there included."""
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + 5 * HOUR, place=WORKTREE)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0 + HOUR)
    result = running.run_once()
    assert result.state == "ok" and result.settings_synced == 1
    for index in range(3):
        copy = read(store, index)
        assert copy["model"] == OPUS
        assert {key: copy.get(key) for key in WORKTREE} == WORKTREE


def test_a_place_without_a_worktree_removes_the_worktree_fields(world):
    """The app detaches a worktree by unsetting `worktreePath` and
    `worktreeName`; an absent field is a value, and it spreads as one."""
    running, store = world
    seed(store, 0, activity=T0 + HOUR, place=ROOT)
    seed(store, 1, activity=T0, place=WORKTREE)
    seed(store, 2, activity=T0, place=WORKTREE)
    running.run_once()
    for index in range(3):
        copy = read(store, index)
        assert copy["cwd"] == REPO
        assert not {"worktreePath", "worktreeName", "branch", "sourceBranch",
                    "gitAnchors"} & set(copy)


def test_ties_go_to_the_later_write_then_the_first_folder(world):
    """23 sessions on 2026-09-26 had two max-rank copies disagreeing on the
    worktree, and in 17 of them the later write had detached it."""
    running, store = world
    seed(store, 0, activity=T0, place=WORKTREE)
    seed(store, 1, activity=T0, written=T0 + 5000, place=ROOT)
    seed(store, 2, activity=T0, place=WORKTREE)
    running.run_once()
    assert {read(store, i).get("worktreePath") for i in range(3)} == {None}


def test_a_model_picked_after_the_last_activity_wins_the_first_decision(world):
    """The rollout's shape (2026-09-26): 120 copies last ran on Fable; one
    account picked opus-5-5 nine minutes later, with no turn, so its copy
    has an older lastActivityAt. The pick is the newest intent."""
    running, store = world
    seed(store, 0, model=FABLE, activity=T0 + HOUR)
    seed(store, 1, model=FABLE, activity=T0 + HOUR)
    seed(store, 2, model=OPUS, activity=T0, written=T0 + HOUR + 9 * 60_000)
    running.run_once()
    assert models(store) == [OPUS] * 3


def test_a_place_saved_after_the_last_activity_does_not_win_the_first_decision(world):
    """Any save rewrites the whole record, so a model fix re-saves a stale
    place along with it. Places move with activity: the most active wins."""
    running, store = world
    seed(store, 0, activity=T0 + HOUR, place=WORKTREE)
    seed(store, 1, activity=T0 + HOUR, place=WORKTREE)
    seed(store, 2, activity=T0, written=T0 + 2 * HOUR, place=ROOT)
    running.run_once()
    assert {read(store, i)["cwd"] for i in range(3)} == {TREE}


def test_a_late_re_save_of_a_model_that_ran_never_beats_newer_activity(world):
    """Review round 1 (PR #49): a late write is no pick by itself. A focus or a
    PR poll re-saves the whole record long after the last turn; the model it
    writes ran before (the transcript says so), so the most active copy wins."""
    running, store = world                  # the transcripts ran claude-fable-5-1
    seed(store, 0, model=FABLE, activity=T0, written=T0 + 3 * HOUR)
    seed(store, 1, model=OPUS, activity=T0 + HOUR)
    seed(store, 2, model=OPUS, activity=T0 + HOUR)
    running.run_once()
    assert models(store) == [OPUS] * 3


def test_without_a_transcript_a_late_model_is_no_pick(world):
    """No evidence it never ran is no evidence of a pick: a session whose
    transcript is gone takes the most active copy's model."""
    running, store = world
    gone = "0d0d0d0d-0000-4000-8000-00000000dead"
    seed(store, 0, model=FABLE, activity=T0 + HOUR, session=gone)
    seed(store, 1, model=FABLE, activity=T0 + HOUR, session=gone)
    seed(store, 2, model=OPUS, activity=T0, written=T0 + 3 * HOUR, session=gone)
    running.run_once()
    assert models(store, gone) == [FABLE] * 3


@pytest.mark.parametrize("late_s, wins", [(59, FABLE), (61, OPUS)])
def test_a_later_pick_must_come_a_settled_minute_after_the_last_activity(world, late_s, wins):
    """The app saves within 1-3 s of the frame that raised lastActivityAt; a
    write within `SETTLE_MS` (60 s) of it is that activity's own save."""
    running, store = world
    seed(store, 0, model=FABLE, activity=T0 + HOUR)
    seed(store, 1, model=FABLE, activity=T0 + HOUR)
    account, org = FOLDERS[2]
    target = fx.index_entry(store, account, org, ONE, model=OPUS, last_activity=T0,
                            settings={"ultracode": True}, **{"cwd": REPO, "originCwd": REPO})
    stamp = (T0 + HOUR) / 1000 + late_s
    os.utime(target, (stamp, stamp))
    running.run_once()
    assert models(store) == [wins] * 3


def test_a_flag_write_keeps_a_model_the_app_changed_during_the_pass(world, monkeypatch):
    """Review round 1: a copy getting only a flag write is checked and patched
    on the flags alone, as before settings sync. A pick landing in it between
    the read and the publish stays, and the archive still spreads."""
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    app_writes(path(store, 0), at=T0 + HOUR, isArchived=True)
    sync = mirror.Mirror.sync_flags

    def a_pick_lands(engine, folder_files, *args, **kwargs):
        app_writes(path(store, 1), at=T0 + 2 * HOUR, model=OPUS)
        return sync(engine, folder_files, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "sync_flags", a_pick_lands)
        result = running.run_once()
    assert result.flags_held == 0 and result.flag_synced == 1
    assert [read(store, i)["isArchived"] for i in range(3)] == [True] * 3
    assert models(store) == [FABLE, OPUS, FABLE]
    running.run_once()
    assert models(store) == [OPUS] * 3, "the pick nobody saw spreads next pass"


def test_a_model_some_copy_held_before_the_last_activity_is_not_a_later_pick(world):
    running, store = world
    seed(store, 0, model=OPUS, activity=T0)                         # opus, early
    seed(store, 1, model=FABLE, activity=T0 + HOUR)                 # then a fable turn
    seed(store, 2, model=OPUS, activity=T0, written=T0 + 2 * HOUR)  # a later save of opus
    running.run_once()
    assert models(store) == [FABLE] * 3


# --- after the first decision ------------------------------------------------------

def test_a_pick_in_a_less_active_account_is_never_undone(world):
    """The bug this rule exists for: `commitSessionModel` never raises
    lastActivityAt, so "the newest lastActivityAt wins" alone would write the
    most active copy's old model back over the pick."""
    running, store = world
    seed(store, 0, model=FABLE, activity=T0 + 5 * HOUR)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    running.run_once()
    app_writes(path(store, 1), at=T0 + 6 * HOUR, model=OPUS)   # a pick in B, no turn
    result = running.run_once()
    assert result.settings_synced == 1
    assert models(store) == [OPUS] * 3
    assert read(store, 0)["lastActivityAt"] == T0 + 5 * HOUR, "the rank is the app's, untouched"


def test_a_stale_save_never_spreads_without_activity(world):
    """The flag protocol's known limit, closed for settings. The mirror wrote
    opus into A; the app, which loaded A before that, re-saves its Fable
    (focus, a PR poll). No pass may spread it."""
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    app_writes(path(store, 1), at=T0 + HOUR, model=OPUS)        # B picks opus
    running.run_once()
    assert models(store) == [OPUS] * 3
    app_writes(path(store, 0), at=T0 + 2 * HOUR, model=FABLE)   # A's stale memory
    result = running.run_once()
    assert models(store) == [OPUS] * 3 and result.settings_synced == 1


def test_a_turn_on_stale_memory_spreads_what_it_ran(world):
    """Intended, the known limit: a turn or respawn in a folder the app loaded
    before the mirror's write runs on the value it remembers. It raises
    lastActivityAt, and nothing on disk tells it from a deliberate switch
    back followed by a turn, so what ran spreads. A relaunch clears it."""
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    app_writes(path(store, 1), at=T0 + HOUR, model=OPUS)
    running.run_once()
    app_writes(path(store, 0), at=T0 + 2 * HOUR, model=FABLE, lastActivityAt=T0 + 2 * HOUR)
    running.run_once()
    assert models(store) == [FABLE] * 3


def test_a_pick_of_a_model_the_session_had_before_waits_for_activity(world):
    """Intended: without a turn, a pick of a value some pass has seen looks
    exactly like a stale re-save, so the base stands on disk until the
    session's next activity in that account, which then spreads it."""
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    app_writes(path(store, 1), at=T0 + HOUR, model=OPUS)
    running.run_once()
    app_writes(path(store, 2), at=T0 + 2 * HOUR, model=FABLE)       # back to Fable
    running.run_once()
    assert models(store) == [OPUS] * 3
    app_writes(path(store, 2), at=T0 + 3 * HOUR, model=FABLE, lastActivityAt=T0 + 3 * HOUR)
    running.run_once()
    assert models(store) == [FABLE] * 3


def test_a_move_spreads_with_every_place_field(world):
    """`change_directory` sets cwd and originCwd together after the turn; the
    app sets the worktree fields together. A copy never mixes two places."""
    running, store = world
    for index in range(3):
        seed(store, index, place=WORKTREE)
    running.run_once()
    elsewhere = {"cwd": "/Users/fixture/elsewhere", "originCwd": "/Users/fixture/elsewhere",
                 "worktreePath": None, "worktreeName": None, "branch": None,
                 "sourceBranch": None, "gitAnchors": None}
    app_writes(path(store, 2), at=T0 + HOUR, lastActivityAt=T0 + HOUR, **elsewhere)
    running.run_once()
    for index in range(3):
        copy = read(store, index)
        assert (copy["cwd"], copy["originCwd"]) == ("/Users/fixture/elsewhere",) * 2
        assert not {"worktreePath", "worktreeName", "branch", "sourceBranch",
                    "gitAnchors"} & set(copy)


def test_a_write_keeps_the_mtime_and_every_field_it_does_not_decide(world):
    """Sidebar order is the file's mtime; the mirror writes only the settings
    and flags it decided, and the rest of the record stays the app's."""
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR, effort="xhigh",
         remoteMcpServersConfig=[{"name": "Gmail"}], permissionMode="auto")
    seed(store, 1, model=FABLE, activity=T0, effort="max", remoteMcpServersConfig=[],
         permissionMode="acceptEdits", prs=[{"prNumber": 41}])
    seed(store, 2, model=FABLE, activity=T0)
    before = os.stat(path(store, 1)).st_mtime_ns
    running.run_once()
    copy = read(store, 1)
    assert (copy["model"], copy["effort"]) == (OPUS, "xhigh")
    assert copy["remoteMcpServersConfig"] == [] and copy["prs"] == [{"prNumber": 41}]
    assert copy["permissionMode"] == "acceptEdits", "permission modes stay per account"
    assert copy["lastActivityAt"] == T0
    assert os.stat(path(store, 1)).st_mtime_ns == before


def test_a_converged_store_is_left_alone(world):
    """Idempotence: a second pass decides the same and writes nothing."""
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR, place=WORKTREE)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    running.run_once()
    snapshot = {i: (path(store, i).read_bytes(), os.stat(path(store, i)).st_mtime_ns)
                for i in range(3)}
    result = running.run_once()
    assert result.settings_synced == 0 and not result.changed
    assert snapshot == {i: (path(store, i).read_bytes(), os.stat(path(store, i)).st_mtime_ns)
                        for i in range(3)}


# --- all or nothing --------------------------------------------------------------

def test_a_copy_that_ran_a_turn_during_the_pass_is_never_overwritten(world, monkeypatch):
    """Newer is never overwritten by older: a copy the pass would write that
    ran a turn between the read and the publish may now be the newest, so the
    session is held and decided again next pass."""
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    sync = mirror.Mirror.sync_flags

    def a_turn_lands(engine, folder_files, *args, **kwargs):
        app_writes(path(store, 1), at=T0 + 2 * HOUR, lastActivityAt=T0 + 2 * HOUR)
        return sync(engine, folder_files, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "sync_flags", a_turn_lands)
        result = running.run_once()
    assert result.flags_held == 1
    assert models(store) == [OPUS, FABLE, FABLE], "nothing written"
    assert "v" not in (settled(running).get("units") or {}).get("model", {}), \
        "no value decided; the displaced ones are marked seen"
    running.run_once()
    assert models(store) == [FABLE] * 3, "B's turn is the newest activity"


def test_a_save_between_two_writes_puts_every_setting_back(world, monkeypatch):
    """The flag protocol's rollback covers settings: a write that finds its copy
    saved since the check puts back the copies already written, and the base
    keeps its value (only `seen` grows)."""
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    app_writes(path(store, 0), at=T0 + HOUR, model=OPUS)
    install = mirror._install
    raced = {"once": True}

    def racing(temporary, destination, **kwargs):
        if raced["once"] and destination == path(store, 2) and kwargs.get("expect"):
            raced["once"] = False
            app_writes(path(store, 2), at=T0 + 2 * HOUR)       # a focus save
        return install(temporary, destination, **kwargs)

    monkeypatch.setattr(mirror, "_install", racing)
    result = running.run_once()
    assert result.flags_held == 1
    assert models(store) == [OPUS, FABLE, FABLE], "B was written, then put back"
    unit = settled(running)["units"]["model"]
    assert unit["value"] == {"model": FABLE}
    running.run_once()
    assert models(store) == [OPUS] * 3


def test_a_crash_after_the_write_ahead_keeps_what_it_displaced_seen(world, monkeypatch):
    """Crash safety: the values a publish displaces are marked seen before any
    copy is written. A crash after B's write and before the base leaves B's old
    model on no disk and in no base but the write-ahead; without it, the app's
    stale save of that model would read as a pick nobody saw, and spread."""
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR)
    seed(store, 1, model=NEXT, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    real = mirror._write_json
    copies = itertools.count()

    def crash_on_the_second_copy(target, value, **kwargs):
        if Path(target).name.startswith("local_") and next(copies) == 1:
            raise KeyboardInterrupt("power loss")
        return real(target, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_write_json", crash_on_the_second_copy)
        with pytest.raises(KeyboardInterrupt):
            running.run_once()
    assert models(store) == [OPUS, OPUS, FABLE], "B written, then the crash"
    unit = settled(running)["units"]["model"]
    assert mirror._unit_digest({"model": NEXT}) in unit["seen"] and "v" not in unit
    restarted = mirror.Mirror(running.root, fx.policy(), now=lambda: fx.NOW)
    restarted.run_once()
    assert models(store) == [OPUS] * 3
    app_writes(path(store, 1), at=T0 + 2 * HOUR, model=NEXT)    # B's stale memory
    restarted.run_once()
    assert models(store) == [OPUS] * 3


def test_an_unreadable_copy_holds_the_settings_too(world, monkeypatch):
    """Decided from every copy or not at all (review round 5): a copy that
    cannot be read holds its session, settings included."""
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    app_writes(path(store, 0), at=T0 + HOUR, model=OPUS)
    app_writes(path(store, 2), at=T0 + HOUR)
    read_entry = mirror._read_entry

    def unreadable(where):
        if str(where) == str(path(store, 2)):
            raise OSError(24, "Too many open files")
        return read_entry(where)

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_read_entry", unreadable)
        result = running.run_once()
    assert result.flags_held == 1 and result.settings_synced == 0
    assert models(store) == [OPUS, FABLE, FABLE]
    running.run_once()
    assert models(store) == [OPUS] * 3


def test_a_cancelled_pass_writes_no_setting(world, monkeypatch):
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    checkpoint = mirror.Mirror._checkpoint

    class Set:
        def is_set(self):
            return True

    def cancel_at_publish(engine, current, stage=None):
        if stage == "publishing flags":
            engine.cancel = Set()
        return checkpoint(engine, current, stage)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_checkpoint", cancel_at_publish)
        assert running.run_once().state == "cancelled"
    running.cancel = None
    assert models(store) == [OPUS, FABLE, FABLE]
    assert not mirror._load(running.flags_path)


# --- switches, dry runs, reports ---------------------------------------------------

def test_settings_sync_can_be_switched_off_and_keeps_its_base(world):
    running, store = world
    everywhere(store, FABLE)
    running.run_once()
    before = settled(running)
    app_writes(path(store, 0), at=T0 + HOUR, model=OPUS)
    result = running.run_once(mirror.Options(settings_sync=False))
    assert result.settings_synced == 0 and models(store) == [OPUS, FABLE, FABLE]
    assert settled(running) == before
    policy = fx.policy(mirror_settings_sync=False)
    assert mirror.options_from(policy).settings_sync is False
    assert mirror.options_from(fx.policy()).settings_sync is True


def test_a_dry_run_counts_and_writes_nothing(world):
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    result = running.run_once(mirror.Options(dry_run=True))
    assert result.settings_synced == 1
    assert models(store) == [OPUS, FABLE, FABLE]
    assert not running.flags_path.exists()


def test_a_settings_write_is_journaled_for_the_load_gap_report(world):
    running, store = world
    seed(store, 0, model=OPUS, activity=T0 + HOUR)
    seed(store, 1, model=FABLE, activity=T0)
    seed(store, 2, model=FABLE, activity=T0)
    running.run_once()
    rows = [row for row in running.journal.rows() if row.kind == "updated"]
    assert sorted(row.folder for row in rows) == ["acct-b/org-b", "acct-c/org-c"]


def test_diverged_conversation_ids_are_reported_and_never_rewritten(world):
    """A /clear changes `cliSessionId` only in the account where it happens.
    `priorCliSessionIds` does not order the ids (an undone clear records the
    newer one), so the mirror reports the split and leaves both."""
    running, store = world
    everywhere(store, FABLE)
    cleared = path(store, 0)
    app_writes(cleared, at=T0 + HOUR, cliSessionId=TWO, priorCliSessionIds=[ONE],
               lastActivityAt=T0 + HOUR)
    result = running.run_once()
    assert result.ids_diverged == 1
    (item,) = result.diverged
    assert item["session"] == f"local_{ONE}" and item["newest"] == TWO
    assert item["ids"] == {ONE: 2, TWO: 1}
    assert json.loads(cleared.read_text())["cliSessionId"] == TWO
    assert read(store, 1)["cliSessionId"] == ONE
    health = running.health()
    assert health["ids_diverged"] == 1 and health["diverged"][0]["newest"] == TWO


# --- properties over random stores -------------------------------------------------

MODELS = st.sampled_from((FABLE, OPUS, NEXT))
PLACES = st.sampled_from((ROOT, WORKTREE, {"cwd": "/Users/fixture/other",
                                            "originCwd": "/Users/fixture/other"}))
COPY = st.fixed_dictionaries({
    "model": MODELS, "place": PLACES,
    "effort": st.sampled_from((None, "max", "xhigh")),
    "activity": st.integers(min_value=0, max_value=4).map(lambda h: T0 + h * HOUR),
    "late": st.integers(min_value=0, max_value=3).map(lambda h: h * HOUR),
    "archived": st.booleans()})


def build(tmp_path, monkeypatch, copies, *, uniform: bool = False):
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    fx.transcript(home, ONE, fx.completed())
    for index, copy in enumerate(copies):
        extra = {} if copy["effort"] is None else {"effort": copy["effort"]}
        seed(store, index, model=FABLE if uniform else copy["model"],
             activity=copy["activity"], written=copy["activity"] + copy["late"],
             place=ROOT if uniform else copy["place"], archived=copy["archived"],
             **({} if uniform else extra))
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    return store, mirror.Mirror(root, fx.policy(), now=lambda: fx.NOW)


PROPERTY = settings(max_examples=60, deadline=None, database=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture,
                                           HealthCheck.too_slow])


@PROPERTY
@given(copies=st.lists(COPY, min_size=3, max_size=3))
def test_after_a_pass_every_copy_agrees_with_a_value_one_copy_held(
        tmp_path_factory, monkeypatch, copies):
    """Convergence, from any store: after one pass every copy holds the same
    value of every unit, and it is a value some copy held before."""
    with monkeypatch.context() as patch:
        store, running = build(tmp_path_factory.mktemp("s"), patch, copies)
        before = [read(store, i) for i in range(3)]
        assert running.run_once().state == "ok"
        after = [read(store, i) for i in range(3)]
    for fields in mirror.SETTING_UNITS.values():
        values = {json.dumps(mirror._unit_value(copy, fields), sort_keys=True) for copy in after}
        assert len(values) == 1
        assert values <= {json.dumps(mirror._unit_value(copy, fields), sort_keys=True)
                          for copy in before}
    for old, new in zip(before, after):
        assert new["lastActivityAt"] == old["lastActivityAt"]


@PROPERTY
@given(copies=st.lists(COPY, min_size=3, max_size=3))
def test_a_second_pass_writes_nothing(tmp_path_factory, monkeypatch, copies):
    """Idempotence, from any store."""
    with monkeypatch.context() as patch:
        store, running = build(tmp_path_factory.mktemp("s"), patch, copies)
        running.run_once()
        snapshot = [(path(store, i).read_bytes(), os.stat(path(store, i)).st_mtime_ns)
                    for i in range(3)]
        result = running.run_once()
        assert result.settings_synced == 0 and result.flag_synced == 0
        assert snapshot == [(path(store, i).read_bytes(), os.stat(path(store, i)).st_mtime_ns)
                            for i in range(3)]


@PROPERTY
@given(copies=st.lists(COPY, min_size=3, max_size=3))
def test_the_first_decision_never_writes_an_older_copys_values_over_a_newer_one(
        tmp_path_factory, monkeypatch, copies):
    """Newer is never overwritten by older: the place every copy ends with is the
    most active copy's (rank, then later write, then folder order), and the
    model is that copy's too unless a model that never ran (the transcript ran
    only Fable 5.1) is held only by copies written after the last activity,
    which was picked after it."""
    with monkeypatch.context() as patch:
        store, running = build(tmp_path_factory.mktemp("s"), patch, copies)
        before = [read(store, i) for i in range(3)]
        mtimes = [os.stat(path(store, i)).st_mtime_ns // 1_000_000 for i in range(3)]
        running.run_once()
        after = read(store, 0)
    key = [(copy["lastActivityAt"], mtime, -index)
           for index, (copy, mtime) in enumerate(zip(before, mtimes))]
    newest = before[max(range(3), key=lambda i: key[i])]
    place = mirror.SETTING_UNITS["place"]
    assert mirror._unit_value(after, place) == mirror._unit_value(newest, place)
    last = max(copy["lastActivityAt"] for copy in before)
    later = [i for i in range(3)
             if before[i]["model"] != FABLE
             and all(mtimes[j] > last + mirror.SETTLE_MS
                     for j in range(3) if before[j]["model"] == before[i]["model"])]
    if later:
        pick = max(later, key=lambda i: (mtimes[i], -i))
        assert after["model"] == before[pick]["model"]
    else:
        assert after["model"] == newest["model"]


@PROPERTY
@given(copies=st.lists(COPY, min_size=3, max_size=3))
def test_settings_never_change_what_flag_sync_decides(tmp_path_factory, monkeypatch, copies):
    """Flags are untouched: the same store with every setting made equal ends
    with the same flags on every copy and the same flag base."""
    results = []
    for uniform in (False, True):
        with monkeypatch.context() as patch:
            store, running = build(tmp_path_factory.mktemp("s"), patch, copies, uniform=uniform)
            running.run_once()
            flags = [{key: read(store, i).get(key) for key in mirror.FLAG_WRITES}
                     for i in range(3)]
            base = {key: value for key, value in
                    (mirror._load(running.flags_path).get(ONE) or {}).items()
                    if key not in ("settings", "tmt")}      # tmt: this world's transcript mtime
            results.append((flags, base))
    assert results[0] == results[1]
