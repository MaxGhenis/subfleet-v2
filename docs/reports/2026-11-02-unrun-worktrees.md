# Unrun worktrees go back (C-6.20)

PR MaxGhenis/subfleet-v2#289, branch `fix/unrun-worktrees-217` on `release/217`. The Subfleet hub wrote this on 2026-11-02. Evidence lives in `~/reviews/subfleet-hub/unrun-worktrees-2026-11-02/`, named below by file.

## Why

On 2026-11-02, with every Codex lane held for the Microcosm US release, Fleet ops asked Subfleet to stop building worktrees for jobs that only wait and to drop the ones it had built. They asked for the change in Subfleet's own code: under d2171, nobody deletes anything under `~/.subfleet` by hand.

Admission cuts `worktrees/<job id>/` on a fresh writable job's first look, before it evaluates the job's route (`_workspace`, C-6.8), and a job that then waits keeps the tree. The daemon writes `jobs.worktree` only when it reserves an attempt. So retention frees such a tree only when it retires the job (`rarch.owned_worktree`, C-8.4), oldest first within its budget, which can take days. Before this change only a job that admission refused gave its tree back at once (C-6.12).

`live_unrun.py` read the live store through a read-only URI and walked each tree with lstat only, following no link. At 18:10:58Z it found (`live-1810.log`):

| Jobs that never had an attempt | Trees | Decimal GB |
|---|---:|---:|
| ended | 125 | 6.99 |
| waiting | 39 | 1.55 |
| all | 164 | 8.54 |

The last decision of each of the 39 waiting jobs named the same refusals: `closed:account`, `desktop` and `disabled`. The script skips income-lane and Microcosm jobs without opening them, and it left one such row unopened. A read-only count at 18:33:55Z found no push job without an attempt, so the reading's exclusion of push jobs drops nothing.

## What changed

### Three kinds of tree go back

The sweep (`_sweep_unrun_worktrees`) gives back the tree of:

1. a job that ended with no attempt (its event's `why`: `ended unrun`);
2. a queued or waiting job that the last detached pass held at the door on the disk hold (C-6.17; `disk hold`);
3. a queued or waiting job that the last detached pass held `unprepared` (`no lane`).

The door takes the second kind before any Git, so the look that lets such a job through cuts its tree again from the HEAD that submit recorded.

### No tree for a job no lane takes now

In `_admit_pass`, before `_workspace`, a detached job in scope asks `scheduler.unadmittable(..., far_s=0)` whether every lane refuses it now for a reason no slot ends. In scope means no retry pin, `workspace-write`, not in place, no recorded worktree and no push. With a horizon of 0, every closure in force counts, however soon it ends. So do a latched credential that only a person can bring back, a disabled lane and the job's own exclusions. When every lane refuses the job:

- the look runs no Git, cuts no tree and records no decision;
- the job waits on its capacity clock (C-6.10: 1 s, doubling to a 30 s ceiling) with a hold marked `unprepared`, whose `reason` is the commonest refusal (`scheduler.refusal_label`, grouping a closure by scope as C-6.11's label does), whose `refused_now` names every refusal, and whose `for_good` names the refusals that C-11.8's 7-day horizon also finds;
- it holds back the younger jobs it held back before (C-6.9): it registers as a waiter unless every lane refuses it for good;
- its wait counts as C-23.16's demand, as the look it skipped would have made it (`lane_wait=True`);
- a task hold that commits while the look runs still takes the job (C-11.10, `_task_held_at_write`).

The job's due look asks again. The first look that a lane would take cuts the tree from the HEAD that submit recorded. `why` prints `Held: no lane admits it (<label>)`. When admission has recorded no decision, it says it evaluated one for that answer; a job that a look decided before the lanes closed shows that older decision beside the current hold (`_why_job`).

### The sweep

The daemon offers the sweep:

- at start, once it serves;
- when a cancel ends a job whose tree it could give back (`_unrun_scope`, the columns `UNRUN_TREES` reads; never a turn's);
- when a detached pass first holds a job on the disk hold or `unprepared` (once per job for each stretch of such holds);
- before each retention pass, and again after it when an offer came while it ran.

One sweep runs at a time and never beside a retention pass: retention holds `_unrun_lock` around `maintenance`. A sweep reads `UNRUN_TREES` again, up to three times, while offers keep arriving. For each tree it:

1. takes the detached pass lock, waiting at most 30 s, so no pass cuts that tree meanwhile;
2. reads the job again and decides;
3. reads the tree's gitfile, and keeps the tree if someone locked its registration;
4. removes the tree with its own registration only, `.git` first (`_discard_worktree`; d635 forbids a repository-wide prune), running git in the repository the gitfile names;
5. records one `job.worktree_given_back` event with `worktree`, `state`, `why`, `common_dir` and, if the registration stayed, `registration_left`.

It reads `dependencies.legacy`, which walks the filesystem, once per read and outside the lock: no job it finds is newer than the read. When the lock wait times out, the daemon is stopping, or anything raises, the sweep stays owed and the next offer or retention pass runs it. It logs an error and never raises it. `daemon.log` gets one line naming the jobs whose trees went back.

### What keeps a tree

A tree stays when:

- its job is a turn, runs in place, pushes (C-8.5), has an attempt or has a recorded `jobs.worktree`;
- its path is a symbolic link;
- someone locked its registration (`git worktree lock`), a claim that git, retention and `discard_registration` all honour;
- its job has not ended and is not queued or waiting, has a cancel pending, or did not get a disk or `unprepared` hold from the last detached pass;
- retention has the job: a `retire:<job id>` lease, a lease the job holds, or `retention/<job id>/`;
- another job needs a place in it, by retention's own check (`dependencies.dependents`), excluding the job itself so its own incomplete evidence never keeps its own tree;
- a turn's row or a `worktree:` lease names a folder in it.

The sweep reads the last two for each tree under the lock, since a pass that ran between two trees may have placed a job in the next one.

### The repository, not the caller's checkout

Before C-6.20 the first look cut the tree, and later looks reused it through the repository's common directory. So a job needed `jobs.workdir` only at that first look. Now a job that no lane takes gets no tree, and a held job gives its tree back, so the look that finally cuts the tree can come hours later. A session in an isolated worktree can submit while every lane is held and archive meanwhile, and the look then failed the job (`git -C <gone> worktree add` exits 128, which is no transient error). A give-back from that checkout also left the registration behind while its event said the tree went.

`_tree_source` now picks where git runs for a cut or a discard:

1. The common directory: submit records it (`manifest.json`, `workspace.common_dir`); for a job older than that record, the latest `job.worktree_given_back` event names it. With neither, the answer is `jobs.workdir`, as before.
2. `jobs.workdir` while it answers with that same common directory, because `git worktree add` copies the per-worktree settings of the checkout it runs in (sparse patterns, `config.worktree`).
3. Otherwise the common directory. That covers a folder that has gone, one whose `.git` names nothing now, and one that has become another repository.

A give-back reads the repository from the tree's own gitfile, so it never depends on `jobs.workdir`.

### Scheduler

`pin_unadmittable` and `unadmittable` take `far_s`, which replaces `admission.pin_hold_far_s` as the horizon a closure must end beyond. The memo key carries the horizon, so one pass's memo serves both questions. `refusal_label` names the commonest of the distinct refusals `unadmittable` lists.

## Invariants

| # | Property | Tests |
|---|---|---|
| 1 | The sweep removes only `worktrees/<job id>` of a job that is no turn, writes, runs neither in place nor as a push, has no attempt and no recorded worktree, and whose path is no link. It removes nothing outside that path, and no registration but the tree's own: never a repository-wide prune. | `property`, `claim_alone`, `link_in_place`, `stale_registration` |
| 2 | An unended job's tree goes only while the job is queued or waiting, has no cancel pending, and holds a disk or `unprepared` hold from the last detached pass. | `property`, `claim_alone`, `not_disk_held` |
| 3 | No tree goes beside a retention pass or while retention has the job. | `beside_retention`, `property`, `claim_alone` |
| 4 | Each tree goes under the detached pass lock. | `detached_pass_may_be_cutting` |
| 5 | The sweep reads what keeps a tree for that tree, under the lock. A job placed in a tree between two give-backs keeps it. A job's own incomplete evidence never keeps its own tree; another job's does. | `between_two`, `incomplete_evidence`, `legacy_read`, `lease_holds`, `another_live_job` |
| 6 | The sweep records one event for a tree exactly when the tree goes, naming why. | `property`, `did_not_go` |
| 7 | `unadmittable(far_s=0)` matches an independent oracle, grows monotonically with the horizon, agrees with `evaluate`, and gives the same answers with a shared memo. | `tests/unit/test_unadmittable_now.py` |
| 8 | C-6.9: a younger job fares as before C-6.20, under a closure of hours or one for good, read-only or writable. | `c6_9` |
| 9 | A sweep that did not finish stays owed. | `read_again`, `sweep_that_fails`, `stopping`, `detached_pass_may_be_cutting` |
| 10 | An `unprepared` wait counts as C-23.16's demand, as its look would have made it. | `c23_16` |
| 11 | A look cuts no tree for a fresh job no lane takes now, and the first look a lane takes cuts it. A retry, a read-only job, an in-place job and a push get their look whatever the lanes. | `no_lane_takes`, `tree_cut_before`, `retry_keeps`, `cuts_no_tree`, `task_hold` |
| 12 | A cancel offers a sweep exactly when it ends a job in the sweep's scope. `_unrun_scope`, which decides that from the job's columns, holds exactly when `UNRUN_TREES` would select the job, for every combination of those columns (differential, exhaustive). | `cancelled_before_any_attempt`, `outside_the_sweeps_scope`, `scope_check_is` |
| 13 | A locked registration keeps its tree, whatever the shape that would give it back; the tree goes at the next sweep after an unlock, and a waiting job runs in it. | `property`, `claim_alone`, `someone_locked` |
| 14 | A job runs at the HEAD submit recorded, in a tree registered in the repository submit read, for each way it can reach its first attempt (held from submit, given back, kept its tree) and each fate of its caller's checkout (stays, goes, loses its registration, becomes another repository). | `whatever_becomes`, `older_than`, `broken_tree`, `subfolder`, `bare`, `after_the_callers_checkout_went` |
| 15 | A tree cut from the common directory matches one cut from the checkout: HEAD, detached HEAD, every tracked mode and object, clean status (differential). Sparse patterns pass on while the checkout lasts. | `the_repository_is`, `sparse` |
| 16 | A give-back takes the tree's own registration wherever its caller's checkout is now, or its event names what stayed. | `give_back_after_the_checkout`, `refused_jobs`, `that_stayed` |
| 17 | `.git` goes first, never through a link, so a removal cut short leaves no tree a look reuses. | `cut_short`, `through_a_link` |

The test names above abbreviate those in `tests/fake/test_unrun_worktrees.py`, and `after_the_callers_checkout_went` the one in `tests/fake/test_review_launch_permissions.py`.

## Tests

`tests/fake/test_unrun_worktrees.py` holds 71 items and `tests/unit/test_unadmittable_now.py` 4.

- The property test draws 150 shapes over 14 fields.
- `test_each_claim_alone_keeps_a_tree_that_would_go_back` starts from each of the three shapes that give a tree back and adds each of 17 claims alone (54 shapes in all).
- `test_a_link_in_place_of_a_tree_leaves_the_tree_it_names_registered` points a link at a real registered worktree. `_discard_worktree` removes nothing through a link, but `discard_registration` resolves `<tree>/.git`, so only the rule's own link check keeps that worktree's registration.
- `test_a_give_back_leaves_a_stale_registration_elsewhere_in_the_repository` leaves a registration whose tree is gone, which `git worktree prune --dry-run` lists, and checks that a give-back keeps it.
- `test_a_cancel_outside_the_sweeps_scope_offers_no_sweep` cancels a read-only job, an in-place job, a push, a job with a recorded worktree and a turn, and checks that none offers a sweep. `test_the_cancels_scope_check_is_the_sweeps_query` compares `_unrun_scope` with `UNRUN_TREES` over all 32 combinations of the five columns.
- `test_a_job_runs_whatever_becomes_of_its_callers_checkout` crosses three ways of reaching a first attempt with four fates of the caller's checkout (12 cases), with real git.
- `swept()` requires each sweep to finish (`_unrun_dirty` false). So a sweep that fails quietly can't pass a test that expects a kept tree.

On 3492445c6 both files passed: 39 passed in 47 s (the baseline in `mutants-rerun-link.log`). On the fix for defect 6, the C-6.20 file and `tests/unit/test_fake_daemon_borrowed_methods.py` pass (48 passed in 17.5 s), as do the ten conversation files CI had failed (430 passed, `ci-fix-conv.log`). The eight test files that read the contract pass on the edited contract, with `test_unadmittable_now.py` (530 passed, `ci-fix-contract.log`). On the fixes for review r1, those two files, `test_fake_daemon_borrowed_methods.py`, `test_review_launch_permissions.py` and `test_salvage.py` pass (147 passed in 71 s, `fix-r2-pr-files.log`). Of r1's 12 reproductions, 8 pass now, those for P1-1 and P2-1 among them. The other 4 fail as this report expects: `partial` models a state with `.git` left, which the removal no longer leaves, and `offers` (2) and `clock` show the limits below.

## Evidence that the tests catch the defects

### Fails before

`fails-before-2ccf3eb0b.log` ran the current tests with `subfleet/daemon.py` at 2ccf3eb0b, the PR's first commit, and every other file at 0e650e5e0:

- `test_incomplete_evidence_keeps_a_tree_unless_it_is_the_jobs_own[its own]` failed with `0 == 1`: the job's own incomplete evidence kept its own tree;
- `test_a_job_placed_in_a_tree_between_two_give_backs_keeps_it` failed with `2 == 1`: the sweep gave back a tree in which a pass had placed a job after the sweep began.

### Mutants

`tools/mutants_unrun_worktrees.py` changes one rule at a time in a scratch copy and requires a test to fail. It runs each mutant's focused tests first, then both files if those pass.

There are 68 mutants. The full run on 0e650e5e0 (`mutants.log`) killed 47, found 3 equivalent and left 11 alive. On rerun 1 (`mutants-rerun.log`), the one-claim test killed nine of those eleven, and the hub found a tenth, `unprepared-turn-pass-included`, equivalent. On rerun 2 (`mutants-rerun-link.log`), the link test killed `rule-link-followed`. None of those 61 survive. The fix for defect 6 added seven (`cancel-offer-ungated`, `cancel-scope-dropped` and one `scope-*-unchecked` for each column) and re-anchored `cancel-offer-removed`. Review r1 ran those eight, and their focused tests kill all eight (`review-r1-scratch/mutants-new8.log`). It also found that the delta's second `SCOPE` constant redirected `unprepared-retry-included`, `unprepared-read-only-included` and `unprepared-turn-pass-included` into invalid Python, so the table's outcomes for those three date from 0e650e5e0. The constant now has its own name, `CANCEL_SCOPE`, and the driver refuses a mutant that does not compile.

The fixes for r1 add 14 mutants, for 82 in all (`rule-lock-ignored`, `rule-left-unsaid`, `rule-event-no-repository`, `rule-discards-from-checkout`, `submit-records-no-repository`, `source-always-checkout`, `source-always-repository`, `source-a-folder-is-enough`, `source-ignores-the-give-back`, `cut-from-checkout`, `broken-tree-from-checkout`, `fresh-from-checkout`, `discard-git-not-first`, `discard-through-a-link`). Each applies once and compiles. Fleet ops allowed those 14 alone before the release: `fix-r2-mutants14.log` ran them at 22:34:37Z to 22:36:15Z, and their focused tests killed all 14. The other 68 wait for the release to start.

| Mutant | Outcome |
|---|---|
| `gives-back-disk-only` | killed by `test_a_tree_cut_before_every_lane_closed_goes_back_and_is_cut_again` |
| `gives-back-unprepared-only` | killed by `test_the_disk_hold_takes_back_a_waiting_tree_and_placement_cuts_it_again` |
| `gives-back-any-hold` | killed by `test_a_waiting_job_that_is_not_disk_held_keeps_its_tree` |
| `cancel-offer-removed` | killed by `test_a_job_cancelled_before_any_attempt_gives_back_its_tree` |
| `start-offer-removed` | killed by `test_the_daemon_offers_a_sweep_at_start` |
| `pass-offer-removed` | killed by `test_the_disk_hold_takes_back_a_waiting_tree_and_placement_cuts_it_again` |
| `pass-offer-every-pass` | killed by `test_the_disk_hold_takes_back_a_waiting_tree_and_placement_cuts_it_again` |
| `retention-first-sweep-removed` | killed by `test_never_beside_retention_and_retention_sweeps_after_its_pass` |
| `retention-unlocked` | killed by `test_never_beside_retention_and_retention_sweeps_after_its_pass` |
| `retention-dirty-sweep-removed` | killed by `test_never_beside_retention_and_retention_sweeps_after_its_pass` |
| `busy-not-dirty` | equivalent: Every caller that can find the lock taken sets the flag first (`_offer_unrun_sweep`, retention's check of it) or is retention's own first sweep, which the running sweep has just made; the running sweep reads its rows after either, so no tree waits longer. |
| `dirty-loop-single-read` | killed by `test_an_offer_while_a_sweep_runs_is_read_again` |
| `pass-incomplete-not-dirty` | killed by `test_never_while_a_detached_pass_may_be_cutting_a_tree` |
| `failure-not-dirty` | killed by `test_a_sweep_that_fails_is_asked_again` |
| `failure-raised` | killed by `test_a_sweep_that_fails_is_asked_again` |
| `pass-unlocked` | killed by `test_never_while_a_detached_pass_may_be_cutting_a_tree` |
| `pass-ignores-stopping` | killed by `test_a_daemon_that_is_stopping_starts_no_give_back_and_owes_the_sweep` |
| `pass-filter-everything` | equivalent: `_give_back_unrun` reads the job again under the lock and keeps every tree the filter drops; the filter only spares it those reads. |
| `legacy-dropped` | killed by `test_a_place_only_the_legacy_read_finds_keeps_the_tree` |
| `rule-turn-allowed` | killed by `test_property_a_tree_is_given_back_exactly_when_nothing_keeps_it` |
| `rule-sandbox-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-in-place-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-recorded-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-push-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-link-followed` | survived; killed on rerun 2 by `test_a_link_in_place_of_a_tree_leaves_the_tree_it_names_registered` |
| `rule-attempt-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-live-state-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-cancel-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-hold-unchecked` | killed by `test_property_a_tree_is_given_back_exactly_when_nothing_keeps_it` |
| `rule-why-disk-only` | killed by `test_a_tree_cut_before_every_lane_closed_goes_back_and_is_cut_again` |
| `rule-retention-dir-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-retire-lease-unchecked` | killed by `test_property_a_tree_is_given_back_exactly_when_nothing_keeps_it` |
| `rule-own-lease-unchecked` | survived; killed on rerun 1 by `test_each_claim_alone_keeps_a_tree_that_would_go_back` |
| `rule-dependents-unchecked` | killed by `test_a_tree_another_live_job_needs_is_kept_until_that_job_ends` |
| `rule-own-evidence-counted` | killed by `test_incomplete_evidence_keeps_a_tree_unless_it_is_the_jobs_own[its own]` |
| `rule-turn-folders-unchecked` | killed by `test_a_folder_a_lease_holds_in_the_tree_keeps_it[turn]` |
| `rule-writer-folders-unchecked` | killed by `test_a_folder_a_lease_holds_in_the_tree_keeps_it[writer]` |
| `rule-removal-unverified` | killed by `test_a_tree_that_did_not_go_records_nothing` |
| `rule-event-unrecorded` | killed by `test_a_job_cancelled_before_any_attempt_gives_back_its_tree` |
| `unprepared-check-removed` | killed by `test_no_tree_is_cut_for_a_fresh_job_that_no_lane_takes_now` |
| `unprepared-default-horizon` | killed by `test_no_tree_is_cut_for_a_fresh_job_that_no_lane_takes_now` |
| `unprepared-retry-included` | killed by `test_a_retry_keeps_its_tree_and_its_look_whatever_the_lanes[False]` |
| `unprepared-read-only-included` | killed by `test_a_job_that_cuts_no_tree_gets_its_look_whatever_the_lanes[read-only]` |
| `unprepared-turn-pass-included` | survived; equivalent: Only the dispatcher submits a turn (`submit` refuses `kind="turn"` without its turn block), and `ConversationService._submit_turn` always sets `in_place=True`, so `not job["in_place"]` already keeps every turn out. |
| `unprepared-in-place-included` | killed by `test_a_job_that_cuts_no_tree_gets_its_look_whatever_the_lanes[in-place]` |
| `unprepared-recorded-included` | equivalent: `jobs.worktree` is set only at reservation, which makes an attempt, so `not previous` already keeps every job with a recorded tree out. |
| `unprepared-push-included` | killed by `test_a_job_that_cuts_no_tree_gets_its_look_whatever_the_lanes[push]` |
| `unprepared-for-good-dropped` | killed by `test_c6_9_the_younger_job_fares_as_it_did_before_c6_20[for good-workspace-write]` |
| `unprepared-holds-back-for-good` | killed by `test_c6_9_the_younger_job_fares_as_it_did_before_c6_20[for good-workspace-write]` |
| `unprepared-holds-back-nobody` | killed by `test_c6_9_the_younger_job_fares_as_it_did_before_c6_20[hours-workspace-write]` |
| `unprepared-task-hold-ignored` | killed by `test_a_task_hold_that_commits_while_the_look_is_under_way_takes_the_job` |
| `unprepared-not-demand` | killed by `test_c23_16_an_unprepared_job_is_reset_demand_as_its_look_would_have_made_it[workspace-write]` |
| `unprepared-flag-dropped` | killed by `test_no_tree_is_cut_for_a_fresh_job_that_no_lane_takes_now` |
| `unprepared-label-raw` | killed by `test_no_tree_is_cut_for_a_fresh_job_that_no_lane_takes_now` |
| `unprepared-clock-dropped` | killed by `test_no_tree_is_cut_for_a_fresh_job_that_no_lane_takes_now` |
| `far-ignored` | killed by `test_c6_20_refused_now_is_exactly_the_oracle_and_the_memo_changes_nothing` |
| `pin-far-ignored` | killed by `test_c6_20_refused_now_is_exactly_the_oracle_and_the_memo_changes_nothing` |
| `pin-far-unforwarded` | killed by `test_c6_20_refused_now_is_exactly_the_oracle_and_the_memo_changes_nothing` |
| `memo-key-without-horizon` | killed by `test_c6_20_refused_now_is_exactly_the_oracle_and_the_memo_changes_nothing` |
| `label-unscoped` | killed by `test_c6_20_refusal_label_groups_closures_by_scope` |
| `label-minority` | killed by `test_c6_20_refusal_label_groups_closures_by_scope` |

## Defects found on the way

1. **Stale context between trees (2ccf3eb0b).** The first version read what keeps a tree once a sweep. It releases the detached pass lock between trees, so a job a pass placed in a later tree meanwhile did not keep it. The same read counted the job's own incomplete evidence, which kept its own tree. 0e650e5e0 reads `dependents(exclude=job_id)` and the folder rows for each tree, under the lock. The two tests above fail on 2ccf3eb0b.
2. **A vacuous test.** `test_a_place_only_the_legacy_read_finds_keeps_the_tree` built `dependencies.Places({...})`, which raises `TypeError`. The sweep caught it and gave nothing back, so the test passed for the wrong reason. The test now builds the mapping the way `dependencies` does, and `swept()` fails any test whose sweep did not finish. The `legacy-dropped` mutant confirms the test now bites.
3. **A C-23.16 gap.** `unprepared` waits first used `lane_wait=False`, so `_reset_demand` skipped them although the looks they replace counted. 0e650e5e0 sets it, and `test_c23_16_...` checks it for a read-only job and a writable one.
4. **A property too sparse for single claims.** The mutant run on 0e650e5e0 found that the random property rarely drew a shape that a single claim keeps, so it could not catch a rule that forgets a claim. `test_each_claim_alone_keeps_a_tree_that_would_go_back` tries each claim alone.
5. **A link test that could not fail.** The property's link pointed at a plain folder, where a sweep that let the link through still removed nothing, so `rule-link-followed` survived the one-claim test too. Through a link to a registered worktree, that sweep would have dropped the worktree's registration (`discard_registration` follows the link). `test_a_link_in_place_of_a_tree_leaves_the_tree_it_names_registered` checks that case.
6. **Every cancel offered a sweep (CI run 41503935107 on 3492445c6).** `_request_cancel` offered one whenever it changed a job, a conversation turn's included. The stand-in daemons of the conversation tests borrow `_request_cancel` but have no `_offer_unrun_sweep`, and `test_legacy_hold.py` builds a `Daemon` with no `stopping`. So CI recorded 118 failures, 65 distinct tests in eleven files across the two Pythons: an `AttributeError`, or a turn left waiting after it. On a real daemon the offer was only waste: a turn runs in place, so its cancel can leave no tree, yet conversation stops cancel turns many times an hour. A cancel now offers a sweep only when it ends a job in the sweep's scope (`_unrun_scope`, the columns `UNRUN_TREES` reads). The stand-ins list the name in `EXEMPT`, with its reason, as they do for other names on paths their tests never take. The local broad run had stopped at about 70% before it reached these files.
7. **The sweep deleted a locked tree's files (review r1, P1-1).** `_discard_worktree` removes the files first, and only `discard_registration` read the lock, so the files went and the locked registration stayed, while the event said the tree went back. A waiting job then failed at its next look: git refuses a missing but locked worktree. The sweep now reads the lock from the tree's gitfile before it removes anything.
8. **A job needed its caller's checkout hours after submit (review r1, P2-1).** See "The repository, not the caller's checkout". It failed jobs that had run before C-6.20, under the lane holds this change targets.
9. **A removal cut short left a tree a look reused (review r1, P3-1).** `_workspace` reads `.git`, HEAD and a copy's marker, never the files, and `rmtree` takes `.git` in no set order. C-6.20 removes trees of jobs that run later, so a daemon stopped part way could have started an attempt in a tree with files missing. `_discard_worktree` now takes `.git` first, never through a link. r1's reproduction modelled that state by deleting one file and keeping `.git`; `test_a_removal_cut_short_leaves_no_tree_a_look_reuses` stops the real removal at its `rmtree` instead.

## Expected effect when installed

The following comes from the code and the 18:10:58Z reading; nobody has measured it yet. At start the daemon offers a sweep, which gives back the trees of ended jobs that no attempt used: 125 trees and 6.99 GB in that reading, less any that another job's need, a lease, a turn's folder or retention keeps. Recovery makes every capacity wait due (`_recover_capacity_waits`). The live policy sets no pool cap and no ramp, so each waiting job then gets its own look in the first detached pass. The door can still hold a job first: for the disk (C-6.17), which gives its tree back too, or for a busy machine, which leaves the tree until a later look. Each job that no lane takes now is held `unprepared`, and the sweep that pass offers gives back its tree: 39 trees and 1.55 GB in that reading. The count at install will differ from the reading.

## Known limits

- **Writes per look.** Like a capacity wait's look, an `unprepared` look updates the job row once, and its transaction records one `job.unprepared_wait` event. It records no decision. At the 30 s ceiling, a job held for an hour writes about 120 such events.
- **Flapping lanes.** A lane that opens for one look and closes before the job starts costs a tree cut and a give-back each time. Nothing damps this.
- **C-6.9's FIFO in a capped pool.** A job held behind an older one (`behind-older-job`), or in a pool at its cap (`fleet-full`), never reaches the new check, so it keeps a tree an earlier look cut until its own look. That happens only in a pool with a count (`scheduler.hold_scope`); the live policy sets none, and admission at 18:30Z showed no such hold. A follow-up chip covers it.
- **The turn pass.** The sweep takes the detached pass lock, not the turn pass lock. A turn placed in a folder inside an unrun job's tree, between the sweep's read and the removal, would lose the folder. Nothing runs in such a tree, though submit's reply names its path, so someone could open a conversation there.
- **No archive and no process look.** Retention archives a tree first and defers one a process holds. The sweep does neither, because no attempt ran there. Someone who wants to keep such a tree locks it (review r1, P3-8; not reproduced beyond the lock).
- **How long a tree waits.** C-6.20 bounds it by the next offer or retention pass, an hour at most, dormant or not. A burst of cancels can leave its last tree that long: an offer made during a sweep's last read finds its key busy (`_schedule` drops it). So can the wall's cascade, which offers inside the transaction that ends the job, before a sweep can read it as ended (review r1, P3-2a and b; both reproduced). Endings at admission other than a route refusal or a pin-grace failure offer nothing (P3-2c; no case built). A follow-up chip covers it.
- **The clock.** An `unprepared` wait keeps C-6.10's backoff without its known-reset clamp, so its look after a closure ends can come up to the 30 s ceiling late (review r1, P3-3: 18 s in the reproduction). A follow-up chip covers it.
- **A fenced job.** The check comes before retention's fence look. So a job on a fence that no lane takes waits `unprepared`, a slot waiter and C-23.16's demand, where the fence would have held it `lease-held` and kept it out of both (review r1, P3-7). The lanes it waits for refuse every job until one opens, and at that look the fence holds it. A follow-up chip covers it.
- **The label.** `unadmittable` lists each refusal string once, so lanes closed until one instant count once and `refusal_label` can settle a tie by walk order, where C-6.11's `dominant_rejection` counts one per lane. This affects display only (review r1, P3-6). A follow-up chip covers it.
- **Unreachable HEADs.** A tree holds its HEAD against `gc`. A job with no tree relies on a ref or on `gc`'s prune window (two weeks by default) for a HEAD that its caller's checkout alone held, so a job could outwait it and fail at the cut. Nobody has built this case.
- **A sparse caller.** The tree comes out whole once the caller's checkout has gone, because the sparse patterns went with it.
- **Push jobs.** The sweep never takes a push job's tree (C-8.5). None existed at 18:33:55Z.
- **Lock time.** Each removal runs under the detached pass lock. On Python 3.13 and later, a blocked pass gets the lock after one tree; on 3.12, the sweep can take it back first, and review r1 measured a pass waiting through up to four trees of five (P3-4). The live daemon runs 3.14.7 (`ps`, 2026-11-02), and `requires-python` allows 3.12. The reading's trees average 56 MB (ended) and 40 MB (waiting). Nobody has timed a removal.

## Local and CI runs

- Both files pass locally on the head (counts above).
- CI run 41503935107 on 3492445c6 failed 9 of 10 shards with defect 6 (`ci289-failed.txt` lists the 118 failures). The fix came in the next commit.
- CI run 41518571289 on 107282ec6 passed 9 of 10 shards. Its tenth (Python 3.14, shard 3/5) ended `cancelled` after 52 minutes. CI runs again on the head.
- A broad serial run of 126 neighbouring files (`broad-files.txt`) started at 17:57:30Z on the working tree between 2ccf3eb0b and 0e650e5e0. It had 2640 passed, 7 skipped and 2 xfailed, with no failures, when the hub stopped it at about 70% at 18:24Z, because the Disk manager asked for hub-local work to hold during the release (`broad-partial-1757.log`). CI runs the full suite on the head.

## Retained files

In `~/reviews/subfleet-hub/unrun-worktrees-2026-11-02/`:

- `live_unrun.py`, `live-1810.log`: the live reading;
- `fails-before-2ccf3eb0b.log`;
- `mutants.log` (0e650e5e0), `mutants-r0-partial.log` (the first run, stopped early), `mutants-rerun.log` (rerun 1, the eleven survivors with the one-claim test) and `mutants-rerun-link.log` (rerun 2, with the link test);
- `broad-files.txt`, `broad-partial-1757.log`;
- `ci289-failed.txt`, `ci-fix-conv.log`, `ci-fix-contract.log`: defect 6's failures and the runs on its fix;
- `review-r1-brief.md` and `review-r1.md`: the independent review's brief and its result; `review-r1-scratch/`: its reproductions and logs;
- `fix-r2-pr-files.log`, `fix-r2-mutants14.log`, `fix-r2-neighbours.txt`, `fix-r2-neighbours.log`: the runs on the fixes for r1.
