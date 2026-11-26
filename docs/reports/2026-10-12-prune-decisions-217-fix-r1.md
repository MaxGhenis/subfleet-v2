# PR #192 review round 1 fixes

Base: `428e5dc530c9`. Work is confined to the assigned detached worktree;
no live store, network remote, or sub-agent is used.

Implementation commit: `97a678fa0`. Original review probes: `0961cb5a1734`.

Status: fixes implemented; pytest verification blocked by the shared test lock.

Changes:

- P1: the plan and all before-fingerprints now share one read transaction.
  The whole-store copy must match every planning-snapshot table's row count
  and canonical content digest before any DELETE. Table inventory drift also
  fails. Successful copy fingerprints and `verified: true` are saved in the
  report. The existing gzip verification remains before DELETE.
- P2: expected schema-7 table names come from this build's schema applied to
  an in-memory database; the source is checked read-only. Missing tables are
  refused before opening a writer. `Store(initialize=False)` opens an existing
  current-version database without migrations or schema DDL, preserving even
  missing indexes in the baseline and backup. Ordinary Store initialization
  keeps its default behavior.
- P2: the complete post-delete event table is counted and hashed under
  `fingerprints.after.events-all`. After VACUUM its count and digest must
  match, including new prune audit payload bytes. Existing append-only,
  audit-kind/count, other-table, kept-row, reader and integrity proofs remain.
- `docs/migration.md` documents that later archives contain no pruning marker
  or original count; provenance requires the prune report and pre-prune
  backups. No archive marker was added. C-11.5 now states the content-equal
  backup invariant and complete post-VACUUM event proof.

The reviewer's nine probes were copied byte for byte in commit `0961cb5a1734`.
The fixed test module adds twelve cases (21 prepared cases total), covering
all-table content/inventory drift, BLOBs, source drift after planning,
verification ordering, missing-index preservation, and both semantic and
whitespace-only audit payload changes.

| Finding / original reproduction | Base `428e5dc53` | Fixed implementation |
| --- | --- | --- |
| P1: `test_well_formed_whole_backup_with_changed_reader_bytes_is_refused` | Unrun: shared lock | Unrun: shared lock |
| P2: `test_schema_7_missing_table_is_refused_without_prebackup_repair` | Unrun: shared lock | Unrun: shared lock |
| P2: `test_after_vacuum_changed_prune_audit_payload_is_rejected` | Unrun: shared lock | Unrun: shared lock |

No before-failure or after-pass execution result is claimed. The isolated base
source has SHA-256
`4a1ac40bf4d3804858d3a13ca4823cea007122ea66c60cfeca21bc26516851a7`,
matching the review, and its probes match the reviewer file byte for byte.

Tests: **0 passed, 0 failed, 0 collected/executed**. Four foreground attempts
stopped at lockf with exit 75, before pytest started: base waits of 30, 30 and
180 seconds, then a fixed-head wait of 30 seconds. No lock hold was acquired.
Each used its own `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-<name>-$$/` and
the required Python `shutil.rmtree(..., ignore_errors=True)` cleanup. All
commands finished; no runner or build was left behind.

Static checks passed: AST parsing of both changed implementation modules and
the regression module; `git diff --check`; base-source and original-probe byte
checks. Dependencies are prepared offline from read-only cached wheels using
CPython 3.12.14, pytest 9.1.1 and Hypothesis 6.168.1. `UV_NO_SYNC=true` avoids
an editable build; pytest imports source through configured `pythonpath`.

Hub retry selections (one foreground pytest process per separate lock hold,
under 20 minutes, with task TMPDIR cleanup):

```sh
# Base: use the unchanged .pr192-base snapshot and the workspace virtualenv.
UV_PROJECT_ENVIRONMENT="$PWD/../.venv" UV_CACHE_DIR="$PWD/../.uv-cache" UV_NO_SYNC=true UV_OFFLINE=true /usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q tests/unit/test_pr192_review_r1.py

# Fixed head: run from the workspace root.
UV_CACHE_DIR="$PWD/.uv-cache" UV_NO_SYNC=true UV_OFFLINE=true /usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q tests/unit/test_pr192_review_r1.py tests/unit/test_prune_decisions.py tests/unit/test_prune_217.py tests/unit/test_prune_restore.py tests/unit/test_test_module_names.py tests/unit/test_store_ids.py tests/unit/test_store_migration_2.py tests/unit/test_store_snapshot_plans.py
```

The base snapshot is ignored and workspace-local, extracted from `428e5dc53`
with `git archive` (`subfleet/` and `pyproject.toml`) plus the original nine-case
probe file from `0961cb5a1734`. Recreate it if only the commits are transferred.

The sandbox rejected writing the shared Git index. Commits therefore use
workspace-local `.git-local` metadata and a local `feat/prune-decisions-217`
branch rooted at the assigned base; shared metadata remains untouched.

Unrun: all base and fixed-head pytest selections above, demonstrated regression
before/after results, full repository suite, full retention/Git archive round
trips, and any live-store dry run/apply. No live state or processes, caller
checkout, network remote, or sub-agent was used.
