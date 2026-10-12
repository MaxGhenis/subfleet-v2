# Suggested conversations: review r2b fixes

Status: reproducing review probes at base `e6284d9e6502`; implementation pending.

Scope: multiline and Unicode secrets, exception text at every suggestions boundary,
intact redaction markers at character cuts, and the C-25.7 contract. Work is confined
to the assigned workspace. No remotes, sub-agents, live state, or live processes.

The full review and all three supplied probe files have been read. The probes will
be copied into `tests/unit/` with module names that follow the repository rule.

## Findings and fixes

- P1: quoted assignment values containing newlines and Unicode survive scrubbing.
- P1: the responder returns raw argument exceptions and logs raw internal failures.
- P2: cuts can split `[REDACTED]` after scrubbing.
- C-25.7: document the implemented boundary and scrub/fold/cut order.

## Exception-to-text audit

Pending source tracing.

## Validation

Base probes: pending. Head probes and Hypothesis: pending. All pytest processes
will run individually in the foreground under the shared lock, with a separate
temporary directory and a hold below 20 minutes.

## Commits and remaining work

Shared-index writes are denied by the sandbox (`index.lock`: operation not permitted).
Commits use workspace-local `.git-local` metadata on `feat/suggested-conversations`,
with the original base as parent and shared objects read-only. No tests have run yet.
