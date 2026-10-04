# Durable conversation continuation and review fixes

Conversation turns record their job as the parent of dispatched runs. When
new results arrive, Subfleet creates one durable continuation message after
the current turn ends, while preserving holds, leases and person priority.
An all-of run request gathers its results into one message; requests whose
results were already delivered are satisfied without another wake.

PR watches wake for new check, review, merge or close events. First polls
establish baselines and use event timestamps for changes after registration.
Partial GraphQL errors retain other watches and report the inaccessible watch
once. Network polling uses a bounded worker independent of dispatch.
Final WAKE-ME requests tolerate empty placeholders, bullets, bold markup and
a standard close-out; invalid lines show a refusal without discarding valid
lines. Timers validate at turn creation.

A blocked Claude conversation clears only when the catalog transcript mtime
is strictly after blocked_at, the fresh outside-writer check finds no writer,
and no Subfleet turn or conversation/native lease remains. These checks are
paced at five seconds. This criterion does not count additional turns.

History uses a separate bounded read pool and exact known paths before catalog
discovery. Interrupts, compaction summaries and task notifications within a
Subfleet turn retain its ownership, so the native answer is displayed once.
Notice repairs use a durable bounded queue and indexed job lookup; automatic
completion scans are paced and exclude runs predating activation. Codex keeps
its own resumed session id, and wait acknowledges returned terminal results.

Validation and all review responses are in
[the continuation report](2026-10-04-continuation.md), with machine-readable
mutation evidence. The app is built locally without installation or launch.
Nothing is pushed from this review worktree.
