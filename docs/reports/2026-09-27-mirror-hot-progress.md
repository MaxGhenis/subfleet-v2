# Mirror flag progress during a slow full pass

The 2026-09-27 incident left flags unchanged across accounts for about six
hours while a full pass advanced through its inventory at roughly one entry
per second. At `03de432d`, both timers share one worker and `mirror.lock`, and
`run_hot` only spreads records: it does not sync existing flags.

The full pass now services flag-only hot passes synchronously at checkpoints.
The lock and daemon worker remain unchanged. The ordinary hot timer also syncs
archive, star, title and setting changes, using every known copy of each
changed session and retaining held candidates for retry.

## Scheduling and invariants

- A service is due `mirror_hot_interval_s` (normally 2 seconds) after the full
  pass starts or the previous embedded service finishes. It starts at the next
  checkpoint between work units, including every 64 directory entries while
  listing and between individual entry reads. A zero interval disables it.
  The inventory can continue for hours without postponing all flag work until its end.
- This bounds scheduling, not arbitrary filesystem latency or hot-pass runtime.
  The current operation or active flag transaction must finish; the hot pass
  must inventory its changed folders and publish. Cold startup must discover
  every possible copy before safely deciding flags. No wall-clock guarantee is
  possible for a filesystem call or thread that stops making progress.
- One flock holder owns every account write and all state publication. Embedded
  service executes synchronously under that same `LOCK_EX|LOCK_NB` flock;
  another process or thread loses the lock and touches no sidecar. The existing
  four full/hot writer pairings remain excluded.
- The helper's inventory containers and payload reference counters are separate;
  projected values and completed folder listings are immutable and shared.
  The journal is shared by the serial writers. Known writes invalidate both
  inventories even when a directory timestamp does not change; an invalidation
  arriving during a listing survives that listing. Embedded service changes only
  flags, titles and settings, so it cannot invalidate the full pass's identity,
  revival, stale-empty repair or pruning decisions.
- A flag decision through its merge-base publication is indivisible with
  respect to another mirror decision. Existing guarded writes and rollback stay
  intact. After embedded service the full pass refreshes its flag snapshot.
  Refresh itself services hot checkpoints; an intervening candidate decision
  invalidates that snapshot. After two invalidated refreshes, the full decision
  is held for the next pass rather than committing a stale merge base.
- Unknown-copy and unknown-folder holds are shared by full and hot paths.
  Hot decisions include all copies of their candidate identities, retain unseen
  merge bases, and retry held identities even without another directory edit.
  A failed store listing is an error in either pass. `flags_held` and `held_by`
  remain visible independently for each pass.
- Every hot start and finish updates only `hot` and its load-gap evidence.
  Full `pass`, `updated_at` and `last_ok_at` remain authoritative for full-pass
  health. Hot progress cannot disguise a stalled full pass. Status text and JSON
  include the separate hot state and held causes.
- The full ten-minute sweep still finds in-place edits; only a completed sweep
  advances its clock. Creation-only revival, pruning policy, regular-file-only
  reads and guarded atomic publication are unchanged.

## Inventory costs under GIL contention

Cold reads use the opened descriptor's stat instead of a preliminary path stat.
The raw nonblocking descriptor avoids stream-wrapper metadata calls and two
`fcntl` calls to clear `O_NONBLOCK`; regular files tolerate it. Reads use 64 KiB
chunks. The post-read path stat remains: a concurrent in-place edit or rename
must not create a valid cache entry for stale bytes. Warm sweeps retain
`DirEntry` and reuse `DirEntry.stat()`.

Production interpreter: CPython 3.14.4, macOS arm64. Each scan ran with three
busy Python threads in the same process, the default 5 ms switch interval,
200 synthetic approximately 12 KiB records, and five rounds per phase.

| Inventory phase | `03de432d` median entries/s | Changed median entries/s |
| --- | ---: | ---: |
| Cold reads | 78.694 | 140.437 |
| Warm stat sweep | 1,157.644 | 1,835.391 |

The shared machine was heavily loaded and samples varied widely. These are
observed medians, not a production speedup or latency guarantee. The warm path
still needs one metadata query per swept file; its timing change is especially
sensitive to scheduling. Raw samples, platform and commands are in
[`2026-09-27-mirror-inventory-contention.json`](2026-09-27-mirror-inventory-contention.json).
Reproduce with `.venv/bin/python tools/measure_mirror_inventory.py
[--revision 03de432df957] --entries 200 --rounds 5 --busy-threads 3 --scratch .`.
The harness measures `_scan` directly and never touches the installed store.

## Regression evidence

Baseline tests load the exact `03de432df957` mirror source from `git show` into
the test process; the checkout and live daemon are untouched.

| Added checks | Count | On `03de432d` |
| --- | ---: | --- |
| Warm/cold full-pass progress before the next entry; hot archive/unarchive, star/unstar and title; unknown-copy holds/retry; transaction exclusion; refresh progress and bounded refresh retry | 12 | Fail |
| All full/hot writer pairings | 4 | Pass: the original flock guarantee is retained |
| Cold stat count and warm DirEntry reuse | 2 | Fail |
| Read races, chunk completeness, FIFO/directory/link rejection | 6 | Pass: safety is preserved |
| Hot health/holds in full healthy, running and stalled states; CLI text/JSON | 6 | Fail |
| Archive coherence with unchanged destination directory metadata; slow directory-listing progress | 2 | Fail |
| Full-to-hot invalidation; cancelled-listing retry | 2 | N/A: these exercise the new inventory-fork protocol |
| Hot sync from an unchanged unlisted folder, followed by truthful full hold and retry | 1 | Fail: other accounts remain unarchived at the due checkpoint |

The progress checks advance a monotonic clock at a read hook and inspect the
other account and sidecar before the full scan advances. Both a warm and a cold
pass fail this assertion on the baseline. They also verify that resumed full
work cannot put back the old flag value or merge base. The two additional
coherence regressions fail semantically on the baseline: the destination keeps
its old archive flag, and a listing advances before due flag sync runs. All
four coherence checks pass on the final implementation (147.65 seconds).

Validation uses the requested production interpreter and `UV_CACHE_DIR=.uv-cache`:

- `uv run pytest -q tests/unit/test_sessions_mirror*.py tests/unit/test_mirror_flags*.py`:
  **251 passed in 244.49 seconds** on the final implementation, including
  all coherence and scheduled unlisted-folder checks. The final test-only
  frozen-clock refinement also passed its focused pair (12.39 seconds).
- The four inventory-count tests explicitly disable the hot timer so they count
  one inventory. Every original cache assertion remains. Before that adjustment,
  a loaded run legitimately read a cold inventory twice (1,280 rather than 640)
  and reported 1 failed / 245 passed; the corrected run passed all 246 checks
  then present, and the final 251-check run above is clean.
- The existing stateful flag model explicitly disables scheduling because each
  rule models one decision/publication and injects its race once. Its 100
  examples × 30 steps are unchanged: **1 passed in 267.74 seconds** in a separate
  focused run. Embedded decisions are covered by the 16 progress tests.
- The broad run exposed one further single-decision test assumption:
  `test_an_unlisted_folder_unchanged_since_its_listing_is_read_by_name` expected
  zero holds. Under an embedded service, hot sync safely reads that unchanged
  folder by name and archives every copy; its write changes the directory, so
  the following full refresh must conservatively hold the now changed,
  unlistable folder. The original test now disables cadence and retains all
  assertions. A new deterministic scheduled test verifies the hot convergence,
  truthful full hold, preserved base and successful retry once listing works.
- `uv run pytest -q --ignore=tests/live`: **6,326 passed, 121 skipped,
  136 failed, 44 errors in 4,784.96 seconds (1:19:44), exit code 1**.
  The exact failure list is retained in
  [`2026-09-27-mirror-full-suite.txt`](2026-09-27-mirror-full-suite.txt).
  This broad run began before the final cache-coherence refinement; the
  complete 251-test mirror/flag run above validates the final implementation.

The full run's 180 failure/error reports comprise 142 explicit
`macOS boot identity is unavailable` errors, eight explicit `/bin/ps` permission
denials, seven shared Swift setup failures (`SwiftUIMacros.StateMacro` could
not load because Xcode's `swift-plugin-server` returned a malformed response),
22 other process/liveness/fixture assertions, and the one mirror scheduling
fixture corrected above. The 22 remaining assertions were not individually
reduced to a root cause. Eight representative unrelated failures reproduce
with the exact baseline mirror module. Every `test_sessions_mirror*.py` case
also passed inside the broad run; no other mirror failure appeared.

The sandbox refuses the assigned worktree's external Git index
(`/Users/maxghenis/subfleet-v2/.git/worktrees/20260927-155443-mirror-hot-pass-astra/index.lock`).
The assigned HEAD therefore remains `03de432d`. Commits are built inside this
workspace with that exact parent and supplied in the verified
`mirror-hot-progress.bundle`; no repository outside the workspace is changed.

Implementation commits, in order:

- `67f116095d0885ec84900e41d83227090ae489ec` — cooperative flag progress,
  inventory optimization, health, contract and regressions.
- `37f6bcd9bb1ef98a70ea8501b24795dc19ef6e28` — deterministic one-transaction
  stateful-model fixture.
- `33ad909680cc8ce440c59adfb5424e06f24eb32d` — bidirectional invalidation,
  listing checkpoints and cancellation coherence.
- `580bb6a3de6986ee2b4a4d56b776c3a404ef4588` — scheduled unlisted-folder
  convergence, truthful holds and deterministic single-decision coverage.

All commits carry the requested `Co-Authored-By: Claude Opus 5.5
<noreply@anthropic.com>` footer. This report is committed as a separate final
step in the same bundle.

