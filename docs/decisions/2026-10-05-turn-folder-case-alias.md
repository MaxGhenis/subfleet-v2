# A row names a folder that is there, spelled in full (decision, 2026-10-05)

Closes finding 1 of the re-review of 8a112986 (#134's F1 follow-up), the
case-alias race. Amends C-8.4. Builds on the admission fence for turns nested in
a tree being retired (fix/admission-nested-fence, 9e159ec9).

## What was observed

The re-review's probe (`rereview-evidence/test_rv_case_race.py` under
`~/reviews/subfleet-2110/retention-turn-p3/`) failed for a writable turn and a
read-only one on Python 3.14.7 and 3.12.14, at 8a112986 and with its F1 removed.
It fails the same way at 9e159ec9 (`logs/probe-base-9e159ec9.log` under
`~/reviews/subfleet-2110/case-alias-race/`): the nested-fence fix does not close it.

What happens, as read in the code at 9e159ec9:

1. A native conversation records the provider session's cwd as its workspace
   unspelled (`conversations/service.py`, `_open_native`). It may name the tree
   `<state>/worktrees/Job` as `…/worktrees/jOB/vendor/lib`. APFS resolves the
   name either way.
2. Submit validates the folder with `resolve(strict=True)`, which keeps the
   typed case. It then spells the turn's folder with
   `folders.canonical(git_toplevel(workdir) or workdir)` and records it
   (`write_target`, or `folder` for a reader, in `job.submitted`).
3. Retention may move the tree into quarantine between those two steps. git
   then finds no checkout, and `folders.spelling` keeps each name it cannot look
   up as given (ENOENT is no doubt), so submit records `jOB`.
4. Admission keyed the turn's row on what submit recorded. The nested-fence
   check compares `worktree:` keys exactly, so `jOB/vendor/lib` was not inside
   the fence on `Job`, and the turn was reserved while its tree was in quarantine.
5. Once the turn's attempt was quarantined and its job `lost`, retention's three
   row checks missed the row: the census (`within`, as recorded), the selecting
   transaction and the commit (`turn_holds(..., inside=True)` on the canonical
   tree). Only `worktree-in-use` ignores ASCII case, and it keeps a tree only
   while a job has not ended. So `Job` was pruned and reclaimed under a live row.

## Options

1. Refuse or delay, at submit, a turn whose folder could not be spelled in full.
2. Spell the folder again at reservation.
3. Have retention's row checks compare names that were not looked up without
   regard to case.

## Decision

Option 2, with option 1's delay moved to where the row is taken. Option 3 is
applied only to the comparisons that read a name not looked up.

- Admission spells the folder a row will name again, off the store lock, right
  before the reserving transaction (`folders.present`). This covers a turn's
  `worktree-turn:` or `worktree-read:` row and an in-place writer's `worktree:`
  lease. A folder whose every name the kernel looked up is keyed on that
  spelling, whatever submit recorded.
- When submit could not spell the folder in full, it marks the job
  (`unspelled` in `job.submitted`). What it recorded is then the path as given,
  which may be a subdirectory, because git found no checkout while the tree was
  away. Admission finds the folder again as submit finds it, from its checkout's
  top level (`git rev-parse --show-toplevel`). So a writable turn in `jOB/src`
  holds the checkout `Job`, and C-6.5 refuses a detached writer there, rather
  than keying the row on `Job/src` beside it (review of 3410b4f0, P1).
- A folder with a name that is not there now reserves no row. It is held, in
  the order below:
  - **Retention's fence on a tree it is in.** The folder is compared with every
    fence `fold`ed (`folders.retiring(..., folded=True)`), so `jOB/vendor/lib`
    finds the fence on `Job`. The turn waits `lease-held` on it as nested-fence
    turns do, and its message says retention is removing the tree.
  - **Otherwise its workspace (C-6.8).** It waits with backoff, the reason naming
    the folder, and fails after `caps.workspace_retry_max` tries.
- `worktree-in-use` compares the tree itself without ASCII case
  (`COLLATE NOCASE`), as its LIKE already compared a folder inside it. A turn
  waiting on the fence for `jOB` then keeps `Job` at the commit, and the
  retirement rolls back.
- Retention's row checks stay exact.

## Why

**Fix the producer of the spelling, not each reader.** `folders.py` rests on
one folder having one string. Every check that reads a row compares strings
exactly:

- retention's census, selecting transaction and commit;
- admission's fence (`retiring`) and C-6.5's writer exclusion;
- C-26.14's overlap of turns in one folder.

A row keyed on a string that is not the folder's one spelling defeats all of
them. Option 3 would fix only retention's three reads, and leave the others
seeing two strings for one folder.

**Rows are taken in one place, and submit is not the only source of their
spelling.** Admission keys a row on `_submitted(...)["write_target"]`, on
`["folder"]`, or, when neither is recorded, on the workspace path. A folder can
also go away after submit and come back before admission: a capacity wait can
last hours, and another tool can hold a tree aside. Spelling at reservation
covers every source. Refusing at submit covers only the submit window.

**Delay, don't refuse.** A submission that retention overtook was accepted
while its folder was there. The nested-fence rule for a turn that arrives during
a retirement is that it waits, and its queued job keeps the tree. The
overtaken turn now gets the same: it waits on the fence, `worktree-in-use` keeps
the tree at the commit, the retirement rolls back, and the next pass reserves
the row on the one spelling. A refusal would let the retirement remove the
folder of a conversation whose message had just been taken.

**Why the fence and not only C-6.8.** A parked retirement keeps its tree in
quarantine, with its fence held, across passes. The recovery resumes it, and
each pass archives for at most `SLICE_S` (120 s). C-6.8's retries are bounded:
`workspace_retry_max` 8, from 5 s doubling to 300 s, about 16 minutes. They can
run out mid-retirement, fail the turn, and let the commit remove the folder.
Waiting on the fence lasts exactly as long as the retirement.

**Why fold only where a name was not looked up.** A spelled folder is the
kernel's spelling, and so is retention's fence: exact comparison is right and
over-matches nothing. For a folder that could not be looked up, the names after
the first missing one are as typed. `fold` is Unicode's canonical caseless form,
NFD(casefold(NFD(s))) (D145). On this machine's APFS volume, nine pairs were
probed (`probes/apfs_fold.py`, `logs/apfs-fold.log`), and lookups agreed with
`fold` on each. On a case-sensitive volume, folding can also find a fence on a
different folder whose name differs only in case. The turn then only waits for
that retirement, then for its workspace.

**Why retention does not fold its row checks.** With this change, every
TURN or READER row admission reserves is spelled in full. Rows spelled otherwise
could come only from an earlier build of integrate/2111-features: release/217
has no such rows, because turns there hold `worktree:<folder>`. Folding
retention's reads would over-match on case-sensitive volumes. It would also scan
every turn row inside the selecting and commit transactions, to guard against
rows that cannot be reserved.

**Why `worktree-in-use` changes.** Its LIKE (a folder inside the tree) already
ignored ASCII case, but its `=` (the tree itself) did not. A queued turn whose
folder was the tree in another case kept nothing, so the folded wait would end
with the tree gone. A job id is `[0-9]{8}-[0-9]{6}-[a-z0-9-]+` (C-1.1), so a
tree's own name differs from an alias only in ASCII case, which NOCASE covers.

## Invariants

These are tested in `tests/unit/test_retention_case_alias.py` and
`tests/unit/test_folders.py`.

- **One spelling per row.** Every row admission reserves names a folder that
  `present` spelled in full just before the reserving transaction. A turn
  submitted under any case alias is keyed on `canonical` of its folder.
- **Checkout top.** A turn submitted while its folder was away is keyed, once
  the folder is back, on the folder submit would have recorded with the tree
  there: its checkout's top level.
- **No row on a missing folder.** If a name of the folder is not there now, the
  job takes no row and holds `lease-held` or `workspace`.
- **I5 under aliases.** No retirement commits while a live TURN or READER row
  names the tree or a folder inside it. This now holds also when the turn was
  submitted under a case alias while the tree was in quarantine. Either the turn
  waits (and keeps the tree) or its row is on the one spelling (`turn-folder`).
- **Bounded.** A turn whose folder never comes back fails after
  `caps.workspace_retry_max` tries. It never waits forever and never takes a row.
- **The two sides agree.** For ASCII names without LIKE's wildcards, the folded
  fence holds a turn exactly when `worktree-in-use` keeps the tree for its job,
  and exactly when the folder is within the tree up to ASCII case
  (differential property).
- **`present`.** Its first value is `spelling`'s. Its second is None exactly
  when every name of the path is there. A folder that is there is spelled as
  `canonical` spells any other spelling of it, and spelling that again gives
  the same string.
- **`fold`.** It ignores ASCII case and normal form, and keeps every `/`.
  Folded `retiring` names exactly what a scan of all rows with `fold` and
  `within` names, includes every key exact `retiring` names, and adds nothing for
  a probe spelled as the fences are.

## Known limits

- **Non-ASCII aliases.** A name above `worktrees/` typed in another case or
  normal form, outside ASCII, is not matched by `worktree-in-use`. SQLite folds
  only ASCII. The turn still waits on the fence and takes no row. If the
  retirement commits, the turn fails after C-6.8's retries, and no row ever
  names the removed tree.
- **Renamed trees.** A tree renamed to another case while a row names it
  (`mv Job JOB`) gives the tree a new one spelling that the row does not have.
- **Firmlinked workdirs.** A queued turn whose workdir is typed through a
  firmlink (`/System/Volumes/Data/Users/…`) is held by the folded fence, since
  its folder is spelled through the kernel. `worktree-in-use` compares the job's
  `workdir` string, so on this branch it does not keep the tree. The turn takes
  no row, and fails after C-6.8's retries if the retirement commits (review of
  3410b4f0, P3). MaxGhenis/subfleet-v2#140 records `jobs.workdir` canonically at
  submit, for writers and turns alike, which keeps the tree for a workdir that
  exists at submit. What is left is a non-ASCII name in a tail submit could not
  look up (above).
- **Earlier builds.** Rows reserved before this change by a build of
  integrate/2111-features are compared as before.
