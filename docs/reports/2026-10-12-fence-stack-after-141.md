# Fence stack after #141 — 2026-10-12

The merge account below preserves the reboot-cutoff report. The fix-round
findings, commits, open item and validation follow it.

This job starts #140 at `3f06f9a1b1f6fc3d1a828c07a8bbac26c6d54ddf` and #142 at
`a9eacada8f4bd22b26ff1da3892b735ff8483898`. The locally available
`origin/release/217` is `24be965c5ce8343f8b3d07082d69d103ecfb2a4f`, including
#141's merge `a6f73092ab260931285db3ab9b7a500d3f176d36` and subsequent release changes.
No fetch or push was performed. No caller worktree was written.

The shared worktree index and refs are outside the writable workspace; the
sandbox refused a shared `ORIG_HEAD.lock` write. Commits and the working index
are therefore in workspace-local `.git-local` (also mirrored in `.git-local/private`), with read-only object
alternates to the source repository. Use `git --git-dir=.git-local`.
The original `.git` pointer remains unchanged. Local branches use the assigned
branch names; neither source branch was rewritten.

#140 merge: `ad06aac26cbb5f505ba56a8c23b2912791735ec3` (parents: original #140,
then local release/217).

#142 merge: `51b9df64cfbd366bf10db7a24551dc94f434cf39` (parents: original #142,
then the resolved #140 merge).

## #140 conflicts and resolutions

Four conflict hunks in three files:

1. `docs/acceptance-contract.md`, C-8.4, whole clause. #140 describes ancestor
   writer fences, canonical submitted workdirs, resolved cwd reader rows and
   detached launch from the reserved cwd. Release describes #141's present-name
   requirement, rediscovery before the early fence look, folded absent-name
   checks, push-top exemption, trace-free Git and the precise early/transaction
   hold equivalence. Keep both as one clause. Remove release's superseded
   own-folder-only nested-writer and quarantined-writer reclamation gaps, and
   #140's superseded case-alias row/fence miss. Keep #140's allocated-source
   gap until #142 is merged. Retention now uses release's NOCASE exact-root
   comparison; canonical submission supersedes release's fresh-raw-workdir
   description. Preserve the remaining legacy/unspellable Unicode/firmlink and
   aliased allocation-root qualifications. Job-directory and quarantine fences
   appear once, as do resolved-cwd rows; the archive/verification suffix is kept.
2. `subfleet/daemon.py`, folder preparation after `_workspace`. #140 resolves
   every job's run folder for fencing, a lifetime reader pin and launch. Release
   applies the look's `derived` answer to the Git hold, calls `folders.present`
   for row subjects and turn cwd, tracks absence separately, and clears workspace
   deferral only when present. Keep release's rediscovery and absence logic;
   turns use its present cwd and detached jobs retain #140's canonical cwd.
3. `subfleet/daemon.py`, reservation blockers/reader row. #140 checks resolved
   cwd fences, excludes a writer's already-contested own key, and reserves a
   job-held cwd reader even for detached jobs. Release centralizes both turn
   folders in `_turn_fences`, folding absent names and selecting the hold's
   folder consistently with the early look. Turns use release's helper;
   detached jobs keep #140's cwd fence and reader. Retain the release folded
   absent in-place Git-folder check and the no-row-on-absence decision. Reader
   rows retain existing holder-keyed cleanup and writer contention.
4. `subfleet/folders.py`, `retiring` docstring/implementation. #140 extends
   ancestor fencing to detached writers and ignores other holders above them.
   Release adds Unicode canonical caseless folding for absent names and a
   retention-only range scan. Keep #140's described scope plus release's
   folded implementation; default exact nearest-first lookup is unchanged.

## #142 conflicts and resolutions

Nine hunks in two files; #140 is merged into the original #142 head, not rebased.

1. `daemon.py`, submit folder selection. #142 records a source/run `folder` for
   read-only detached jobs and allocated writers; incoming #140/#141 obtains
   row subjects and `unspelled` together through `_row_folder`, with the
   validated push-top exception. Keep that one lookup and also #142's separate
   canonical source-folder field. Do not call rediscovery on all detached jobs.
2. `daemon.py`, `job.submitted` payload. #142 emits the broadened `folder`;
   incoming emits a turn reader folder plus `unspelled`. Emit #142's `folder`
   and #141's conditional `unspelled`; the flag still refers to the row subject,
   not the detached source folder or whether push validation is available.
3. `daemon.py`, `_turn_folder`. #142 returns only its recorded Git/read hold;
   incoming supports rediscovery and introduces a separate `_recorded_folder`
   fallback. Keep incoming, preserving the actual-cwd fence separately.
4. `daemon.py`, `_fence_hold`. #142 accepts the legacy detached `fold` comparison,
   names `retiring`, tracks blocked keys/wakeups and priority waiters. Incoming
   spells turn folders with `present`, folds absent names via `_turn_fences`,
   and uses the rediscovered top consistently. Use the incoming preparation and
   helper only for turns; detached calls keep #142's comparison and #142's
   hold/queue/wakeup/priority code. The flags are deliberately not conflated.
5. `daemon.py`, checks before `_workspace`. #142 puts early turn and detached
   fence checks before workspace preparation; incoming moves the turn check
   into the rediscovery/error-handling block. Keep the incoming turn ordering
   and insert #142's detached-folder look after it and before `_workspace`.
   An unspelled in-place job may do read-only rediscovery; a held detached job
   runs no workspace snapshot or source `git worktree add`.
6. `daemon.py`, folder preparation. #142 refreshes a reader/in-place job's
   current folder with the exact resolved cwd that will be pinned. Incoming
   checks row spelling/absence and computes every job's resolved run folder.
   Keep both, refresh after that run folder is computed, and retain the
   presence-conditioned workspace-ready transition.
7. `daemon.py`, reserving blockers/reader rows. #142 rechecks recorded/current
   source folders with each tuple's legacy fold flag; incoming uses unified
   turn fences and the shared lifetime cwd reader. Keep detached source checks,
   incoming turn fences, and every detached cwd fence/reader, excluding already
   contested own keys. Do not duplicate the turn's reader when cwd equals hold.
8. `daemon.py`, hold payload/folder choice. #142 adds `retiring` and identifies
   the detached folder covered by it; incoming chooses the turn's folder with
   `_turn_fences`. Keep #142's metadata and detached matching with
   `folders.fences`; turns use the helper's `held_folder`. Replace the stale
   `run_fences` consumer in #142's turn folder choice, since incoming's helper
   now owns that calculation. Preserve signatures, blocked queue exclusion,
   priority ordering and late-fence wakeups.
9. `acceptance-contract.md`, C-8.4. #142 extends the writer contract to all
   detached recorded/current/source folders, legacy folded names and early
   checks; incoming supplies the reconciled #140/#141 clause. Add the detached
   rules to incoming, remove the now-fixed allocated-source exception, retain
   resolved-cwd lifetime pins, #141's spelling/push/trace/absence rules, and
   qualified queued-workdir protection. State the early and reserving hold
   equivalence only when retention is the only blocker. C-8.4 is one clause,
   with no duplicated job-directory fence or contradictory old alias gap.

`folders.py`, `retention.py`, and `test_retention_shared_folders.py` auto-merged.
Their combined behavior was inspected: retain both folding helpers with their
original callers, and retain release's NOCASE exact-root `worktree-in-use` pin.

## Consumers checked

The merge does not redefine `unspelled`, the absence flags or stored folder
fields. Every production occurrence of `unspelled` was checked with `rg`:

- Submit's `_row_folder(workdir, push_top)` call and `job.submitted` event:
  `write_target` for in-place writers, `folder` for readers/turns, `unspelled`
  only when the single recorded spelling is incomplete. `_row_folder` retries
  Git discovery when a restored `.git` warrants it. A supplied validated push
  top always returns `False`, even if currently absent.
- `_rediscovered`: consumes `unspelled` only for turns/in-place jobs, explicitly
  excludes push jobs, and asks `_row_folder` once per look. `_turn_folder` and
  `_recorded_folder` fallbacks, and `_write_target`, preserve that distinction.
- `_admit`'s early rediscovery, failure fallback, `_fence_hold`, later `derived`
  application, Git-folder spelling, `git_absent`, turn `run_absent`, combined
  `absent`, reserving `_turn_fences`, `status == "absent"` and `_folder_absent`:
  absence means a name could not be looked up, never permission to reserve an
  unspelled row. Early and transaction turn checks use the same discovered top.
- `folders.present`, `fold`, `under_git`, `retiring`, `exclusive_key`, `turn_key`,
  `parse`, `turn_holds`, `exclusive_inside`; exclusive keys stay exact
  `worktree:<folder>` strings. Folded scans select retention holders only;
  ordinary ancestor writers do not become blockers.
- Daemon writer precheck, turn/writer reservation, retention `_pin_reasons`,
  `_PIN_QUERIES`/unrecorded-tree pin, selecting transaction, archive commit,
  `Retirement._fence_job_folder` and `_quarantine_turn_pin`: the canonical
  exclusive keys and both shared row kinds agree at production and consumption.
- Attempt `evidence_json.run_folder`, `_launch`'s detached override,
  `_launch_dir` (including canonical resume comparison), `_spec`, retry,
  quarantine and final holder-keyed lease deletion, `Store.release_leases`:
  detached cwd rows are held by job, not attempt, and survive quarantine/retry.
- `_capacity_wait`, capacity-clock queue rebuilding, `blocked`, `queue_for`,
  `contested`/`queued`, `_leases_seen` and fence release wakeup; only taken keys
  enter the queue. Hold display/`why` and `render` consumers retain folder and
  fence metadata semantics.
- `salvage._git_env` and all its call sites: `_git`, stdin Git, add-all, tree
  restore, daemon read-only Git helpers/prefix lookup and conversations diff.
  Preserve removal of repository/pathspec environment and every `GIT_TRACE*`,
  C locale, and Trace2's three explicit `0` targets. This auto-merged unchanged.
- Contract clauses C-6.5, C-6.8, C-6.10, C-6.11, C-6.16, C-8.4, C-8.5,
  C-13.4, C-24.5 and C-26.14 were checked against these paths. The only
  textual conflict in #140 is C-8.4.


Additional #142 consumer audit: `_detached_folders` produces `(folder, fold)`;
its early `_fence_hold`, reserving source checks and `folders.fences` hold-folder
selection all retain the meaning **no recorded folder/write target** for that
flag. `folders.retiring_folded`/`folders.folded` keep NFC + casefold for this
legacy path. #141's independent `retiring(..., folded=True)`/`folders.fold` keep
NFD(casefold(NFD(path))) for **absent names** and are used by turn/absent-row
checks. These helpers are not substituted for each other. `_fence_hold`,
`_seen_held`, `_capacity_wait`, clocked queue reconstruction and `render` were
checked for `retiring`, `folder`, `blocked` and priority metadata; no consumer
continues to read the removed transaction-local `run_fences` for turns.

## Remaining pathname replacement gap (not fixed; follow-up audit below)

Source inspection supports the suspected gap. Reservation canonicalizes the
cwd into a pathname, fences and pins that string, and stores it in
`evidence_json.run_folder`. `_launch` puts that string back into the
job's worktree/workdir, then `_launch_dir`/`_spec` pass a pathname to the provider.
No directory descriptor or identity check binds that pathname to the reserved
inode. If the **resolved pathname itself** (or one of its ancestors) is moved
aside after reservation and replaced by a symlink into another tree, the
provider can follow it into a tree not covered by the reservation's fence or
row. Retargeting only the original submitted symlink is covered by the existing
resolved-cwd tests; it does not establish safety against replacement of the
resolved pathname. This merge does not add a fix or a regression test for this
gap. It remains a review item, confirmed here from source only.

## Validation at the reboot cutoff

Both heads were still pending validation at the cutoff; the results below supersede this pending status. Each suite uses a fresh `TMPDIR` under
`getconf DARWIN_USER_TEMP_DIR`, one foreground pytest under its own
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock` hold, with
`uv run pytest -q <files>`. Dependencies were installed into this workspace's
`.venv` using `uv sync --frozen`, with a workspace-local UV cache.

The required file union was obtained from
`git diff --name-only be7afd599 a6f73092a -- tests/`, branch-only test history
(`git log origin/release/217..3f06f9a1b --name-only -- tests/` and
`git log 3f06f9a1b..a9eacada8 --name-only -- tests/`), and the module-name guard.

Unit group, on both heads (seven files):

- `tests/unit/test_folders.py`
- `tests/unit/test_host_push_validated_top.py`
- `tests/unit/test_rediscovery_git_trace.py`
- `tests/unit/test_retention_case_alias.py`
- `tests/unit/test_retention_rediscovery.py`
- `tests/unit/test_retention_shared_folders.py`
- `tests/unit/test_test_module_names.py`

Fake group on #140 (four files), and #142 (these plus its detached fence file):

- `tests/fake/test_detached_cwd_pins.py`
- `tests/fake/test_policy_sandbox.py`
- `tests/fake/test_turn_retention_cwd.py`
- `tests/fake/test_writer_retention_fence.py`
- #142 only: `tests/fake/test_detached_retention_fence.py` (absent from #140)

#140 is validated in an archive of its exact merge commit at
`.git-local/validation140`, so preparing #142 cannot alter its imported code.
Two initial waits were cancelled before pytest started, then relaunched there;
they produced no test counts and their empty TMPDIRs were cleaned. Temporary
folders from earlier jobs were left alone. All source syntax, conflict-marker,
C-8.4 uniqueness and whitespace checks pass. Both merge parents and release
ancestry were verified. No new test or behavior was added for the pathname gap.


## Fix round after the host run

The assigned workspace starts at #142 `664f964fda78`, which includes #140
`2b2959774`. All changes stay in that workspace. The sandbox permits no writes
to either caller worktree or the shared Git metadata. The fix commits therefore
use workspace-local `.git-local` on `fix/admission-detached-nested-fence`, with
read-only object alternates. Use `git --git-dir=.git-local`. The original `.git`
pointer is unchanged. The initial checkout was clean. No remote or sub-agent was used. No live store or pre-existing process was
modified.

### Commits and attribution

- #140-compatible fix, applied on #142 as authorized: `ebf605101`,
  `Align writer and cwd fence expectations with #141 canonical reservation`.
- #142-only fix: `0d4edfa62`,
  `Expect retention fence metadata in rediscovery turn holds`.
- This report is committed separately on the same branch.

No production code changes were required. These two fixes reconcile the tests
and contract with behavior already present in the merged stack.

### Writer failures: stale expectations after #141

The `None` was the result of the test's `Store.one` lookup for
`worktree:<outside>`, then subscripted as `["holder"]` in
`tests/fake/test_detached_cwd_pins.py`. The reader pin and reservation exist;
the exclusive writer lease is on the nested folder the replacement symlink
resolves to. #141 commit `3410b4f0ac` deliberately spells an in-place writer's
row subject again with `folders.present` before reservation, whatever submit
recorded. Its commit message expressly includes an in-place writer's
`worktree:` lease. Release #141 (`a6f73092a`) already contains the same
`named, absent` / `write_target = spelled` code, so this is not a merge
resolution error. C-8.4 requires a row subject that is present and fully spelled;
C-6.5 requires the canonical exclusive write target.

The test now records and checks the submission target before replacing it,
asserts that submission evidence remains unchanged, then requires the exclusive
lease on the canonical nested target and no exclusive lease on the old
pathname. It still checks the lifetime cwd reader through reserved, starting,
running, finalizing and quarantined states, as well as final lease cleanup and
subsequent reclamation. C-8.4 clarifies that re-spelling the submitted target
can follow a replacement symlink; the reader row itself does not change
exclusive writer contention. This replacement is **before reservation**, not
the open post-reservation gap below.

### Case-alias hold: canonical submission spelling

`tests/fake/test_turn_retention_cwd.py` expected the original `rETIRED` spelling
when admission happened after quarantine. While the directory still exists,
submission records the filesystem's canonical `retired` spelling, per C-8.4
and #141's canonical row/fence rule (also canonical submission in #140). Moving
it later makes that name absent without changing the spelling already stored.
The test now requires both the recorded run folder and the hold's folder to
equal the canonical `nested` path before and after quarantine. No code change
is appropriate.

### #142 turn holds: keep `retiring`

The additional key is intentional #142 metadata, not an accidental merge
addition. C-8.4 already says a retention hold names its fence keys as `retiring`
and the checked folder. That applies to turns as well as detached jobs and
allows `why` to distinguish retirement from ordinary writer contention. The
early look lists only retirement fences; reservation can also list ordinary
writer or other held leases. Updated rediscovery assertions require `retiring`
on all nine previously failing cases. The mixed-blocker comparison requires
`leases == [writer, fence]` and `retiring == [fence]`, so an ordinary writer
cannot be mislabelled as retention.

C-8.4 now explicitly specifies this shape and omission when no retirement key
exists. C-6.16 remains unchanged: selected detached callers retain their
priority ordering, while turns remain attended in their separate pass and
queue. The extra display metadata does not alter that ordering, the queue
keys, capacity signatures, or which blockers each check finds.

### Open item: replacement of the resolved reservation pathname

Leave this unfixed here, as requested. A concrete sequence is:

1. Admission resolves the submitted cwd to canonical pathname `/tree/a`,
   checks retention fences and pins `/tree/a`, and records it as
   `attempts.evidence_json.run_folder`.
2. After reservation, another actor moves `/tree/a` (or an ancestor) aside
   and replaces that resolved pathname with a symlink into `/other/b`.
3. Detached launch restores the stored string `/tree/a` into the job and
   passes a pathname to the adapter; the eventual cwd lookup can follow the
   new symlink to `/other/b`. No reservation fence or reader row protects
   `/other/b`.

Retargeting just the original submitted alias after reservation is covered
by the existing pinned-cwd tests. Replacing the **resolved pathname itself**
is a different window. Current code stores a string, not a directory identity
or open descriptor, so it cannot prove that the launched directory is the
reserved one. A writable-branch validation does not bind that identity and
read-only jobs skip that validation.

A launch check would go in `subfleet/daemon.py:6436`, in `Daemon._launch`,
immediately after decoding evidence and before restoring `run_folder` at
line 6440. The adapter-boundary check belongs before the cwd is passed to
`_spec` / `_launch_dir` at `subfleet/daemon.py:6466`. For an identity-based
check, capture the reservation identity outside the store lock alongside
`run_folder` at `subfleet/daemon.py:5378`, and persist it with evidence at
`subfleet/daemon.py:5816`. Re-resolving once can detect the described symlink
replacement already in place, but by itself leaves a check-to-use race; a
complete fix must bind provider launch to the verified directory identity,
such as a suitable directory-descriptor launch mechanism. No fix or regression
test for this item was added. These line numbers refer to the unchanged
production file at this job's final code tree.

### Validation in this fix round

The shared lock prevented post-fix pytest from starting. The foreground
attempts used the requested `TMPDIR` prefix and `/usr/bin/lockf -k` on
`/private/tmp/claude-501/subfleet-suites.lock`, with an acquisition timeout.
The command under the lock was `uv run pytest -q <files>` with a task-local
JUnit output path. A foreground wrapper capped the complete attempt at 1,130 s
and would terminate only its own process group on timeout, keeping every hold
below 20 minutes. No pytest processes overlapped. `uv sync --frozen` installed
workspace dependencies with a workspace-local cache; validation #140 used an
archive of `2b2959774` with only the two corrected fake files copied in.

Completed acquisition attempts:

| Target | Files | Acquisition wait | Result |
| --- | --- | --- | --- |
| #142 | `test_retention_rediscovery.py` | 180 s | lockf exit 75; zero tests started; 183.9 s including cleanup |
| #140 | `test_retention_shared_folders.py`, alone | 180 s | lockf exit 75; zero tests started; 187.3 s including cleanup |
| #140 | `test_detached_cwd_pins.py`, `test_turn_retention_cwd.py` | 180 s | lockf exit 75; zero tests started; 188.3 s including cleanup |
| #140 | `test_retention_shared_folders.py -k <ordinary cases>` | 60 s | lockf exit 75; zero tests started; 68.9 s including cleanup |

The #140 host run was stopped at its 20-minute cap without a pytest result.
Accordingly a second attempt split the shared-folder file with `-k`, excluding
`fence_hold_against_a_model_of_the_fence`,
`early_fences_equal_reservation_before_and_after_quarantine`, and
`interleavings_never_delete_a_folder_named_by_a_turn`: 59 ordinary cases, with
the three expensive property cases designated for separate holds. That attempt
also could not acquire the lock. The remaining three property partitions were
not started; host validation is still needed for #140, and no pass is claimed.

Each completed attempt cleaned its own `TMPDIR` using
`python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)'`.
No detached runner or background build was created.

#### Host baseline, before these fix commits

Read `~/reviews/subfleet-hub/host-runs/run-fence-after141.log` and every available
`out-fence-141/` result. #142's shared-folder result appeared during this job:
62 passed in 862.61 s, completed at 14:51:04Z. The #142 fake group was then
queued; its output and the separate detached-fence output were not yet present.
These are **pre-fix** results, not claimed post-fix passes.

The per-file counts below combine the host's completed group totals and named
failures with an AST inventory of their parameterized cases. The totals agree:
u1 is 23 + 8 + 17 + 1 = 49, u2 is 56 + 17 = 73, and fake is
18 + 25 + 6 + 23 = 72. The failed case names identify the file-level split;
no completed host group reported skipped cases. Unrun inventories are counts
of expected pytest cases, not test results.

| File (relative to `tests/`) | #140 host baseline | #142 host baseline | Post-fix local result |
| --- | --- | --- | --- |
| `unit/test_folders.py` | 23 passed | 23 passed | unrun (23 cases/head) |
| `unit/test_host_push_validated_top.py` | 8 passed | 8 passed | unrun (8/head) |
| `unit/test_rediscovery_git_trace.py` | 17 passed | 17 passed | unrun (17/head) |
| `unit/test_test_module_names.py` | 1 passed | 1 passed | unrun (1/head) |
| `unit/test_retention_case_alias.py` | 56 passed | 56 passed | unrun (56/head) |
| `unit/test_retention_rediscovery.py` | 17 passed | 8 passed, 9 failed | #142 lock timeout; otherwise unrun (17/head) |
| `unit/test_retention_shared_folders.py` | cap, no completed result | 62 passed | #140 lock timeout; #142 unrun (62/head) |
| `fake/test_detached_cwd_pins.py` | 13 passed, 5 failed | queued, no output | #140 lock timeout; #142 unrun (18/head) |
| `fake/test_policy_sandbox.py` | 25 passed | queued, no output | unrun (25/head) |
| `fake/test_turn_retention_cwd.py` | 5 passed, 1 failed | queued, no output | #140 lock timeout; #142 unrun (6/head) |
| `fake/test_writer_retention_fence.py` | 23 passed | queued, no output | unrun (23/head) |
| `fake/test_detached_retention_fence.py` | absent from #140 | no output yet | unrun (66 cases on #142) |

The known six #140 and nine #142 failures are covered by the corrected
expectations, but an unavailable lock prevents claiming that they now pass.
#140 shared-folder completion and all post-fix pytest validation remain open
for the hub.

Static validation passed: AST syntax for the production daemon/folder modules
and all three changed test files; no conflict markers; one C-8.4 clause;
`git diff --check`. Production Python files have no diff. All fix commits have
the required `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer.
