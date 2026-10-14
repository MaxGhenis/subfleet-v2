# PR #181 rounds 5 to 9: retained events, secrecy and compatibility

The original round-5 record follows. Round-6, round-7 and round-8 fixes and results are appended below.

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

## Round 6: fix the host-run compatibility regressions

Assigned base: `09a4018f46a1`. The host's round-5 log confirms six CLI failures,
three suggestion unit failures and one frontend failure. The fixes preserve
round 5's protocol and retained-event secrecy assertions.

A complete malformed daemon reply now raises an operational `ProtocolError`,
exit 1, instead of `ResponseLost`: no resend and no response/exception text in
the fixed `malformed response` diagnostic. The CLI tests retain their old
wording expectation and now also assert exactly one submission, no resend
notice, and sentinel absence, including malformed response fields.

Caller request ids use the correlation token validator and are returned
unchanged. CLI converters give argparse a fixed refusal instead of allowing
it to quote the rejected value. The old path-id test's success expectation was
wrong under the requested safe-token contract: it now expects exit 2 before
staging/transport and still checks that the victim is untouched. The wire test
now uses valid mixed-case and maximum-length tokens, asserting both envelope
and payload identity. Rejection coverage includes paths, lengths and
credentials for run, resume and handoff. C-1.5 now states that contract.

Suggestion refusals keep structural detail separately from exception prose:
known origin scopes, integer counts/bounds from 0 through SQLite's signed
64-bit maximum, and known closed states. The service decorator, responder,
retained diagnostic reader and CLI reconstruct complete canonical templates
from those fields. Invalid types, boolean counts, out-of-range counts and
untrusted strings receive the existing fixed fallback. The count and
dismissed/withdrawn wording assertions remain unchanged. The adversarial test's
shorter remedy was stale; it now requires `dismiss it or restore its checkout`,
preserving both ways to recover the promised worktree start.

The frontend fixture now passes a structured `ConversationError` with the
`worktree-failed` reason to `note_error`, matching the production boundary.
Its app wording assertion remains exactly `worktree-failed: git worktree add
failed`. Arbitrary already-rendered strings still receive the fallback; that
secrecy assertion was not weakened. Coercion and notice checks are independent
parameterized cases, so one cannot prevent the other from executing.

**Direct checks, separate from pytest suite counts:**

| Check | Passed | Failed | Interpretation |
| --- | ---: | ---: | --- |
| Fixed head: round-5 three fixtures, list-valued unknown op, coercion, notice, explicit structural review example | 7 | 0 | Regressions fixed |
| Client/socket and parser integration | 32 | 0 | Six malformed replies never resend; two valid wire ids preserved; 24 rejected parser inputs stay secret |
| Structural diagnostics across wire, CLI and retained reads | 15 | 0 | Four template round trips, seven invalid counts, four secret-bearing structural fields |
| `75ffe34f5` archive: three original regressions plus unknown op `[op2]`, coercion and notice | 0 | 6 | All expected historical regressions reproduced |
| Isolated refusal-echo mutation: original three fixtures plus explicit structural review example | 1 | 3 | Version/id/property catch the leak; retained-event control still passes |
| Restored mutation archive: identical four checks | 4 | 0 | Failure came from the mutation |

The mutation changes only an ignored isolated archive's daemon decoder refusal
to `protocol.fail("", 2, f"request: {line!r}")`. Both the locked attempt and
direct run restore its source in a finally block and compare bytes. The restored
decoder matches production, which was never mutated. These runs execute the
explicit Hypothesis review example, not its 500 generated cases. The new
structural secrecy property specifies `max_examples=100`; all other existing
explicit example bounds remain in place.

**Pytest: 0 collected/executed, 0 passed, 0 failed.** Nine sequential foreground
attempts timed out after 60 seconds in the shared lock queue, exit 75; none
acquired a hold. Each had its own `sf-r181f6-$$` TMPDIR, Python cleanup, cache
disabled, Hypothesis storage outside the worktree, and a foreground controller
with a 19-minute post-acquisition deadline. No pytest runner was left behind.

| Attempt | Requested checks (all unrun) |
| --- | --- |
| 1 | CLI |
| 2 | Protocol secrecy |
| 3 | All 12 suggestion unit files, fake suggestions, both suggestion e2e files |
| 4 | Core suggestions frontend |
| 5 | Test module names |
| 6 | Six historical protocol secrecy fixtures at `75ffe34f5` |
| 7 | Refusal-echo mutation: three review fixtures and structural property |
| 8 | Final CLI implementation |
| 9 | Core suggestions, adversarial core suggestions, visual suggestions and core protocol frontend |

For the host run, the 22 current-tree files still unrun by pytest are:

- `tests/unit/test_cli.py`, `test_protocol_secrecy.py`, `test_test_module_names.py`;
  `test_suggestions.py`, `test_suggestions_r1b_adversarial.py`,
  `test_suggestions_r3_channels.py`, `test_suggestions_review_boundaries.py`,
  `test_suggestions_review_multiline.py`, `test_suggestions_review_response.py`,
  `test_suggestion_error_boundaries.py`, `test_suggestion_structural_errors.py`,
  `test_suggestion_secret_properties.py`, `test_suggestion_properties.py`,
  `test_suggest_cli.py`, `test_suggest_hint.py`.
- `tests/fake/test_suggestions_fake.py`.
- `tests/e2e/test_suggestions_e2e.py`, `test_suggestions_r1b_peer_e2e.py`.
- `tests/frontend/test_core_suggestions.py`, `test_core_suggestions_r1b.py`,
  `test_visual_suggestions.py`, `test_core_protocol.py`.

All 12 changed Python files parse and named-file whitespace checks pass. No
native compile/render or generated property run is claimed. Offline workspace
environment: Python 3.14.7 free-threaded, pytest 9.1.1, Hypothesis 6.168.1.
Copied dependencies exclude caller editable-project paths; imports resolve to
this workspace. No dependency/lockfile changes or worktree `.hypothesis` folder.

Implementation commits, in order: `e5b9de94b` (client response/id behavior),
`3c7f812bc` (suggestion structural detail and fixtures), `201405630` (structural
secrecy checks), `e7d03c5d5` (independent coercion/notice checks), `257bf4b9f`
(CLI structural reconstruction), `18551d8db` (parser import), `39329a0bd` (fixed
CLI request-id refusals). The final contract/report update is a separate commit.
Every commit has the required co-author trailer.

Commits are on `feat/suggested-conversations` in workspace-local `.git-local`,
parented at the assigned head, because shared Git metadata is outside the
writable sandbox. Only named source, test, contract and report files are staged.
No network remotes, pushes, sub-agents, caller writes, live-state changes,
staged logs/evidence/bundles or detached runners.

## Round 7: restore submission request-id compatibility

Assigned base: `234237b171f0`. Round 6 applied the protocol correlation-token
alphabet to submission idempotency keys, refusing the daemon's own
`turn:<message UUID>:<sequence>` and `gate:<id>:round:<number>:<token>` formats.

`ids.request_id` restores round 5's nonempty-string, 128-character bounds and
preserves the caller's string. It still refuses recognized credentials through
the same whole-text scrubber, with a fixed error that excludes the value.
`request_id_arg` keeps argparse from quoting refused values. Run, resume and
handoff share that converter; daemon submission still validates through
`ids.request_id`. Both internal builders found by `git grep -n 'request_id=f'
subfleet` retain their existing formats. Conversation/suggestion/wake keys keep
their separate existing validation rules.

The client also used the submission key as the protocol envelope's `id`. For
accepted keys outside the correlation alphabet, it now sends a separate UUID
correlation token while preserving the payload key and the key used to settle
lost-response retries. Existing correlation tokens retain their spelling.
Protocol correlation validation and credential refusal remain in place. C-1.5
and C-6.2 now state the restored submission rule and distinguish the wire field.

Regressions cover real daemon submission and idempotent retries for both turn
and gate keys; colon, path and Unicode CLI keys; all three submission parsers;
unchanged bounded Unicode keys; and fixed credential refusals without stored
jobs. The CLI path-staging test again verifies round 5's successful submission
under a hashed filename while preserving the victim. Existing correlation and
credential assertions remain, with a new colon-bearing correlation refusal.

Final current-head pytest results: **699 passed, 12 skipped, 0 failed**.

| Separate lock hold | Passed | Skipped | Failed |
| --- | ---: | ---: | ---: |
| `tests/e2e/test_suggestions_e2e.py` | 0 | 2 | 0 |
| `tests/unit/test_cli.py` | 207 | 0 | 0 |
| `tests/unit/test_protocol_secrecy.py` | 229 | 0 | 0 |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 | 0 |
| All eight tracked gate test files | 200 | 10 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| `tests/unit/test_store_ids.py` | 22 | 0 | 0 |

The gate hold included all six `tests/unit/test_gate_*.py` files,
`tests/fake/test_gate_end_to_end.py` and
`tests/fake/test_launch_gates_above_stdio.py`. The new two-case real submission
regression passed in the gate hold, and the five-case CLI wire regression passed
in the CLI hold. In a separate hold against an ignored workspace-local archive
of `234237b17`, with those two current test files copied in, **all seven failed
as expected**: the turn and gate submissions reported the safe-ASCII refusal,
and the CLI refused the colon, path and Unicode keys. No production source was
mutated. Separate lightweight source comparisons also confirmed the restored
three keys and both versions' fixed credential refusals. All six changed Python
files parse; named-file whitespace checks pass.

**Still unexecuted assertions:** both suggestion e2e tests and the ten gate
process checks. The e2e skip reason was `C-5.3 e2e requires permitted ps/sysctl
inspection: macOS boot identity is unavailable`; the gate fake-daemon fixture
likewise requires permitted boot/process identity inspection. The host must run
these checks; no e2e pass is claimed here.

Six earlier foreground attempts timed out after 60 seconds in the shared lock
queue (exit 75): the initial seven-case regression check, two e2e attempts, two
CLI attempts and one protocol-secrecy attempt. Every requested file eventually
acquired its own hold. The first structural hold stopped at collection with one
missing-Hypothesis error because a local pytest console script was absent and
`uv run pytest` selected the global installation. A workspace-local entry point
fixed the environment, and the structural suite then passed all 40 tests.

Every hold ran one foreground pytest process with cacheprovider disabled, a
19-minute post-acquisition deadline, its own `sf-r181f7-$$` TMPDIR and Hypothesis
storage there, followed by Python `shutil.rmtree` cleanup. The local environment
uses Python 3.14.7 free-threaded, pytest 9.1.1 and Hypothesis 6.168.1. Cached test
dependencies were copied read-only from the caller's environment, excluding its
editable-project paths; imports and commits use this workspace. No dependency
or lockfile changes.

Implementation commit: `33b7c872b`. This report is committed separately. Both
commits are on `feat/suggested-conversations` in workspace-local `.git-local`,
parented at the assigned head, because shared Git metadata is outside the
writable sandbox. Only named source, test, contract and report files are staged;
both commits carry the required co-author trailer. No network remotes, pushes,
sub-agents, caller writes, live-state changes or staged logs/evidence/bundles.

## Round 8: handler refusals say what they name; a malformed reply is resent

Review r5 (`178e9438b`) found the round-5 to round-7 responders gave every
handler refusal the argument template `args: request was refused` (P1), and
round 6 had stopped the CLI resending after a reply that does not decode
(P2). The first round-8 job committed the P2 fix as `a29f83319` and was then
cut off three times by provider capacity, leaving its P1 work uncommitted;
the hub saved it as `fix8-uncommitted.patch` (12 files). This continuation
applied that patch at `a29f83319`, read every hunk, and finished P1.

**P2, `a29f83319`.** A complete reply line that does not decode raises
`ResponseLost` with the fixed `malformed-response` text, so C-16.3 holds
again: the CLI sends the identical submission once more and, if that also
goes unanswered, reports the outcome as unknown with the request id and the
`--request-id` command that settles it. The two `test_cli.py` tests that had
pinned a single submission now require two identical submissions, the
resend notice, the request id and `--request-id`. Their bodies include the
reviewer's three probes (not JSON, an array, an `error` that is not an
object).

**P1.** The request vocabulary now covers only the decoding and argument
errors that `request_error` tags. Every refusal a handler raises
(`ProtocolError`, `AdapterError`, and `TransferError`, which the daemon passes
on) carries a reason from `subfleet/refusals.py`. Where the reason's template
names a value, the refusal carries that value in `details`. The wire text is
the reason's most specific form whose values pass their checks:

- a job id or lane id of its form that the C-25.5 scrubber leaves unchanged;
- a task name of its form;
- a known isolation variable or Codex configuration layer;
- `main` or `master`, a provider, or a count.

A value that fails its check is left out by a less specific form of the same
refusal, which keeps the refusal's kind and repair. Exception messages, their
args and their own repair text are never inputs. A refusal whose reason is
unknown reads `operation: request was refused`. The table holds 148 reasons,
24 of which have less specific forms.

| Command (review r5's cases first) | `75ffe34f5` | rounds 5–7 | now |
| --- | --- | --- | --- |
| `run --request-id R` again with another prompt | `…different payload by job <id>` | `args: request was refused` | `request id already used with a different payload by job <id>` |
| `run --task build --tier standard -C <non-git>` | `build jobs write by policy, and this directory is not a git repository` + fix with `-s read-only` | `args: request was refused` (exit 7, no fix) | same as `75ffe34f5` |
| `runs show <unknown>`, `kill <unknown>` | `unknown job <id>` | `args: request was refused` | `unknown job <id>` |
| `lanes release codex-99 --limits --note x` | (no such option) | `args: request was refused` | `lanes release --limits: unknown lane codex-99` + `fix: subfleet lanes list` |
| `lanes transfer codex-99 --to v2` | `unknown lane codex-99` | `args: request was refused` | `lanes transfer: unknown lane codex-99` + fix |
| `lanes hold <not a lane id> --until …` | the value quoted | `args: request was refused` | `lanes hold: unknown lane` + fix (value left out) |

Rerunning the reviewer's `probe_daemon.py` against this tree gives its
refusal cases the "now" text above, which is `75ffe34f5`'s wherever that
version had the case. Its three round-4 secrecy cases still report
`leaks: false`, and its `notice.ack` case keeps the argument template.

What the hub's draft patch got right, and kept: the closed reason plus checked
details, the split in `protocol.refuse`, `ProtocolError` no longer defaulting
to `refused-args`, tags on every raise site in `daemon.py` and the adapters, the isolation variable
and layer, the four conversation seams, and its tests' intent. What changed:

1. **Reason names.** The draft's 70 generated `handler-<first words of the
   message>` slugs are short names (`batch-id`, `workdir-tmp`,
   `reenroll-lease`).
2. **A failed value no longer swallows the refusal.** In the draft, any value
   that failed its check made the whole refusal the operation fallback:
   `lanes hold foo` read `operation: request was refused` with no fix. Each
   reason that names a value now has less specific forms, ending in one that
   names none.
3. **C-11.1 under policy.** `_written_by_policy` dropped the reason of the
   writer refusal it wraps, so every writer refusal of a job writing by policy
   (d261) became the fallback, without `pass -s read-only`. It now keeps the
   reason and details. The renderer adds `(<task> jobs write by policy)` and
   `; or pass -s read-only`, as the exception's own text does.
4. **Refusal sites the draft missed.** It left these untagged:
   - the 14 operator-command refusals (`operations.py`);
   - the 16 `lanes transfer` refusals, which shared one generic template
     whose fix it had invented;
   - host push at submit, admission and checkout;
   - credential resolution, the adapter registry, and the conversation turn
     launch.

   They read the fallback or lost their repair. `--i-understand-v1-edit`, for
   instance, is again what the transfer refusal tells the operator to pass.
   An AST test now requires a known reason on all 167 refusal constructions
   in the daemon's code (the client's own errors and `request_error` aside).
5. **Detail the draft dropped.** These are back, each value checked:
   - the task in the policy-workdir refusal (the draft read `jobs write by
     policy, and …`);
   - the holding or source job in the writer-conflict, revive and resume
     refusals;
   - the count and limit under `caps.max_writable_per_session`;
   - the provider for resume and MCP, the lane already bound at enrollment,
     and the `sessions` action.
6. **The conversation fix.** The conversation-session and resume-of-a-turn
   refusals use `registry.CONVERSATION_FIX` again; the draft had invented
   different text.
7. **Unknown layers.** An unknown Codex layer source is left out. The draft
   rendered it as the word `unknown` ("an unsupported unknown configuration
   layer").
8. **The daemon log.** A handler refusal's own words go to daemon.log only,
   scrubbed whole (`refusals.log_detail`), as C-25.7 allows. The host-push
   and transfer templates that say "the daemon log names …" rely on this.
9. **A fifth conversation seam.** `_handoff_brief` rendered `str(HandoffError)`
   (transcript paths, exception text), the same leak as `_handoff_history`.
   It now uses fixed text.
10. **The draft's CLI tests never reached the daemon.** `served` records the
    daemon as booted at `routing-boot` and started at `routing-start`. The CLI
    read the machine's real values, judged `daemon.lock` stale and exited 69
    before sending anything. The tests now stub the client's identity to
    match.
11. **Two argument refusals lost their words.** The draft replaced
    `ProtocolError(str(exc))` in two submit argument checks with
    `request_error("invalid-args")`. Those checks cover reading the submission
    and a resume's `--mcp` names. The change dropped the exception's own
    message, which direct callers read.
    `tests/fake/test_admission_route_isolation.py:1080` matches "was given as
    a claude lane", and `tests/fake/test_mcp_job_contract.py:101` matches
    "unknown MCP server …". Both would have failed. They are
    `ProtocolError(str(exc), reason="invalid-args")` again (`76d3e2c1c`). The
    wire reads the argument template and the exception keeps its message. This
    was found by reading those tests, not by running them.

Nothing else was discarded.

**`test_protocol_secrecy.py:172`.** That case fed `ProtocolError(VALUE,
fix=VALUE)` and `AdapterError(VALUE, fix=VALUE)` to the argument-refusal
test and required the `args:` template: it asserted the over-broad
behaviour review r5 found. Those inputs now live in their own cases:

- with no known reason, they require the operation fallback, still with no
  prose and no fix;
- with reasons, they require the job, lane, policy fix and isolation names;
- with values that fail their checks, they require the less specific form.

The argument case keeps `request_error("invalid-args")`, which is what it
tests.

**P3 dispositions.**

- *Credential-shaped request ids* stay refused, both as submission keys
  (C-1.5) and as correlation ids (C-25.7). Both clauses already state this,
  and the value is never shown. The cost is a legitimate key such as
  `sk-learn-benchmark-2026` needing another spelling. Accepting it would put
  a string the scrubber recognizes into stored rows and replies.
- *Conversation refusals outside suggestions.* Five seams are templated: the
  four the reviewer named (`bad-decision`, `diff-failed`, `bad-native`,
  `handoff-source`) and the handoff brief. C-25.7 now describes the rest as
  they are: `reason: text` lines in our own words, which may name the ids,
  states and paths the request or store holds and errno text, through the
  scrubber, with no exception text. An AST check of
  `subfleet/conversations` found no other `ConversationError` that renders
  a caught exception, apart from errno `strerror`.

**Invariants**, stated in `tests/unit/test_handler_refusals.py` and
executed:

- the text is the reason's first form whose values all pass their checks,
  plus the by-policy suffix exactly when the details carry it;
- a known reason never reads the fallback, and an unknown one always does;
- the exception's own message and repair never appear;
- a checked value has its stated shape (an oracle written apart from the
  module's patterns);
- each refusal the code builds names a known handler reason, or a request
  reason for an argument check;
- where a raise site's message or fix is a constant, the wire shows that
  same text. This differential check compares more than 100 pairs; their
  meanings cannot drift apart.

Hypothesis runs the rendering properties over all three exception types, and
every reason goes through the daemon's real responder.

**Not changed.** Three limits remain:

- Handler `ValueError`, `TypeError` and `KeyError` refusals still read
  `args: required fields are missing or invalid`, as in rounds 5 to 7 (for
  example `output directory must exist`, or a gate review's `round_lease`).
  C-25.7 counts those as argument refusals, and review r5 did not raise
  them.
- Host-push refusals at submit share one template; the check that refused is
  in daemon.log.
- `gate.*` operations return their errors as results (`message: str(exc)`),
  outside the refusal path. That is unchanged.

**Tests.** Each hold ran one foreground pytest process under the shared lock,
with its own `sf-r181f8-$$` TMPDIR and Hypothesis storage, a hard deadline
inside 20 minutes, and Python `shutil.rmtree` cleanup. It used the caller's
`.venv` read-only (Python 3.14.7 free-threaded, pytest 9.1.1, Hypothesis
6.168.1), with `PYTHONPATH` unset and imports from this workspace.

| Hold | Tree | Passed | Failed |
| --- | --- | ---: | ---: |
| `tests/unit/test_handler_refusals.py` | P1 tree (before `14fad997f` and `76d3e2c1c`) | 307 | 0 |
| `tests/unit/test_daemon_refusals_cli.py`, `tests/fake/test_request_routing.py`, `tests/fake/test_lost_acknowledgement.py` | P1 tree | 14 | 0 |
| `tests/unit/test_protocol_secrecy.py` | P1 tree | 317 | 0 |
| the same three files, CLI test from `babc5c8e8` | `git archive a29f83319` | 4 | 10 |
| `tests/unit/test_daemon_refusals_cli.py` (identity stubs) | `git archive a29f83319` | 0 | 8 |

At `a29f83319`, the two fake-daemon tests review r5 cited fail on
`'unknown job' in 'args: request was refused'` and `'different payload by
job …' in 'args: request was refused'`; both pass on the P1 tree. In the
first archive hold, the CLI test from `babc5c8e8` also failed on exit 69:
its `served` daemon looked stale to the CLI (item 10 above). With the
identity stubs, all eight CLI cases fail at `a29f83319` on `subfleet: args:
request was refused` and pass on the P1 tree. `14fad997f` only moves
`value_names` beside the checks it lists. `76d3e2c1c` restores the two
argument messages, adds one test and widens the AST check to request reasons.
An import check after `14fad997f` confirmed the same value names and
rendering, and one after `76d3e2c1c` that the daemon loads. No pytest hold
ran on either.

**Not run: the lock queue kept these out.** From 06:20 to after 10:00 the
shared suite lock was held by other sessions' runs of 20 to 50 minutes,
including an integration hub's, with five to eight sessions waiting and load
between 40 and 125. More than 30 of this session's attempts timed out in the
queue (exit 75). A foreground call can wait at most about nine minutes. These
are for the hub's host run, at the final head:

- `tests/unit/test_handler_refusals.py`. It last passed before `14fad997f`
  and `76d3e2c1c`; its new argument-refusal test has not run.
- `tests/unit/test_cli.py`, after P1. It passed 208 at `a29f83319` in the
  first job's third attempt. P1 changes no client code, and its fake daemon
  sends its own replies.
- `tests/unit/test_test_module_names.py`. The one new module,
  `test_handler_refusals.py`, has a basename of its own.
- The suggestion unit files: `test_suggestion_error_boundaries.py`,
  `test_suggestion_structural_errors.py`, `test_suggestion_secret_properties.py`,
  `test_suggestion_properties.py`, `test_suggestions.py`,
  `test_suggestions_r1b_adversarial.py`, `test_suggestions_r3_channels.py`,
  `test_suggestions_review_boundaries.py`, `test_suggestions_review_multiline.py`,
  `test_suggestions_review_response.py`, `test_suggest_cli.py` and
  `test_suggest_hint.py`. Also `tests/fake/test_suggestions_fake.py`.
- `tests/e2e/test_milestone1_codex.py`, whose different-payload assertion
  `88c6e806c` tightened, and `tests/e2e/test_suggestions_e2e.py`.
- The files the brief's grep names, and those holding fragments of the
  changed refusal messages:
  - `tests/unit/`: `test_outcome_unknown.py`, `test_lanes_transfer.py`,
    `test_sessions_cli.py`, `test_host_push_copy.py`, `test_wait_hub.py`,
    `test_store_migration_2.py`, `test_salvage_unindexable.py`,
    `test_retention_case_alias.py`, `test_host_push_mutations.py`,
    `test_host_push_build_budget.py`, `test_host_push_admission.py`,
    `test_host_push_metadata.py`, `test_host_push_validated_top.py`,
    `test_guard_overlay.py`, `test_folders.py`, `test_daemon_init_release.py`,
    `test_daemon_connections.py`, `test_cli_batch.py`,
    `test_operator_notices.py`, `test_conversation_service.py` and
    `test_conversation_handoff*.py`;
  - `tests/fake/`: `test_mcp_job_contract.py`, `test_lanes_enroll.py`,
    `test_canonical_identity_fix2.py`, `test_canonical_identity_fix4.py`,
    `test_daemon_stacks.py`, `test_state_contract.py`,
    `test_sessions_end_to_end.py`, `test_routing_end_to_end.py`,
    `test_notice_delivery.py`, `test_notice_header.py`,
    `test_lanes_reenroll.py`, `test_admission_route_isolation.py`,
    `test_policy_sandbox.py`, `test_salvage_finalization.py`,
    `test_workspace_transient.py` and `test_writable_fanout.py`.

  Each of their refusal-text assertions was read against the new code. They
  check the raised exception's own `str`, `code` or `fix`, a job notice, a
  fake daemon's own reply, or a launch receipt. Apart from the two messages
  `76d3e2c1c` restores, P1 leaves those unchanged: it adds keyword
  arguments, and only the wire text is templated. None checks daemon refusal
  text on the wire, so none is expected to change.

Commits on `feat/suggested-conversations` after `a29f83319`, all in the
shared repository (detached in this worktree, kept by Subfleet's salvage
ref):

- `babc5c8e8`: the CLI regressions;
- `7d1204825`: the P1 fix with its unit tests;
- `bb38acd37`: the CLI tests reach their daemon;
- `88c6e806c`: the e2e assertion;
- `08024d1d9`: the contract;
- `14fad997f`: the refactor;
- `76d3e2c1c`: the argument messages;
- this report.

Only named files were staged, and every commit carries the co-author
trailer. There were no network remotes, pushes or sub-agents, no writes to
the caller's checkout, no live-state changes, and no staged logs, evidence
or bundles. The reviewer's daemon probe ran from a temporary copy, which has
been removed. The `/tmp/sf-failed` state root that the first archive hold
left was removed; other sessions' state roots there were left alone.

## Round 9: caller and environment text stays out of records

Review r6 (`26104f78d`) found three places where text that a caller or its
environment controls still reached the wire or persisted state, against
C-25.7. The first job on this brief was cancelled before it committed. The
hub saved its work: a 684-line tracked patch and one new test file. This job
applied both at `169645a48` as two WIP commits (`81422b6ba`, `117b9307a`),
read every hunk as an unverified draft, and finished it.

**P1: a submit argument check's filename in `state_reason`.** `76d3e2c1c`
gave the argument check's `ProtocolError` its exception's words again, for
direct callers. The conversation service then wrote `str(exc)` into the
message's `state_reason`, which `message.status`, `conversation.open` and
`conversation.watch` show. `_refusal` now builds the reason from fixed parts
only:

- a conversation refusal's reason;
- a request or handler refusal's template message (`protocol.refusal_text`,
  which `protocol.refuse` now calls too);
- an exception's class with, for an OS error, its errno and `os.strerror`
  text (`refusals.fixed_error`).

The exception keeps its words for direct callers, so `76d3e2c1c`'s purpose
holds, and the words go to daemon.log, scrubbed whole
(`refusals.detail_line`). The reviewer's probe now reads `not-delivered:
args: required fields are missing or invalid` in both views, as it did
before `76d3e2c1c`.

**P1: a checkout hook's output in a job's notice.** Admission wrote
`str(exc)` of the `workspace-prepare` refusal into the job's notice, and that
text quoted `git worktree add`'s stderr. A failed `git worktree add` now logs
git's output, scrubbed whole, and raises fixed text. When git's words say to
wait, that is a `SalvageError` saying how git ended; otherwise it is the
`workspace-prepare` refusal, `could not allocate the worktree; daemon.log has
git's output`. Git's words still choose between the two (C-6.8). These are
the persistence boundaries checked.

Changed:

| Boundary | Shown by | Now holds |
| --- | --- | --- |
| A handler refusal at admission, at a push workspace's build, or from the conversation-session check (`_fail_queued`) | the notice, `job.failed`, `notice.pending` | the reason's template and fix (`_refused_job`) |
| A `workspace` hold | `why`, `status`, a turn's `state_reason` | the template, or C-6.8's record (`_hold_error`) |
| A `conversation-blocked` hold whose check raised | the same | the class, and `its text is in daemon.log` |
| C-6.8's workspace record | `job.workspace_deferred`, `job.workspace_failed`, the notice, `show` | git's verb and its cap, exit status or signal (`SalvageError.public`); an OS error's class, errno and strerror; any other exception's class |
| A folder that is not there now (`_folder_absent`) | the same | the folder the store holds, and the errno text |
| A handler refusal at launch | `exit.json`'s `spawn_error`, the attempt's detail, the notice, a turn's `state_reason` | the template; a guard preflight that timed out or could not probe has its own (item 5 below) |
| The launch-time branch check | the attempt's detail, the notice, `job.workspace_deferred` | C-6.8's record |
| A guardian that never gave its identity | the attempt's detail and notice, `attempt.no_launch` | an OS error's class and errno; `procs`' own messages as before |
| A failed salvage or checkpoint | `salvage.json`, the attempt's evidence (`show`), the notice | C-6.8's record |
| A failed turn end snapshot | `trees.json`, `turn_trees.error`, `turn.diff`'s `detail` | C-6.8's record |
| A refused or deferred turn submit | `state_reason` | fixed parts only (the first P1 above) |

Checked and unchanged. C-25.7 now names each of these as outside its rule:

| Boundary | Shown by | Holds |
| --- | --- | --- |
| A workspace completion SQLite could not record | the notice | SQLite's message |
| C-6.12's route refusals and deferrals, and `why`'s evaluation | the notice, the events, a route hold, `why` | the scheduler's message, or the evaluation error's class and text (C-6.12 wants `KeyError: 'utilization'` in `why`, and tests pin it) |
| Host push results | `push_error`, the notice's push line | `host_push`'s own messages; git's stderr is discarded where git runs, but a message may name the pushed branch and paths in the pushed tree |
| An attempt's outcome | the attempt, the notice | its adapter's reading of the provider's output |
| C-6.17's `disk` hold | `why`, `status` | its reading's OS error, for the policy's path |
| A quarantine | the notice, `quarantine_reason` | the containment census (pids, identities, `procs` errors) |
| `gate.*` results | the result | `message: str(exc)`, as round 8 noted |

These were checked and already hold fixed text: a pin no lane can admit
(`render.pin_failure`), `turn.diff`'s other unavailable reasons
(`diff.Unavailable`), and a turn whose manifest cannot be read.

**P2: credential-bearing job slugs.** The scrubber starts no token straight
after a letter, digit, `-` or `_`. Read whole, `20261013-000000-sk-…` was
nothing to it. `ids.holds_credential` now reads a value whole and from just
after each `-` and `_`. In C-1.1's alphabet a token runs to the id's end, so
these reads cover every place one can start.

The same check backs every id that may be shown or kept:

- the job ids a refusal names (`refusals.checked`);
- `daemon._job`'s own message, which reaches daemon.log;
- a suggestion's calling job;
- submission request ids (C-1.5) and correlation ids (C-25.7).

`show`, `why`, `kill` and `wait` answer `unknown job` and name nothing.
`_job` names an unknown id only when it is at most 200 characters, since
each read is a scrub. The result is cached, because a reply checks the
correlation id its request was admitted with.

The cost is one scrub for each hyphen or underscore. Measured once each:

- an app correlation id (`app-<uuid>`): about 0.33 ms, against 0.065 ms read
  whole;
- a 128-character id made of joiners: about 4.7 ms.

A UUID cannot spell a token (lowercase hex has no `s` or `k`), so the app's
and the CLI's ids pass as before.

**C-25.7 and C-1.5** now state what the code guarantees. That covers the
job records above and what each may say, the `state_reason` rule, how ids
are read for credentials, and, by name, the records that stay outside.

**What the draft got right, and kept:**

- `protocol.refusal_text`;
- the `refusals` helpers: `refusal_detail`, `class_name`, `errno_text`,
  `fixed_error`, `scrubbed_line`, `scrubbed_words`, `detail_line`, and
  `log_detail` with a subject;
- the worktree-add change and its new `workspace-prepare` wording;
- `_refused_job`, `_hold_error` and the rewritten `_workspace_error`;
- `SalvageError.public` and `os_error_text`, set on every `SalvageError`
  that quotes git's output or an OS error's text (`salvage.py`,
  `conversations/diff.py`, `_folder_absent`);
- `_refusal` and its logging;
- the test file's probes and properties.

**What changed:**

1. **The slug check.** The draft read a job id's slug after its timestamp,
   but not after any other hyphen. `build-sk-…` still passed, and the
   draft's own test pinned it as an ordinary slug. It now fails, as do
   `r6-glpat-…` and `run_sk-…`, and request and correlation ids follow the
   same rule. `ids.job_id_holds_credential` and its timestamp pattern are
   discarded.
2. **The hook property.** It reused one session across examples. A
   transient example left a waiting writer, so the next example's submit
   was refused as `unidentified-writer`. Each example now has its own
   session, and the property also checks that daemon.log holds the secret
   only scrubbed.
3. **The workspace log line.** `_workspace_failed`'s line had lost the
   cause's class (`TimeoutExpired`), which the C-6.8 tests read. The class
   is back, followed by the error's scrubbed words.
4. **Three more records** quoted git or OS text in the draft: the salvage
   receipt, the turn end-snapshot receipt and a guardian identity failure.
   They now say how git ended (`28800c97b`).
5. **Guard preflight wording.** Templating launch refusals would have
   reported a guard preflight timeout as `Codex guard trust preflight
   refused the writable job`. C-14.2 says a timeout is unverified, not
   mismatched. Two new reasons, `guard-preflight-timeout` and
   `guard-preflight-probe`, keep that wording with `guard.preflight`'s own
   fixes, chosen by the verdict's kind. A differential test holds them to
   those fixes.
6. **Existing tests.** The draft updated none of the tests that pinned the
   old text (next paragraph).

**Existing tests updated for the new records:**

- eleven tests in `test_workspace_transient.py`,
  `test_salvage_finalization.py`, `test_notice_header.py` and
  `test_turn_trees.py`;
- six in `test_salvage_adversarial.py`;
- seven `state_reason` cases in `test_conversation_service.py` and
  `test_conversation_steer_service.py`.

Each now requires the record's fixed form and finds git's words, still valid
UTF-8, in daemon.log. Where a test raised an `AdapterError` with no reason,
it now gives the reason that refusal's real raise site carries.

**Already failing at `169645a48`, now fixed.** Each was confirmed by running
it on `git archive 169645a48`:

- `test_conversation_service.py`, three tests:
  - The conversation worktree-add test expected git's words, but an earlier
    round made `worktree-failed` fixed text.
  - The two close-overtook tests. The tick logged each step through
    `suggestions.scrub_error`, which drops a prefix it does not know, so the
    line no longer named the step. It now logs the step's name and a fixed
    diagnostic. A first version put the error's scrubbed words in
    parentheses. `test_resumption_errors_are_scrubbed_by_the_outer_control_loop`
    caught that a `)` after a marker scrubs again, and C-25.7 gives
    control-loop errors fixed diagnostics (`409fd4915`).
- `test_turn_trees.py`: the end-snapshot test's live-diff assertion pinned
  `diff-failed`'s old text.
- `test_daemon_connections.py`, four tests: the helper sent the op's name,
  such as `daemon.status`, as the correlation id, and C-25.7's token rule
  refuses it.
- `test_notice_delivery.py`, four cases: they matched `notice id must be an
  integer`, which an earlier round replaced with the `bad-notice-id`
  template.
- `test_daemon_descriptors.py`, one test: it expected the thread error's
  words in a log line that `ed2100d45` made fixed.

**Not changed:** `tests/fixtures/admission_ramp_before.py` still holds the
old `str(exc)` lines. It is the frozen pre-ramp pass from release/217 at
`5bd0a3a`, and its differential test stubs `_workspace` and the
conversation check, so it never reaches them.

**Invariants**, stated and executed:

- **Turn submit.** Take any secret the scrubber recognizes, in any
  placement, in an argument check's filename, a handler refusal's words and
  repair, an OS error's filename or a defect's argument. No
  `message.status`, `conversation.watch`, `conversation.open` or
  `conversation.list` reply holds it, and no row of either store does
  (Hypothesis, 40 examples).
- **Checkout hook.** The same holds for a checkout hook's stderr, whether
  the failure is permanent or transient. It covers `notice.pending`,
  `notice.list`, `show`, `why`, `list`, `daemon.status`, every row, and
  daemon.log's raw text (30 examples).
- **Job slugs.** For every token family the slug alphabet can spell,
  `show`, `why`, `kill` and `wait` answer exactly `unknown job`, and no
  reply or row holds the token (60 examples).
- **`holds_credential`.** It refuses everything the whole-text scrubber
  refused (50 examples). It also refuses a token after any hyphen or
  underscore, and so does `request_id` (60 examples).
- **Differential.** The two guard templates equal `guard.preflight`'s
  fixes. Round 8's constant-text differential passes with the 150 reasons.

**The reviewer's probes, rerun against this tree:**

- `probe_bypasses.py`: 6 cases, none leaks. The three ids read `unknown
  job`, the notice reads the template, and both conversation views read the
  argument template.
- `probe_shapes.py`: 230 checks, no unexpected acceptance, 150 reasons.
- `probe_daemon.py`: 14 cases, each identical to the review's head output
  apart from a temporary path. Round 8's closures stay closed.
- `probe_malformed.py`: 3 cases, each two identical submissions under one
  submission id.

On `169645a48` the new record-secrecy file fails 38 of its 44 tests; the 6
that pass there are the ordinary-slug cases.

**Tests.** Each hold ran one foreground pytest process under the shared
lock, with its own `sf-r181f9-$$` TMPDIR and Hypothesis storage, a hard
deadline well inside 20 minutes, and Python `shutil.rmtree` cleanup. It used
the caller's `.venv` read-only (Python 3.14.7 free-threaded, Hypothesis
6.168.1), with `PYTHONPATH` unset and imports from this workspace. The
lane's own `SUBFLEET_RETENTION_DORMANT=1` made
`tests/fake/test_native_maintenance_startup.py` fail wholesale, so the
later fake holds unset it.

| Hold | Head | Passed | Failed |
| --- | --- | ---: | ---: |
| `tests/unit/test_protocol_secrecy.py` | `43ab51026` | 322 | 0 |
| `tests/unit/test_handler_refusals.py`, `tests/unit/test_daemon_refusals_cli.py` | `43ab51026` | 321 | 0 |
| `tests/unit/test_cli.py` | `43ab51026` | 208 | 0 |
| `tests/fake/test_request_routing.py` | `43ab51026` | 3 | 0 |
| `tests/fake/test_lost_acknowledgement.py` | `43ab51026` | 3 | 0 |
| the 12 suggestion unit files and `tests/fake/test_suggestions_fake.py` | `409fd4915` | 177 | 0 |
| `tests/e2e/test_suggestions_e2e.py` | `409fd4915` | 1 (1 skipped) | 0 |
| `tests/unit/test_test_module_names.py` | `409fd4915` | 1 | 0 |
| `--collect-only`, whole suite | `409fd4915` | 13,176 collected | 0 errors |
| `test_record_secrecy.py`, `test_workspace_transient.py`, `test_salvage_finalization.py`, `test_notice_header.py`, `test_turn_trees.py`, `test_daemon_launch.py` (fake) | `43ab51026` | 159 | 0 |
| `test_conversation_service.py`, `test_conversation_steer_service.py`, `test_conversation_reconcile.py`, `test_legacy_hold.py` (unit) | `43ab51026` | 235 | 0 |
| 16 related unit files (ids, contract indexes, salvage, retention, handoff, outcome-unknown, host-push storage, connections) | `409fd4915` | 501 | 4 |
| `tests/unit/test_daemon_connections.py` after its helper fix | `43ab51026` | 30 | 0 |
| 8 admission and workspace fake files | `43ab51026` | 212 | 4 |
| `tests/fake/test_notice_delivery.py` after its fix | `5404e4709` | 15 | 0 |
| fake files 1, 4, 7, … (32 files) | `5404e4709` | 518 (1 xfailed) | 6 |
| `tests/fake/test_salvage_adversarial.py` after its fix | `0010034f2` | 8 | 0 |
| fake files 2, 5, 8, … (32 files), without the dormant flag | `0010034f2` | 474 | 1 |

The four failures in the related-unit hold are the connection tests, the
four in the admission hold are the notice-id cases, and the one in the
second fake third is the descriptor test. All nine fail the same way at
`169645a48`. The six in the first fake third are the salvage-adversarial
cases this round's records changed. Each file was fixed and its rerun is in
the table. An ENOTCONN case of the connection file once failed its 0.2 s
read under load and passed on the next run of the whole file.

The skipped e2e test is person-only and cannot run inside a Subfleet lane.

**Not run: the lock queue kept these out.** From about 05:57 other sessions
held the shared lock, one run for more than 30 minutes, with up to eight
sessions waiting and load between 40 and 170. Each attempt waited about
seven minutes and exited 75 before pytest started. These are for the hub's
host run, at the final head:

- `tests/unit/test_store_ids.py`, with its two new properties. Both held
  when called directly, without pytest, under Hypothesis's engine: the
  token property's 200 examples took 15 s at load 143. The bounds were then
  cut to 60 and 50 examples.
- `tests/fake/test_daemon_descriptors.py`, after its one-assertion fix.
- The last third of `tests/fake` (31 files).
- The rest of `tests/unit`, about 10,000 tests, `tests/process` and
  `tests/frontend`. CI runs the full suite.

Commits on `feat/suggested-conversations` after `169645a48`, all in the
shared repository (detached in this worktree, kept by Subfleet's salvage
ref):

- `81422b6ba`, `117b9307a`: the cancelled job's draft, restored unverified;
- `b765fdf0d`: ids read for credentials from each hyphen and underscore;
- `28800c97b`: salvage, end-snapshot and launch records;
- `f6c23e601`: existing record tests and the guard-preflight templates;
- `97df05b4c`: new record tests and the guard differential;
- `9bf6542e1`: C-25.7 and C-1.5;
- `5df63990a`: `state_reason` tests and the tick's log lines;
- `409fd4915`: the tick's fixed diagnostic;
- `43ab51026`: the connection tests' correlation token;
- `5404e4709`: the notice-id test;
- `0010034f2`: the adversarial salvage tests;
- `95600a1ba`: C-25.7 names the disk hold;
- `dcb2d63e2`: the credential properties and the descriptor test;
- `903c9f2c8` and this commit: this report.

Only named files were staged, and every commit carries the co-author
trailer. There were no network remotes, pushes or sub-agents, no writes to
the caller's checkout and no live-state changes. No logs, evidence or
bundles were staged. The reviewer's probes ran against this tree with their
temporary roots in this job's TMPDIR, which was removed. Twice, a direct
call of the new properties outlasted the shell's two-minute cap and moved to
the background; both finished on their own. The `.hypothesis` folder one of
them left in the worktree was removed. Three `sf-r181f9-*` folders from
13 October, which the cancelled job left, were left alone.
