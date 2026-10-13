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

In a sparse checkout (`core.sparseCheckout` on for the worktree; an unset key
reads as off, and any other failure to read it fails the snapshot):

1. The real index is copied to a private file, and the copy is listed. Each
   entry marked skip-worktree whose file is absent is one the patterns keep
   off disk (`_off_disk`).
2. The temporary index reads the baseline, with those entries as the real
   index holds them (`_sparse_base`). When every one of them is the
   baseline's, as when no job moved the index past it, that is the baseline
   itself. Otherwise it is a tree written from one stream of entries in index
   order.
3. Those entries get their skip-worktree bit, and every other bit is cleared
   as before (`_clear_skip_bits`). An entry the temporary index does not hold
   fails the snapshot.
4. The worktree is recorded with `add -A --sparse`. It leaves an entry whose
   bit is set alone and reads every other path from disk.
5. Every git command that touches the temporary index runs with
   `index.sparse=false` last in `GIT_CONFIG_PARAMETERS`, so that index is
   always a full one (`_full_index`).

The entries come from the real index, not the baseline, so a job that checks
out or commits newer work gets that work's files outside the patterns. If the
real index is missing or cannot be listed, the baseline cannot be listed, or
the entries cannot be written or marked, the snapshot raises. A checkout that
is not sparse runs the same commands as before, plus one `git config` read.

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
tests that used one had passed. With `index.sparse` off for every command
that touches the temporary index, git writes a full index at each step
(checked in the file: no `sdir` extension after any of them), and both reads
give the baseline's tree. The real index is only read and stays sparse.

## Review of the first version

An independent review (GPT-6.1 Sol, 2026-10-10, of the release-line port)
asked for changes. Every finding was reproduced here before it was fixed.

| Finding | What was wrong | Now |
|---|---|---|
| P1 | `index.sparse=false` was passed as `GIT_CONFIG_COUNT` settings. An inherited `GIT_CONFIG_PARAMETERS="'index.sparse=true'"` takes precedence over those, and the snapshot was the malformed tree again; `salvage()` wrote a ref to it. | The setting goes last in `GIT_CONFIG_PARAMETERS`, where git takes the last value over every other source. Tested with the setting inherited both ways. |
| P2 | `core.sparseCheckout` was read as an optional lookup, so a config read that failed was taken as "not sparse", and the snapshot lost a file a job wrote outside the patterns. | `--default=false` answers an unset key; any other failure raises. |
| P2 | The real index was checked to exist, then listed. Removed in between, git listed it as empty, and the snapshot recorded every file left out as deleted. | The index is copied once and the copy is listed. |
| P2 | Entries were written into an index that already held the baseline. git moves the rest of the index for each entry it inserts: 21 s for 332,176 paths the baseline did not have. | One sorted stream, appended in order (step 2 above). |
| P2 | The release line's finalization test wrapped `_add` with four arguments; the port passed five. | The test's wrapper takes and passes `sparse`. |
| P2, P3 | The report and the tests claimed more than holds: a sparse snapshot is not a full checkout's for every edit, and the `git status` invariant fails with `sparse.expectFilesOutsideOfPatterns` set. | The exceptions are stated below and pinned by tests; each property's docstring states its domain. |

## Invariants

Each property holds for every input of the domain its Hypothesis strategy
draws from, which its docstring states.

1. **Nobody edited it (Hypothesis).** For tracked files at up to three levels
   under four top-level directories (names with spaces, tabs, newlines,
   quotes and non-ASCII), patterns keeping all, some or none of those
   directories, and a cone, a cone with a sparse index, or patterns: the
   snapshot of a sparse worktree with no edits is its baseline's tree. This
   holds read from the real index or into an empty one, from the top level or
   a subdirectory, and the real index's bytes are unchanged.
2. **Sparse is full (Hypothesis, differential).** For up to eight edits drawn
   from: tracked files written in and outside the patterns; files created,
   and symlinks made, in directories in and outside them; tracked files in the
   patterns removed or made executable; a path staged with `--sparse`; a
   commit. The sparse worktree's snapshot is the tree a full checkout with the
   same edits snapshots to.
3. **The snapshot reads the worktree as git does (Hypothesis, differential
   against git).** For up to eight file edits as in 2, plus removing a tracked
   file outside the patterns: the paths at which the snapshot differs from the
   baseline are the paths `git status` reports. This holds with no
   assume-unchanged bit and with `sparse.expectFilesOutsideOfPatterns` unset.
   With it set, git trusts a skip-worktree bit over a file on disk; the
   snapshot reads the file, which a test pins.
4. **Both reads agree.** Seeded from the real index or read into an empty one,
   the tree is the same (C-6.8). Every test above checks both.
5. **Snapshots touch nothing.** HEAD, the real index's bytes, its skip-worktree
   bits, `git status` and every file are as they were.
6. **Not sparse, not changed.** With `core.sparseCheckout` off, `add -A` gets
   no `--sparse`, the real index is not listed, git is given no setting, and a
   skip-worktree file that is gone is still a deletion.
7. **Fails closed.** Fail any one git call a snapshot makes (git exiting 128,
   or no git at all), at every position in the sequence, for both reads, with
   the real index at the baseline and at a newer commit: the snapshot raises
   or returns the tree it returns when nothing fails, and the real index's
   bytes are unchanged. A timeout always raises.

**Intended exceptions.** A file outside the patterns that is not on disk is
recorded as the real index holds it and is not read. So, where a full
checkout would record something else, the snapshot records what `git status`
in the sparse worktree reports, and tests pin each case:

- a tracked file outside the patterns that a job writes and then removes
  reads as never written (`git rm --sparse` removes the entry, and the
  snapshot then records the deletion);
- a repository cloned where a directory the patterns left out was: the files
  it does not hold read as unchanged;
- attributes changed so that such a file would be read differently (CRLF
  normalization, a clean filter): the file keeps its blob.

A file or a symlink written where such a directory was does replace it, as in
a full checkout.

## Measured

On a real sparse worktree of chief-of-staff at c854ffef7 (332,176 index
entries, 332,117 off disk), at load averages between 89 and 115:

| | Before | After |
|---|---|---|
| Nobody edited it, seeded read | 4.5 s, 332 MiB peak | 3.6 to 4.3 s, 367 to 378 MiB |
| Nobody edited it, read into an empty index | | 5.5 to 6.2 s, about 500 MiB |
| An untracked file outside the patterns | `SalvageError` | one path added |
| `PLAN.md` written (outside), `README.md` edited (inside) | `README.md` only | both |

Memory is the peak RSS of the process taking the snapshot.

The review's case: a real index holding N files under `a/`, all off disk, and a
baseline holding N other files under `z/`. Timings on this shared host vary
with what else runs; these were taken at load averages between 59 and 65, the
full checkout's at 125:

| N | First version | Now, seeded | Now, empty-index read | Full checkout of the same change |
|---|---|---|---|---|
| 128,000 | | 4.5 to 4.6 s, 175 MiB | 5.0 to 5.5 s | 82 s |
| 332,176 | 21 s inserting the entries, 77 s for the review's whole probe | 15.2 to 16.0 s, 410 MiB | 14.2 to 15.2 s | stopped by the 60 s cap on one git call |

At N = 128,000, building the index of the off-disk entries takes 0.2 s and its
tree 1.4 to 2.0 s; `add -A --sparse`, recording the baseline's files as
deleted, takes 1.9 to 2.0 s. A full checkout of the same change records the
same deletions. `git add -A` recording N deletions took 0.5, 1.0 and 2.0 s for
N = 32,000, 64,000 and 128,000: linear. While other test suites ran on the
host, the same snapshots took up to 50 s at N = 128,000 and 79 s at
N = 332,176.

## Not covered

- A job that is not in place gets a full `git worktree add` of the caller's
  head, whatever the caller's checkout is (`Daemon._workspace`). From
  chief-of-staff that is the 15 GB checkout. MaxGhenis/subfleet-v2#82 (C-6.14,
  sparse job worktrees) is the open work.
- In a sparse checkout the snapshot needs a git whose `add` has `--sparse`
  (2.34 or later).
