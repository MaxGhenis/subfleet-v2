# Suggested conversations: review r2b fixes

Status: base reproduced (9 expected failures, 7 passes); fixes implemented; head validation queued.

Scope: multiline and Unicode secrets, exception text at every suggestions boundary,
intact redaction markers at character cuts, and the C-25.7 contract. Work is confined
to the assigned workspace. No remotes, sub-agents, live state, or live processes.

The full review and all three supplied probe files have been read. They were copied unchanged into
`tests/unit/` as `test_suggestions_review_multiline.py`,
`test_suggestions_review_response.py`, and `test_suggestions_review_boundaries.py`.

## Findings and fixes

- P1 multiline values: suggestions use whole-text scrubbing (quoted values are masked before header/reminder rules can consume their closing quote), whose quote scanner crosses line breaks and retains Unicode; original input is scrubbed before folding, and the folded form is scrubbed again.
- P1 exception text: all responder exception branches scrub the final message and optional repair text; complete log lines are scrubbed at responder/retry/control-loop catches. Shared workspace wrappers and local CLI notices use the same policy.
- P2 character cuts: `bounded` retreats to the beginning of any of the scrubber's four redaction markers, then checks incomplete sensitive assignments while preserving markers at each retreat. Bounds count Python characters, including Unicode, not UTF-8 bytes.
- C-25.7: records the raw scrub, whitespace fold, second scrub, marker-preserving cut order, exact output boundaries, and their bounds.

## Exception-to-text audit

Source paths found so far (all reachable by suggestions):

- `respond`: ConversationError reason/message/fix; ProtocolError and AdapterError
  message/fix; ValueError, TypeError, KeyError; generic exception daemon logs.
- `op_suggestion_start`: rewraps ConversationError for the direct Start caller.
- `_finish_start` -> `Suggestions.note_error`: persists failures, then publishes
  views/list/feed/events used by the app's card.
- `_resume_start`: daemon retry warning; `tick`: outer catch around
  `_resume_suggestions`, including its closed-service log.
- `op_workspace_check`: refusal reason and repair text in its result object.
- Shared workspace helpers `_validate_workspace`, `_git_toplevel`, `_cut_worktree`
  turn adapter, salvage, OS and subprocess failures into ConversationError.
  `_cut_worktree` also keeps a stderr tail; it needs a safe character cut.
- `cmd_suggest`: brief-read exceptions and daemon error/unavailable notices;
  workspace resolution can reach `main`'s raw OSError handler.
- Swift `startSuggestion`/`dismissSuggestion` -> `report` -> `describe` displays
  the responder's refusal message and fix in the notice. Those fields need
  scrubbing at their daemon boundary.

## Validation

Base direct execution of the nine supplied finding probes: **9 failed**, including
all four real hook/store multiline cases, both Unicode marker cuts, and all three
wire/log exception classes. Locked base pytest: **9 failed, 7 passed in 209.43 seconds**. Production source
was unchanged from `e6284d9e6502`. All nine finding regressions failed for the
expected leaks or partial markers; all seven additional boundary probes passed. The first head attempt timed out in the lock queue after eight minutes (exit 75);
pytest did not run and its temporary directory was removed. A fresh head run is
queued. It adds a local SIGALRM interruption before 20 minutes of pytest work;
there is no detached runner or background build. All pytest processes
will run individually in the foreground under the shared lock, with a separate
temporary directory and a hold below 20 minutes.

## Commits and remaining work

Shared-index writes are denied by the sandbox (`index.lock`: operation not permitted).
Commits use workspace-local `.git-local` metadata on `feat/suggested-conversations`,
with the original base as parent and shared objects read-only.
Probe/report step: `63b6b88b7`. Fix commit pending. Every commit uses the requested
co-author trailer.
