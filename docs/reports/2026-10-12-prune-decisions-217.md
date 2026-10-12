# PR #36 decision prune port to release/217

Ported from local `origin/feat/prune-decisions` (`3c8a6b862`) onto
`be7afd5992a660b68c3ccd2edf3fc16e92578e50`. The three final source files
(`subfleet/prune_decisions.py`, `tests/unit/test_prune_decisions.py`, and
`tests/unit/test_prune_restore.py`) and PR #36's migration section were copied
first. The port then corrected the reader promises, extended the before/after
proof, added release/217 tests in `test_prune_217.py`, and made C-11.5 describe
the tool. This only lands the tool; applying it remains Max's decision d204.

## Reader verdicts

| Reader on release/217 | Verdict and promise |
| --- | --- |
| `daemon.py:3072`, `_why_job` (`why`) | Must remain unchanged. Keep the greatest `decision_id`. Compare the exact stored JSON and `evaluated_at`, which supplies the answer's `decided_at` and refusal-event comparison. |
| `offline.py:275`, `show_job` (`runs show`) | Must remain unchanged. Keep the greatest `(evaluated_at, decision_id)`. Compare exact stored JSON separately from the daemon ordering; these may choose different rows. |
| `retention_archive.py:312`, `job_rows` | Intentionally changes for a pruned job: all-row history becomes the retained subsequence in `decision_id` order, with every surviving row unchanged. An archive made after pruning holds fewer decision rows than one made before. |
| `store.py:789`, `Store.list_decisions` | Intentionally changes in the same way. `rg -n list_decisions subfleet tests` found the definition and test callers only: `test_probe_turn`, `test_routing_end_to_end`, `test_admission_visibility`, and `test_capacity_wait_backoff`. No production caller uses it on this line. |

Existing retention archives are untouched, retain their original `rows.json`,
and remain verifiable, readable, and restorable. `retention_archive.restore`
restores files and Git objects; it does not reinsert `rows.json` into the live
store. The whole-store backup and gzip deleted-row backup preserve the original
history for recovery. No original full-history row count is promised after a
prune.

The keep set remains the union of every attempt-linked decision, every row of
an unfinished job, both latest-row choices, and both latest attempt-less
choices. The new proof checks all jobs, including those with no decisions,
rather than only jobs losing rows, and includes all stored fields each latest
reader consumes. It repeats after `--vacuum`. The generated-store test also
calls the actual `Daemon._why_job` with fixed admission context and actual
`Offline.show_job`, using independent SQL copies for raw-byte comparisons.

## Assumptions checked

- **Schema 7:** `Store.SCHEMA_VERSION` is 7; all disposable stores use this
  build. The PR's older-schema refusal now exercises version 6 against build 7,
  before a writing `Store` can migrate it. Its newer-schema refusal remains.
  The dry run can inspect an older initialized store without migrating it.
- **No inbound decision foreign keys:** inspected `store_schema.sql` and
  `store.py` migrations and added `PRAGMA foreign_key_list` introspection for
  every schema-7 table. No foreign key targets `decisions`. The schema has no
  triggers, also checked by introspection.
- **Complete table coverage:** schema 7 has 15 application tables:
  `schema_version`, `lanes`, `readings`, `closures`, `jobs`,
  `job_owned_branches`, `job_pushes`, `attempts`, `artifacts`, `notices`,
  `decisions`, `leases`, `actions`, `events`, and `service_notices`.
  `sqlite_master` enumeration covers all 15. The 13 tables other than
  `decisions` and `events` are counted and hashed; the latter two have their
  own proofs. A synthetic additional BLOB table verifies dynamic coverage.
- **Daemon exclusion:** inspected this daemon's exclusive nonblocking `flock`
  on `daemon.lock` and its close path. Kept the PR's lock refusal and
  whole-pass lock tests, and added a real disposable `Daemon` test in both
  directions (daemon blocks prune; prune lock blocks daemon startup).
- **Audit tolerance:** `events.kind` is unrestricted TEXT. `Store.transaction`
  appends one event for each changed delete batch. `Store.list_events` and
  generic daemon event inspection accept arbitrary kinds; recovery and `why`
  use explicit kind filters. `decisions.pruned` has valid JSON and null job,
  attempt, and lane ids. The new test reads these audit rows through the store
  and compares actual job answers; the PR's append-only/event-count checks
  remain.
- **Terminal states:** schema CHECK and retention's terminal set agree on
  `succeeded`, `failed`, `cancelled`, and `lost`. Generated examples cover
  these plus `queued`, `waiting`, and `running`; all unfinished histories stay.
- **Archive compatibility:** a disposable pre-prune archive with a stored job
  file retains every byte and all four decision rows, passes `check_archive`,
  and restores through the real restore API after the live history shrinks to
  one row. This is a minimal archive without Git objects, not the full
  retention/Git round-trip suite.

## Invariants and validation

The new Hypothesis property (`max_examples=40`, plus an explicit example
covering all seven job states and divergent reader orderings) independently
re-derives the keep set, proves keep/delete form a disjoint complete partition,
proves no unfinished job row is deleted, checks both unchanged readers for
every job, checks the two full-history readers return exactly the unchanged
retained subsequence, verifies deleted-row backup content, and executes the
published shell restore of the tool's actual schema-7 backup. Every table,
including the original decisions table, must then match exactly.

Kept the PR's existing Hypothesis restore property (`max_examples=30`) for
exact table contents, arbitrary WAL/SHM sidecars, and shell metacharacters in
paths. Also kept its report-save differential property (`max_examples=30`,
two explicit examples) and all its unit scenarios. The port also repeats the audit kind/count proof
after `--vacuum`; a new regression removes the appended audit rows between
the first proof and the rebuild proof and requires a restore-bearing failure. The PR's partition and
unfinished-job coverage were example tests; the port adds generated coverage.

Validation is in progress: one foreground pytest command covers the three
prune test modules and `tests/unit/test_test_module_names.py`, under
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, with a unique
`TMPDIR` and the required Python cleanup. Final counts will be recorded after
it completes. `git diff --check` passes.

Unrun: the full repository suite, full retention/Git archive round trips, and
any dry run or apply against a live store. No `~/.subfleet` or
`~/chief-of-staff/state` data was read or written. No network Git remote or
sub-agent was used.

## Commit storage

The shared Git index refused `index.lock` with `Operation not permitted`.
Commits therefore use workspace-local `.git-workspace` metadata, on
`refs/heads/feat/prune-decisions-217`, with the original repository's objects
as read-only alternates. The shared worktree HEAD remains detached at the base.
All stages name specific files; there is no push or history rewrite. Each
commit carries the requested `Co-Authored-By` trailer. The hub can carry these
commits directly from this metadata; no logs, JSON evidence, or bundles are
committed.
