# Retention pacing port for Subfleet 2.1.11

Base: `08d6a09a0d566d2b94e14a109259f23001d95576` (#129, including #76).
Source: #109, `d5e6a7a6`, and `review-109-r1.md` §5 / `review-109-r2.md`.
All source work, synthetic measurements and foreground validation use the assigned
workspace or disposable temporary directories. No live Subfleet state is used.

## Before editing: #76 against the six §5 rules

The locations below refer to the **base commit**, before this port.

| §5 rule | #76 behavior, with base file:line | Gap |
|---|---|---|
| 1. Outcome order; cancellation / empty deadline / advancing deadline / completion; arm last | `subfleet/daemon.py:3400` marks cancellation but returns without setting `_last_maintenance`; `:3403` raises every deadline; `:3421` and `:3431` mark before arming successful catch-up/completion | Cancellation must rearm; empty deadlines must warn once with job count and rearm without raising; advancing deadlines must warn and raise. Preserve mark-before-arm. |
| 2. Advancement counts as progress; only no advancement waits an hour; update C-8.4 | `subfleet/retention.py:433` tracks measured sizes, archive work, starts, deferrals and reclaims in `acted`; `:603` returns `progressed`; `:388` and `:399` interrupt returns lose it. `subfleet/daemon.py:3408` resets advancing catch-up to 5 s but doubles no-progress waits from 10 s toward an hour | Keep progress in interrupted results, including cached size advancement. Resumed publication or partial verified deletion (`subfleet/retention.py:835` / `:845`) can lose both progress and remaining-work reporting. No-progress catch-up must wait the hour immediately. C-8.4 (`docs/acceptance-contract.md:213`) still describes doubled waits. |
| 3. Notice prune first, once per pass, never fails the pass | `subfleet/daemon.py:3404` calls it after maintenance; `:3436` deletes delivered service notices in one transaction | Interrupted/raising passes miss the notice prune; its exception fails retention. Move it first and log its failure. |
| 4. Both budgets, turn keep time, pins on every batch; pins re-asked at deletion | `subfleet/daemon.py:3392` passes both budgets, `turn_keep_s`, conversation `pins` and remote-less history limit on every call. `subfleet/retention.py:463` re-asks pins; `subfleet/retention_archive.py:758` calls `Context.pinned` inside the delete transaction | No argument or transaction gap; preserve and extend multi-batch wiring coverage. |
| 5. Leftover `retention:<job>` lease on half-removed job goes first | `subfleet/retention.py:471` recovers journals first; `:615` releases journal-less retention leases, then `:472` / `:516` select jobs by ordinary age | Journal recovery already goes first. Remember journal-less leftover jobs and select them before ordinary candidates, with archive and pin checks intact. |
| 6. Port the tests and measurement tool | `tests/fake/test_native_maintenance_startup.py:58` covers generic deadline retries, `:103` wiring, `:128` doubled catch-up waits; archive tests cover resumption. #109's two-clock model, real deadline passes and notice cases are absent; measurement tool absent | Port all #109 cases onto journal/batch boundaries, retain #76 coverage, and measure the full live-shaped counts in the foreground. |

## Required invariants

- Retention never deletes a byte its archive does not hold (including verified
  remote blobs and #76's explicit regenerable-file proofs).
- A pass that advances is retried promptly while work remains.
- A pass that does not advance waits the hour.
- The notice prune runs once per pass.

## Implementation and validation

Implementation commits: `5ddb520` and `24fc304`, after mapping commit
`4edf36b`. Measurement tool: `356fc82`. Each carries the required Claude Opus
5.5 co-author trailer. The full-count measurement used `356fc82`; `24fc304`
adds exceptional cleanup-progress reporting and does not change the successful
paths measured below.

| Change | Final location |
|---|---|
| Notice pruning precedes maintenance, once per daemon pass; failures log only their type and cannot fail retention | `subfleet/daemon.py:3387` |
| Cancellation takes precedence; empty deadline marks/warns/rearms; advancing deadline warns/raises; all arms follow status bookkeeping | `subfleet/daemon.py:3401` |
| Advancing successful batches keep #76's 5 s catch-up; no-progress batches wait the hour immediately | `subfleet/daemon.py:3421` |
| Interrupted results retain durable progress, using one injectable clock consistently for deadlines | `subfleet/retention.py:383`, `:406`, `:490`, `:585` |
| Orphan leases prioritize remaining bytes ahead of ordinary age order; priority survives an interrupted batch | `subfleet/retention.py:336`, `:524`, `:632`, `:794` |
| Publication and partial verified deletion count as advancement; incomplete journals report remaining work, while unchanged blocked remnants report no progress | `subfleet/retention.py:860`, `:866`, `:901`; `tests/unit/test_timers_retention.py:294` |
| Size attempts count as advancement after saving a size or recording a deferral, rather than before a cancelled walk | `subfleet/retention.py:741`, `:750` |
| #109's Hypothesis two-clock model, deadline-before/after-prune, separate turn budget, pins, notices and wiring ported onto real archive/batch boundaries | `tests/fake/test_native_maintenance_startup.py:133`, `:259`, `:277`, `:312`, `:328`, `:340`, `:363`, `:399` |
| New regressions for retained sizing progress, cancellation without a saved size, orphan priority and interruption; #109's committed-prune regression retained | `tests/unit/test_timers_retention.py:192`, `:223`, `:246`, `:259`, `:276` |
| Cancellation after progress, every mark-before-arm path, and first/once notice order | `tests/fake/test_native_maintenance_startup.py:471`, `:485`, `:496` |
| C-8.4 and the archive driver's pacing documentation rewritten around retained advancement | `docs/acceptance-contract.md:214`, `docs/desktop/retention-archive.md:665` |

The archive builder, verified deletion, both pool budgets, `turn_keep_s`,
conversation pins inside each delete transaction, and remote-less history limit
remain #76's. The wiring test now checks two consecutive advancing batches.

The wiring and real-pass tests preserve C-26.12's inside-transaction
conversation pin re-check. Validation uses the locked Python 3.14.7 environment,
pytest 9.1.1 and Hypothesis 6.168.1. Final test results are recorded below.


## Synthetic measurement

Command (foreground, supervised to 540 s; completed successfully):

```
.venv/bin/python tools/measure_retention_cost.py --jobs 3800 --worktrees 890 \
  --files 1 --max-passes 2 --pass-s 180 --worktree-bytes 280000 --keep-store
```

All 3,800 terminal detached jobs had a directory and a 112-byte artifact; exactly
890 had an allocated worktree, spread throughout age order. Each held a
280,000-byte payload, scaling the live ~0.28 GB average down by 1,000 without
reducing directory or worktree counts. Fixture registrations are copies of one
Git-created detached worktree's real HEAD/index/reflog/admin data, with independent
backlinks; registration counts and sampled HEADs are checked. Production archive
creation, bundle verification, pin checks, transactions and verified deletion run
unchanged. Seeding uses one fixture transaction and is excluded from timing.

| Pass | Jobs remaining | Cumulative pruned | Wall seconds | Parent CPU seconds | Child CPU seconds | Result |
|---|---:|---:|---:|---:|---:|---|
| Seed | 3,800 | 0 | excluded | excluded | excluded | 890 worktrees |
| 1 | 3,768 | 32 | 157.293 | 2.385 | 2.683 | advanced; more waiting |
| 2 | 3,736 | 64 | 78.391 | 2.443 | 2.571 | advanced; more waiting |

**Time to the first committed prune: 137.514 s.** Both passes reported
`progressed=True`, with no errors, and cumulative pruning was monotonic. A separate
read-only check verified all 190 stored entries (3,935,022 bytes) against their
manifest SHA-256 digests, including all 64 original job artifacts and all 14
retired worktree payloads, compared byte for byte with the fixture inputs. The
production scheduler selected a 5 s catch-up wait (4.9997 s fast-forwarded by the
probe). #76 skips whole-store sizing when the count already proves pressure;
this measurement reached pruning without walking 3,800 trees first. The
retained-sizing regression separately proves cursor/cache advancement across an
interrupted real pass, then pruning in its next pass.

The same synchronous tool's 40-job/8-worktree smoke check forced an advancing
archive deadline: pass 1 archived but pruned nothing (`progressed=True`,
`interrupted=deadline`), then the worker clock selected a 0.5 s retry and pass 2
pruned 30. Its first prune was 74.032 s including the simulated worker wait.

Limits: these are this loaded machine's synthetic timings, not a prediction for
live content or a full 3,300-job catch-up. Holder checks return empty because no
other process uses these fixtures; no global lsof listing is included. Idle
waits are fast-forwarded, but archive wall time and CPU are real. Parent and
child CPU exclude fixture creation and cleanup. Two earlier seed attempts were
stopped within their foreground supervision budget; they yielded no benchmark
claims. Cleanup is kept outside the measured passes. No live `~/.subfleet`,
application state, running daemon or `~/chief-of-staff` was accessed.


## Mutation checks

Each mutant was applied alone, tested in a foreground subprocess supervised to
540 s, then restored from the original bytes. All nine were killed by an assertion
or the incorrect outcome in the named regression; no production mutation remains.
The initial six-mutant run took approximately 306 s of subprocess time. Three
additional cleanup-progress mutants took 85.64 s. The restored cleanup regression
passed again (8.00 s); a Git diff confirmed only this report remained uncommitted.

| Mutant | Killing regression | Seconds |
|---|---|---:|
| Cancellation loses to progress, so a cancelled advancing pass raises | `test_cancellation_wins_even_after_a_prune` | 36.55 |
| `_last_maintenance` is set before cancellation's mark | `test_every_rearm_records_its_status_before_setting_last_maintenance[result0]` | 227.28 |
| Driver decides progress using pruning alone, discarding sizing advancement | `test_a_pass_that_advances_sizing_but_pruned_nothing_is_retried` | 25.99 |
| Every `more` batch is retried promptly, including a non-advancing batch | `test_retention_catch_up_waits_the_hour_only_when_a_pass_changes_nothing` | 7.83 |
| Interrupted maintenance always returns `progressed=False` | `test_an_interrupted_sizing_pass_retains_advancement_and_resumes_at_the_next_job` | 4.01 |
| Ordinary age order replaces leftover-lease priority | `test_a_journal_less_leftover_lease_takes_priority_over_older_jobs[retire]` | 4.64 |
| Successful publication does not count as progress | `test_partial_verified_deletion_advances_until_only_a_blocked_remnant_is_left` (publication assertion) | 30.76 |
| Partial verified deletion does not count as progress | Same regression (partial-deletion assertion) | 28.42 |
| Incomplete cleanup forgets its remaining-work signal | Same regression (initial `more` assertion) | 26.46 |


## Foreground test results

The requested globs collect 409 unique pytest items (408 before the new
incomplete-cleanup regression). They are run in slices of at most 20 collected
items or as individual properties. Remaining broad suites and individual
properties use a 540 s foreground supervisor; no test runs in the background.
Every completed slice was under 10 minutes. The largest completed
slice reported 437.51 s. Repeated verification of the 20 timer-retention items
passed in 48.88 s after the final recovery change; repeated items are not counted
twice below.

| Coverage | Unique items | Result |
|---|---:|---|
| Native maintenance startup / retention timers | 38 + 20 | passed |
| Turn budgets / worktrees / QoS / optional-transient salvage / daemon timers | 7 + 10 + 8 + 15 + 4 | passed |
| Archive examples / round-trip properties | 109 + 2 | passed |
| #81 notes | 38 | passed |
| Regenerable output examples / property | 26 + 1 | passed |
| Salvage examples / retention-salvage property | 31 + 1 | passed |
| Unindexable salvage | 86 | passed |
| Worktree sweep races | 12 + 1 | passed |

Hypothesis settings remain at their checked-in defaults: the ported two-clock
model uses 80 examples; archive round trips use 10 each, regenerable output uses
8, salvage protection uses 20 generated plus 5 explicit, and the sweep schedule
uses 40 generated plus 15 explicit. The #81 verified-deletion property (200
examples) and the unindexable snapshot oracle (40) also passed. No examples were
removed or reduced to fit a slice. Individual final property times were 58.05 s
and 53.43 s for the archive round trips, 177.62 s for regenerable output, and
204.37 s for salvage protection and 402.10 s for the sweep schedules.

**All 409 unique items passed.** The native-startup/timer slice originally passed
57 items in 437.51 s; the final 20-item timer rerun includes the added recovery
regression. The other files were sliced into at most 20 items, except the small
combined 44-item budget/worktree/QoS/optional-salvage/daemon-timer suite (60.24 s).
Fresh collection confirmed 409 items in 11.13 s.


## Delivery

Shared Git metadata lies outside the writable sandbox. The requested fallback
uses workspace-local `.git-local`, on `port/retention-pacing-2111`; the caller's
checkout and shared refs are untouched, and nothing is pushed. The local Git
directory is ignored and never committed. All port commits carry:

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

The final bundle is `docs/reports/2026-10-05-retention-pacing-port.bundle`, ignored
rather than committed. Its sole prerequisite is
`08d6a09a0d566d2b94e14a109259f23001d95576`; its head is
`refs/heads/port/retention-pacing-2111`, including this report's delivery commit.
Verification uses `git bundle verify`, `git bundle list-heads`, and an independent
bare-repository import seeded only with that prerequisite. The exact head hash
is supplied with the delivery.
