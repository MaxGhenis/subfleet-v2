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

Every test names the clause it proves (C-20.5). The desktop store lives under
`tmp_path`; nothing here reads or writes the operator's own.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
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
        """Every file of the store and the state root: its inode and its bytes."""
        return {str(file.relative_to(self.base)): (file.stat().st_ino, file.read_bytes())
                for top in (self.store, self.root) for file in sorted(top.rglob("*"))
                if file.is_file() and file.name != mirror.LOCK_NAME}

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
#: The app's saves, the sessions whose publishes fail, the dry runs, then a real pass.
ROUNDS = st.tuples(st.lists(EDITS, max_size=3),
                   st.sets(st.sampled_from(SESSIONS), max_size=2),
                   st.lists(st.sampled_from(["hot", "full"]), max_size=2),
                   st.sampled_from(["hot", "hot", "full"]))


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
def test_real_passes_do_the_same_with_dry_runs_between_them(warm, rounds, tmp_path_factory,
                                                            monkeypatch):
    """C-17.4, C-23.28: two stores given the same rounds of app saves, failed
    publishes and real passes. The second also runs each round's dry runs,
    and each of those leaves its instance as it was. Every real pass then
    reports the same in both (what it scanned and listed too, so a dry run
    neither used the inventory up nor warmed it), and at the end the stores,
    the merge base, the retries and the ledger are the same. `warm` starts
    both from an instance that has passed; without it the first dry run meets
    a new one."""
    with monkeypatch.context() as patch:
        base = tmp_path_factory.mktemp("twins")
        plain, previewed = World(base / "plain", patch), World(base / "previewed", patch)
        stuck: set[str] = set()
        refuse_publishing(patch, stuck)
        for world in (plain, previewed):
            world.use()
            for session in SESSIONS:
                for index in range(3):
                    world.put(index, session)
            if warm:
                assert world.full().state == "ok"
        for edits, failing, previews, kind in rounds + [([], set(), [], "full"), ([], set(), [], "hot")]:
            for step in edits:
                edit(plain, step)
                edit(previewed, step)
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
            first = plain.hot() if kind == "hot" else plain.full()
            second = previewed.hot() if kind == "hot" else previewed.full()
            assert brief(second, previewed.base) == brief(first, plain.base), (edits, kind)
        assert previewed.copies() == plain.copies()
        assert previewed.base_flags() == plain.base_flags()
        for name in ("_flag_retry", "_activity_tries", "_inventoried"):
            assert getattr(previewed.running, name) == getattr(plain.running, name), name
        assert set(previewed.running._retry) == set(plain.running._retry)
