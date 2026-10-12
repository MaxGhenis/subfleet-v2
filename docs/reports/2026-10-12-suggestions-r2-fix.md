# Suggested conversations: review r2b fixes

Status: finish-lane fixture corrections implemented at `fa925a8a03df`; original
failure reproductions and final suites are queued. Final results are recorded below
as each foreground lock hold completes. All work is in the assigned workspace;
no remotes, sub-agents, live-state writes, or live-process changes.

## Findings and production fixes inherited from the cut-off lane

- P1 multiline values: whole-text quoted assignments cross line breaks and retain
  Unicode. They are masked before header/reminder rules can consume a closing
  quote. Raw text is scrubbed before folding, and the folded form is scrubbed again.
- P1 exception text: responder messages and repair text, complete responder/retry/
  control-loop log lines, shared workspace wrappers, and CLI notices are scrubbed.
  Original exception string arguments and OS filenames are scrubbed before Python
  diagnostic quoting can obscure assignment delimiters. Overlapping quoted ranges
  are masked together.
- P2 character cuts: `bounded` retreats to the beginning of any of the scrubber's
  four markers, then checks sensitive assignment fragments while preserving whole
  markers at each retreat. Bounds count Python characters, including Unicode.
- Brief normalization: a second scrub after CR/newline normalization removes
  Authorization headers that the normalization creates.
- C-25.7 records raw scrub, fold/normalize, second scrub, marker-preserving cut;
  output channels and bounds; overlapping assignments and raw exception fields.
  The finish lane changes no production rule, so no further contract edit is needed.

## The two host failures

The hub's filtered log at `fa925a8a0` reported 1 failed / 33 passed for the four
new regression files and 1 failed / 6 passed for secret properties. Saved Hypothesis
choice data was read from the hub's isolated test directory, copied into ignored
workspace-local test metadata, and used for the original-file reproduction attempts.

1. Responder property: the saved minimal choices select `secret=AKIA` plus 16 zeroes,
   `error_type=ConversationError`, and `op=suggestion.list`, with otherwise empty
   surrounding text and space separators. The fake connection has only `sendall`.
   The real responder calls `getpeername()` before handling a read-only request,
   raising `AttributeError` on this fixture before the injected error is reached.
   The fixture now supplies a connected `getpeername`, and this case is explicit.
   `OperationalError: password="[REDACTED]"` is safe output: the credential value
   is absent, the marker is whole, and the scrubber leaves that output unchanged.
2. Folder/session/job property: the saved minimal choices select
   `password="SECRET7- \U0002ee5e-右秘密-TAIL8"` as a workspace name. APFS cannot
   create that name (`EILSEQ`), so `folder.mkdir()` fails before the service runs.
   The fixture handles only `EILSEQ` and still sends the uncreated path to the
   service. It must reject it as `bad-workspace`, with no row/feed change and no
   secret in its error. Other OS failures still fail the test. The full Unicode
   generator remains in place, and this input is now an explicit example.

Neither failure shows a product secret leak. All full-value, forbidden-prefix,
intact-marker, output-bound and recursive row/view/event/log/notice assertions
remain in place.

## Exception-to-text audit

- `respond`: ConversationError reason/message/fix; ProtocolError and AdapterError
  message/fix; ValueError, TypeError, KeyError; generic exception daemon logs.
- `op_suggestion_start`: rewraps ConversationError for the direct Start caller.
- `_finish_start` -> `Suggestions.note_error`: persists failures, then publishes
  views/list/feed/events used by the app card.
- `_resume_start`: daemon retry warning; `tick`: outer catch around
  `_resume_suggestions`, including its closed-service log.
- `op_workspace_check`: refusal reason and repair text in its result object.
- `_validate_workspace`, `_git_toplevel`, `_cut_worktree`: adapter, salvage, OS and
  subprocess failures become ConversationError. Complete worktree stderr is
  scrubbed/folded before a marker-safe first 300 characters are retained.
- `cmd_suggest`: brief-read OSError/UnicodeDecodeError, workspace-resolution
  OSError, DaemonError, DaemonUnavailable, ProtocolError; messages and optional
  repair text are scrubbed before CLI rendering.
- Swift `startSuggestion`/`dismissSuggestion` -> `report` -> `describe` displays
  clean responder messages and repair text in the app notice.
- `_resume_suggestions` pool-shutdown RuntimeError and `descriptors.send_reply`
  OSError are discarded without rendering. `suggestions.py` otherwise formats
  caught exceptions only via `scrub_error` and `note_error` above.

## Boundary regressions finished

The real-service regression now injects KeyError, an OSError filename, both
OSError rename filenames, and an overlapping quoted assignment at brief
submission. Every case checks the stored row, ordinary/by-id lists, watch feed,
origin events, wire reply, and retry log. Separate regressions cover original
arguments (single/multiple strings and mixed types), headers/reminders inside
quoted values, and overlapping values with either quote style. Final execution
results are pending below.

## Validation

Historical cut-off-lane evidence: the supplied review probes at `e6284d9e6502`
were **9 failed, 7 passed in 209.43 seconds** under the lock. Its direct head
probes passed, but its final two commits had no pytest validation before reboot.
Those direct checks are not counted as finish-lane pytest results.

Finish-lane environment: offline copies of cached packages into the workspace's
`.venv` (Python 3.14.7 free-threaded, pytest 9.1.1, Hypothesis 6.168.1); workspace
cache; no dependency/lockfile changes. Each pytest is foreground under its own
shared-lock hold, with a unique `sf-r181f` temporary directory removed using
Python. Final holds have a 19-minute alarm, and a 30-second queue timeout. Earlier
original-test attempts timed out after 3, 8, 8, 2 and 2 minutes in the queue, all
without reaching pytest. No detached runner or background build is used.

Original-file reproductions: pending.
Final per-file results: pending.

## Commits

Inherited: `63b6b88b7` (review probes), `4e8771067` (boundary fixes), `f9cd7ac9d`
(brief normalization), `fa925a8a0` (exception quoting and overlapping assignments).
Finish-lane commits use workspace-local `.git-local` metadata on
`feat/suggested-conversations`, with `fa925a8a03df` as the original parent because
the shared Git index is outside the writable sandbox. Named files only; requested
co-author trailer; shared checkout/history unchanged. Finish-lane commits and
results will be recorded when complete.
