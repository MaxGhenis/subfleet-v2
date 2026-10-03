# Retention #81: defect classification and sweep follow-up

2026-10-01 findings; sweep follow-up 2026-10-03.

The narrative classification report referenced by revision 5 of
[`retention-archive.md`](../desktop/retention-archive.md) was absent from the
checkout supplied for the continuation. This report summarizes the committed
evidence in [`2026-10-01-retention-81-notes/`](2026-10-01-retention-81-notes/)
and documents the remaining defect 2 fix. Historical results below are
attributed to those artifacts; they are not new runs of the old experiments.

## Classification

The required invariant is that retention deletes no bytes its archive does
not hold and never orphans a tree. All four original findings violate that
invariant or prevent a finished job from ever reaching safe retirement.

| Note | Failure and consequence | Protection documented in revision 5 |
|---|---|---|
| 1: hard links | A same-size rewrite with the old mtime restored passed the comparison when ctime was ignored for a file with other links. The archived old version survived, but the new bytes were deleted. | Compare ctime; when only ctime changed on a file that had other links, rehash and delete only if its bytes equal the archive. Verify the digest around its read. |
| 2: sweep quarantine | Retiring while a tree was temporarily elsewhere could drop its owning rows, delete its admin directory while a live tree still named it, or remove the returned tree without bundling its private HEAD and reflog history. | Keep a tree found in a sibling quarantine, record initial presence, check presence before quarantine and before committing, and find moved registrations by their backlinks. The lookup round trip remaining in 43f09ea4 requires the revision-6 protections below. |
| 3: unrecorded allocation | `_workspace` created `worktrees/<job id>` before `jobs.worktree` was recorded. A job cancelled in between retired without removing or owning that tree. | Treat the daemon's allocation as the job's tree even when the row is NULL; retain its expected path when a writable git job's tree is temporarily absent. |
| 4: missing workdir/source | Looking up a registration through a missing workdir found no repository. A job without salvage retired without its private history; one with salvage could be deferred forever despite an available repository. | Discover the source through an existing ancestor, known job repositories, or verified salvage refs. Keep the rows when no source can be discovered; verify inferred salvage commits against their recorded digests. |

[`baseline-tests.txt`](2026-10-01-retention-81-notes/baseline-tests.txt)
identifies its baseline as **52520723**, not 43f09ea4. Its real-git probes
record note 2 in three forms: a tree kept away for the whole pass returned
without an owning row; a tree returned before quarantine was removed with
its private commit absent from the bundle and subsequently absent after
prune/gc; and a tree moved away after `begin` retained a gitfile pointing at
the deleted admin directory. The same artifact records four expected pytest
failures for the original notes. The baseline observations are also in
[`observed-at-52520723.txt`](2026-10-01-retention-81-notes/observed-at-52520723.txt).

[`observed-at-6921884e.txt`](2026-10-01-retention-81-notes/observed-at-6921884e.txt)
records the earlier protections preserving the tree or rewritten bytes in
the probed schedules. The historical
[`final-mutation-results.txt`](2026-10-01-retention-81-notes/final-mutation-results.txt)
reports 25 valid mutants killed (M1–M24 and M7b), with explicit assertion
failures rather than collection failures or timeouts. Those results cover
the original note fixes and their completion cases; they do not establish
coverage of the remaining registration-lookup round trip.

## Defect 2 remaining in 43f09ea4

The continuation brief reports that an independent review of 43f09ea4
found this schedule:

1. Retention observes the original tree present at `begin`.
2. The sweep runs `git worktree move` to
   `<parent>/.disk-guard-removing.<name>` while retention reads the gitfile.
   The lookup sees no gitfile and records no registration.
3. The sweep moves the tree back before retention's quarantine check.
4. The presence comparison sees the tree present both times. Retention
   removes the tree after archiving its files without bundling its private
   HEAD and reflog commits. Its admin directory names the removed tree.

This is a data-loss and orphaning defect, not an accepted entry-unlink
micro-race: a complete round trip can occur between ordinary retention
steps. Untracked and ignored bytes can already be copied while the private
commit graph remains missing. The stash is checked separately: `refs/stash`
lives in the shared common directory and must survive retirement there.

The chief-of-staff scripts were read without running or editing them.
`disk-guard.move_worktree` runs `git worktree move` without `--force`;
`move_back` refuses an occupied original path. Both tools write a recovery
record before moving, recheck under the quarantine name, and move back on
failed checks. Both can restore a leftover quarantine on a later pass; the
archive sweep's destructive `removing` stage is completed instead. Their
shared guard lock does not serialize retention.

## Revision-6 design

The fix retains the registration selected at `begin` and proves that it
belongs to the directory actually quarantined. It does not attempt to share
the external tools' lock or depend on observing a move while it happens.

- Read the worktree's gitfile once and pass those same bytes into
  `registration`; record its sha256 or its absence alongside the tree's
  device/inode and the admin directory's device/inode.
- After the quarantine renames, verify those identities and the admin
  `gitdir` backlink against the original worktree path. A tree that vanishes
  between the presence check and rename also defers. A gone tree must remain
  absent, including from a sibling quarantine.
- Repeat the identity check as the final check's last operation. A mismatch
  rolls the trees back, releases retention's own lock, and keeps the job to
  retry. Pre-commit journals lacking identity follow the same retry path.
- Immediately before deleting an admin directory, preserve it if a live
  checkout names it at the original path, a sibling quarantine, or the path
  named by its current backlink. Release only retention's own lock in that
  case so the surviving checkout remains usable.

The implementation is in `subfleet/retention_archive.py` (`begin`,
`quarantine`, `_identity_changed`, `_final_check`, `_reclaim`, `_claimed`)
and `subfleet/retention_git.py` (`gitfile_admin`, `registration`, `backlink`).
The identity check intentionally defers the missed-gitfile lookup rather
than rediscovering and bundling under a different registration in the same
attempt. The later quiet pass reads a consistent registration and uses the
existing verified archive/bundle pipeline.

## New regression coverage

[`test_retention_sweep_race.py`](../../tests/unit/test_retention_sweep_race.py)
uses real repositories, actual git worktree moves, and temporary state roots.
It constructs a private detached HEAD, a commit retained only through the
HEAD reflog/`ORIG_HEAD`, a stash, and untracked and ignored file bytes. Its
reproduction moves away exactly at the gitfile read and back immediately
after the lookup or before quarantine; a later quiet pass must retire safely.

Additional regressions cover a move after the presence check but before the
rename, a gitfile changed before archival, a gone registration moved or
repaired elsewhere before the lock, a copy replacing the original directory,
a checkout returned after the final check, and an older journal lacking
identity.

The Hypothesis property generates away/back actions at 13 boundaries, with
explicit examples for the lookup window and moves left away. Its oracle
requires either an intact, usable retained tree or a removed tree whose
private commits can be imported from the verified bundle into a fresh clone
of the remote and whose untracked/ignored files match their archive copies.
It rejects gitfiles naming missing admin directories and admin backlinks
naming vanished trees without a bundle; it also checks the shared stash
ref. After the sweep restores any held tree, a quiet retention pass must
finish, preventing safe-but-permanent deferral from satisfying the property.

Current baseline, property, mutation and suite results belong to the
continuation's validation record and must be distinguished from the
2026-10-01 artifacts above.
