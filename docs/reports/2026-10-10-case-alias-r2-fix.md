# #141 fix round 2 (review of 2cbf6fca)

Base: `2cbf6fca1`, #141 with release/217 (4c6a8a9c9: #158, #144, #139) merged.
The work stayed in the assigned checkout. There were no network remotes,
sub-agents, history rewrites or pushes. Commits are on a detached HEAD in this
workspace.

## P2-A: an `unspelled` turn's look and its reservation read one folder

The look before the workspace (`_fence_hold`) read the folder submit recorded.
An `unspelled` turn's reservation instead read the folder admission found again
with `_row_folder`, after `_workspace` had prepared the workspace. Take a
repository nested in a job's tree, quarantined during submit and put back before
admission, whose persisted `core.worktree` names a fenced top elsewhere. That
turn prepared its workspace under the fence.

- `subfleet/daemon.py:2603` adds `_rediscovered(job)`. For a turn or in-place job
  whose row is `unspelled`, it finds the folder again as submit does
  (`_row_folder`: `git rev-parse --show-toplevel` in the job's folder, a second
  time under a `.git` git did not take). Otherwise it returns None, and always
  None for a push job.
- `subfleet/daemon.py:5208` asks it once per look, before the look. The look
  (`:5218`) and the reservation's `derived` override both use that one answer.
  `_turn_fences` stays the one comparison.
- `subfleet/daemon.py:2637`: `_turn_folder` is `_rediscovered or
  _recorded_folder`, the folder the reservation names. `_recorded_folder`
  (`:2646`) is what submit recorded, with no git.
- `subfleet/daemon.py:5213`: when that git cannot finish (C-6.8), a turn whose
  recorded folder is fenced still waits on the fence, as before git was asked.
  Any other turn waits for its workspace.

This lookup is the only git the look runs, measured twice:

- A standalone script ran `git_toplevel` in a linked worktree, in a repository
  nested in it, and in that repository with `core.worktree` naming an external
  top (git 2.55.0, APFS). It recorded mtime, ctime and atime of every entry in
  the tree (nested `.git` and its index included), the linked worktree's admin
  directory, the main `.git` and the external top. No mtime or ctime moved.
  One atime moved: the nested `.git/config` in the external case, read for the
  first time since `git config` wrote it. Retention does not sign atimes.
- `test_finding_an_unspelled_turns_folder_again_writes_nothing_where_it_looks`
  checks the same through admission. It covers the tree, nested and external
  cases, writable and read-only, each under its fence. It asserts the turn is
  held, its workspace is never prepared, and the only git is one or two
  `rev-parse --show-toplevel` in the turn's own folder. Contents, modes, mtimes
  and ctimes of the tree, admin directories and external top are unchanged.

## P2-B: a push job is never `unspelled`

- `subfleet/daemon.py:2593`: with a validated `top`, `_row_folder` returns
  `(folders.present(top)[0], False)`. `present(top)[0]` is the string
  `canonical(top)` gave on release/217, so only the absence flag changed.
- `_rediscovered` (`:2622`) returns None for any job with a `push_branch`, as
  defence in depth for rows 2cbf6fca wrote.

A push job whose checkout is not there at admission behaves as on release/217.
`_workspace`'s in-place recheck (`host_push.validate_write_location`, metadata
read as data) refuses it. The job fails with exit 7 and the notice "workspace
refused: push requires a committed Git checkout with an origin". It gets no
attempt and no lease, and no git runs on the checkout. The last test in
`tests/unit/test_host_push_validated_top.py` pins this, and it is run against
release/217 too.

## The property

`tests/unit/test_retention_shared_folders.py:633` now draws `row` from
`spelled` and `unspelled`.

- `unspelled` rows use real repositories. When `separate` is set, the cwd's own
  repository has a persisted `core.worktree` naming the Git tree, an external
  top.
- The row is written by the real `_row_folder` while both trees are in
  quarantine, and the trees are put back before admission looks. Each example
  then runs with the trees there, then moved aside again.
- Rediscovery and the workspace snapshot run for real on this branch.
- The look is now the admission pass's own (`_admit_turns` with the real
  `_fence_hold`). The test asserts the look prepares no workspace exactly when
  fences exist. It also checks the hold's fence keys and folder against the
  reserving transaction and an independent model of the folder each row names.
- Its run uses 200 generated examples plus 12 explicit ones, 6 of them
  `unspelled`.

## Tests

- `tests/unit/test_retention_rediscovery.py` adopts the review's turn probes
  (`probe_turns.py:63` and `:90`), its mixed-absence-flags regression
  (`probe_mutations.py:11`) and its C-8.4 wording probe (`probe_contract.py:10`).
  Only names and docstrings changed. It adds the measurement and the
  git-cannot-finish path.
- `tests/unit/test_host_push_validated_top.py` adopts the three push probes
  (`probe_push.py:34`, `:52` and `:69`). Each runs on a `fresh` row and on a
  `legacy` row with `unspelled` planted, and records every git process at
  `subprocess.Popen`. The probes' setup had asserted the defect's precondition
  (`unspelled` True). It now asserts the row this daemon writes.
- `tests/unit/test_retention_case_alias.py:236`-`:342` add round 1's P3
  regressions:
  - a folder renamed in case before reservation is spelled again there;
  - a folder below a renamed name is spelled again in full;
  - an in-place writer's lease is keyed on the spelling at reservation;
  - an absent nested writer under a parent fence waits on it, with typed `Job`
    and `jOB`;
  - C-8.4's known limit for a parent typed beyond ASCII case (`Ünï`/`ünï`).

## Contract

C-8.4 gains these changes:

- The look's folder for an `unspelled` turn, and the read-only lookup as the
  only git it may run in a tree being retired.
- What the look's hold shares with the transaction's: the fence keys and their
  order, the folder, the signature, and the `waiting` row and clock. This holds
  when retention's fences are the only blockers. With another blocker, the
  transaction lists that key as well.
- Its `worktree-in-use` known limit now covers every name of the tree's path,
  parents included. Folded fences hold such a turn. SQLite's `NOCASE` and `LIKE`
  do not keep the tree.

C-8.5 gains the push write target rule. There is also a changelog entry.

## Results

Pending: one serial lock session runs the new tests, mutants, before/after runs
and the requested suites. See the next commit.
