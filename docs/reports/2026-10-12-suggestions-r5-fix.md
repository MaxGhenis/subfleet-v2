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

Added checks beyond the recovered patch for falsey/non-object arguments, strict
integer versions, response-helper id validation, exact unknown-op discovery,
and repeated retained reads leaving stored bytes unchanged. No recovered changes
were dropped, and no existing secrecy assertions were weakened.

Changed the recovered encoder's broad empty-id omission: only invalid-id
refusals omit the field. Native `ResponseHeader` requires a String id, so normal
uncorrelated busy/unknown-operation/success replies keep `id: ""`. A regression
protects that existing wire contract; malformed-line checks still require the
fixed diagnostic and sentinel absence, with the legacy empty id retained.

The three review-r4 fixtures were executed directly in an archive of
`75ffe34f5`, using the ported regression file: **0 passed, 3 failed**, matching
retained-event, version-field and correlation-id leaks. The identical fixtures
at `c95c519fe`: **3 passed, 0 failed**. The extended structural property's exact
review example also passed directly. These are direct checks, separate from
pytest counts; its 500 generated examples remain unrun.

At this checkpoint, three sequential foreground pytest attempts each timed out
after 60 seconds waiting for the shared lock (exit 75, no hold acquired). They
covered new regressions, all suggestion unit/fake files, and protocol/CLI/
handoff/module-name files. **Pytest: 0 executed, 0 passed, 0 failed.** Native/e2e,
historical pytest and mutation checks are still pending. All seven changed
Python files parse and the named diff passes whitespace checks.

Commits so far: `ed2100d45` (production boundaries and contract), `c95c519fe`
(recovered and additional regressions, actual-protocol property extension).

Commits use workspace-local `.git-local` on `feat/suggested-conversations`,
parented at the assigned base, because the shared Git index is outside the
writable sandbox. Only named source, test and report files are staged. No
remotes, pushes, sub-agents, live-state changes or detached runners are used.
