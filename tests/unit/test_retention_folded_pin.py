"""C-8.4: a queued job keeps a tree exactly when retention's fence holds it.

Admission compares a turn's folder with retention's fence as the volume compares
names when a name of the folder was not there to look up (`folders.retiring(...,
folded=True)`, `folders.fold`), and spelled through the kernel otherwise. The
queued job must then keep the tree at the commit (`worktree-in-use`), or the
retirement deletes the tree the turn waits to work in, and the turn fails after
C-6.8's retries. That pin compared recorded strings in SQLite, whose NOCASE and
LIKE fold ASCII only. Two kinds of job were held and kept nothing (review of
3410b4f0, P3):

- a job whose directory submit kept through the Data volume's firmlink
  (`/System/Volumes/Data/Users/…`, which `resolve` keeps), while it spelled the
  turn's folder through the kernel (`/Users/…`);
- a job whose folder names the tree with a letter of its job id typed as one the
  volume folds to it (`ﬁ` for `fi`, `ﬆ` for `st`, `ſ` for `s`, the Kelvin sign for
  `k`), spelled while the tree was in quarantine, so the typed name stayed.

`worktree-in-use` now also compares, folded, the folders a job not yet ended
names: its directory, its worktree, and the folder submit spelled for its row
(`job.submitted`'s `write_target` or `folder`). Strings only, as the commit
transaction requires.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module, folders, retention
from subfleet import retention_archive as rarch
from subfleet.store import Store
from tests.unit.retention_world import git, snapshot

ID = "20261005-120000-first-desk"            # C-1.1: `[a-z0-9-]`
#: Spellings of `ID` that APFS finds (each folds to it) and SQLite's NOCASE and LIKE
#: do not match: checked on 2026-10-06 (logs/apfs-ascii-aliases.log).
EXOTIC = {"fi-ligature": ID.replace("fi", "\ufb01"), "long-s": ID.replace("first", "fir\u017ft"),
          "st-ligature": ID.replace("st", "\ufb06"), "kelvin": ID[:-1] + "\u212a",
          "all": "20261005-120000-\ufb01r\ufb06-de\u017f\u212a"}


def queued(store: Store, job_id: str, *, workdir: str, submitted: dict | None = None, kind: str = "turn",
           sandbox: str = "read-only", state: str = "waiting") -> None:
    """A job not yet ended, with what submit records beside its row (`job.submitted`)."""
    with store.transaction("job.submitted", job_id=job_id, data=submitted or None):
        store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind=kind, workdir=workdir,
                      prompt_path="/p", sandbox=sandbox, state=state)


def finished_tree(store: Store, job_id: str, worktree: str | None) -> None:
    """A finished job with its own allocated worktree, recorded or not yet recorded."""
    store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind="dispatch", workdir="/repo",
                  worktree=worktree, prompt_path="/p", sandbox="workspace-write", state="succeeded")


def fence(store: Store, tree: str, job_id: str) -> None:
    with store.transaction() as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (folders.exclusive_key(tree), f"retention:{job_id}", "2026-10-06T00:00:00Z"))


def kept(store: Store, job_id: str, root: Path) -> str | None:
    return retention._pin_reasons(store, set(), None, only=job_id, root=root).get(job_id)


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "state" / "subfleet.db")
    yield opened
    opened.close()


def test_a_turn_whose_directory_names_the_tree_through_the_firmlink_keeps_it(tmp_path, store):
    """The review's probe P3, made a test. A read-only turn's directory typed through
    the Data volume's firmlink, as `resolve(strict=True)` keeps it, and its folder as
    submit spells it, through the kernel. The tree is in quarantine: the folded fence
    holds the turn, and its job must keep the tree. Failed before: `worktree-in-use`
    compared the directory string, and kept nothing."""
    root = tmp_path / "state"
    firm = Path("/System/Volumes/Data" + str(root))
    if not str(root).startswith(("/Users/", "/private/")) or not firm.exists() or not firm.samefile(root):
        pytest.skip("needs a state root reached through the Data volume's firmlink")
    (root / "worktrees").mkdir()
    tree = folders.canonical(root / "worktrees" / "job")        # not there: in quarantine
    workdir = str(firm / "worktrees" / "job" / "vendor")
    folder, missing = folders.present(workdir)
    finished_tree(store, "job", str(root / "worktrees" / "job"))
    queued(store, "turn", workdir=workdir, submitted={"folder": folder})
    fence(store, tree, "job")
    assert missing is not None and folder.startswith(str(root)) and not workdir.startswith(str(root))
    assert folders.retiring(store.query, folder, folded=True) == [folders.exclusive_key(tree)]
    assert kept(store, "job", root) == "worktree-in-use"


@pytest.mark.parametrize("typed", sorted(EXOTIC), ids=sorted(EXOTIC))
@pytest.mark.parametrize("where", ["submitted", "workdir"])
@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "unrecorded"])
def test_a_job_id_typed_with_letters_the_volume_folds_keeps_the_tree(tmp_path, store, typed, where, recorded):
    """The tree's name is its job id, ASCII (C-1.1), but the volume finds it typed
    with letters that fold to its own (`ﬁ`, `ﬆ`, `ſ`, the Kelvin sign), which SQLite
    does not fold. A folder so typed while the tree was in quarantine stays as typed,
    in the folder submit records and in the directory: the folded fence holds the
    job, and its job keeps the tree, recorded or allocated and not yet recorded,
    whichever of the two names it. Failed before: kept nothing."""
    root = tmp_path / "state"
    tree = str(root / "worktrees" / ID)
    alias = str(root / "worktrees" / EXOTIC[typed] / "vendor" / "lib")
    finished_tree(store, ID, tree if recorded else None)
    queued(store, "turn", workdir=alias if where == "workdir" else "/elsewhere",
           submitted={"folder": alias} if where == "submitted" else None)
    fence(store, tree, ID)
    assert folders.retiring(store.query, alias, folded=True) == [folders.exclusive_key(tree)]
    assert kept(store, ID, root) == "worktree-in-use"


@pytest.mark.parametrize("name", [ID + "2", ID[:-1], "\ufb01" + ID, ID.replace("first", "f\u0131rst")],
                         ids=["extends", "prefix", "before", "dotless-i"])
def test_a_name_that_only_looks_like_the_tree_keeps_nothing(tmp_path, store, name):
    """Folding is not likeness: a name extending the tree's, a prefix of it, or one
    with a dotless `ı` (which folds to itself) is another folder, which no fence on
    the tree holds, and keeps nothing."""
    root = tmp_path / "state"
    tree = str(root / "worktrees" / ID)
    other = str(root / "worktrees" / name / "src")
    finished_tree(store, ID, tree)
    queued(store, "turn", workdir=other, submitted={"folder": other})
    fence(store, tree, ID)
    assert folders.retiring(store.query, other, folded=True) == []
    assert kept(store, ID, root) is None


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "lost"])
def test_an_ended_job_keeps_no_tree_its_folder_is_in(tmp_path, store, state):
    """Only a job not yet ended keeps a tree: one that has ended will not work in it
    (its rows, if any are left, keep it as `turn-folder`)."""
    root = tmp_path / "state"
    tree = str(root / "worktrees" / ID)
    alias = str(root / "worktrees" / EXOTIC["all"])
    finished_tree(store, ID, tree)
    queued(store, "turn", workdir=alias, submitted={"folder": alias}, state=state)
    assert kept(store, ID, root) is None


def test_what_submit_records_beside_the_row_is_read_whatever_its_shape(tmp_path, store):
    """A `job.submitted` that is not a JSON object, or names no folder, or a folder
    that is not a string, adds nothing, and breaks nothing: the directory still
    counts."""
    root = tmp_path / "state"
    tree = str(root / "worktrees" / ID)
    finished_tree(store, ID, tree)
    queued(store, "a", workdir="/elsewhere", submitted={"folder": 7, "write_target": None})
    with store.transaction() as tx:
        tx.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES('t','job.submitted','b','[1]')")
        tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,workdir,prompt_path,sandbox,state,"
                   "created_at) VALUES('b','b','d','turn',?,'/p','read-only','queued','t')",
                   (str(root / "worktrees" / EXOTIC["kelvin"]),))
    assert kept(store, ID, root) == "worktree-in-use"
    with store.transaction() as tx:
        tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id='b'")
    assert kept(store, ID, root) is None


# The differential property: on the strings retention and admission compare, the
# pin keeps a tree exactly when the folded fence holds the job. Each letter of the
# job's folder below `/s/worktrees` may be any letter the volume folds alike: ASCII
# in either case, `ſ` for `s`, the Kelvin sign for `k`, `ﬁ`, `ﬂ`, `ﬀ`, `ﬆ` for their
# pairs, a non-ASCII name in either case and in NFC or NFD. No `%` or `_`: with
# LIKE's wildcards in a tree's spelling the SQL keeps more than the fence holds,
# which only keeps a tree longer.
FOLDS_TO = {"s": ["s", "S", "\u017f"], "k": ["k", "K", "\u212a"], "fi": ["fi", "FI", "\ufb01"],
            "fl": ["fl", "Fl", "\ufb02"], "ff": ["ff", "fF", "\ufb00"], "st": ["st", "sT", "\ufb06"]}
ID_LETTERS = st.text(st.sampled_from("fiklst0-"), min_size=1, max_size=8).filter(lambda n: n not in (".", ".."))
TAIL = st.sampled_from(["src", "vendor/lib", "l\u00efb", "Stra\u00dfe", "\u00c5ngstr\u00f6m"])


@st.composite
def respelled(draw, name: str) -> str:
    """`name` as typed: each run of letters the volume folds alike, any of them."""
    out, i = [], 0
    while i < len(name):
        pair = name[i:i + 2].lower()
        if pair in FOLDS_TO and draw(st.booleans()):
            out.append(draw(st.sampled_from(FOLDS_TO[pair])))
            i += 2
            continue
        letter = name[i]
        choices = FOLDS_TO.get(letter.lower(), [letter, letter.swapcase()])
        out.append(draw(st.sampled_from(choices)))
        i += 1
    return unicodedata.normalize(draw(st.sampled_from(["NFC", "NFD"])), "".join(out))


@st.composite
def tree_and_job_folder(draw):
    """`(tree id, shape, folder)`: a folder on the tree, inside it, beside it,
    extending its name, or above it, every name below `/s/worktrees` respelled."""
    tree_id = draw(ID_LETTERS)
    shape = draw(st.sampled_from(["tree", "inside", "beside", "extends", "above"]))
    names = {"tree": [tree_id], "inside": [tree_id, *draw(TAIL).split("/")],
             "beside": [draw(ID_LETTERS.filter(lambda other: folders.fold(other) != folders.fold(tree_id)))],
             "extends": [tree_id + draw(ID_LETTERS)], "above": []}[shape]
    typed = [draw(respelled(name)) for name in names]
    return tree_id, shape, "/".join(["/s/worktrees", *typed])


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@example(("first-desk", "inside", "/s/worktrees/\ufb01r\ufb06-de\u017f\u212a/vendor/lib"), "submitted", True)
@example(("k", "tree", "/s/worktrees/\u212a"), "workdir", False)
@example(("st", "extends", "/s/worktrees/\ufb06s"), "submitted", True)
@given(tree_and_job_folder(), st.sampled_from(["submitted", "workdir"]), st.booleans())
def test_the_pin_keeps_a_tree_exactly_when_the_folded_fence_holds_the_job(case, where, recorded):
    tree_id, shape, folder = case
    tree = "/s/worktrees/" + tree_id
    with tempfile.TemporaryDirectory() as scratch:
        store = Store(Path(scratch) / "subfleet.db")
        try:
            finished_tree(store, tree_id, tree if recorded else None)
            queued(store, "turn", workdir=folder if where == "workdir" else "/elsewhere",
                   submitted={"folder": folder} if where == "submitted" else None)
            fence(store, tree, tree_id)
            held = bool(folders.retiring(store.query, folder, folded=True))
            keeps = kept(store, tree_id, Path("/s")) == "worktree-in-use"
        finally:
            store.close()
    assert held == keeps, (case, where, recorded, held, keeps)
    if shape in ("tree", "inside"):
        assert held, case                          # the volume finds the tree by this name


# Through the daemon: real submission, workspace and admission, the archive
# driver's own retirement. Holder scanning is stubbed and no provider is launched.

def host(daemon, harness, job_id: str) -> tuple[Path, str]:
    """A finished job `job_id` with its own worktree under the state root, recorded
    as `_workspace` records it, and a repository nested in it on a task branch:
    `(tree, nested)`, the nested folder canonical."""
    from tests.unit.test_retention_shared_folders import nested_repository
    wt = daemon.root / "worktrees" / job_id
    git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(wt), "HEAD")
    nested = nested_repository(wt, branch="task/nested")
    daemon.store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind="dispatch",
                         workdir=str(harness.workdir), worktree=str(wt), prompt_path="/prompt",
                         sandbox="workspace-write", state="succeeded", workdir_head=git(wt, "rev-parse", "HEAD"))
    directory = daemon.root / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "stdout").write_text("host output")
    return wt, nested


def retire(daemon) -> dict:
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {})


def held_or_live(daemon, job_id: str) -> dict:
    from tests.fake.test_admission_liveness import _live
    return {"live": _live(daemon, job_id), "hold": dict(daemon._holds.get(job_id) or {}),
            "state": daemon.store.get_job(job_id)["state"]}


@pytest.mark.parametrize("who", ["TURN", "READER", "writer"])
def test_a_job_typed_through_the_firmlink_during_a_retirement_keeps_the_tree(tmp_path, who):
    """A conversation's turn in the repository nested in a finished job's tree, writable
    or read-only, typed through the Data volume's firmlink and submitted after retention
    fenced the tree: admission holds it on the fence (its folder is spelled through the
    kernel), and its queued job must keep the tree at the commit. Or a detached writer
    in place on the tree itself typed so, queued before the pass (submit refuses one
    while the fence is held, C-6.5): its job must keep the tree from selection on.
    Submit kept the directory through the firmlink, so `worktree-in-use` kept nothing:
    the retirement deleted the tree the turn waited for (review of 3410b4f0, P3), and
    fenced the writer's tree and deleted it. Now the folder submit spelled keeps it."""
    from tests.fake.test_admission_latency import fleet_daemon, measure, submit
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS, message_in

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, nested = host(daemon, harness, ID)
        tree = folders.canonical(wt)
        place = Path(tree) if who == "writer" else Path(nested)
        firm = Path("/System/Volumes/Data" + str(place))
        if not firm.is_dir() or not firm.samefile(place) or str(daemon.root).startswith("/System/"):
            pytest.skip("the state root is not reached through the Data volume's firmlink")
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        before = snapshot(wt)
        seen, real_begin = {}, rarch.Retirement.begin

        def begin(self, job, pool):
            if not seen:
                options = {**SETTINGS, "permission": "accept-edits" if who == "TURN" else "read-only"}
                _, _, job_id = message_in(daemon, harness, "Firm", workspace=firm, settings=options)
                daemon.store.update_job(job_id, next_check_at=None)
                daemon._admit_turns()
                seen.update(job=job_id, **held_or_live(daemon, job_id))
            return real_begin(self, job, pool)

        if who == "writer":
            seen["job"] = submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(firm))
            assert retention._pin_reasons(daemon.store, set(), None, root=daemon.root).get(ID) == "worktree-in-use"
        else:
            patch.setattr(rarch.Retirement, "begin", begin)
        result = retire(daemon)
        job_id = seen["job"]
        assert daemon._job(job_id)["workdir"].startswith("/System/Volumes/Data/"), daemon._job(job_id)
        if who != "writer":
            assert not seen["live"] and seen["hold"].get("reason") == "lease-held", seen
            assert folders.exclusive_key(tree) in seen["hold"]["leases"], seen
        assert ID not in result["pruned"] and ID in result["protected"], result
        assert {p: e[:3] for p, e in snapshot(wt).items()} == {p: e[:3] for p, e in before.items()}
        daemon.store.update_job(job_id, next_check_at=None)
        daemon._admit_turns() if who != "writer" else daemon._admit()
        assert _live(daemon, job_id), daemon._holds.get(job_id)


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_turn_typed_with_letters_the_volume_folds_while_its_tree_is_away_keeps_it(tmp_path, writable):
    """A conversation whose recorded cwd names the tree with letters of its job id the
    volume folds to them (`ﬁ`, `ﬆ`, `ſ`, the Kelvin sign), its message submitted while
    retention has the tree in quarantine: submit and admission cannot look the name up,
    so both keep it as typed. Admission holds the turn on the fence, found folded; its
    queued job must keep the tree, but SQLite's NOCASE and LIKE do not fold those
    letters, and the retirement deleted the tree it waited for. Now the commit is rolled
    back, the tree comes back as it was, and the turn runs there once the fence goes."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import SETTINGS, message_in

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt, nested = host(daemon, harness, ID)
        tree = folders.canonical(wt)
        alias = wt.with_name(EXOTIC["all"]) / "vendor" / "lib"
        if not alias.is_dir() or not alias.samefile(nested):
            pytest.skip("the volume does not find a name by letters that fold to its own")
        before = snapshot(wt)
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        real_quarantine, real_head, seen = rarch.Retirement.quarantine, daemon_module.git_head, {}

        def quarantine_during_submit(retirement):
            if seen:
                return real_quarantine(retirement)
            target, moved = os.path.realpath(alias), []

            def head_after_validation(workdir, *args, **kwargs):
                if not moved and str(workdir) == target:
                    real_quarantine(retirement)          # strict validation has passed: now the tree goes
                    moved.append(True)
                return real_head(workdir, *args, **kwargs)

            with pytest.MonkeyPatch.context() as inner:
                inner.setattr(daemon_module, "git_head", head_after_validation)
                _, _, turn = message_in(daemon, harness, "Folded", workspace=alias, settings=options)
            assert moved and not wt.exists()
            daemon._admit_turns()
            seen.update(turn=turn, recorded=daemon._submitted(turn), **held_or_live(daemon, turn))

        patch.setattr(rarch.Retirement, "quarantine", quarantine_during_submit)
        result = retire(daemon)
        turn = seen["turn"]
        recorded = seen["recorded"].get("write_target") or seen["recorded"].get("folder")
        assert EXOTIC["all"] in recorded and EXOTIC["all"] in daemon._job(turn)["workdir"], seen
        assert not seen["live"] and seen["hold"].get("reason") == "lease-held", seen
        assert seen["hold"]["leases"] == [folders.exclusive_key(tree)], seen
        assert ID not in result["pruned"] and ID in result["protected"], result
        assert {p: e[:3] for p, e in snapshot(wt).items()} == {p: e[:3] for p, e in before.items()}
        daemon.store.update_job(turn, next_check_at=None)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds.get(turn)


# The same property on the volume itself: real directories, real lookups and
# spellings (`folders.canonical`, `folders.present`), the tree there or in
# quarantine when submit spells the folder, when it records the directory and when
# admission looks. The directory is recorded as `resolve` keeps it (this submit) or
# in its one spelling (#140's).
INSIDE = ("", "src", "vendor/lïb", "Straße/sub", "ﬁx")
VOLUME_SHAPES = ([("inside", rel) for rel in INSIDE]
                 + [("extends", ID + "2/src"), ("beside", "other/src"), ("above", None)])


@pytest.fixture(scope="module")
def volume():
    import shutil
    base = Path(tempfile.mkdtemp(prefix="folded-pin-"))
    root = base / "stäte"
    tree = root / "worktrees" / ID
    for rel in INSIDE:
        (tree / rel).mkdir(parents=True, exist_ok=True)
    (root / "worktrees" / (ID + "2") / "src").mkdir(parents=True)
    (root / "worktrees" / "other" / "src").mkdir(parents=True)
    store = Store(root / "subfleet.db")
    try:
        if not (root / "worktrees" / EXOTIC["all"]).is_dir():
            pytest.skip("the volume does not find a name by letters that fold to its own")
        canonical_root = Path(folders.canonical(root))
        finished_tree(store, ID, str(canonical_root / "worktrees" / ID))
        queued(store, "turn", workdir="/elsewhere")
        fence(store, str(canonical_root / "worktrees" / ID), ID)
        firm = Path("/System/Volumes/Data" + str(canonical_root))
        yield {"root": canonical_root, "tree": canonical_root / "worktrees" / ID, "store": store,
               "aside": canonical_root / "worktrees" / f".{ID}.quarantined",
               "firm": firm if firm.is_dir() and firm.samefile(canonical_root) else None}
    finally:
        store.close()
        shutil.rmtree(base, ignore_errors=True)


def away(volume: dict, gone: bool) -> None:
    there = volume["tree"].exists()
    if gone and there:
        volume["tree"].rename(volume["aside"])
    elif not gone and not there:
        volume["aside"].rename(volume["tree"])


@st.composite
def typed_on_volume(draw):
    """`(shape, names below the root's parent, through the firmlink)`."""
    shape, rel = draw(st.sampled_from(VOLUME_SHAPES))
    names = ["worktrees", *([ID, *rel.split("/")] if shape == "inside" else rel.split("/") if rel else [])]
    return shape, [name for name in names if name], draw(st.booleans())


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow,
                                                                   HealthCheck.function_scoped_fixture])
@given(typed_on_volume(), st.data(), st.lists(st.booleans(), min_size=3, max_size=3),
       st.sampled_from(["resolved", "canonical"]))
def test_on_the_volume_the_pin_keeps_a_tree_exactly_when_the_fence_holds_the_job(volume, case, data, gone,
                                                                                  recorded):
    shape, names, firmlinked = case
    root = volume["root"]
    typed_names = [data.draw(respelled(root.name))] + [data.draw(respelled(name)) for name in names]
    base = volume["firm"].parent if firmlinked and volume["firm"] is not None else root.parent
    typed = str(base.joinpath(*typed_names))
    at_submit, at_record, at_admission = gone
    store = volume["store"]
    try:
        assert os.path.isdir(typed), typed          # strict validation: the folder is there
        resolved = os.path.realpath(typed)
        away(volume, at_submit)                      # retention may take the tree before submit spells it
        folder = folders.canonical(typed)            # submit's `write_target` or `folder`
        away(volume, at_record)
        workdir = resolved if recorded == "resolved" else folders.canonical(typed)
        with store.transaction() as tx:
            tx.execute("UPDATE jobs SET workdir=? WHERE job_id='turn'", (workdir,))
            tx.execute("UPDATE events SET data_json=? WHERE job_id='turn' AND kind='job.submitted'",
                       (f'{{"folder": {folder!r}}}'.replace("'", '"'),))
        away(volume, at_admission)
        named, absent = folders.present(folder)      # admission
        held = bool(folders.retiring(store.query, named, folded=absent is not None))
        keeps = kept(store, ID, root) == "worktree-in-use"
    finally:
        away(volume, False)
    assert held == keeps == (shape == "inside"), (typed, folder, workdir, gone, held, keeps)
