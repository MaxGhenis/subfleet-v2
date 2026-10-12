# PR #181 round 4: structural suggestion error boundary

Base: `3d7eb5cad406`. Both review-r3-full.md and review-r2b-full.md were read.

Suggestion errors now come from fixed templates, known exception classes and
validated errno numbers with os.strerror. Exception text, args, filenames and
caller values are never rendered in the public/persisted diagnostic boundary.
Invalid/retired models are refused without echo. Storage builds its diagnostic
from the exception; views replace legacy free-form errors with a fixed fallback.
Direct suggestion refusals, responder fields, notices, CLI errors and retry/
control-loop logs share that rule. The CLI validates daemon templates exactly,
retains a known reason enum and reconstructs its own message/fix. Exact legacy
unknown-op templates preserve old-daemon discovery. Invalid CLI arguments also
receive fixed errors. Hook stderr is scrubbed whole and written only to local
`<state root>/daemon.log`. Scrubbing/bounds remain defence in depth. C-25.7 states
the rule and identifies that log.

Existing assertions that required raw hook prose/redaction markers now require
fixed diagnostics; secrecy, bounds, state and channel assertions remain. R3
probes were ported from the review worktree with padded dict/tuple cases and
actual CLI/notice checks added. The new structural property requests 500
examples over seven exception families, four args shapes, sixteen caller fields,
nested values and Unicode/newlines. Unrenderable args/exceptions guard against
str/repr. Legacy views and untrusted reason/fix/parser fields have regressions.

Validation pending. First new-test attempt timed out in the shared lock queue
(exit 75), before collection; zero tests executed. Each acquired hold has a
19-minute alarm, each queue has a 30-second timeout, and each TMPDIR is removed
with the requested Python cleanup. No older host results count for this head.

Commits use workspace-local `.git-local` on `feat/suggested-conversations`,
parented at the assigned head; the shared Git index is outside the writable
sandbox. Named files only and requested co-author trailer. No remotes, pushes,
history rewrites, sub-agents, live-state changes or background runners.
Dependencies are copied offline into the workspace; no dependency/lock edits.
