# PR #183: completed checkout storage recovery

Base: `287942a72a45`. Status: implementation and host verification in progress.

The round-2 regression uses a real SQLite page quota and ownership trigger to
raise `SQLITE_FULL`, restores capacity after rollback, and checks that admission
retains and eventually places the completed checkout. The current completion
path retries only `SQLITE_BUSY` and `SQLITE_LOCKED`; other SQLite errors fail the
job and schedule checkout deletion.

Planned verification: reproduce the review regression against the base, then run
the four host-push admission/store files and the renamed regression under the
shared suite lock, one foreground pytest process per hold. Results, classified
codes, retry bounds, invariants, and any unrun checks will be recorded here.

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

## Verification status

An immutable workspace-local archive of `287942a72a45` contains the original
production code plus the renamed, unchanged reviewer probes. Its daemon SHA-256
is `604fc65fbfa4f6adfa9efd1d514379e72e621d08c5e0324e9a96f188de9b2132`,
verified against the base blob. Python syntax checks pass. Pytest results are
pending dependency setup and shared-lock acquisition.
