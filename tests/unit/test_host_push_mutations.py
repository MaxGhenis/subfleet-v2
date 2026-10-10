"""Exercise each requested safety mutation against the integration invariants.

Mutations live only in monkeypatched memory. Each runs the same real submit,
acceptance and local push invariant, and must cause its assertion to fail.
"""
import hashlib
import math
import os
from pathlib import Path
import subprocess

import pytest

from subfleet import host_push
from subfleet.daemon import Daemon, read_regular
from test_host_push_export import (
    test_a_push_settles_with_leases_held_then_the_canonical_export_is_an_ordinary_jobs as export_invariant,
)
from test_host_push import (
    test_escaping_symlinks_including_chains_are_refused as symlink_invariant,
    test_never_executes_job_controlled_git_config_or_hooks as config_invariant,
    test_never_pushes_a_tip_not_descending_from_recorded_base as ancestry_invariant,
    test_never_pushes_github_changes_including_reverted_intermediate_commits as workflow_invariant,
    test_never_submits_or_pushes_a_protected_branch as protected_invariant,
    test_new_branch_push_is_non_forcing_non_deleting_and_reexport_is_idempotent as refspec_invariant,
    worlds,
)
from test_host_push_intake import test_intake_refuses_anything_but_a_regular_file_in_a_plain_directory as intake_invariant
from test_host_push_metadata import (
    test_a_submit_refused_on_planted_metadata_leaves_the_submit_lock_free as metadata_invariant,
)
from test_host_push_names import (
    test_three_spellings_of_one_repository_racing_for_one_branch_have_one_owner as ownership_invariant,
)
from test_host_push_trees import test_a_tree_bomb_past_any_cap_is_refused_by_the_one_deadline as deadline_invariant


def test_mutation_drop_protected_check_is_caught(worlds, monkeypatch):
    monkeypatch.setattr(host_push, "check_branch", lambda *args, **kwargs: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        protected_invariant.hypothesis.inner_test(worlds, "main")


def test_mutation_allow_forcing_refspec_is_caught(worlds, monkeypatch):
    monkeypatch.setattr(host_push, "push_refspec", lambda sha, branch: f"+{sha}:refs/heads/{branch}")
    with pytest.raises(AssertionError):
        refspec_invariant.hypothesis.inner_test(worlds, "mutated")


def test_mutation_skip_descends_from_base_is_caught(worlds, monkeypatch):
    actual = host_push.git
    def mutate(repo, *args, **kwargs):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return b""
        return actual(repo, *args, **kwargs)
    monkeypatch.setattr(host_push, "git", mutate)
    with pytest.raises(AssertionError):
        ancestry_invariant.hypothesis.inner_test(worlds, "orphan")


def test_mutation_skip_github_check_is_caught(worlds, monkeypatch):
    # Every commit's `.github` root entries read as the base's.
    monkeypatch.setattr(host_push, "github_entries", lambda *args, **kwargs: frozenset())
    with pytest.raises(AssertionError):
        workflow_invariant.hypothesis.inner_test(worlds, ".github/workflows/ci.yml", False)


def test_mutation_push_in_job_worktree_is_caught_by_sentinel(worlds, monkeypatch):
    actual = host_push.git
    def mutate(repo, *args, **kwargs):
        if args[0] == "push":
            # The sole attacker payload here writes the test-owned sentinel.
            # Deliberately omit config overrides to model the forbidden intake.
            worktree = repo.parents[3] / "work"
            result = subprocess.run([host_push.GIT, "-C", str(worktree), *args],
                                    env=host_push._environment(), capture_output=True, timeout=60)
            assert result.returncode == 0, result.stderr
            return result.stdout
        return actual(repo, *args, **kwargs)
    monkeypatch.setattr(host_push, "git", mutate)
    with pytest.raises(AssertionError):
        config_invariant.hypothesis.inner_test(worlds, "core.hooksPath")


# Fix round 1 (review of #158): each fix, removed, fails its integration test.

@pytest.mark.parametrize("branch", ["MAIN", "Release/217", "HEAD"])
def test_mutation_casefold_removed_from_the_protected_check_is_caught(worlds, monkeypatch, branch):
    monkeypatch.setattr(host_push, "protected_key", lambda name: name)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        protected_invariant.hypothesis.inner_test(worlds, branch)


def test_mutation_symlink_rule_removed_is_caught(worlds, monkeypatch):
    monkeypatch.setattr(host_push, "check_symlinks", lambda links: None)
    with pytest.raises(AssertionError, match=r"pushed [0-9a-f]{40}"):
        symlink_invariant(worlds, "case-alias")


def test_mutation_nofollow_dropped_on_an_intermediate_component_is_caught(worlds, monkeypatch, tmp_path):
    monkeypatch.setattr(host_push, "DIRECTORY_FLAGS", host_push.DIRECTORY_FLAGS & ~os.O_NOFOLLOW)
    with pytest.raises(AssertionError, match=r"pushed [0-9a-f]{40}"):
        intake_invariant(worlds, tmp_path, "symlinked-subfleet")


def test_mutation_verification_deadline_removed_is_caught(worlds, monkeypatch):
    # Each Git call keeps only its own TIMEOUT_S; none shares a deadline.
    def unbounded(self, seconds):
        self.seconds, self.at = seconds, math.inf
    monkeypatch.setattr(host_push.Deadline, "__init__", unbounded)
    with pytest.raises(AssertionError, match=r"got error .host git ls-tree failed \(TimeoutExpired\)"):
        deadline_invariant(worlds)


def test_mutation_ownership_keyed_on_the_raw_url_is_caught(worlds, monkeypatch):
    monkeypatch.setattr(host_push, "ownership_key", lambda remote: remote)
    with pytest.raises(AssertionError, match="was not refused as another family's: pushed [0-9a-f]{40}"):
        ownership_invariant(worlds)


# Fix round 2 (re-review of #158): each fix, removed, fails its integration test.

#: kind -> what the metadata invariant reports when `read_text` reads it: a
#: FIFO holds the submit and its lock; a link or an oversize file is accepted.
READ_TEXT_CAUGHT = {"fifo": "still blocked", "symlink": "'value'", "oversize": "'value'"}


@pytest.mark.parametrize("kind", sorted(READ_TEXT_CAUGHT))
def test_mutation_read_text_restored_is_caught(worlds, monkeypatch, kind):
    def read_text(path, cap, *, optional=False):
        try:
            return path.read_text()
        except (FileNotFoundError, NotADirectoryError):
            if optional:
                return None
            raise
    monkeypatch.setattr(host_push, "_read_metadata", read_text)
    with pytest.raises(AssertionError, match=READ_TEXT_CAUGHT[kind]):
        metadata_invariant(worlds, kind)


@pytest.mark.parametrize("case", ["into-git", "into-git-any-case"])
def test_mutation_dot_git_component_check_removed_is_caught(worlds, monkeypatch, case):
    monkeypatch.setattr(host_push, "_dotgit_component", lambda part: False)
    with pytest.raises(AssertionError, match=r"pushed [0-9a-f]{40}"):
        symlink_invariant(worlds, case)


# Merge of release/217 at cf3a22e2: #158's push gate inside #148's export.

def pre_merge_export_locked(self, job_id):
    """#158's `_export_locked` as it stood before the merge, verbatim: the push
    gate, then a raw `out:<path>` lookup, where a push job whose lease that
    misses releases its leases and records nothing."""
    job = self._job(job_id)
    if not job["accepted_attempt_id"]:
        return
    if job.get("push_branch") is not None and not self._push_settled(job_id):
        self._schedule("push:" + job_id, self._push_job, job_id, paced=True, pool=self.pushes)
        return
    a = self.store.get_attempt(job["accepted_attempt_id"])
    if job["out_path"]:
        lease = self.store.one("SELECT holder FROM leases WHERE lease_key=?", (f"out:{job['out_path']}",))
        if not lease or lease["holder"] != job_id:
            if job.get("push_branch") is not None:
                with self.store.transaction("job.export_superseded", job_id=job_id) as tx:
                    tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job_id, a["attempt_id"]))
                self._notify()
            return
    artifact = self.store.one("SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'", (a["attempt_id"],))
    export_error = None
    exported = None
    if job["out_path"] and not self.store.one("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'", (a["attempt_id"],)):
        try:
            contents = read_regular(artifact["path"])
            destination = Path(job["out_path"])
            if hashlib.sha256(contents).hexdigest() != artifact["sha256"]:
                raise OSError("accepted deliverable digest changed")
            self._publish("export", destination, contents)
            self._boundary("export", job_id, a["attempt_id"])
            exported = {"role": "export", "path": str(destination), "sha256": artifact["sha256"], "bytes": artifact["bytes"]}
        except OSError as exc:
            export_error = f"export failed: {type(exc).__name__} (errno={exc.errno})"
    with self.store.transaction("job.export_failed" if export_error else "job.exported", job_id=job_id, attempt_id=a["attempt_id"]) as tx:
        if exported:
            self.store.add_artifact(a["attempt_id"], **exported)
        if export_error:
            tx.execute("UPDATE jobs SET export_error=? WHERE job_id=?", (export_error, job_id))
            tx.execute("UPDATE notices SET text=text || ? WHERE job_id=?", ("\n" + export_error, job_id))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job_id, a["attempt_id"]))
    self._notify()


def test_mutation_merge_keeping_only_release_export_is_caught(worlds, monkeypatch):
    # No push gate: the export decides at acceptance, frees the leases, and nothing is pushed.
    monkeypatch.setattr(Daemon, "_push_settled", lambda self, job_id: True)
    with pytest.raises(AssertionError, match="leases released before the push ended"):
        export_invariant(worlds, "pushed", "owner")


def test_mutation_merge_keeping_only_158_export_is_caught(worlds, monkeypatch):
    # After the push, a lease the raw spelling misses is released with no export_error.
    monkeypatch.setattr(Daemon, "_export_locked", pre_merge_export_locked)
    with pytest.raises(AssertionError, match="export decision"):
        export_invariant(worlds, "refused", "unheld")


def test_mutation_leases_released_before_the_push_ends_is_caught(worlds, monkeypatch):
    actual = Daemon._push_job

    def early(self, job_id):
        self.store.release_leases(job_id)
        return actual(self, job_id)
    monkeypatch.setattr(Daemon, "_push_job", early)
    with pytest.raises(AssertionError, match="leases released before the push ended"):
        export_invariant(worlds, "pushed", "superseded")


def test_mutation_export_annotation_not_replay_safe_is_caught(worlds, monkeypatch):
    # The base's unconditional annotation: a push job's replayed export appends its error again.
    def append(tx, job_id, error):
        tx.execute("UPDATE jobs SET export_error=? WHERE job_id=?", (error, job_id))
        tx.execute("UPDATE notices SET text=text || ? WHERE job_id=?", ("\n" + error, job_id))
    monkeypatch.setattr(Daemon, "_export_error", staticmethod(append))
    with pytest.raises(AssertionError, match="replay changed notices"):
        export_invariant(worlds, "pushed", "unheld")
