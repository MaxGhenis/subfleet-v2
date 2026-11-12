# Retention delete path for kept trees (d2660, C-8.7)

Max ruled on d2660 on 2026-11-09: delete the kept trees of all 79 finished
Subfleet jobs (25.00 GB private), keeping their commits, records and outputs,
by a reviewed delete path through the daemon. This report covers the build
of that path on `feat/retention-delete-d2660` (PR #302) against the hub's
brief (`~/reviews/subfleet-hub/d2660-delete-path/brief-build.md`). The hub
built it in its own session, starting at 16:41Z, because Fleet ops asked for
the one job to run "through a single pinned admission or by building it
in-session". Nothing here has run against the live store: the install, the
dry run over the 79, the canary and the full run come after review.

## November 12 review fixes

Codex's primary and commit-preservation reviews found counterexamples in the
original head `dba9ac3ca`: a newly added Git root escaped the keep plan, a
substituted tombstone escaped the earlier identity check, configuration
includes read excluded files, multi-value pseudo-refs lost later entries,
remote-tracking refs incorrectly stood for proof of remote possession, and a
restart could make marker facts differ from their single event.

The working-tree fixes save the root/commit census, recheck it after the
rename and at the destructive boundary, bind descriptor removal to the saved
identity, restore complete bundles into empty repositories, collect every
pseudo-ref entry, reuse durable marker/event facts, and put exclusion-aware
configuration/storage checks before classification and every later Git
command. The repository census also includes available unreachable/dangling
commit objects. Unresolved legacy discovery keeps the candidate. New focused tests
cover those cases. Combined validation and fresh independent review remain
pending; this report does not record an installation or a live deletion.

The commands, counts and mutant results below are the **original build's
evidence**, at the heads each row names. They do not validate these later
fixes. The mechanism sections describe the current working-tree code.

## Summary

- `subfleet/retention_delete.py` (new, about 2,100 lines) holds the path:
  request checks, twelve gates, the commit-keeping step, a durable journal, a
  rename to a tombstone, re-checks, the removal, registration clean-up, a
  marker and one event. The daemon steps it on the retention worker
  (`subfleet/daemon.py`), the `retention.delete` op reaches it
  (`subfleet/protocol.py`, `subfleet/refusals.py`), and
  `subfleet retention delete` drives it (`subfleet/retention_cli.py`).
- `subfleet/retention_marks.py` (new) holds the marker and journal names that
  the normal pass (`subfleet/retention.py`) and the survey
  (`subfleet/retention_survey.py`) read, so they keep a deleted job for good.
- Original build tests: 5 new files, 50 test functions, 120 collected cases (apply 13,
  daemon 16, Hypothesis properties 2, resume 22, request, CLI and Excluder
  67); every one passed in its last run (see the commands below).
- Mutants: the brief's 15, 4 for defects the tests found, and 1 that must
  survive. 19 of 20 killed, 20 of 20 as expected.
- Docs: C-8.7 in `docs/acceptance-contract.md`, section 18 of
  `docs/desktop/retention-archive.md`.
- Testing found five defects in the first build, and the build fixed all
  five (listed under the commit-keeping rule and the journal).

## Design: a sibling class, and what it reuses

The brief allowed a delete mode on `Retirement` or a small sibling class on
the same primitives. I wrote the sibling class, `DeletePath`, for three
reasons:

1. `Retirement` exists to archive before it deletes. Its journal (`selected`
   through `quarantined`, `archived` and `committing`) and its readback prove
   that the archive holds every byte before anything goes. A delete mode would
   have to skip exactly that guarantee inside the class that promises it.
2. `retention_archive.journals` reads every journal under
   `<state>/retention/` as a retirement. This path's journals live under
   `<state>/retention-delete/` so the pass never mistakes one for its own
   (mutant 15).
3. The commit-keeping rule here (refs for linked worktrees, verified bundles
   for repositories inside) differs from the archive's anchor-and-bundle of
   every file, and the pass must stay unchanged for the jobs it still retires.

What it reuses, unchanged:

- **Detection.** `retention_survey.job_info` decides gate 7, so the path
  deletes only what the survey itself still calls `nested-linked-worktree` or
  `remote-less-history`.
- **Pins.** `retention._pin_reasons(only=job)` with exclusion-aware hosted
  registration discovery, plus the reads `_Pass._start` makes when it
  selects a job: `folders.turn_holds(inside=True)`,
  `dependencies.dependents`, `folders.exclusive_inside`
  (`DeletePath._pinned`).
- **Holders.** libproc working directories (`procs`) and `lsof_holders` with
  a `Watch` on the tree's prefixes and inode; a failed scan skips the job.
- **Leases and fences.** `retire:<job>` and `folders.exclusive_key(folder)`
  (`worktree:<folder>`) under the holder `retention:<job>`, the holder a
  retirement uses, so `folders.retiring` fences admission off the tree and
  every folder inside it.
- **Filesystem.** `retention_fs` descriptor walks and removals (never
  following a link, never crossing a device), `rfs.read_regular`,
  `rfs.write_atomic`, `rfs.sync_path`.
- **Git.** `rgit.run`, `rgit.object_format`, `rgit.admin_objects`,
  `rgit.network_remotes`, `rgit.held_arguments`, `rgit.held_commit`,
  `rgit.create_bundle`. The delete path now verifies complete bundles by
  restoring them into an empty repository without source alternates.
- **Scheduling.** The retention worker's key and `_unrun_lock`, which the
  hourly pass and the unused-tree sweep already take.

## The gates and where each lives

All in `subfleet/retention_delete.py`, in the order of `GATES`. Gates 1 to 4
run once before the leases (so a failing job names its gate) and again once
the path holds the leases. The rest run while it holds them.

| # | Gate | Where | Refuses |
|---|---|---|---|
| 1 | listed | `DeletePath._first_gates` | a job not in the request |
| 2 | finished | `_first_gates` | no row; a turn; a non-terminal state; a live attempt; an unreadable or in-flight retirement journal; a deletion journal; a marker |
| 3 | age | `_first_gates` | `created_at` unreadable or under `min_age_days`, unless `age_exempt` |
| 4 | owned path | `_first_gates` via `retention._owned_worktree` | an in-place job; another sandbox; a symlinked path; a path at or outside `<state>/worktrees`; missing; not a directory; another volume |
| 5 | excluded, top level | `DeletePath._top`, `borrowed` | the job's workdir, the tree's `.git` (a symlink, or a gitfile naming an excluded place), its registration's repository, or borrowed objects in an excluded repository |
| 6 | excluded, nested | `layout` (`exclude-nested`), `_gates`, `GitReadGuard` | a nested gitfile, registration or alternates naming an excluded repository; unreadable or symlinked Git metadata; configuration outside the plain checked subset |
| 7 | class | `_gates` via `retention_survey.job_info` | an issue other than the two classes, or no issue |
| 8 | pins | `_pinned` | any pin the pass would honour; asked again before the rename and before the removal |
| 9 | holders | `_holders` | a process with its working directory or a file open in the tree; asked again after the rename |
| 10 | locks | `_gates` | a registration carrying git's `locked` |
| 11 | outside | `_gates` | a repository inside registering a worktree outside; a repository borrowing objects from inside |
| 12 | free space | `_gates` | `statvfs` free under `min_free_gb` |

An exclusion input preflight precedes reused path helpers. The complete
layout/configuration proof precedes classification because the survey invokes
Git. A root whose commits the path cannot keep (`plan.problems`) fails the
job with gate `keep`, before anything moves.

`Excluder` resolves pointers component by component and refuses an excluded
component before it stats it. `GitReadGuard` checks local configuration and
metadata before classification and every later Git command. It rejects
includes, unknown configuration forms and metadata symlinks, and checks
commondir/object/alternate storage before following it. Its command context
suppresses ambient config and init templates for that caller/thread without
changing the daemon environment. A tree whose top-level gitfile names an
excluded repository is refused before the tree walk. Unresolved legacy
dependency discovery keeps the candidate rather than dropping a possible pin.
Persisted worktree, tombstone, registration and rollback data paths are
resolved again before following them. The daemon's private state directory,
request and journal control files remain trusted; this does not promise
integrity against arbitrary same-user replacement of those control files.

## The commit-keeping rule

Before anything moves, every commit the tree alone holds gets a home outside
it (`_plan_keep`, `_keep`):

- **A linked worktree whose repository lies outside the tree.**
  `admin_commits` collects the commits its admin files name: HEAD,
  pseudo-refs, per-worktree refs (`refs/worktree/`, `refs/bisect/`,
  `refs/rewritten/`) and the HEAD reflog. `independent` reduces them to tips
  with `git merge-base --independent`. `_write_refs` writes
  `refs/subfleet-kept/<job>/<n>/<m>` in that repository for each tip and
  reads each back. The journal records each ref before git creates it, and
  records the ref folders that git creates for it (`made_folders`).
- **A repository inside the tree.** `repository_commits` collects what its
  refs (`refs/stash` included), HEAD, reflogs and registrations name, plus
  every available commit object, including unreachable/dangling commits.
  `_bundle` builds, in a scratch repository that borrows the repository's
  objects (it writes nothing into the tree), a bundle of the complete history
  reached by all its tips, without remote prerequisites. Pseudo-ref collection
  includes every entry of multi-value `MERGE_HEAD` and `FETCH_HEAD` files.
  Local remote-tracking refs are not proof of a server's object possession.
  `_verify_bundle` restores the bundle into an empty repository without
  alternates, checks its heads, fetches with object checking and reads every
  tip back before the bundle moves to
  `<state>/jobs/<job>/kept-commits/<n>.bundle` and the journal records `kept`.

A removable linked admin containing another Git root, or object storage
outside the tree but below that admin, keeps the whole job when preservation
cannot be proved. Unreadable or oversized admin records fail safe rather
than silently omitting their commits.

The journal saves the Git-root census and commits. `_after_rename` and the
checks before the destructive state and its first unlink reject new/changed
roots and commits absent from that census. Partial resume allows old roots
already removed, but rejects new ones. After the removal,
`_remove_admins` removes a registration only when its `gitdir` names a place in
the removed tree, no other tree names it and nobody locked it. A registration
naming a commit that no written ref holds gets a late ref and stays.
The final admin census also retains a registration with a newly added nested
Git root or object store before its removal.

Defects the tests found here, all fixed in d4da08768:

- `_remove_admins` counted the plan's tips as kept even where no ref held
  them. Only written refs count now.
- A rollback left an empty `refs/subfleet-kept` folder: git prunes a deleted
  ref's empty parent folders but never `refs/<namespace>` itself. The journal
  now lists the folders a ref made, and the rollback removes them, deepest
  first, after the refs (mutant 18).

## The journal's steps and the restart behaviour

`<state>/retention-delete/<job>/journal.json`, written atomically at every
step: `gating`, `keeping`, `kept`, `renaming`, `renamed`, `checked`,
`removing`, `removed`, `admins`, `marking`, `marked`. The journal exists
before the leases, so the pass never takes the leases for a dead selection's.

- **Rename.** At `renaming` the path renames the tree to
  `<state>/retention-delete/<job>/tree`, then records `renamed`.
- **Checks.** `_after_rename` checks that the tombstone is the gated
  directory (device and inode), that the top-level `.git` has the digest it
  read, that no process holds it, and that the commits match. Pins come
  again, and the journal records `checked`.
- **Removal.** At `removing` (`DESTRUCTIVE`), `remove_tree` removes the
  tombstone through an anchored parent descriptor, bound to its saved
  device/inode and rechecked during traversal, never crossing a device.
- **Restart before `removing`.** `_resume` calls `_rollback`: it renames the
  tombstone back (it refuses if something occupies the original place), deletes the
  bundles, deletes each ref it wrote whose value it still holds, removes the
  ref folders it made, releases the leases, drops the journal, and records
  the job `interrupted`. That run does not try the job again; the same
  request, applied again, does.
- **Restart from `removing` on.** `_finish_deletion` finishes: removal,
  registrations, marker, event, lease release.
- **Failures.** A journal it can neither finish nor roll back waits an hour
  (`RETRY_S`). A step that raises what the path did not expect ends the
  request (the daemon logs the exception's type, never its text) and leaves
  every journal for an hour. A store failure ends the run. An exception on
  one job fails that job and the run moves on.

`_finish_deletion` writes `<state>/jobs/<job>/tree-deleted.json` and then one
`retention.tree_deleted` event, in a transaction that first looks for an
earlier one, so a restart never writes two. Defect found and fixed:
`Store.add_event` adds an audit row of the same kind after the event, so the
first build wrote two rows. `_record_event` now writes the row itself with
the transaction's audit row off, as the daemon's hold writes do (mutant 17).
Resume reuses an existing marker/event payload rather than recomputing its
time/free-space readings after an event commit followed by a crash.

The final census is an observation, not an OS lock against arbitrary external
writers. History introduced after that check and before an unlink remains a
concurrent-writer bound. The leases fence daemon admission; the implementation
does not claim per-entry Git recensus or protection against every external
write into the private tombstone.

## Invariants and their tests

| # | Invariant (the brief's words, shortened) | Tests |
|---|---|---|
| 1 | An unlisted job's tree, and a listed job's tree that fails a gate, stay unchanged entry by entry | properties `test_a_dry_run_then_an_apply_keep_invariants_one_to_four`; apply `test_gates_one_to_four_keep_their_jobs_whole`, `test_gates_five_to_twelve_keep_their_jobs_whole`, `test_a_holder_seen_after_the_rename_puts_the_tree_back`, `test_a_failure_on_one_job_fails_it_whole_and_the_run_goes_on` |
| 2 | Every commit reachable from a root's HEAD, refs or reflogs before the run is reachable afterwards from a ref in a surviving repository or a verified bundle | properties (both tests); apply `test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left`, `test_a_remote_less_history_tree_is_deleted_and_its_commits_kept_by_refs`; resume `test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept`. The check (`still_kept`) uses `git rev-list --all` of the surviving repository (refs only: no reflog, no admin directory), or fetches the bundle into an empty repository, or into a clone of the network remote when the bundle has prerequisites |
| 3 | Nothing outside the tree changes, apart from the refs, the bundle and marker files, the job's journal and tombstone folder, and the admin directories the path removes | properties (both tests, `outside_all`); the rich apply test (`changes`, `allowed`) |
| 4 | A dry run changes nothing anywhere | properties `test_a_dry_run_then_an_apply_keep_invariants_one_to_four` (dry run first); apply `test_a_dry_run_changes_nothing_anywhere_and_says_what_an_apply_would_do`; daemon `test_the_op_reaches_the_path_and_the_worker_runs_a_dry_run_to_its_end` |
| 5 | A kill at any journal step, then a restart, ends with the tree whole and no destructive step, or the tree gone with the commits kept | resume: every step boundary (19 points, including each repeat of `keeping`, the moment between the rename and its journal write, and two points mid-removal), plus a kill inside the bundle check, a second kill while the restart finishes, a kill while the restart rolls back; properties `test_a_kill_at_a_generated_step_keeps_invariant_five` |
| 6 | A marked job is never retired, archived or pruned by a normal pass | the rich apply test (a normal pass after the deletion keeps it, the survey labels it `tree-deleted`); apply `test_the_normal_pass_never_takes_a_job_whose_deletion_is_in_flight` |
| 7 | The path reads nothing under a tree whose top-level gitfile names an excluded repository, beyond that gitfile | apply `test_a_tree_whose_gitfile_names_an_excluded_repository_is_not_read_beyond_it`; request `test_resolve_never_touches_an_excluded_place_and_agrees_with_realpath`, `test_resolve_refuses_an_excluded_place_without_touching_it` |

The Hypothesis tests generate one to three jobs. A tree may hold nested
linked worktrees of other repositories at depth 0 to 2 (some with a commit
only the reflog names, some of the excluded repository, the first locked),
standalone repositories inside at depth 0 to 2 (some with a stash), symbolic
links pointing inside, outside, nowhere and into the excluded repository,
and read-only, unreadable and executable modes. The request lists some
jobs, and some jobs are young. An oracle written from the specification predicts each job's
outcome. Every example builds real repositories, so the default is 4
generated examples per test plus the explicit ones (`DEEP`, `MIXED`, and two
kill points), derandomized, with no example database and no shrinking.
`RETENTION_DELETE_PROP_EXAMPLES` and `RETENTION_DELETE_PROP_SHRINK=1` raise
both.

The invariant 7 test records every stat, lstat, open, listdir and scandir
that names an absolute path, and every git command. Descriptor-relative reads
fall outside what it records. What keeps the tree's walk from happening is the
gate order (gate 5 runs before the walk in gate 6), and mutant 2 shows the
difference: without gate 5's check, the path read 25 paths in the excluded
repository.

Further invariants the tests check, beyond the brief's seven: the event appears
exactly once per deleted job, whatever the restarts; the same request applied
again leaves deleted jobs deleted (`deleted_this_run` counts only new
deletions); `--limit N` ends an apply after N new deletions; the daemon takes
no delete step while an hourly pass is due or while retention is dormant.

## The ruling's acceptance bar and its tests

The menu Max ruled on
(`~/reviews/subfleet-hub/retention-tree-errors-2026-11-08/d635-delete-menu-20261109.md`)
sets the bar in two sections, "What a yes gives up, and what stays" and "How
a yes gets carried out".

| Clause | Where it holds | Test |
|---|---|---|
| Each job's record and accepted output under `~/.subfleet/jobs` stay | the path never writes a job row; it adds only `kept-commits/` and `tree-deleted.json` to the job's folder | the rich apply test: the row compares equal, and every earlier entry of the folder (the test world writes the job's output there) stays byte for byte |
| Every commit the tree alone holds, in a bundle or under a ref in its repository | the commit-keeping rule | the invariant 2 tests |
| A tree whose commits can't be kept stays whole | gate `keep` before anything moves; a bundle that fails verification fails the job before the rename | gates 5 to 12 (`g-symlink-root`: `keep`, `failed`, tree unchanged); `test_a_failure_on_one_job_fails_it_whole_and_the_run_goes_on` (mutant 6) |
| An age gate | gate 3 | gates 1 to 4 (mutant 10) |
| Through the daemon; nobody deletes in `~/.subfleet` by hand | the `retention.delete` op and the retention worker | the daemon tests; the CLI tests in the request file |
| Each run re-reads every tree's state and class before it deletes | every gate reads just before its job's turn; gate 7 asks the survey | gates 5 to 12 (`g-plain`: `class`, "retention would retire it normally") |
| Leaves out any tree whose gitfile names `~/PolicyEngine/microcosm` | gates 5 and 6 and the `Excluder`; the request's `exclude_repositories` | the invariant 7 tests; `test_covers_compares_folded_and_normalised_names_but_not_prefixes` and `test_an_excluder_spelling_through_a_link_also_excludes_the_real_place`, for the review notes' point that APFS ignores case and a gitfile may spell a relative or differently cased path |
| An independent Opus review | not yet | queued after this report |

The real request (`~/reviews/subfleet-hub/d2660-delete-path/request.json`)
parses with `read_request`, the function the CLI calls: 79 unique jobs,
`min_age_days` 14, `min_free_gb` 30, `age_exempt` naming the two quarantined
jobs, `exclude_repositories` naming `/Users/maxghenis/PolicyEngine/microcosm`,
ruling `d2660`, digest `e137b268b0696d80`.

## Mutants and the tests that kill them

`docs/reports/2026-11-09-retention-delete-d2660/mutants.py` applies each
mutant alone in a `git archive HEAD` copy of `subfleet/`, `tests/` and
`pyproject.toml` (a folder named `sf-d2660-b1.mut.*` under the temporary
folder). Each lane checks that every edit's old text occurs exactly once,
runs the killer alone (`pytest -x`, no xdist, no bytecode), writes the
pristine bytes back and checks their SHA-256, and the runner removes the
lanes at the end. Every kill below is an assertion failure in the targeted
check, not a crash. Records: `mutants-run1.json` (the first run) and
`mutants.json` (the current one).

| # | Mutant | File | Killer | Verdict |
|---|---|---|---|---|
| 1 | Skip the owned-path gate (all ten checks) | retention_delete.py | apply `test_gates_one_to_four_keep_their_jobs_whole` | killed |
| 2 | Skip the top-level excluded-repository check | retention_delete.py | apply `test_a_tree_whose_gitfile_names_an_excluded_repository_is_not_read_beyond_it` | killed |
| 3 | Skip the nested excluded-repository check | retention_delete.py | apply `test_gates_five_to_twelve_keep_their_jobs_whole` | killed |
| 4 | Skip the holder scan after the rename | retention_delete.py | apply `test_a_holder_seen_after_the_rename_puts_the_tree_back` | killed |
| 5 | Ignore a nested linked worktree when keeping commits | retention_delete.py | apply `test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left` | killed |
| 6 | Carry on after a failed `git bundle verify` | retention_delete.py | apply `test_a_failure_on_one_job_fails_it_whole_and_the_run_goes_on` | killed |
| 7 | Remove the tree before the journal records the kept commits | retention_delete.py | the rich apply test | killed |
| 8 | Let a dry run write the ref | retention_delete.py | apply `test_a_dry_run_changes_nothing_anywhere_and_says_what_an_apply_would_do` | killed |
| 9 | Let a normal pass retire a marked job | retention.py | the rich apply test | killed |
| 10 | Drop the age gate | retention_delete.py | apply `test_gates_one_to_four_keep_their_jobs_whole` | killed |
| 11 | Ignore `--limit` | retention_delete.py | apply `test_limit_ends_the_run_and_the_same_request_continues_it` | killed |
| 12 | Remove an admin directory whose `gitdir` points elsewhere | retention_delete.py | apply `test_an_admin_directory_that_names_another_place_stays` | killed |
| 13 | Skip gate 11 | retention_delete.py | apply `test_gates_five_to_twelve_keep_their_jobs_whole` | killed |
| 14 | Keep HEAD only, losing a commit only a reflog or a per-worktree ref names | retention_delete.py | the rich apply test | killed |
| 15 | Put the journal under `<state>/retention/` | retention_marks.py | apply `test_the_journal_lives_under_retention_delete_never_under_retention` | killed |
| 16 | Excluder: finish a path lexically after a missing part (defect 1) | retention_delete.py | request `test_resolve_never_touches_an_excluded_place_and_agrees_with_realpath` | killed |
| 17 | Write the event with `Store.add_event` (defect 2) | retention_delete.py | the rich apply test | killed |
| 18 | Leave the ref folder a rollback made (defect 3) | retention_delete.py | resume `test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept[checked-1]` | killed |
| 19 | Survey: label a deleted job by the budget (defect 5) | retention_survey.py | the rich apply test | killed |
| 20 | Drop only the path's own copies of four owned-path checks | retention_delete.py | apply `test_gates_one_to_four_keep_their_jobs_whole` | survived, as expected |

In the first run, mutant 1 had mutant 20's edit and survived, because
`rarch.owned_worktree` repeats all four checks. It returns None for an
in-place job or another sandbox, and it resolves the path and refuses one at
or outside `<state>/worktrees`. Under the tests the four copies change no
outcome, so mutant 20 is an equivalent mutant. The copies stay as defense in
depth, should `owned_worktree` change. Mutant 1 now removes every
owned-path check, as the brief asks, and the gates test kills it: four of the
twelve jobs in its table no longer stop at `owned-path` (`g-elsewhere`, for
one, went on to gate 6). Mutant 3 is defense in depth too: with the nested check gone,
`layout` still records the excluded root as a problem, so the tree stays
whole either way. The killer tells them apart by the gate name
(`exclude-nested` against `keep`).

## Commands, results and counts

All tests ran from `/Users/maxghenis/subfleet-v2-lanes/retention-delete-d2660` with
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`,
`uv run --quiet --no-sync --with 'pytest-xdist>=3.6' pytest -q -p no:cacheprovider`,
at most 4 workers, with `--basetemp` in folders named `sf-d2660-b1.*` (for
the runs at 19:40 to 19:53, `sf-d2660-e2e.*`, and for the 20:42 run,
`sf-d2660-run6.*`). Host load ran between 53 and 85 until the app's account
switch at about 20:08Z, and between 5 and 21 after it. Free disk stayed
between 37.20 and 44.40 GB (decimal, `df` available) at every reading, above
Fleet ops' 35 GB floor.

| Start (UTC) | Head | Command | Result |
|---|---|---|---|
| 18:03 | working tree | `-n 4 tests/unit/test_handler_refusals.py tests/unit/test_protocol_registry.py tests/unit/test_protocol_secrecy.py` | 705 passed, 20.8 s |
| 18:23 | working tree | `-n 4 tests/unit/test_retention_delete_request.py` | 67 passed, 23.6 s |
| 18:33 | working tree | `-n 4 tests/unit/test_retention_delete_apply.py` | 12 passed, 1 failed (the rich test: two event rows, defect 2), 228 s |
| 18:47 | working tree | the next run with `-x --dist loadfile` | stopped at once: loadfile put all 19 resume cases on one worker |
| 18:48 | working tree | `-n 4` properties, resume, and the rich apply test | 23 passed, 2 failed (the kill property: an empty `refs/subfleet-kept`, defect 3; the rich test: the survey's "within budget", defect 5), 422 s |
| 18:59 | d4da08768 | `-n 4` daemon, properties, resume, apply | 52 passed, 1 failed (the daemon test expected gate "listed" for jobs with no row; the path rightly said "finished", "no job row"; the test was wrong), 454 s |
| 19:11 | working tree on d4da08768 | `-n 4 tests/unit/test_retention_delete_daemon.py` | 16 passed, 10.5 s |
| 19:11 | same | `-n 4 tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | 433 passed, 46.0 s |
| 19:18 | 38471f4bc | `mutants.py --lanes 4` | 18 of 19 killed (mutant 1 survived, above), 5 min |
| 19:26 | a359f1b42 | `mutants.py --lanes 2 --only 1,20` | mutant 1 killed, mutant 20 survived as expected, 64 s |
| 19:28 | a359f1b42 | `-n 4` over the 198 test files that import a changed module (below) | cut off at 73%: 4,980 of 6,776 cases ended, 1 failed (the tenth to end), 5 skipped, 27 xfailed, the rest passed. The app's account switch at about 20:08Z ended the session's background processes (the log's last write came at 20:06:25Z), so pytest printed no summary and the failed case has no name |
| 19:40 | a5090c06c | `tests/e2e/test_review_publish_e2e.py`, no workers | 4 passed, 93.2 s |
| 19:42 | a5090c06c | `tests/fake/test_admission_disk.py tests/fake/test_admission_latency.py`, no workers | 41 passed, 319.3 s |
| 19:49 | a5090c06c | `read_request` on the real `request.json` | 79 unique jobs, digest `e137b268b0696d80` (the parse below) |
| 19:50 | a5090c06c | `--collect-only` over the same 198 files | 6,776 cases, 113.9 s |
| 19:53 | a5090c06c | cases 424 to 453, 847 to 876 and 1,270 to 1,299 of that collection, no workers | 90 passed, 125.9 s |
| 20:42 | a5090c06c | the same 198 files, `-n 4 -v --tb=short -rfE`, through `launch_ctl.sh` (launchd label `d2660-run6`), so another account switch can't end it | **6,744 passed, 5 skipped, 27 xfailed, none failed**, 1,075.9 s, rc 0 (load 5 to 21) |

The runs at 19:40, 19:42 and 19:53 looked for the 19:28 run's early failure.
pytest-xdist 3.8.0 starts each worker on its own run of consecutive cases, a
quarter of that worker's share (`xdist/scheduler/load.py`, the
`node_chunksize` branch): with 6,776 cases and 4 workers, runs of 423
starting at cases 1, 424, 847 and 1,270. The failure came tenth, so it lies
near one worker's start. Every case near each start passes alone (the first
worker's start is `test_review_publish_e2e.py` and `test_admission_disk.py`),
and no case failed in the 20:42 run, so the failure looks load-sensitive (the
19:28 run met load 85). It has no name, since that run stopped before its
summary. Commit a5090c06c landed while the 19:28 run went on; it changes only
`docs/desktop/retention-archive.md` and the mutants' record, which no test
reads, so `subfleet/` and `tests/` match a359f1b42 throughout
(`git diff --quiet a359f1b42 a5090c06c -- subfleet tests`).

The 198 files of the last run are every test file that imports
`retention`, `retention_cli`, `retention_survey`, `retention_marks`,
`retention_delete`, `protocol` or `refusals`, every test file that touches
the retention worker in the daemon (`_retention`, `_last_maintenance`,
`RETENTION_INTERVAL_S`, `RETENTION_DORMANT`, `_unrun_lock`), and the four
suites the brief names. That run includes `test_retention_81_notes.py` and
`test_retention_archive.py`, whose exact compares of the survey's
`jobs_by_reason` this change could have broken, and every test that walks
`protocol.OPS`.

Other checks:

- CI at 38471f4bc passed all 10 checks (read at 20:46Z). `subfleet/` and
  `tests/` have not changed since that commit.
- `git merge-tree --write-tree HEAD origin/release/217` exited 0 before the
  merge. The merge (38471f4bc) conflicted only in the contract.
  `~/reviews/subfleet-hub/host-runs/merge_contract.py HEAD origin/release/217`
  resolved it: 257 clauses, none duplicated. Every line either side added to
  `daemon.py`, `protocol.py`, `refusals.py` and the contract is in the result.
- `uvx ruff check --select F,E9` passed on every changed module and test.
- The exact-compare grep (step 4 of the brief) covered: `protocol.OPS`
  compares and walks, op-name lists, transaction and event kind lists, the
  survey's `jobs_by_reason`, and refusal registries. It found nothing outside
  the 198 files that this change could break.
- An active-voice grep over C-8.7 and section 18 found no passive
  constructions.

## What didn't run

- **The whole suite.** The brief leaves that to CI.
- **Anything live.** No install, no dry run over the 79, no canary, no full
  run; only `read_request` read the real `request.json`, and it reads
  nothing else. The release has no build or rehearsal yet, so no measured
  GB figure exists. The only GB figure here is the ruling's.
- **A Hypothesis run beyond the defaults.** Each property test ran its 4
  generated examples and its explicit ones; no run raised
  `RETENTION_DELETE_PROP_EXAMPLES` or turned shrinking on.
- **Independent review.** The PR's review comes next. No local lane can
  start it while free disk sits under Fleet ops' 67 GB line, and the deletion
  it gates is what adds the space, so the hub files it for a cloud session
  held for Fleet ops' word, and as a queued lane job.
- **Mutants of the daemon's scheduling.** The daemon tests cover it, but no
  mutant targets it.

## Deviations from the brief

1. **Built in-session, not on a lane.** Fleet ops asked for the one job to
   run through a single pinned admission or in-session; the hub built it in
   its own session.
2. **New tests ran in groups after the first two files.** The brief asks for
   new test files one at a time. The request and apply files ran alone; the
   properties and resume files ran only together with others (runs at 18:48
   and 18:59).
3. **Mutant lanes, not the working tree.** Each mutant ran in a `git
   archive` copy of HEAD, restored from its pristine bytes and checked by
   SHA-256, so the working tree never held a mutant. Mutants 2 to 19 ran at
   38471f4bc and mutants 1 and 20 at a359f1b42; the two heads' `subfleet/`
   and `tests/` are byte-identical (`git diff --quiet`).
4. **Five more mutants than the brief's 15.** Mutants 16 to 19 guard the
   defects the tests found, and mutant 20 records the equivalence above.
5. **Mutant 1 redefined after its first run.** See the mutants section.
6. **The branch merged origin/release/217** (b51f08f96, 11 commits) before the
   last test run, so the head carries #296 and #293's changes.
7. **Hardening beyond the brief:** `_remove_admins` counts only written refs,
   and the survey checks kept-for-good before the budget.
8. **The related-files run finished after the 4-hour mark.** The mark fell at
   20:41Z. An account switch cut off the 19:28 attempt, and the hub re-ran it
   through launchd from 20:42Z to 21:00Z before it committed this report.

## Folders outside the job's scratch

All under `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`, each with
`sf-d2660-b1` in its name: `sf-d2660-b1.explore.izmrjqph`,
`sf-d2660-b1.explore.c16wv6h_`, `sf-d2660-b1.Kyq3Ss`, `sf-d2660-b1.eQfttJ`
(the apply run's basetemp), `sf-d2660-b1.JqXsr4`, `sf-d2660-b1.3DHR1u` (the
later runs' basetemps), `sf-d2660-e2e.un44k2` (the basetemps of the runs at
19:40, 19:42 and 19:53) and `sf-d2660-run6.yvEfRK` (the 20:42 run's). The
mutant lanes (`sf-d2660-b1.mut.*`) went earlier. The hub removed all of these
by 21:02:38Z with a guarded `shutil.rmtree` that asserted `sf-d2660-` in each
name; nothing named `sf-d2660-` remains there. The report cites nothing in
them.

## Head

Built and tested at a5090c06c: its `subfleet/` and `tests/` match a359f1b42
and 38471f4bc, and they are the code this report describes. a359f1b42
changes only the mutant runner, a5090c06c adds section 18 and the mutants'
record, and the commit that adds this report changes nothing else.
