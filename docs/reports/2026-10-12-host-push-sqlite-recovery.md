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
