# PR #183: completed checkout storage recovery

Base: `287942a72a45`. Implementation: `ab2ee5e4eaf4da3754afa2ae4344100339899025`.
Verdict: implementation complete; host pytest verification remains unrun because
the prescribed shared suite lock could not be acquired.

The round-2 regression uses a real SQLite page quota and ownership trigger to
raise `SQLITE_FULL`, restores capacity after rollback, and checks that admission
retains and eventually places the completed checkout. At the base, the completion
path retries only `SQLITE_BUSY` and `SQLITE_LOCKED`; other SQLite errors fail the
job and schedule checkout deletion.

The unchanged two-probe review file is committed with the unique module name
`tests/unit/test_host_push_storage_recovery.py`. A separate budget file adds 55
parameterized cases for transaction rollback, classification, cancellation and
exhaustion, including a failure/notice transaction that itself needs retry.

## Implementation

Completion writes classify BUSY/LOCKED (including extended codes), FULL,
PROTOCOL, and the exact IOERR_WRITE, IOERR_FSYNC, IOERR_DIR_FSYNC and
IOERR_TRUNCATE subcodes as retryable. Lock contention and reclaimed capacity can
recover; the selected I/O codes describe failed writes, flushes or truncation.
This is an application retry policy, based on SQLite's documented meanings,
not a promise that the device will recover. Other I/O codes, including plain
IOERR, IOERR_DATA and IOERR_CORRUPTFS, retain immediate failure handling.
See [SQLite result codes](https://www.sqlite.org/rescode.html).

The first seven failed completion writes retain the same completed Future and
checkout. The eighth schedules the existing failure transaction. Once that
budget is exhausted, only the pending failure and notice can be retried; a
healthy store cannot reopen ownership/readiness. If even the failure cannot
commit, its cause and slot remain pending, preserving the existing durable
failure rule. Success and cancellation clear the counter; restart resets it.

Invariants: ownership and its audit commit before readiness; one outstanding
preparation and at most one new offer per fresh detached pass; completed results
are consumed before unrelated holds; a pending completion keeps its slot;
cancellation of a running worker keeps the slot until it returns; ordinary jobs
continue while one push completion is deferred; failures and notices commit
together, with cleanup on the serialized push executor; no duplicate builds,
placements or ownership events; conventional checkout paths remain owned by
retention across restart.

The sandbox rejected the shared index write (`index.lock: Operation not
permitted`). Commits use workspace-local `.git-local` metadata on
`fix/host-push-p3`; the original detached Git metadata is unchanged.

## Before and after

An immutable workspace-local archive of `287942a72a45` contains the original
production code plus the renamed, unchanged reviewer probes. Its daemon SHA-256
is `604fc65fbfa4f6adfa9efd1d514379e72e621d08c5e0324e9a96f188de9b2132`,
verified against the base blob. Both archive and head regression files match
the review ref byte for byte. Dependencies were installed in this workspace only.

| Run | Result | Pytest counts |
| --- | --- | --- |
| Hub at `287942a72` (provided evidence) | Real recovered FULL regression failed: `state=failed; checkout_exists=False` | Named regression: 1 failed |
| Checkpoint before-fix attempt | Exit 75, `already locked`, after 120.35 s | 0 executed |
| Resumed before-fix attempt | Exit 75, `already locked`, after 120.34 s | 0 executed |
| Resumed after-fix selection | Exit 75, `already locked`, after 120.40 s | 0 executed |

The checkpoint preserved no result for its previously queued after-fix attempt.
There is **no observed after-fix pytest pass** to report. In this continuation,
six changed Python files passed AST syntax checks and `git diff --check` passed.

Each resumed invocation had a separate shared-lock hold request, a 120-second
queue cap (`lockf -k -t 120`) and an 18-minute total watchdog. No lock hold or
pytest process began. Both foreground runners exited; their task TMPDIRs were
removed with the prescribed Python `shutil.rmtree` cleanup. No runner was left
behind, and no remote, sub-agent, caller checkout or live process was used.

Unrun before-fix selection:
`tests/unit/test_host_push_storage_recovery.py::test_recovered_real_sqlite_full_retains_the_completed_checkout`.

Unrun after-fix selection (all requested admission/store files, both new files,
and two additional checks):

```text
tests/unit/test_host_push_storage_recovery.py
tests/unit/test_host_push_storage_retry_budget.py
tests/unit/test_host_push_admission.py
tests/unit/test_host_push_build_budget.py
tests/unit/test_host_push_store_completion_pr183.py
tests/unit/test_host_push_store_interleavings.py
tests/unit/test_host_push_build.py
tests/unit/test_test_module_names.py
```

The broader repository suite and optional benchmarks were not run.

Commits: `a1f5bef10` imports the unchanged review probes; `ab2ee5e4e` implements
bounded retry, updates C-8.5 and adds fault/budget coverage. The final report is a
separate commit. All commits carry the required coauthor trailer and remain on
`fix/host-push-p3` in workspace-local `.git-local` metadata. The report commit's
head is available through `git --git-dir=.git-local rev-parse HEAD`.
