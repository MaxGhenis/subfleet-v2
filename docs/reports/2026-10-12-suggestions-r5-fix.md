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
at `c95c519fe`, repeated at final implementation `787a00121`: **3 passed,
0 failed**. The extended structural property's exact review example also passed
directly. Two direct ID-envelope compatibility/exclusion checks passed. These
are direct checks, separate from pytest counts; **500 generated examples remain
unrun**.

Mutation: only an isolated copy's daemon decoder refusal was changed from
`protocol.refuse("", exc, reason=reason)` to
`protocol.fail("", 2, f"request: {line!r}")`. Direct execution gave **3 failures,
1 pass**: the version refusal, correlation-id refusal and extended structural
property's explicit review example failed; the retained-event regression passed.
The source was restored in a finally block and its bytes compared with committed
HEAD. A locked pytest mutation attempt timed out before collection and also
restored/verified the source. The mutation was repeated against the final encoder
and test file. No production source was mutated.

**Pytest: 0 collected/executed, 0 passed, 0 failed.** Eight sequential foreground
attempts exited 75 waiting for the shared lock; none acquired a hold. Each used
its own `sf-r181f5b-$$` TMPDIR, Python cleanup, and a foreground controller with
a 19-minute post-acquisition deadline. No runner or build was left behind.

| Attempt | Queue timeout | Requested checks (all unrun) |
| --- | ---: | --- |
| 1 | 60 s | Protocol secrecy and structural errors/property |
| 2 | 60 s | All suggestion unit files, suggest CLI/hint, fake suggestions |
| 3 | 60 s | Protocol files, general CLI, five handoff files, module names |
| 4 | 60 s | Both suggestion e2e files, three suggestion frontend files, core protocol |
| 5 | 30 s | Three review fixtures at `75ffe34f5` |
| 6 | 30 s | Mutated protocol secrecy and structural property |
| 7 | 30 s | Committed protocol secrecy, structural property, r3 channels |
| 8 | 30 s | Final encoder: protocol secrecy, structural property, native core protocol |

Unrun files, for the hub's host run (27 total):

- `tests/unit/test_suggestion_error_boundaries.py`, `test_suggestion_structural_errors.py`,
  `test_suggestion_secret_properties.py`, `test_suggestion_properties.py`,
  `test_suggestions.py`, `test_suggestions_r1b_adversarial.py`,
  `test_suggestions_r3_channels.py`, `test_suggestions_review_boundaries.py`,
  `test_suggestions_review_multiline.py`, `test_suggestions_review_response.py`,
  `test_suggest_cli.py`, `test_suggest_hint.py`, `test_protocol_secrecy.py`, `test_cli.py`,
  `test_conversation_handoff.py`, `test_conversation_handoff_briefs.py`,
  `test_conversation_handoff_dispatch_races.py`, `test_sessions_handoff.py`,
  `test_sessions_handoff_excerpts.py`, `test_test_module_names.py`.
- `tests/fake/test_suggestions_fake.py`.
- `tests/e2e/test_suggestions_e2e.py`, `test_suggestions_r1b_peer_e2e.py`.
- `tests/frontend/test_core_suggestions.py`, `test_core_suggestions_r1b.py`,
  `test_visual_suggestions.py`, `test_core_protocol.py`.

No native compilation/rendering or broader suite result is claimed. All seven
changed Python files parse; named-file whitespace checks pass. Offline workspace
environment: Python 3.14.7 free-threaded, pytest 9.1.1, Hypothesis 6.168.1. Copied
dependencies exclude the caller's editable-project paths; imports use this
workspace. No dependency or lockfile changes.

Implementation commits: `ed2100d45` (production/contract), `c95c519fe` (regressions
and real-protocol property), `68248f446` (historical-check checkpoint),
`787a00121` (native envelope compatibility). Final results are a separate report
commit; all commits carry the requested co-author trailer.

Commits use workspace-local `.git-local` on `feat/suggested-conversations`,
parented at the assigned base, because the shared Git index is outside the
writable sandbox. Only named source, test and report files are staged. No
remotes, pushes, sub-agents, live-state changes or detached runners are used.
