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

Local pytest: **0 executed, 0 passed, 0 failed**. Seven sequential foreground
attempts all exited 75 after a 30-second shared-lock queue timeout, before
collection. No hold or pytest process started. Attempts covered: new regressions;
all suggestion unit/fake suites; handoff/CLI/module names; e2e/frontend/protocol;
new regressions again; the wire mutation; final structural/r3/CLI files. Every
attempt used its own requested TMPDIR, removed with Python. Each runner has a
19-minute pytest-exit alarm if it acquires a hold. The 500-example Hypothesis
property is **unrun: 0 examples executed**. Native rendering/compilation and the
wider repository suite are also unrun. No earlier host results count for this head.

Offline environment: Python 3.14.7 free-threaded, pytest 8.4.2, Hypothesis
6.168.1. Packages were copied from named cached wheels; no dependency/lock edits.
Initial setup picked an older cached pytest and omitted a Hypothesis helper;
these were corrected before the final attempts. PYTHONDONTWRITEBYTECODE avoids
writes to the read-only interpreter. Nine changed Python files parse, and named
file `git diff --check` passes.

Direct pure checks (not pytest): the exact r3 dict, tuple and byte-filename inputs
produce fixed errors with no credential sentinel. A wire mutation was applied
(`str(exc)` in the suggestion invalid-argument responder), attempted under its
own hold, and restored automatically on queue failure. The restored source was
verified. Its locked pytest failure counts remain unobserved.

A separate isolated responder execution used the real `respond` method with an
in-memory fake connection, no daemon/store and no worker. All **11** ported
exception shapes passed the same `assert_secret_absent` helper as the r3 tests.
A compiled in-memory mutation of that one wire channel to `str(exc)` failed
**9** of those assertions: `key dict`, `key tuple`, `key padded dict`, `key padded
tuple`, `key string`, `unicode`, `unicode decode`, `unicode encode`, `unicode
translate`. These identify the affected parametrizations of
`test_exception_argument_shapes_are_safe_in_all_start_channels`; the byte/string
OS filenames still use the separate generic responder branch. These are direct
wire assertions, not pytest test counts or a run of the storage/feed/CLI suite.
The generated property and no-rendering tests also target this boundary, but
no failure counts are claimed for them. An initial direct mutation setup assertion
stopped before executing probes; the corrected invocation completed the matrix.
No mutation remains in any source file.

The final audit additionally wrapped the whole suggestion CLI handler, covering
unexpected caller, path, client and result exceptions before main's raw OS-error
renderer. Both outer and suggestion argument parsers now return fixed errors,
including unknown options; other commands retain argparse's usual errors.

Commits use workspace-local `.git-local` on `feat/suggested-conversations`,
parented at the assigned head; the shared Git index is outside the writable
sandbox. Named files only and requested co-author trailer. No remotes, pushes,
history rewrites, sub-agents, live-state changes or background runners.
Dependencies are copied offline into the workspace; no dependency/lock edits.

## Per-file pytest results

| File | Tests executed | Result |
| --- | ---: | --- |
| `tests/unit/test_suggestion_error_boundaries.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestions_review_boundaries.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestions_review_multiline.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestions_review_response.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestion_secret_properties.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestion_properties.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestions.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggest_cli.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggest_hint.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestions_r3_channels.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_suggestion_structural_errors.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/fake/test_suggestions_fake.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/e2e/test_suggestions_e2e.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/e2e/test_suggestions_r1b_peer_e2e.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/frontend/test_core_suggestions.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/frontend/test_core_suggestions_r1b.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/frontend/test_visual_suggestions.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/frontend/test_core_protocol.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_conversation_handoff.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_conversation_handoff_briefs.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_sessions_handoff.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_cli.py` | 0 | Unrun: lock queue, exit 75 |
| `tests/unit/test_test_module_names.py` | 0 | Unrun: lock queue, exit 75 |

## Commits and handoff

- `cbe2da222`: fixed vocabulary, storage/view boundary, model validation,
  wire/retry/CLI handling, C-25.7 and adapted exact-template assertions.
- `c2bef1e6e`: r3 channel probes and the 500-example structural property.
- `5208c7c3a`: unexpected CLI exceptions and both argument-parser boundaries,
  with additional regressions.

All commits are on the workspace-local assigned branch and have the requested
co-author trailer. The shared checkout stays at `3d7eb5cad406`; extract the local
branch from `.git-local` when salvaging. The final report is a separate commit.
The hub must run the 26 files above, the 500 generated examples, and the wire
mutation with the new tests. This report claims no pytest success for unrun work.
