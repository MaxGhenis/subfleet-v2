# Snapshots of a sparse checkout, 2026-10-09

## What happened

Job `20261009-133338-closes-with-sol` was a hard-tier review, writable and in
place, submitted at 17:33:38Z in
`~/chief-of-staff/.claude/worktrees/adoring-boyd-93d72c`. It failed at 17:38:52Z
with rc 1 and no attempt:

```
workspace preparation failed: SalvageError: git add failed: The following paths and/or pathspecs matched paths that exist
outside of your sparse-checkout definition, so will not be
updated in the index:
.review-scratch/opus/baseline.txt
```

That worktree is a sparse checkout, cut as chief-of-staff's README says to cut
one (`git sparse-checkout set --no-cone '/bin/' '/tests/' '/skills/' '/docs/'
'/README.md' '/.gitignore'`). The repository tracks 332,176 files, and its
README puts a full checkout at about 15 GB; a sparse one holds 59 files. The
worktree held one untracked file outside the patterns,
`.review-scratch/opus/baseline.txt`.

Writable jobs take a snapshot of the working tree before every admission
(C-6.8) and again at finalization (C-13.1). The snapshot records the worktree
into a temporary index with `git add -A`.

## What was reproduced

Everything below was run, with git 2.55.0, against `main` at c230d1d8 and the
release line at 694723d9, in a cone and in a pattern (no-cone) sparse checkout.
The results were the same in all four.

| Case | Before |
|---|---|
| An untracked file outside the patterns | `working_tree()` and `salvage()` raise the error above. The job fails before launch, or its finished attempt cannot be salvaged. |
| A sparse worktree nobody edited | The snapshot equals the baseline's tree, read from the real index or into an empty one. |
| The same, with `--sparse` added to `add -A` and nothing else changed | The snapshot drops every file the patterns keep off disk, and `salvage()` writes a ref whose commit deletes them. |
| A tracked file outside the patterns that the job wrote | `git status` shows it modified. `add -A` exits 0 and records nothing for it. The snapshot equals the baseline, so salvage keeps none of that work. |
| Edits inside the patterns | Recorded correctly. |

So the first report was right. The second, that clearing the skip-worktree bits
makes `add -A` record every file left out as a deletion, did not reproduce in
the code as it stood: without `--sparse`, git's `add` leaves every path outside
the patterns alone, the deletions included. It is exactly what the obvious fix
does, though. The fourth row is a defect nobody had reported: work lost without
an error.

On a real sparse worktree of chief-of-staff (332,176 index entries, 332,117 of
them off disk), before the fix: the untracked file raised; a write to `PLAN.md`
(tracked, outside the patterns) beside an edit to `README.md` snapshotted as
the `README.md` edit alone.

## Cause

git 2.34 changed `add`, `mv` and `rm` to "avoid updating paths outside of the
sparse-checkout definition unless the user specifies a `--sparse` option" (its
release notes). For `add -A` that is two behaviours: an untracked file outside
the patterns is an error (exit 1), and a tracked one is passed over (exit 0).

`--sparse` makes `add` read every path, and then the snapshot's own preparation
is wrong for a sparse checkout. It clears every skip-worktree bit in the
temporary index, because a bit hides a file's edits from `add`. With the bit
cleared, a file the patterns keep off disk is a tracked file that is gone.

## Fix

In a sparse checkout (`core.sparseCheckout` on for the worktree):

1. The real index is listed. Each entry marked skip-worktree whose file is
   absent is one the patterns keep off disk (`_off_disk`).
2. Those entries are written into the temporary index as the real index holds
   them, each with its bit set (`_overlay`). Every other bit is cleared as
   before.
3. The worktree is recorded with `add -A --sparse`. It leaves an entry whose
   bit is set alone and reads every other path from disk.
4. The temporary index is a full one, whatever the real index is: git runs on
   it with `index.sparse` off (`_full_index`). See the next section.

The entries come from the real index, not the baseline, so a job that checks
out or commits newer work gets that work's files outside the patterns. If the
real index is missing or cannot be listed, or the entries cannot be written,
the snapshot raises. A checkout that is not sparse runs the same commands as
before, plus one `git config` read.

## A defect in the first version of the fix

The first property below (nobody edited it) failed on the fix as first
written. Hypothesis reduced it to a repository with one empty file, `a/f`, in
a cone checkout with a sparse index and nothing in the cone.

A sparse index holds one directory entry (`040000 a/`) in place of the files
under a directory outside the cone, and the temporary index, a copy of it, did
too. Step by step, with git 2.55:

1. `read-tree -m <baseline>` leaves the directory entry in place.
2. `update-index --index-info` with `a/f` exits 0 and adds the file beside the
   directory entry that already stands for it.
3. `write-tree` writes a tree that names `a` twice. `git fsck` reports
   `duplicateEntries`.

The read into an empty index gave the right tree, so the two reads disagreed,
which is what the test saw. Sparse-index checkouts with more than one
directory had given the right tree from the same commands, so the example
tests that used one had passed. With `index.sparse` off in the environment of
every command that touches the temporary index, git writes a full index at
each step (checked in the file: no `sdir` extension after any of them), and
both reads give the baseline's tree. The real index is only read and stays
sparse.

## Invariants

1. **Nobody edited it.** For any tracked files, any patterns, and a cone, a
   cone with a sparse index, or patterns: the snapshot of a sparse worktree
   with no edits is its baseline's tree. This holds read from the real index
   or into an empty one, from the top level or a subdirectory. (Hypothesis)
2. **Sparse is full.** For any sequence of edits in and outside the patterns,
   staged or committed along the way, the sparse worktree's snapshot is the
   tree a full checkout with the same edits snapshots to. (Hypothesis,
   differential)
3. **The snapshot reads the worktree as git does.** For any edits to files,
   the paths at which the snapshot differs from the baseline are the paths
   `git status` reports. (Hypothesis, differential against git)
4. **Both reads agree.** Seeded from the real index or read into an empty one,
   the tree is the same (C-6.8). Every test above checks both.
5. **Snapshots touch nothing.** HEAD, the real index's bytes, its skip-worktree
   bits, `git status` and every file are as they were.
6. **Not sparse, not changed.** With `core.sparseCheckout` off, `add -A` gets
   no `--sparse`, the real index is not listed, and a skip-worktree file that
   is gone is still a deletion.
7. **Fails closed.** No index, an index git cannot list, or entries that
   cannot be written: a `SalvageError`, and no salvage ref.

One case is intended and is not a full checkout's. A tracked file outside the
patterns that a job writes and then removes is off disk with its bit set,
exactly as if never written. git reports it unchanged, and so does the
snapshot. `git rm --sparse` removes the index entry, and the snapshot then
records the deletion. Invariant 3 covers this case; invariant 2 leaves it out.

## Measured on chief-of-staff

One sparse worktree, 332,176 index entries, at a load average near 190:

| | Before | After |
|---|---|---|
| Nobody edited it | 4.2 to 4.5 s, equal to HEAD's tree | 4.8 to 6.1 s, equal to HEAD's tree |
| An untracked file outside the patterns | `SalvageError` | one path added |
| `PLAN.md` written (outside), `README.md` edited (inside) | `README.md` only | both |
| Peak memory of the snapshot's process | 331 MiB | 473 to 488 MiB |

Both lines give the same three trees. In two timed runs, writing the entries
took 1.2 and 2.1 s and setting their bits 1.0 and 1.5 s; `add -A --sparse` then
took under 0.2 s, where `add -A` had taken 1.7 and 1.9 s. In a worktree whose
index is at the baseline, the temporary index already holds every one of those
entries with its bit before they are written (`read-tree -m` keeps the bits of
the copied index). Writing only the entries that differ is a possible
follow-up; it is not done here.

## Not covered

- A job that is not in place gets a full `git worktree add` of the caller's
  head, whatever the caller's checkout is (`Daemon._workspace`). From
  chief-of-staff that is the 15 GB checkout. MaxGhenis/subfleet-v2#82 (C-6.14,
  sparse job worktrees) is the open work.
- In a sparse checkout the snapshot needs a git whose `add` has `--sparse`
  (2.34 or later).
- On `main`, retention's own check of a worktree the daemon cut
  (`retention._remove_worktree`) still records it with plain `add -A`. Those
  worktrees are full checkouts. If a job made its own worktree sparse and left
  an untracked file outside the patterns, that `add` fails and
  `_remove_worktree` raises before it removes anything.
