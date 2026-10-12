# PR #181 round 5: retained events and protocol secrecy

Assigned base: `4cd38b44beef`, including the release/217 merge and the
round-5 reproductions in `3c36728ee`. Read the fix brief, full round-4 review,
and every hunk of the recovered cutoff patch before retaining it.

Kept all eight files' changes: event replay filters to public snapshot fields
and reconstructs only known diagnostics; generic framing/envelope/argument
refusals use fixed templates instead of caller/exception/repair text; correlation
ids are bounded ASCII tokens, with recognized credentials rejected; the
500-example structural property exercises the actual decoder/responders; and
C-25.7 documents the protocol and replay boundaries. No weakened check or
unfinished mutation was found in the recovered patch.

Retained events are normalized on each read, without changing their historical
state/content or rewriting stored data. Unknown diagnostic fields are dropped;
legacy free-form `start_error` receives the fixed fallback. The read projection
is idempotent. release/217 contains no suggestion module, and the installed
release `20261010T090547Z` also contains no suggestion module; neither released
build writes these suggestion diagnostics. Reads are safe regardless.

Audited every match of the requested `error(/refuse/ProtocolError/repr(` search
in protocol.py and daemon.py. Parser, coercion, notice-id, version, operation,
argument-shape and correlation-id errors use the refusal vocabulary. The generic
daemon/conversation responders do not render ProtocolError/AdapterError or
argument exceptions. Busy replies use fixed text and the daemon's own connection
count; the reader-thread failure no longer renders its exception. Existing
operation-specific ConversationError diagnostics outside suggestions retain
their separate domain boundary.

Tests and historical/mutation comparisons are pending at this first checkpoint.
No pytest result is claimed yet. All seven changed Python files parse and the
recovered diff passes whitespace checks.

Commits use workspace-local `.git-local` on `feat/suggested-conversations`,
parented at the assigned base, because the shared Git index is outside the
writable sandbox. Only named source, test and report files are staged. No
remotes, pushes, sub-agents, live-state changes or detached runners are used.
