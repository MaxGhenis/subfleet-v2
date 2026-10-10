"""A dry run leaves the mirror's instance as it found it: C-17.4, C-23.28.

A dry run decides as a real pass does and publishes nothing. It used to keep
what deciding left on the instance: the store read into the inventory, so
that each change it read was no longer new to the next hot pass, and the
retries it had "served" cleared. On an instance that passes again, the real
hot pass after a dry run then found no candidates (second review of #167: ten
sessions held at publish, a dry hot pass, and a real hot pass with
`sessions=0`), a change the dry run was first to read waited for the next
full pass, and a new instance's first hot pass was no longer the full one.

The daemon's timers never ask for a dry run (`timers.mirror_cycle`,
`mirror_hot_cycle`: `options_from(policy)`, which reads no such setting), and
`sessions mirror --dry-run` makes a Mirror of its own for its one pass. So
this is the API's promise, kept for whoever calls it next.

A dry run deletes nothing either. A full pass that sweeps removes the
temporaries a killed pass left, once they are stale, and a dry run's sweep
removed them too, from all three places: beside the store's records
(`_scan`), among the mirror's own state files (`_pass`) and in the project
directories (`transcript_stems`). That one `sessions mirror --dry-run` did
reach.

Every test names the clause it proves (C-20.5). The desktop store lives under
`tmp_path`; nothing here reads or writes the operator's own.
"""

from __future__ import annotations

import dataclasses
import io
import json
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

ONE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
TWO = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
THREE = "0b5e7c11-2d3f-4a55-8e6d-7f8091a2b3c4"
STUCK = "5ca1ab1e-0000-4000-8000-00000000dead"
#: A record whose transcript does not exist (yet): the hot pass retries it.
ORPHAN = "0ddba11c-0000-4000-8000-00000000cafe"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: 2026-10-10T12:26:40Z in the app's milliseconds; the passes' clock is three days on.
NEW = 1_791_635_200_000
DAY = 86_400_000
CLOCK = datetime.fromtimestamp((NEW + 3 * DAY) / 1000, timezone.utc)
#: Where a killed pass leaves a temporary, and what a sweep finds each from.
PLACES = {"store": "_scan", "state": "_pass", "project": "transcript_stems"}


# --- the fixture store ---------------------------------------------------------------

class World:
    """A state root, a `~/.claude` and a three-login desktop store under `base`."""

    def __init__(self, base: Path, patch, **policy):
        self.base, self.patch = base, patch
        self.home, self.store, self.root = base / "claude", base / "claude-code-sessions", base / "state"
        self.use()
        (self.home / "projects").mkdir(parents=True)
        for account, org in FOLDERS:
            (self.store / account / org).mkdir(parents=True)
        self.root.mkdir()
        self.running = mirror.Mirror(self.root, fx.policy(**{"mirror_hot_interval_s": 0, **policy}),
                                     now=lambda: CLOCK)

    def use(self) -> "World":
        """Point the kit at this world: the tests of two worlds take turns."""
        self.patch.setenv("SUBFLEET_CLAUDE_DIR", str(self.home))
        self.patch.setenv("SUBFLEET_SESSION_STORE", str(self.store))
        self.patch.setenv("SUBFLEET_DESKTOP_LOG", str(self.base / "logs" / "main.log"))
        self.patch.setenv("SUBFLEET_HOME", str(self.root))      # the CLI's state root
        return self

    def options(self, **overrides) -> mirror.Options:
        return mirror.options_from(self.running.policy, **overrides)

    def full(self, **overrides) -> mirror.Pass:
        return self.use().running.run_once(self.options(**overrides))

    def hot(self, **overrides) -> mirror.Pass:
        return self.use().running.run_hot(self.options(**overrides))

    def path(self, index: int, session: str = ONE) -> Path:
        account, org = FOLDERS[index]
        return self.store / account / org / f"local_{session}.json"

    def put(self, index: int, session: str = ONE, *, date: int = NEW,
            transcript: bool = True, **fields) -> Path:
        if transcript:
            fx.transcript(self.home, session, fx.completed())
        account, org = FOLDERS[index]
        return fx.index_entry(self.store, account, org, session, last_activity=date,
                              settings={"ultracode": True}, **fields)

    def record(self, index: int, session: str = ONE) -> dict:
        return json.loads(self.path(index, session).read_text(encoding="utf-8"))

    def save(self, index: int, session: str = ONE, **fields) -> None:
        """The app's own write: beside the file, then rename (never in place)."""
        target = self.path(index, session)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps({**json.loads(target.read_text()), **fields}))
        temporary.replace(target)

    def files(self) -> dict[str, tuple[int, bytes]]:
        """Every file of the store, the state root and `~/.claude`, the projects
        directory in it: its inode and its bytes. Less the pass's lock, which
        a dry run takes as any pass does."""
        return {str(file.relative_to(self.base)): (file.stat().st_ino, file.read_bytes())
                for top in (self.store, self.root, self.home) for file in sorted(top.rglob("*"))
                if file.is_file() and file.name != mirror.LOCK_NAME}

    def leave(self, place: str, index: int = 0) -> Path:
        """A temporary a killed pass left in one of `PLACES`, as `_temporary` names them."""
        account, org = FOLDERS[index]
        path = {"store": self.store / account / org / f"local_{ONE}.json.k1ll3d{index:02d}",
                "state": self.root / "sessions" / f"{mirror.SIDECAR_NAME}.k1ll3d{index:02d}",
                "project": (self.home / "projects" / fx.project_slug()
                            / f"{ONE}.jsonl.k1ll3d{index:02d}")}[place]
        path = path.with_name(path.name + (".tmp-revive" if place == "project"
                                           else mirror.TEMPORARY_SUFFIX))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        return path

    def left(self) -> set[str]:
        """The temporaries that stand in the three places, by their place."""
        return {str(file.relative_to(self.base))
                for top in (self.store, self.root, self.home) for file in top.rglob("*.tmp-*")}

    def copies(self) -> dict[str, dict]:
        """Every record of the store, parsed, by its place."""
        return {str(file.relative_to(self.store)): json.loads(file.read_text(encoding="utf-8"))
                for file in sorted(self.store.glob("*/*/local_*.json"))}

    def base_flags(self) -> dict:
        """The merge base, less the transcripts' own mtimes."""
        try:
            rows = json.loads(self.running.flags_path.read_text(encoding="utf-8"))
        except OSError:
            return {}
        return {identity: {key: value for key, value in row.items() if key != "tmt"}
                for identity, row in rows.items()}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def refuse_publishing(patch, stuck: set[str]) -> None:
    """Flag sync's writes of the sessions in `stuck` fail, as when the app
    saves the copy in the instant after the pass's check."""
    install = mirror._install

    def refusing(temporary, destination, **kwargs):
        if kwargs.get("expect") is not None and any(name in destination.name for name in stuck):
            temporary.unlink()
            return False
        return install(temporary, destination, **kwargs)

    patch.setattr(mirror, "_install", refusing)


def picture(value):
    """Everything the instance holds, all the way down, as plain values."""
    if isinstance(value, dict):
        return ("dict", sorted((repr(key), picture(item)) for key, item in value.items()))
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted(repr(item) for item in value))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [picture(item) for item in value])
    if dataclasses.is_dataclass(value) or type(value).__name__ in ("_Journal", "DesktopLog"):
        return (type(value).__name__, picture(vars(value)))
    if value is None or isinstance(value, (bool, int, float, str, bytes, Path)):
        return repr(value)
    return ("object", id(value))            # a clock, a cancel event: the same one or not


def held(running: mirror.Mirror) -> dict[str, int]:
    """Which objects the instance's attributes are."""
    return {name: id(value) for name, value in vars(running).items()}


def brief(result: mirror.Pass, base: Path) -> dict:
    """A pass's record, less its clock readings and this world's own path."""
    row = {key: value for key, value in result.to_dict().items()
           if key not in ("started_at", "finished_at")}
    return json.loads(json.dumps(row).replace(str(base), "<base>"))


# --- the retries --------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["hot", "full"])
def test_a_dry_run_keeps_the_retry_of_a_session_held_at_publish(world, monkeypatch, kind):
    """C-17.4, C-23.28: a session whose flag write failed is retried by the
    hot pass. A dry run decided it, cleared the retry and published nothing,
    and the real hot pass after it saw `sessions=0`: the archive stayed in one
    folder until the next full pass."""
    world.put(0, archived=True)
    world.put(1)
    world.put(2)
    stuck = {ONE}
    refuse_publishing(monkeypatch, stuck)
    assert world.full().flags_held == 1
    assert world.running._flag_retry == {ONE}
    preview = world.hot(dry_run=True) if kind == "hot" else world.full(dry_run=True)
    assert preview.dry_run and preview.kind == kind and preview.flag_synced == 1
    assert world.running._flag_retry == {ONE}
    stuck.clear()                                   # the app has stopped saving
    real = world.hot()
    assert real.sessions == 1 and real.flag_synced == 1 and real.flags_held == 0
    assert [world.record(index)["isArchived"] for index in range(3)] == [True, True, True]
    assert world.running._flag_retry == set()


def test_a_dry_run_keeps_the_retries_and_the_ledger_of_dates_that_did_not_publish(
        world, monkeypatch):
    """C-17.4, C-23.28: the review's own case. Ten sessions whose date raise
    cannot be published are held, retried, and counted in the ledger that
    makes them take turns. A dry hot pass left `_flag_retry` empty; the real
    hot pass then reported `sessions=0` and `activity_synced=0`."""
    sessions = [f"{index:08d}-0000-4000-8000-00000000dead" for index in range(10)]
    for session in sessions:
        world.put(0, session)
        world.put(1, session, date=NEW - 30 * DAY)
    world.path(2).parent.rmdir()
    refuse_publishing(monkeypatch, set(sessions))
    first = world.full()
    assert (first.activity_synced, first.flags_held) == (10, 10)
    retries, ledger = set(world.running._flag_retry), dict(world.running._activity_tries)
    assert retries == set(sessions) and ledger == dict.fromkeys(sessions, 1)
    preview = world.hot(dry_run=True)
    assert preview.sessions == 10 and preview.activity_synced == 10
    assert world.running._flag_retry == retries and world.running._activity_tries == ledger
    real = world.hot()
    assert (real.sessions, real.activity_synced, real.flags_held) == (10, 10, 10)
    assert world.running._activity_tries == dict.fromkeys(sessions, 2), "it tried them again"


# --- the inventory ------------------------------------------------------------------

def test_a_change_a_dry_hot_pass_read_is_still_new_to_the_next_hot_pass(world):
    """C-17.4, C-23.28: a hot pass acts on what changed since the instance
    last read the store. A dry run's read used the change up: the real hot
    pass saw nothing new, and the archive waited for the next full pass."""
    for index in range(3):
        world.put(index)
    assert world.full().state == "ok"
    world.save(0, isArchived=True)                  # the user archives it under one login
    preview = world.hot(dry_run=True)
    assert preview.sessions == 1 and preview.flag_synced == 1
    assert [world.record(index)["isArchived"] for index in range(3)] == [True, False, False]
    real = world.hot()
    assert real.sessions == 1 and real.flag_synced == 1
    assert [world.record(index)["isArchived"] for index in range(3)] == [True, True, True]


def test_the_first_hot_pass_is_still_the_full_one_after_a_dry_run(world):
    """C-17.4, C-23.28: the first pass in a process is a full one, because
    spreading needs to know what every folder holds. After a dry run it was a
    hot pass over an inventory with nothing new in it, and the session waited."""
    world.put(0)
    preview = world.hot(dry_run=True)
    assert preview.kind == "full" and preview.added == 2
    assert not world.running._inventoried, "a preview's inventory is not the instance's"
    real = world.hot()
    assert real.kind == "full" and real.added == 2
    assert [world.path(index).exists() for index in range(3)] == [True, True, True]


# --- everything else ----------------------------------------------------------------

def busy(world: World, patch) -> None:
    """An instance with something of everything a pass keeps, and work waiting:
    an inventory, a session held at publish with a date in the ledger, a
    record whose transcript has not come, and three changes not yet read."""
    for index in range(3):
        world.put(index)
        world.put(index, TWO)
    world.put(0, STUCK, starred=True)
    world.put(1, STUCK, date=NEW - 30 * DAY)
    world.put(2, STUCK, date=NEW - 30 * DAY)
    world.put(0, ORPHAN, transcript=False)
    refuse_publishing(patch, {STUCK})
    assert world.full().flags_held == 1
    running = world.running
    assert running._inventoried and running._flag_retry == {STUCK}
    assert ORPHAN in running._retry and running._activity_tries == {STUCK: 1}
    world.save(0, isStarred=True)                   # a change the next pass would sync
    world.put(0, THREE)                             # a session it would spread
    world.path(2, TWO).unlink()                     # a copy it would put back


@pytest.mark.parametrize("interval", [0, 1e-9], ids=["no-embedded-hot", "embedded-hot"])
@pytest.mark.parametrize("kind", ["hot", "full"])
def test_a_dry_run_leaves_every_part_of_the_instance_as_it_was(world, monkeypatch, kind,
                                                              interval):
    """C-17.4, C-23.28: the retries, the ledger, the inventory with its
    payloads' reference counts, the journal, and every other attribute hold
    what they held, and are the very objects they were; the store and the
    state root are byte for byte the same. With an embedded hot service the
    full pass's worker runs dry passes of its own, and none of them reaches
    the instance either."""
    world.running.policy["sessions"]["mirror_hot_interval_s"] = interval
    busy(world, monkeypatch)
    running = world.running
    before, objects, files = picture(vars(running)), held(running), world.files()
    preview = world.hot(dry_run=True) if kind == "hot" else world.full(dry_run=True)
    assert preview.state == "ok" and preview.dry_run and preview.kind == kind
    assert preview.added and preview.flag_synced, "it did decide the work that waits"
    assert picture(vars(running)) == before
    assert held(running) == objects
    assert world.files() == files


def test_a_dry_run_works_on_copies_of_every_container_the_instance_holds(world, monkeypatch):
    """C-17.4, C-23.28: what the comparison above rests on. While a dry run
    has the instance, each dict, set and list it holds is a copy with the same
    contents, and so is each payload, whose reference count a pass changes in
    place. Afterwards the instance holds the originals again."""
    busy(world, monkeypatch)
    running = world.running
    before, objects = picture(vars(running)), held(running)
    kept = running._borrow()
    try:
        assert picture(vars(running)) == before, "the same contents"
        containers = [name for name, value in kept.items() if type(value) in (dict, set, list)]
        assert {"_entries", "_folders", "_dirty", "_flag_retry", "_retry", "_activity_tries",
                "_stems", "_payloads", "_unlisted_accounts"} <= set(containers)
        for name in containers:
            assert vars(running)[name] is not kept[name], name
        assert kept["_payloads"], "the instance has read the store"
        for digest, payload in kept["_payloads"].items():
            assert running._payloads[digest] is not payload
    finally:
        running._give_back(kept)
    assert picture(vars(running)) == before and held(running) == objects


@pytest.mark.parametrize("kind", ["hot", "full"])
def test_a_dry_run_that_fails_gives_the_instance_back(world, monkeypatch, kind):
    """C-17.4, C-23.28: an exception out of the pass is no reason to keep
    half of its reading, or anything the pass set on the way."""
    busy(world, monkeypatch)
    running = world.running
    before, objects = picture(vars(running)), held(running)

    def failing(self, *args, **kwargs):
        self._left_behind = True                    # nor anything it added on the way
        raise RuntimeError("after the pass read the store")

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", failing)
        with pytest.raises(RuntimeError):
            world.hot(dry_run=True) if kind == "hot" else world.full(dry_run=True)
    assert picture(vars(running)) == before and held(running) == objects
    assert not hasattr(running, "_left_behind")


@pytest.mark.parametrize("kind", ["hot", "full"])
def test_a_cancelled_dry_run_gives_the_instance_back(world, monkeypatch, kind):
    """C-17.4, C-23.28: nor is the daemon's stop."""
    busy(world, monkeypatch)
    running = world.running
    running.cancel = threading.Event()
    before, objects = picture(vars(running)), held(running)
    spread = mirror.Mirror._spread

    def stopping(self, *args, **kwargs):
        self.cancel.set()
        return spread(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", stopping)
        preview = world.hot(dry_run=True) if kind == "hot" else world.full(dry_run=True)
    assert preview.state == "cancelled"
    running.cancel.clear()
    assert picture(vars(running)) == before and held(running) == objects


def test_a_dry_run_that_finds_the_lock_held_touches_nothing(world, monkeypatch):
    """C-17.4, C-23.28: it borrows only once the lock is its own."""
    busy(world, monkeypatch)
    running = world.running
    before, objects = picture(vars(running)), held(running)
    lock = running._lock()
    try:
        for preview in (world.hot(dry_run=True), world.full(dry_run=True)):
            assert preview.error == "another pass holds the lock"
    finally:
        lock.close()
    assert picture(vars(running)) == before and held(running) == objects


# --- the sweep's leftovers ----------------------------------------------------------

@pytest.fixture
def stale(monkeypatch):
    """Every temporary is a stale one, as one a killed pass left an hour ago is."""
    monkeypatch.setattr(mirror, "TEMPORARY_STALE_S", -1)


def run_cli(argv: list[str]) -> tuple[int, str]:
    """One `subfleet` command against the world in use: its exit and its stdout."""
    from subfleet import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue()


@pytest.mark.parametrize("kind", ["full", "hot"])
@pytest.mark.parametrize("place", sorted(PLACES), ids=[PLACES[place] for place in sorted(PLACES)])
def test_a_dry_runs_sweep_removes_no_stale_temporary(world, stale, place, kind):
    """C-17.4, C-23.28: a deletion is a write. A dry run whose pass sweeps
    reads everything again, as the real pass would, and removed the stale
    temporary of each place on the way: beside the store's records (`_scan`),
    among the mirror's own state files (`_pass`) and in a project directory
    (`transcript_stems`). A new instance's first hot pass is the full pass,
    so a dry hot pass removed them too. The real pass still removes them."""
    world.put(0)
    left = world.leave(place)
    files = world.files()
    preview = world.hot(dry_run=True) if kind == "hot" else world.full(dry_run=True)
    assert preview.dry_run and preview.kind == "full" and preview.swept and preview.added == 2
    assert world.files() == files, "the temporary, and every other file, is as it was"
    assert world.left() == {str(left.relative_to(world.base))}
    real = world.hot() if kind == "hot" else world.full()
    assert real.kind == "full" and real.swept and real.added == 2
    assert world.left() == set(), "a real pass's sweep removes it, as before"


def test_a_real_pass_removes_the_stale_temporaries_exactly_when_it_sweeps(world, stale):
    """C-23.28: what stays as it was. A full pass that does not sweep removes
    none, in any of the three places; the next one that sweeps removes all."""
    world.put(0)
    assert world.full().swept
    waiting = {str(world.leave(place).relative_to(world.base)) for place in PLACES}
    between = world.full()
    assert between.state == "ok" and not between.swept
    assert world.left() == waiting
    world.running._last_sweep = None                # the sweep interval has passed
    assert world.full().swept
    assert world.left() == set()


@pytest.mark.parametrize("dry", [True, False], ids=["dry", "real"])
def test_a_hot_pass_of_an_instance_with_an_inventory_never_sweeps(world, stale, dry):
    """C-17.4, C-23.28: the sweep is the full pass's. A hot pass lists only
    the folders that changed and removes no temporary, dry or real, though a
    sweep is due: so a dry hot pass reaches a removal only as the full pass a
    new instance's first one is."""
    world.put(0)
    assert world.full().swept
    waiting = {str(world.leave(place).relative_to(world.base)) for place in PLACES}
    world.running._last_sweep = None
    files = world.files()
    result = world.hot(dry_run=dry)
    assert result.state == "ok" and result.kind == "hot" and not result.swept
    assert world.left() == waiting
    if dry:
        assert world.files() == files


def test_a_dry_run_with_an_embedded_hot_service_removes_no_stale_temporary(world, stale,
                                                                           monkeypatch):
    """C-17.4, C-23.28: a full pass services hot passes at its checkpoints,
    and a dry one runs them dry. None of them removes a temporary either."""
    world.running.policy["sessions"]["mirror_hot_interval_s"] = 1e-9
    busy(world, monkeypatch)
    for place in PLACES:
        world.leave(place)
    world.running._last_sweep = None
    served = []
    locked = mirror.Mirror._run_hot_locked

    def counting(self, options, **kwargs):
        served.append(options.dry_run)
        return locked(self, options, **kwargs)

    monkeypatch.setattr(mirror.Mirror, "_run_hot_locked", counting)
    files = world.files()
    preview = world.full(dry_run=True)
    assert preview.state == "ok" and preview.swept and preview.added
    assert served and all(served), "the embedded hot passes ran, each of them dry"
    assert world.files() == files


def test_a_dry_run_does_not_use_the_sweep_up(world, stale):
    """C-17.4, C-23.28: only a completed full sweep advances the sweep's
    clock, and a dry run's is not one: the next real full pass still sweeps,
    and removes what the dry run left."""
    world.put(0)
    assert world.full().swept
    running = world.running
    running._last_sweep -= mirror.SWEEP_INTERVAL_S  # the sweep interval has passed
    last = running._last_sweep
    for place in PLACES:
        world.leave(place)
    assert world.full(dry_run=True).swept
    assert running._last_sweep == last and len(world.left()) == 3
    assert world.full().swept
    assert world.left() == set() and running._last_sweep > last


def test_listing_a_folder_removes_a_stale_temporary_only_for_a_pass_that_writes(world, stale):
    """C-17.4, C-23.28: `_scan`'s `sweep` is a way of reading: every entry
    again. Removing is `tidy`, which only `_pass` asks for, and never in a
    dry run."""
    world.put(0)
    left = world.leave("store")
    world.running._scan(left.parent, mirror.Pass("start"), sweep=True)
    assert left.exists(), "a sweep's reading removed it"
    world.running._scan(left.parent, mirror.Pass("start"), sweep=False, tidy=False)
    assert left.exists()
    files, _fresh = world.running._scan(left.parent, mirror.Pass("start"), sweep=True, tidy=True)
    assert not left.exists() and set(files) == {world.path(0).name}


def test_finding_the_transcripts_is_a_read_for_every_caller_but_a_pass_that_writes(world, stale):
    """C-17.4, C-23.28: `transcript_stems` is also the read behind `sessions
    mirror --list`, which removed a project directory's stale temporaries as
    a dry run did. It removes them only when a pass that writes asks."""
    world.put(0)
    left = world.leave("project")
    assert set(world.running.transcript_stems()) == {ONE}
    assert set(world.running.transcript_stems(sweep=False)) == {ONE}
    assert left.exists(), "a read removed it"
    assert set(world.running.transcript_stems(tidy=True)) == {ONE}
    assert not left.exists()


def test_sessions_mirror_dry_run_removes_no_stale_temporary(world, stale):
    """C-17.4, C-23.28: the command that reached it. `sessions mirror
    --dry-run` makes a new Mirror, whose first pass sweeps."""
    world.put(0)
    for place in PLACES:
        world.leave(place)
    files = world.use().files()
    code, out = run_cli(["sessions", "mirror", "--dry-run"])
    assert code == 0 and out.startswith("Would mirror across 3 account folders"), out
    assert "added 2" in out
    assert world.files() == files
    code, out = run_cli(["sessions", "mirror"])
    assert code == 0 and "added 2" in out, out
    assert world.left() == set(), "the real command's sweep removes them, as before"


def test_sessions_mirror_list_removes_no_stale_temporary(world, stale):
    """C-17.4, C-23.28: `sessions mirror --list` counts each folder's openable
    and dead sessions. It is a listing, and it removed a project directory's
    stale temporaries on the way."""
    world.put(0)
    for place in PLACES:
        world.leave(place)
    files = world.use().files()
    code, out = run_cli(["sessions", "mirror", "--list", "--json"])
    assert code == 0, out
    assert [json.loads(line)["openable"] for line in out.splitlines()] == [1, 0, 0]
    assert world.files() == files


def test_a_dry_run_that_would_revive_a_transcript_writes_none(world):
    """C-17.4, C-23.28: the projects directory is where a revival writes. A
    dry run counts the transcript an archive would give back, and neither
    places it nor leaves the temporary of a copy."""
    archive = world.base / "archive"
    archive.mkdir()
    (archive / f"{ORPHAN}.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in fx.completed()), encoding="utf-8")
    world.put(0, ORPHAN, transcript=False)
    revived = world.home / "projects" / fx.project_slug() / f"{ORPHAN}.jsonl"
    files = world.files()
    preview = world.full(dry_run=True, archive=str(archive / "*.jsonl"))
    assert preview.state == "ok" and (preview.revived, preview.added) == (1, 2)
    assert world.files() == files and not revived.parent.exists()
    real = world.full(archive=str(archive / "*.jsonl"))
    assert (real.revived, real.added) == (1, 2) and revived.is_file()


def test_a_dry_run_on_a_new_state_root_makes_the_lock_and_nothing_else(world, stale):
    """C-17.4, C-23.28: the one thing a dry run does make. It takes the
    pass's lock as any pass does, so on a state root that has none it leaves
    `sessions/mirror.lock`, empty, and the directory it is in. The comparisons
    of this file leave that one name out."""
    world.put(0)
    world.leave("store")
    world.leave("project")

    def tree() -> dict[str, bytes | None]:
        return {str(path.relative_to(world.base)): path.read_bytes() if path.is_file() else None
                for path in sorted(world.base.rglob("*"))}

    before = tree()
    assert "state/sessions" not in before
    assert world.full(dry_run=True).swept
    after = tree()
    assert {name: after[name] for name in after.keys() - before.keys()} == {
        "state/sessions": None, f"state/sessions/{mirror.LOCK_NAME}": b""}
    assert {name: after[name] for name in before} == before


# --- for every sequence of passes ---------------------------------------------------

SESSIONS = tuple(f"{index:08d}-0000-4000-8000-00000000beef" for index in range(3))
FOLDER = st.integers(min_value=0, max_value=2)
EDITS = st.one_of(
    # The app creates a record, or saves it again with another date.
    st.tuples(st.just("record"), st.sampled_from(SESSIONS + (ORPHAN,)), FOLDER,
              st.integers(min_value=0, max_value=40)),
    st.tuples(st.just("save"), st.sampled_from(SESSIONS), FOLDER,
              st.sampled_from([{"isArchived": True}, {"isArchived": False}, {"isStarred": True},
                               {"isStarred": False},
                               {"title": "renamed", "titleSource": "manual"},
                               {"title": "again", "titleSource": "manual"}])),
    st.tuples(st.just("delete"), st.sampled_from(SESSIONS), FOLDER),
    st.tuples(st.just("transcript")),               # the orphan's transcript arrives
)
#: The app's saves, the sessions whose publishes fail, the dry runs, then a real
#: pass; and before the dry runs, the temporaries a killed pass left, and
#: whether the sweep interval has passed.
ROUNDS = st.tuples(st.lists(EDITS, max_size=3),
                   st.sets(st.sampled_from(SESSIONS), max_size=2),
                   st.lists(st.sampled_from(["hot", "full"]), max_size=2),
                   st.sampled_from(["hot", "hot", "full"]),
                   st.lists(st.tuples(st.sampled_from(sorted(PLACES)), FOLDER), max_size=2),
                   st.booleans())
#: The rounds that end every sequence: a full pass and a hot one, nothing new.
LAST = [([], set(), [], "full", [], False), ([], set(), [], "hot", [], False)]


def edit(world: World, step: tuple) -> None:
    """One of the app's own changes to a store."""
    world.use()
    match step:
        case ("record", session, index, days):
            if world.path(index, session).exists():
                world.save(index, session, lastActivityAt=NEW - days * DAY)
            else:
                world.put(index, session, date=NEW - days * DAY, transcript=session != ORPHAN)
        case ("save", session, index, fields):
            if world.path(index, session).exists():
                world.save(index, session, **fields)
        case ("delete", session, index):
            world.path(index, session).unlink(missing_ok=True)
        case ("transcript",):
            fx.transcript(world.home, ORPHAN, fx.completed())


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(warm=st.booleans(), rounds=st.lists(ROUNDS, min_size=1, max_size=6))
@example(warm=True, rounds=[([], set(), ["full", "hot"], "full",
                             [(place, 0) for place in sorted(PLACES)], True)])
@example(warm=False, rounds=[([], set(), ["hot", "full"], "hot",
                              [(place, 1) for place in sorted(PLACES)], False)])
def test_real_passes_do_the_same_with_dry_runs_between_them(warm, rounds, tmp_path_factory,
                                                            monkeypatch):
    """C-17.4, C-23.28: two stores given the same rounds of app saves, failed
    publishes and real passes. The second also runs each round's dry runs,
    and each of those leaves its instance as it was, and every file of the
    store, the state root and the projects directory, a killed pass's stale
    temporaries among them. Every real pass then reports the same in both
    (what it scanned and listed too, so a dry run neither used the inventory
    up nor warmed it), and removes those temporaries exactly when it sweeps,
    as it did before; at the end the stores, the merge base, the retries and
    the ledger are the same. `warm` starts both from an instance that has
    passed; without it the first dry run meets a new one."""
    with monkeypatch.context() as patch:
        base = tmp_path_factory.mktemp("twins")
        plain, previewed = World(base / "plain", patch), World(base / "previewed", patch)
        stuck: set[str] = set()
        refuse_publishing(patch, stuck)
        patch.setattr(mirror, "TEMPORARY_STALE_S", -1)  # every temporary is a stale one
        for world in (plain, previewed):
            world.use()
            for session in SESSIONS:
                for index in range(3):
                    world.put(index, session)
            if warm:
                assert world.full().state == "ok"
        for edits, failing, previews, kind, left, due in rounds + LAST:
            for step in edits:
                edit(plain, step)
                edit(previewed, step)
            for world in (plain, previewed):
                for place, index in left:
                    world.leave(place, index)
                if due:
                    world.running._last_sweep = None
            stuck.clear()
            stuck.update(failing)
            for preview_kind in previews:
                running = previewed.running
                before, objects, files = picture(vars(running)), held(running), previewed.files()
                preview = (previewed.hot(dry_run=True) if preview_kind == "hot"
                           else previewed.full(dry_run=True))
                assert preview.state == "ok" and preview.dry_run
                assert picture(vars(running)) == before and held(running) == objects
                assert previewed.files() == files
            waiting = plain.left()
            assert previewed.left() == waiting, "a dry run removed none"
            first = plain.hot() if kind == "hot" else plain.full()
            second = previewed.hot() if kind == "hot" else previewed.full()
            assert brief(second, previewed.base) == brief(first, plain.base), (edits, kind)
            assert plain.left() == previewed.left() == (set() if first.swept else waiting)
        assert previewed.copies() == plain.copies()
        assert previewed.base_flags() == plain.base_flags()
        for name in ("_flag_retry", "_activity_tries", "_inventoried"):
            assert getattr(previewed.running, name) == getattr(plain.running, name), name
        assert set(previewed.running._retry) == set(plain.running._retry)
