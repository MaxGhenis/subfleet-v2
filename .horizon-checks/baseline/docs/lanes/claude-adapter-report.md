# Lane report: claude-adapter (milestone 2)

Branch `lane/claude-adapter`. Clause numbers are `docs/acceptance-contract.md`.

## Built

- `subfleet/adapters/claude_stream.py` — a pure, tolerant parser for the
  `--output-format stream-json --verbose` event set, typed against the schemas the
  installed Claude Code 2.1.260 binary validates its own output against.
- `subfleet/adapters/claude.py` — `ClaudeAdapter`: credential resolution that never
  logs a value, the Haiku enrolment and probe turn, `rate_limit_event` to `Reading`,
  authentication-then-admission-then-quota classification, transcript attestation that
  never gives a false positive, v1's `prefer_transcript_text` deliverable rule, and
  build and resume launches carrying v1's `PERM_ARGS` verbatim.
- `subfleet/contracts.py` — one additive field, `Launch.notes`.
- `tests/fixtures/claude/` — 19 cases with `stdout`, `stderr`, `rc`, `expected.json`,
  and a `transcript.jsonl` where attestation needs one.
- `tests/fixtures/claude/make_fixtures.py` — the committed generator; the provenance
  record for every byte in the corpus.
- `tests/bin/claude` — the fake provider: replays a fixture, threads a caller-chosen
  `--session-id` through the stream and a transcript, reports environment-variable
  presence (never values), honours a delay.
- `tests/conftest.py` — the pinned clock, the pinned session id, and helpers that let
  every test call adapter methods on fixture directories with no store and no daemon.
- `tests/unit/test_claude_stream.py`, `tests/unit/test_claude_adapter.py`,
  `tests/unit/test_claude_attest.py`, `tests/unit/test_claude_v1_parity.py`,
  `tests/process/test_claude_isolation.py`, `tests/live/test_claude_live.py`.

## The sensor, in one paragraph

Every headless `claude -p` run emits a `rate_limit_event` whose `unifiedWindows` carry
`utilization` as a fraction and `resetsAt` as epoch seconds. `status: allowed` (or
`allowed_warning`) becomes two `provider` readings scoped to the account. `status:
rejected` becomes no utilization reading at all — only an `admission-observed`
rejection for the requested model, carrying the event's `resetsAt` as the closure
clock, with the refusing window's numbers kept in the outcome's evidence. Nothing in
either module multiplies by 100 or formats a `%`; a test asserts that, so the routing
lane is the only place a percentage can appear.

## Fixture provenance

| Case | Class | rc | Real | Synthetic |
|---|---|---|---|---|
| `success-allowed` | ok | 0 | `rate_limit_info` (experiment-0, max@axiom.org) | frames, transcript |
| `allowed-out-of-credits-overage` | ok | 0 | `rate_limit_info` (experiment-0, max@thesisinstitute.org) | frames, transcript |
| `allowed-on-table-exhausted-lane` | ok | 0 | `rate_limit_info` (experiment-0, max.ghenis@gmail.com) | frames, transcript |
| `rejected-credits-fable` | limited | 1 | `rate_limit_info` and the result text (experiment-0, max@policyengine.org) | init and result envelope |
| `limit-session-with-clock` | limited | 1 | — | all (v1 keeps no text-limit artifact) |
| `limit-weekly-with-clock` | limited | 1 | — | all (shape follows the CLI's own validator) |
| `limit-no-clock` | limited | 1 | — | all |
| `auth-401` | auth-dead | 1 | the Anthropic `authentication_error` envelope shape | the message |
| `org-block` | auth-dead | 1 | the org-block sentence (CLI 2.1.260 string table) | frames |
| `cli-too-old` | cli-too-old | 1 | the sentence (quoted in v1 from CLI 2.1.228) | the 400 envelope |
| `cli-too-old-current` | cli-too-old | 1 | the sentence (CLI 2.1.260 string table) | the 400 envelope |
| `stream-disconnect` | transient | 1 | the stderr sentence (CLI 2.1.260 string table) | frames, the cut tail |
| `model-downgrade` | ok / mismatch | 0 | `rate_limit_info` (experiment-0) | frames, transcript |
| `empty-result-rc0` | unknown | 0 | `rate_limit_info` (experiment-0) | frames |
| `transient-server-throttle` | transient | 1 | the sentence (CLI 2.1.260 string table) | frames |
| `auth-signature-false-positive` | transient | 1 | `rate_limit_info` (experiment-0) | frames |
| `content-filter` | content-filter | 0 | `stop_reason: "refusal"` (CLI 2.1.260) | frames |
| `spawn-failure` | unknown | 127 | — | all |
| `ok-with-background-task-warning` | ok | 0 | the stderr line (v1 run `20260905-063243-us-housing-source-graph`) | frames, transcript |

Every case is marked `"synthetic": true` in its `expected.json` because every one has
an assembled event sequence around whatever real bytes it carries; the `provenance`
field names exactly which parts are real. No fixture holds a token, a cookie, an
`Authorization` value, or prompt text; a test asserts that too. Emails are kept — they
are the account keys (C-1.4).

Why so much is synthetic: the newest 150 v1 run directories hold 121 Claude runs, and
exactly one keeps a non-empty `err.log`. v1's text-classified limits (`rc=4`) leave an
empty `err.log` and an empty `out.md`, and its `lane-usage.jsonl`, `alerts.json`, and
`history.jsonl` retain no provider text. So the message strings were taken from the
next most authoritative sources available: the installed CLI's own string table and
v1's classifier comments, which quote messages observed in production.

## Clauses covered

- **C-1.4, C-1.7** — account key from the credential reference or a home's
  `.claude.json`; epoch seconds to ISO 8601 UTC with `Z` and second precision.
- **C-2.3, C-8.1** — `prompt.sent.md` written temp-and-rename with `fsync` on the file
  and the directory, mode 0600; every artifact inside the attempt directory.
- **C-6.7** — the headless block prepended unless the marker is present, `prompt.md`
  never rewritten, `stdin_path` pointing at `prompt.sent.md`.
- **C-9.1** — five reading labels, no sixth; fractions only, no percentage anywhere.
- **C-9.2** — precedence authentication, admission, quota, with which evidence
  answered each recorded in `Outcome.evidence["answered"]`; the raw rc and signal kept
  beside every class.
- **C-9.3** — `auth-dead` needs an organisation block or an explicit provider error
  kind; a 401 signature with a successful `system/init` is not `auth-dead`.
- **C-9.4** — `limited` carries scope, the reported clock or now + 3600 s marked
  `guessed`, and the evidence.
- **C-9.5** — `transient` for 5xx, retries, stream drops, and connection failures;
  never a closure.
- **C-9.6** — closures are (lane, scope, until, reason, clock source, source event).
- **C-9.8** — the whole of it, plus `overageStatus` parsed independently and never
  read as admission evidence.
- **C-10.1, C-10.2, C-10.5** — keychain and home credentials; one Haiku turn at
  enrolment; refusal with exit 5 when `system/init` never arrives; the value only ever
  in `Launch.env_add`.
- **C-11.4** — `probe_with_model`, and `probe_outcome` so "a `limited` result closes
  the scope" is actionable.
- **C-12.2, C-12.4** — the launch line, per-sandbox permission flags equal to v1's,
  `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` removed, the prompt on stdin, the
  session id chosen up front, and `claude -p --resume` for a continuation.
- **C-12.5** — attestation from the transcript located by session uuid, within the
  attempt's own byte range, with exactly one match required.
- **C-12.6** — v1's `prefer_transcript_text` rule; empty with rc 0 is `unknown`.
- **C-12.7, C-12.8** — the fixture corpus and the fake provider.
- **C-14.3** — a Claude launch relies on the global hook; a `guard_override` is
  accepted and never reaches the argv.
- **C-14.4** (the Claude column) — no API key reaches the child; the credential does;
  stdout, stderr, and the sent prompt land in the attempt directory.
- **C-20.1, C-20.5** — the test layout, the opt-in live test, and a clause in every
  docstring.
- **C-21 milestone 2** — all four experiment-0 payloads; a rejected event closing the
  model scope with the reported clock; `mismatch` on the model-downgrade fixture; no
  percentage rendered without a `provider` reading; and a hard limit producing a
  closure that names the lane, the scope, and an expiry — the three things C-11.2
  rejects a candidate on, so the next attempt can carry the exclusion.

## Clauses not covered, and why

- **C-14.4's write matrix** ("could not write outside the workdir in `read-only`, could
  write inside it in `workspace-write`"). Claude Code has no OS sandbox: `read-only` is
  enforced entirely by the permission flags in the argv. Asserting a filesystem denial
  would be asserting something this provider does not do. The tests assert instead that
  the flags are present, that the bypass flag is absent, and that no writing tool is
  named — and `tests/unit/test_claude_v1_parity.py` ties that flag list to v1's own
  source at test time. The full cross-provider matrix belongs to the core lane, which
  owns `procs.py` and the guardian.
- **C-4.5's retry admission, C-11.2's exclusion walk, C-13.3's reconciliation.** The
  adapter produces the closure and the class those rules consume; the daemon and the
  scheduler apply them, and they are other lanes' files.
- **C-8.2's artifact rows, C-15's notices, C-19's actions.** Store-side; adapters
  return data and never touch the store (C-12.1), and a test asserts that `classify`,
  `attest`, and `deliverable` neither spawn a process nor write a file.
- **C-12.7's `refresh-token-revoked` case.** That is a Codex event
  (`~/.codex/auth.json`); the Claude equivalent, a dead setup token, is `auth-401`.
- **C-20.3's crash matrix.** Daemon-side.

## Seam changes

1. **`Launch.notes: dict[str, Any] = field(default_factory=dict)`** (additive, with a
   default, at the end of the dataclass — nothing that constructs a `Launch` today
   breaks). An adapter needs facts back at classification, attestation, and resume time
   that no parameter carries: the lane id and attempt id a `Reading` or `Closure` must
   be stamped with, the model requested, and for Claude the expected transcript path
   with its byte size at launch. The daemon must persist it beside the attempt and hand
   it back unchanged; it is JSON-serialisable and holds no secret, and a test asserts
   both.
2. **`Adapter.resume_launch` gained a keyword-only `model_id: str | None = None`** on
   the Claude implementation. The base signature has no model, but an attempt never
   changes model (C-4.6), so a resume must pin the same one. The default keeps the base
   signature satisfied; without it the job's `pinned_model` is used, and failing that
   `--model` is omitted and the session keeps its own.
3. **`Reading.window` may be `"admission"`.** C-9.1 enumerates
   `"five_hour" | "seven_day" | "<minutes>"`, none of which describes an
   `admission-observed` reading — admission is not a quota window. See the open
   questions below.
4. **No other name or semantic in `contracts.py` or `base.py` changed.**

## Open questions for the integrator

1. **`Reading.window == "admission"`.** Is this the right spelling, or should
   `admission-observed` readings carry the refused window's key (`five_hour`), or an
   empty window? The label already says the reading is not a utilization; a window key
   that names a quota window risks a comparator treating it as one. Whatever is chosen,
   C-9.1's enumeration should say so.
2. **`stdout` versus `stream.jsonl`.** `--output-format stream-json` writes the stream
   to stdout, so there is no second descriptor to redirect and the two files are the
   same bytes. `build_launch` sets `stdout_path` to `<attempt>/stdout` and
   `raw_stream_path` to `<attempt>/stream.jsonl`; the adapter reads whichever exists.
   `subfleet.adapters.claude.link_raw_stream(attempt_dir, launch)` hard-links one to the
   other at finalization so the raw stream gets its own artifact role (C-8.2) without a
   second copy. Does the daemon want to call it, or should `raw_stream_path` be `None`
   for Claude and `stdout` recorded under both roles?
3. **A rejected event's window numbers.** C-9.8 says a rejection "yields no reading",
   which this adapter follows literally: the windows go into `Outcome.evidence` rather
   than becoming `provider` readings. That is the conservative choice (a refusing
   window's utilization is not headroom), but it does discard a live measurement. If
   the router would rather have them as readings, C-9.8 needs amending, not the code.
4. **`--effort`.** C-12.4's Claude line carries no effort, but the interface passes one
   and `policy.json` models may have one. The adapter emits `--effort <level>` only when
   the caller supplies it, placed after `--verbose` and before the permission flags so
   v1 parity of the permission block is untouched. If Claude effort should never be
   pinned, delete four lines in `build_launch` and one test.
5. **Enrolment readings have `lane_id=""`.** The lane does not exist yet; the daemon
   should stamp the id it assigns onto `LaneInfo.readings` before writing them.
6. **Account verification on a token lane.** A `claude-quota-<email>` keychain item
   holds a bare setup token, and a transcript row carries no account field (checked: the
   row keys are `type`, `sessionId`, `cwd`, `version`, `gitBranch`, `uuid`, `message`,
   and friends — no email anywhere). So a token lane's account key comes from the
   credential reference alone and cannot be verified against anything on disk. Only a
   home lane can be verified, through its `.claude.json` `oauthAccount.emailAddress`,
   and a contradiction there is refused with exit 7. If verification matters for token
   lanes, it needs a new source — the usage endpoint, or an enrolment-time echo.
7. **`--max-turns` is real but undocumented.** It is absent from `claude --help` in
   2.1.260 and present in the binary's flag table with a description; experiment 0 ran
   it successfully. The enrolment turn uses it as the brief specifies. If it is ever
   removed, the enrolment turn would run unbounded — worth a `doctor` check.
