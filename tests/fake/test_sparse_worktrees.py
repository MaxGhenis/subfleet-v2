"""C-6.14 end to end in the daemon, with real temporary git repositories and no provider.

A repository over the (lowered) threshold gets a sparse worktree at admission, planned
at submit from the brief; the job is told what is checked out; the worktree's size is
recorded when it is cut and after each attempt; salvage records only real changes.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from subfleet import checkout, ids, protocol
from subfleet.checkout import CheckoutError
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from subfleet.offline import Offline
from subfleet.salvage import working_tree
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401 (fixture)
from tests.unit.test_checkout import git

LAYOUT = {
    "README.md": b"top\n",
    "pyproject.toml": b"[project]\n",
    "src/app.py": b"print('app')\n" * 10,
    "src/pkg/mod.py": b"VALUE = 1\n" * 10,
    "docs/guide.md": b"guide\n" * 20,
    "data/index.json": b"{}\n",
    "data/big/one.bin": b"1" * 6000,
    "data/big/two.bin": b"2" * 6000,
    "data/big/sub/three.bin": b"3" * 3000,
}
BRIEF = b"Fix the bug in src/app.py. The inputs it reads are under data/big/; do not rewrite them.\n"


def large_repository(daemon, harness, *, threshold=5000, budget=1000, files=LAYOUT) -> Path:
    workdir = harness.workdir
    git(workdir, "init", "-b", "task/sparse")
    git(workdir, "config", "user.name", "Test User")
    git(workdir, "config", "user.email", "test@example.invalid")
    for name, data in files.items():
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(workdir, "add", "-A")
    git(workdir, "commit", "-m", "baseline")
    daemon.policy["caps"]["sparse_checkout_min_bytes"] = threshold
    daemon.policy["caps"]["sparse_cone_budget_bytes"] = budget
    # Writable admission requires measured capacity (tests/fake/test_workspace_contract.py).
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .1,
                                     after(3600), ReadingLabel.PROVIDER, "fixture", utcnow()))
    checkout._CACHE.clear()
    return workdir


def brief(harness, text: bytes = BRIEF) -> str:
    path = harness.root / f"brief-{uuid.uuid4().hex}.md"
    path.write_bytes(text)
    return str(path)


def writable(harness, **overrides) -> dict:
    # A session of its own: C-6.5 refuses a second writable job one unidentified
    # instance of a session submits.
    overrides.setdefault("caller_session", f"session-{uuid.uuid4().hex[:8]}")
    return harness.submit_args(sandbox="workspace-write", prompt_path=brief(harness), **overrides)


def manifest(daemon, job_id) -> dict:
    return json.loads((daemon.root / "jobs" / job_id / "manifest.json").read_text())


def files_in(worktree: Path) -> set[str]:
    return {path.relative_to(worktree).as_posix() for path in worktree.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(worktree).parts}


def edit(worktree: Path) -> None:
    (worktree / "src" / "app.py").write_text("print('fixed')\n")
    (worktree / "reports").mkdir(exist_ok=True)
    (worktree / "reports" / "notes.md").write_text("what I did\n")
    (worktree / "data" / "big").mkdir(parents=True, exist_ok=True)
    (worktree / "data" / "big" / "added.bin").write_bytes(b"new input\n")


def test_c6_14_a_large_repository_gets_a_sparse_worktree_planned_from_the_brief(state_daemon, tmp_path):
    """C-6.14, C-6.7, C-13.1 plan at submit, note, sparse cut at admission, sizes, and a
    salvage equal to a full checkout's with the same edits."""
    daemon, harness = state_daemon
    workdir = large_repository(daemon, harness)
    reply = daemon.dispatch("submit", writable(harness))
    job_id = reply["job_id"]
    plan = manifest(daemon, job_id)["workspace"]["checkout"]
    assert plan["mode"] == "sparse" and plan["reason"] == "over-threshold"
    assert plan["tree_bytes"] == sum(len(data) for data in LAYOUT.values())
    assert "src" in plan["cone"] and not any(path.startswith("data/big") for path in plan["cone"])
    assert reply["checkout"]["mode"] == "sparse" and reply["checkout"]["left_out"] == ["data/big"]
    note = (daemon.root / "jobs" / job_id / "prompt.prepared.md").read_text()
    assert "Only part of this repository is checked out" in note
    assert "data/big (15.0 KB), which is not checked out" in note and "git sparse-checkout add <dir>" in note
    assert note.endswith(BRIEF.decode())

    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    daemon._pending_launches.discard(attempt["attempt_id"])
    worktree = Path(daemon.store.get_job(job_id)["worktree"])
    assert worktree == daemon.root / "worktrees" / job_id
    assert git(worktree, "sparse-checkout", "list").splitlines() == plan["cone"]
    assert files_in(worktree) == checkout.cone_files(LAYOUT, plan["cone"])
    assert attempt["baseline_tree"] == git(worktree, "rev-parse", "HEAD^{tree}")
    record = json.loads((daemon.root / "jobs" / job_id / "worktree.json").read_text())
    assert record["created"]["complete"] is True and record["created"]["files"] == len(files_in(worktree)) + 1
    log = (daemon.root / "daemon.log").read_text()
    assert f"worktree {job_id} created: sparse (" in log

    edit(worktree)
    adir = daemon.root / "jobs" / job_id / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700)
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    refs = [row["path"] for row in daemon.store.list_artifacts(attempt["attempt_id"]) if row["role"] == "salvage"]
    assert len(refs) == 1
    full = tmp_path / "full"
    checkout.create_worktree(str(workdir), str(full), git(workdir, "rev-parse", "HEAD"), None, timeout_s=60)
    edit(full)
    assert git(workdir, "rev-parse", f"{refs[0]}^{{tree}}") == working_tree(full, git(full, "rev-parse", "HEAD"))
    assert sorted(git(workdir, "diff", "--name-only", "HEAD", refs[0]).splitlines()) == [
        "data/big/added.bin", "reports/notes.md", "src/app.py"]

    record = json.loads((daemon.root / "jobs" / job_id / "worktree.json").read_text())
    assert record["latest"]["attempt"] == 1 and record["latest"]["files"] >= record["created"]["files"]
    assert f"worktree {job_id} after a1: sparse (" in (daemon.root / "daemon.log").read_text()
    shown = daemon.dispatch("show", {"job_id": job_id})["worktree_report"]
    assert shown["path"] == str(worktree) and shown["checkout"]["mode"] == "sparse"
    assert shown["created"]["bytes"] > 0 and shown["latest"]["attempt"] == 1
    assert Offline(daemon.root).show_job(job_id)["worktree_report"] == shown


def test_c6_14_a_sparse_worktree_nobody_changed_is_not_salvaged(state_daemon):
    """C-13.1 no phantom deletions: an untouched sparse worktree leaves no salvage ref."""
    daemon, harness = state_daemon
    workdir = large_repository(daemon, harness)
    job_id = daemon.dispatch("submit", writable(harness))["job_id"]
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    daemon._pending_launches.discard(attempt["attempt_id"])
    adir = daemon.root / "jobs" / job_id / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700)
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert [row for row in daemon.store.list_artifacts(attempt["attempt_id"]) if row["role"] == "salvage"] == []
    assert git(workdir, "for-each-ref", "refs/subfleet-salvage/") == ""


def test_c6_14_a_repository_under_the_threshold_is_checked_out_whole(state_daemon):
    """C-6.14 the default threshold (2 GiB) leaves ordinary repositories as they were."""
    daemon, harness = state_daemon
    large_repository(daemon, harness, threshold=2 * 1024 ** 3)
    reply = daemon.dispatch("submit", writable(harness))
    job_id = reply["job_id"]
    plan = manifest(daemon, job_id)["workspace"]["checkout"]
    assert plan["mode"] == "full" and plan["reason"] == "under-threshold"
    assert "Only part of this repository" not in (daemon.root / "jobs" / job_id / "prompt.prepared.md").read_text()
    daemon._admit()
    worktree = Path(daemon.store.get_job(job_id)["worktree"])
    assert files_in(worktree) == set(LAYOUT)
    assert json.loads((daemon.root / "jobs" / job_id / "worktree.json").read_text())["created"]["complete"]


def test_c6_14_paths_are_checked_out_whatever_they_cost_and_dot_is_everything(state_daemon):
    """C-6.14 `--paths` replaces the brief's names; `.` asks for a full checkout."""
    daemon, harness = state_daemon
    large_repository(daemon, harness)
    job_id = daemon.dispatch("submit", writable(harness, checkout_paths=["data/big/sub/three.bin", "src"]))["job_id"]
    plan = manifest(daemon, job_id)["workspace"]["checkout"]
    assert plan["reason"] == "paths" and {"data/big/sub", "src"} <= set(plan["cone"])
    assert "data/big" not in plan["cone"]
    everything = daemon.dispatch("submit", writable(harness, checkout_paths=["."]))["job_id"]
    assert manifest(daemon, everything)["workspace"]["checkout"]["reason"] == "paths-all"


@pytest.mark.parametrize(("overrides", "message"), [
    ({"checkout_paths": ["nope"]}, "not in the job's commit"),
    ({"checkout_paths": ["../outside"]}, "leaves the repository"),
    ({"checkout_paths": ["/abs"]}, "relative to the repository"),
    ({"checkout_paths": []}, "non-empty list"),
    ({"checkout_paths": ["src"], "in_place": True}, "worktree of its own"),
    ({"checkout_paths": ["src"], "sandbox": "read-only"}, "worktree of its own"),
])
def test_c6_14_paths_are_refused_where_they_cannot_apply(state_daemon, overrides, message):
    daemon, harness = state_daemon
    large_repository(daemon, harness)
    args = writable(harness)
    args.update(overrides)
    with pytest.raises(protocol.ProtocolError, match=message) as refused:
        daemon.dispatch("submit", args)
    assert refused.value.code == 2
    assert daemon.store.list_jobs() == []


def test_c6_14_paths_are_part_of_the_request_digest_and_additive(state_daemon):
    """C-6.2, C-6.14 a retry with the same paths in another order is the same request;
    other paths are another request; a submission without paths keeps its old digest."""
    daemon, harness = state_daemon
    large_repository(daemon, harness)
    args = writable(harness, checkout_paths=["src", "docs"])
    first = daemon.dispatch("submit", args)
    again = daemon.dispatch("submit", {**args, "checkout_paths": ["docs", "src"]})
    assert again["created"] is False and again["job_id"] == first["job_id"]
    with pytest.raises(protocol.ProtocolError, match="different payload"):
        daemon.dispatch("submit", {**args, "checkout_paths": ["docs"]})
    assert ids.payload_digest(b"x", workdir="/w") == ids.payload_digest(b"x", workdir="/w", checkout_paths=None)
    assert ids.payload_digest(b"x", workdir="/w") != ids.payload_digest(b"x", workdir="/w", checkout_paths=["a"])


def test_c6_14_an_unfinished_sparse_cut_is_rebuilt_before_any_attempt(state_daemon):
    """C-6.8, C-6.14 a worktree with a HEAD but no `created` record (a cut stopped after
    `worktree add --no-checkout`) is never handed to a provider: it is cut again."""
    daemon, harness = state_daemon
    workdir = large_repository(daemon, harness)
    job_id = daemon.dispatch("submit", writable(harness))["job_id"]
    target = daemon.root / "worktrees" / job_id
    git(workdir, "worktree", "add", "--no-checkout", "--detach", str(target), "HEAD")
    assert files_in(target) == set()
    daemon._admit()
    assert daemon.store.list_attempts(job_id)
    plan = manifest(daemon, job_id)["workspace"]["checkout"]
    assert files_in(target) == checkout.cone_files(LAYOUT, plan["cone"])


@pytest.mark.parametrize("transient", [True, False])
def test_c6_14_a_refused_cut_waits_when_transient_and_fails_otherwise(state_daemon, monkeypatch, transient):
    """C-6.8 a held lock is the machine's moment (the job waits on `workspace`); any
    other refusal fails the job with git's words, as a refused `worktree add` did."""
    daemon, harness = state_daemon
    large_repository(daemon, harness)
    job_id = daemon.dispatch("submit", writable(harness))["job_id"]

    def refuse(*args, **kwargs):
        raise CheckoutError("git sparse-checkout failed: could not lock config file", transient=transient)

    monkeypatch.setattr(checkout, "create_worktree", refuse)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert not (daemon.root / "worktrees" / job_id).exists()
    if transient:
        assert job["state"] == "waiting" and job["wait_reason"] == "workspace"
    else:
        assert job["state"] == "failed"
        assert "could not allocate worktree: git sparse-checkout failed" in daemon.store.list_notices()[0]["text"]


def test_c6_14_a_head_that_cannot_be_measured_gets_a_full_checkout(state_daemon, monkeypatch):
    """C-6.14 measuring is an optimization: it never refuses a submit."""
    daemon, harness = state_daemon
    large_repository(daemon, harness)

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git", "ls-tree"], 60)

    monkeypatch.setattr(checkout, "measure_tree", slow)
    job_id = daemon.dispatch("submit", writable(harness))["job_id"]
    plan = manifest(daemon, job_id)["workspace"]["checkout"]
    assert plan["mode"] == "full" and plan["reason"] == "unmeasured" and "TimeoutExpired" in plan["error"]
    assert "could not measure" in (daemon.root / "daemon.log").read_text()


def test_c6_14_a_callers_place_past_the_budget_starts_the_job_at_the_top(state_daemon):
    """C-6.6, C-6.14 run from data/big, over the budget: not checked out, the job starts at
    the top, and the note says how to add it."""
    daemon, harness = state_daemon
    large_repository(daemon, harness)
    args = writable(harness)
    args["workdir"] = str(harness.workdir / "data" / "big")
    job_id = daemon.dispatch("submit", args)["job_id"]
    workspace = manifest(daemon, job_id)["workspace"]
    assert workspace["prefix"] == "." and workspace["checkout"]["place_checked_out"] is False
    note = (daemon.root / "jobs" / job_id / "prompt.prepared.md").read_text()
    assert "is not checked out here, so you start at" in note and "git sparse-checkout add data/big" in note
