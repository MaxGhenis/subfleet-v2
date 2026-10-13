# Provider active-attempt cap (D-RT1), review round 3

The missing attempt directory is a fake setup defect. A turn cannot reach the
production quarantine paths before its directory is created:

- `Daemon._launch` creates `attempt_dir(...)` before spawning the guardian,
  recording `starting`, or arming the start-grace deadline. Cancellation before
  that creation goes through `_unlaunched`, without a containment census.
- `_process_attempt` handles `reserved` first and returns. Only a launched
  `starting` attempt reaches the start-grace census; a late or absent
  `start.json` therefore does not imply an absent attempt directory. The
  running-attempt census and termination also follow launch.
- Recovery uses that same `_process_attempt` worker. A recovered `reserved`
  attempt outside `_pending_launches` goes through `_unlaunched` with
  `reserved-no-launch`, rather than quarantine. Recovered `starting` and
  `running` rows already passed directory creation in the previous daemon.
- `_finalize` creates the directory before its containment census and the
  `writers remain after exit receipt` quarantine.

The provider-cap fake calls `_admit` and `_quarantine` directly, bypassing
`_launch`. It now creates the attempt directory before quarantine, matching
launch's setup. The same setup is applied to the generated schedule's
quarantine action, which can select a turn. This allows `_resolve_quarantine`
to publish a turn's `trees.json` receipt as it does for a launched attempt.
The 24-case matrix and its provider-slot and wake-up assertions are retained.

Round-3 validation:

| Requested selection | Passed | Failed | Unrun |
| --- | --- | --- | --- |
| Quarantine retention matrix | 0 | 0 | 24 |
| Remaining provider-cap fake cases | 0 | 0 | 36 |
| `tests/fake/test_provider_cap_review_evidence.py` | 0 | 0 | 5 |
| `tests/unit/test_provider_active_cap.py` | 0 | 0 | 46 |

**0 pytest cases started; all 111 requested cases remain unrun.** Counts are
statically expanded from the decorators, not pytest collection. Seven sequential
foreground queue attempts each exited 75 (`already locked`): the matrix, the
remaining fake cases, evidence and units, then one retry of each full file.
There were zero lock holds and zero pytest processes started. Each queue wait
was configured for 45 seconds; each complete command group had a 1100-second
watchdog, below the 20-minute hold ceiling. Every task-specific
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-r194f3-$$/` was cleaned with the required
Python `shutil.rmtree` command. No detached runner or background build remains.
The hub must rerun the three files; its results at `ab2c5a44c` describe the input
head, not validation of this change. No additional suites or mutants ran here.

Static checks pass: the changed test file parses and the named-file
`git diff --check` is clean. Pinned dependencies were installed in this
workspace; its environment imports pytest 9.1.1, Hypothesis 6.168.1 and Subfleet
from this workspace. These checks do not establish a runtime test pass.

Commit `ee4069d24` contains the fixture correction; the following documentation
commit records this report. Commits use workspace-local metadata
`.git-local/provider-cap.git` on `feat/provider-active-cap`, directly based on
`ab2c5a44ccf1`, because the shared index lies outside the writable sandbox.
Inspect with `git --git-dir=.git-local/provider-cap.git --work-tree=. log`.
Only `tests/fake/test_provider_active_cap_fake.py` and this report are staged.

## Review round 2 (historical report)

The four host failures at `0c3a0d68a` exposed a missing admission wake-up.
`_resolve_quarantine` already changes the attempt to `lost` (or `interrupted`
for cancellation) and releases its remaining leases, so the provider count is
correctly freed. However, `_quarantine` releases lane leases before the waiter
arrives, and a read-only detached attempt has no other lease to release. The
waiter's next admission pass therefore sees no freed lease and skips its
backed-off capacity wait. The original test expectation agrees with C-6.4a:
verified containment or operator release frees the provider slot.

`d02a971ce` fixes that wake-up. Each pool keeps its own set of counted quarantine
attempt IDs under the admission lock. A quarantine absent from that set on the
next pass is freed capacity, including a recorded verified-empty census while
its attempt state is still `quarantined`. Both pools then use the existing
capacity-wait expedition rule, preserving a probe's own retry clock. No new
containment worker is introduced: confirmed containment and forced release
continue through `_resolve_quarantine`; the census-only regression exercises
the recorded-evidence rule directly.

The regression now crosses both source pools and both waiting pools, with
live, unverifiable and empty censuses and confirmed-dead versus force-release
resolution: 24 cases, formerly six. It verifies that an unsuccessful
confirmation still holds the slot, that resolution changes the attempt state,
and that a waiter resumes with no attempt lease left to release. Four additional
cases cover a recorded verified-empty census in each waiting pool after live
or unverifiable evidence. The admission clock is fixed so an expired one-second
recheck cannot conceal the missing wake-up. C-6.4a and C-6.10 document it.

Round-2 validation:

| Requested suite | Passed | Failed | Unrun cases |
| --- | --- | --- | --- |
| `tests/fake/test_provider_active_cap_fake.py` | 0 | 0 | 60 |
| `tests/fake/test_provider_cap_review_evidence.py` | 0 | 0 | 5 |
| `tests/unit/test_provider_active_cap.py` | 0 | 0 | 46 |

**0 pytest cases started; all 111 requested cases remain unrun.** The counts
are statically expanded from the test decorators, not pytest collection.
Each file was attempted twice, separately and sequentially. All six lock queue
attempts exited 75 (`already locked`): zero lock holds and zero pytest processes
started. Each queue window was bounded to 45 seconds (270 seconds configured
across six attempts); a foreground process-group watchdog bounded every whole
invocation to 1100 seconds, below the 20-minute hold ceiling. Each task-specific
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-r194f2-$$/` was cleaned with the required
Python `shutil.rmtree` command. No detached runner or background build remains.

Static checks pass: both changed Python files parse; `git diff --check` is
clean; the corrected local environment imports pytest 9.1.1 and Hypothesis
6.168.1 and imports Subfleet from this workspace. The pinned dependency files
were copied read-only from the caller's installed dependencies into local
virtual environments without contacting a remote. The final retries used the
matching Python 3.14 free-threaded interpreter, via
`UV_PROJECT_ENVIRONMENT="$PWD/.git-local/test-venv" UV_NO_SYNC=1 UV_OFFLINE=1`
and workspace-local `UV_CACHE_DIR`. This is environment validation, not a test
pass. The initial Python 3.12 environment did not load the installed Hypothesis
native module and was replaced for the retries; no suite started in it.

The hub must execute the three requested files at the new head. The new
regressions, both properties' examples, additional admission/scheduler suites
and the mutants were not run in this round. The host results at `0c3a0d68a`
remain evidence about the input head, not validation of this fix.

Commits: `d02a971ce` contains the daemon fix, deterministic regressions and
contract clarification; the following documentation commit records this report.

Git commits use workspace-local metadata `.git-local/provider-cap.git` on
`feat/provider-active-cap`, based directly on `0c3a0d68a26c`, because the shared
Git index is outside the writable sandbox. Inspect with
`git --git-dir=.git-local/provider-cap.git --work-tree=. log --oneline`.
Only `subfleet/daemon.py`, `tests/fake/test_provider_active_cap_fake.py`,
`docs/acceptance-contract.md` and this report are staged. The shipped policy,
caller checkout, live installation and network remotes are untouched.

## Review round 1 (historical report)

Both P2 fixes are implemented and committed. The shared test lock has blocked
local runtime validation; the exact outcomes below distinguish static checks
from executed tests. No live installation was changed.

`providers.<provider>.max_active_attempts` remains optional and off in the shipped
policy. Max chose Codex 40 (d1407); installation must set that value in the live
policy. This task did not install or edit it.

Changes:

- Provider counts include ordinary active attempts and unresolved quarantines
  with recorded live PIDs or unverifiable containment. Missing or unreadable
  containment evidence counts conservatively. A verified-empty census or an
  operator release frees the count. Existing pool/lane counts remain unchanged.
  Route snapshots, transactional revalidation, reservation and status use this
  same rule; quarantine resolution uses its existing attempt state transition.
- Provider FIFO follows routing, including transactional revalidation, regardless
  of lane, pool or parent caps. Existing competition is scoped separately and
  considers only demand for the selected provider. The class/tier traversal,
  family ancestry, lease FIFO and last fleet slot kept for an older waiter stay
  in force. A rejected route restricted to one provider keeps that provider's
  queue position; a mixed route with no selection has no provider FIFO peer yet.
- Timer and admission probes stay exempt. Status reports `provider_caps[].probes`
  and prints their held reservations separately, including quarantined probe
  leases conservatively. Each kind shares the lane's exclusive `slot:0`, so at
  most one probe reservation per enrolled lane is held. Counting reservations
  separately keeps recovery probing available at the work cap and makes its
  extra runtime budget visible. The attempt invariant is **counted attempts <=
  cap**; provider runtimes may exceed it by live probes or survivors explicitly
  force-released by an operator. Codex 40 must therefore
  be operated with the extra probe count in view; this is not a total-runtime
  bound of 40. The generated tests use fake probe leases and containment PIDs,
  not escaped provider processes.
- C-6.4a now specifies the quarantine lifecycle, probe exemption/accounting and
  the order of saturation, routing, FIFO/count checks and leases.

Regression inputs are copied from the review's evidence file to
`tests/fake/test_provider_cap_review_evidence.py`, with the two diagnostic
expectations inverted into regressions and a unique module basename. The
cap-40 restart and transient retry inputs are retained too.

The hub completed its original-head diagnostic run at 13:37:18 UTC: **5 passed
in 2.94 s**. Its quarantine diagnostic confirmed that another attempt reserved
with the survivor still quarantined; its two optional-cap diagnostics confirmed
that the mixed job waited despite an available Claude route. These are
hub-reported diagnostics, not passes of the fixed regression assertions.

Added coverage includes both aggregate and row-based provider counts, unreadable
containment, contained/forced release, changes to quarantine evidence between
routing and reservation in both pools, and separate probe status accounting.
The random schedule property generates arrivals, state changes, completions,
quarantines with live/absent/unknown survivors, releases, and both probe kinds.
It checks the independent count before/after admission and at each reservation,
and compares route/status counts after each action. It configures 50 examples.
The mixed-chain property varies lane, fleet/pool and parent caps, shared and
unrelated parents, both selected providers and one to three older opposite-
provider peers; it configures 60 examples. The existing default-policy
scheduler differential configures 300 examples.

Local validation:

- **0 pytest cases started, 0 passed, 0 failed; 0 lock holds.** Every completed
  queue attempt exited 75 (`already locked`). There were 12 configured 45-second
  queue windows (540 seconds): three baseline attempts, two provider/head batches,
  one regression batch and two runs of all three mutants.
- The copied assertions were queued against the unchanged implementation and an
  isolated `e541c1796` source tree; neither acquired the lock. They cannot yet be
  claimed failing at the base or passing at the fixed head. The hub's five
  diagnostic passes above corroborate the old behavior only.
- Provider/module coverage has 90 statically expanded cases: 46 provider units,
  38 provider fakes, five review inputs and one module-basename case. **All 90
  are unrun.** Executed property examples: **0/50** schedule examples, **0/60**
  mixed-cap examples and **0/300** default-policy differential examples.
- Requested regressions were queued in a regression batch and again in the
  final-head batch: `tests/unit/test_scheduler.py`,
  `tests/unit/test_scheduler_turns.py`, and
  `tests/fake/test_admission_lease_waiters.py`. They remain **unrun**. Additional
  route/status checks (`test_route_view.py`, `test_route_check.py`, and the
  quarantine-related functions in `test_status_payload.py`) are also unrun.
- The final-head batch selected **31 existing quarantine-related test functions**
  by examining each function's source for `quarant`, excluding the new cap
  modules. All remain unrun, including daemon/state containment, escaped probe
  recovery, turn quarantine/release, retention fences, salvage, native-session
  ownership, re-enrolment, notice and admission cases. Parameter expansions were
  not collected because pytest never started.
- Static checks pass: all seven changed Python files parse; test module basenames
  have no duplicates; all three mutation recipes apply once at every intended
  site and compile; `git diff --check` is clean. The shipped policy is identical
  to the base with SHA-256
  `47e1c5111b9cef28751257667cd8aa2eb9ba0ac39ef7e55beff30879a429e49c`.

The review mutation script is ported to `tools/pr194_review_mutations.py`.
Its running-only recipe follows the separate indexed ordinary/quarantine SQL
reads; the turn-exemption and equality recipes are unchanged. Artifact paths
are workspace-local and ignored, and it restores both sources in `finally`.
Both runs queued each mutant separately and restored the sources afterward.
The final run used the committed implementation's indexed census.

| Mutation | Final outcome | Executed failures |
| --- | --- | --- |
| Count only `running` | Queue exited 75; 0 cases started | Unverified |
| Exempt turns | Queue exited 75; 0 cases started | Unverified |
| Allow equality | Queue exited 75; 0 cases started | Unverified |

No mutant was killed or survived an executed suite. Logs and structured outcomes
remain ignored in `.git-local/provider-cap-mutations/` and
`.git-local/provider-cap-mutations.json`; source restoration leaves no diff.

For host reruns, each pytest invocation must have its own shared-lock hold:

```sh
TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-r194f1-$$/
export TMPDIR
mkdir -p "$TMPDIR"
UV_CACHE_DIR="$PWD/.uv-cache" /usr/bin/lockf -k \
  /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q \
  tests/unit/test_provider_active_cap.py tests/fake/test_provider_active_cap_fake.py \
  tests/fake/test_provider_cap_review_evidence.py tests/unit/test_scheduler.py \
  tests/unit/test_scheduler_turns.py tests/fake/test_admission_lease_waiters.py \
  tests/unit/test_test_module_names.py --hypothesis-show-statistics
suite_rc=$?
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMPDIR"
exit "$suite_rc"
```

Run the mutation tool separately with the same task-specific temporary-directory
setup; it acquires one bounded hold per pytest. The prepared base tree is
`.git-local/baseline-e541c1796`; pass its absolute path as pytest's `pythonpath`
override to execute the copied assertions against the original implementation.
The selected quarantine node inventory is `.git-local/provider-cap-quarantine-nodes.txt`.

Dependencies were installed in the workspace. Every suite wrapper bounds its
whole command group to 1100 seconds (less than 20 minutes), waits for at most
45 seconds in the lock queue, and cleans its temporary directory with the
required Python command. No pytest, detached runner or background build remains.
Temporary helper/log files were removed with Python cleanup; ignored workspace
metadata retains the mutation outcomes and isolated base input for host review.

Commits: `1c7068b46` ports the evidence inputs; `b0e376c6f` implements the fixes,
contract and extended tests; this report is finalized in the following docs
commit. Git commits use workspace-local metadata `.git-local/provider-cap.git` on
`feat/provider-active-cap`, based directly on `e541c17962c4`. The shared index is
outside the sandbox's writable roots. Inspect with
`git --git-dir=.git-local/provider-cap.git --work-tree=. log --oneline`.
Only named source, test, tool and Markdown files are staged; mutation logs/JSON,
baseline copies and dependencies are untracked/ignored artifacts. No remote,
caller checkout, live store, policy or process was touched.
