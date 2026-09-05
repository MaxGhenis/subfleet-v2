# Proposed clauses for the uncovered invariants

Version 1, 2026-09-05. `docs/invariants.md` adjudicated all 220 rows of the v1 ledger
(`reports/A-invariants.md`). Ninety-two kept or replaced rows carried `GAP` in the
`contract clause` column: the behaviour survives into v2, but no clause of
`acceptance-contract.md` states it. This file closes that column. Each proposal is clause text in
the contract's voice, numbered `P-23.<n>` so the integrator can fold the set in as section 23 of
the contract without renumbering anything that exists.

Nothing here is a new invariant. Every proposal names the ledger rows it covers, and every row it
covers previously read `GAP`. Where a proposal changes or extends a clause the contract already
has, or a rule `plan.md` already adopted, it is listed under [Conflicts](#conflicts) rather than
worded around.

## How to read a proposal

- The blockquote is the clause as it would read in `acceptance-contract.md`: present tense, one to
  three sentences, no rationale inside it. The `**P-23.<n>**` marker matches the contract's
  `**C-x.y**` convention so a fold-in is a copy.
- **Ledger rows** names every row the clause covers, with the row's disposition and class. One
  clause covers a behaviour, not a row: 92 rows become 55 clauses.
- **Milestone** is the milestone that owes the behaviour, per `plan.md`'s build sequence. The
  contract binds milestones 1 to 3; a proposal marked 4 to 8 is written now so the obligation is
  recorded, not because milestone 0 accepts it.
- **Acceptance owner** and **v2 module** carry over from the ledger row, per C-20.1.
- **Rationale** quotes the incident from the ledger row and says why no existing clause covers it.
  The test applied throughout: a clause covers a row only when a test of that clause fails if the
  invariant is violated. Where code already on this branch implements the row, the rationale says
  so — code without a clause is not accepted.

## Contents

| class | rows | clauses |
|---|---|---|
| [safety-guard](#safety-guard) | 25 | P-23.1 to P-23.14 |
| [capacity-truth](#capacity-truth) | 11 | P-23.15 to P-23.20 |
| [ops-hygiene](#ops-hygiene) | 15 | P-23.21 to P-23.29 |
| [session-continuity](#session-continuity) | 14 | P-23.30 to P-23.36 |
| [routing-policy](#routing-policy) | 3 | P-23.37 to P-23.39 |
| [provenance/attestation](#provenanceattestation) | 5 | P-23.40 to P-23.43 |
| [identity](#identity) | 8 | P-23.44 to P-23.47 |
| [UX-contract](#ux-contract) | 8 | P-23.48 to P-23.53 |
| [process-survival](#process-survival) | 3 | P-23.54 to P-23.55 |
| **total** | **92** | **55** |

Row 218 is an `ops-hygiene` row covered by a `provenance/attestation` clause (P-23.40), which is
why the two class counts here are 15 and 5 rather than the disposition's 16 and 4. Every other row
is filed under its own class.

## safety-guard

Twenty-five rows, fourteen clauses.

### P-23.1 — a caller's files are read-only to subfleet

> **P-23.1** No component of subfleet deletes, truncates, or rewrites a file the caller supplied.
> A job runs from the daemon's own copy of the prompt at `jobs/<job id>/prompt.md` (C-2.3), and the
> caller's `-p` file, the contents of `-C` that the provider does not itself change, and any file
> named in the prompt are read only to the daemon, the guardian, and every adapter.

- **Ledger rows:** 8 (`replace`, safety-guard)
- **Milestone:** 1
- **Acceptance owner:** fake
- **v2 module:** `subfleet/daemon.py`
- **Rationale:** row 8's rationale is that "inherited env markers do not prove ownership" — v1
  deleted a `delegate-prompt-*.md` it believed it owned, on the strength of an environment marker a
  parent could have set. C-2.1 does not cover the row: it permits writes to "job workdirs and
  worktrees the caller named", so a caller `-p` file inside its own `-C` tree is inside a C-2.1
  exception and a C-2.1 test still passes while the file is deleted.

### P-23.2 — an isolated review inherits no context and holds no hosted capability

> **P-23.2** `run -I` (isolated review) requires `-s read-only` and a review root `-D`; the review
> root and any further sources are exposed only through the provider's read-only source flag
> (`--add-dir` for Claude), and the Claude launch's tool allowlist is `Read`, `Glob`, and `Grep`.
> A `read-only` Claude launch removes `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`,
> `CLAUDE_MEMORY_STORES`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`, and the `CLAUDE_COWORK_MEMORY_*` family
> from the child environment before the provider initialises. An isolated Codex review runs
> `--ephemeral --ignore-user-config --ignore-rules` and disables every hosted capability v1's
> `prepare_isolated_review` disables — apps, plugins and remote plugins, hooks, multi-agent, tool
> suggestion, computer and browser use, image generation, goals, memories, shell snapshots and
> profile sourcing, project docs, web search, and each MCP server in the home's inventory by name —
> with `history.persistence` set to `none`.

- **Ledger rows:** 29 (`keep`, safety-guard), 31 (`keep`, safety-guard), 45 (`keep`, safety-guard)
- **Milestone:** 1 for the CLI and Codex halves, 2 for the Claude environment
- **Acceptance owner:** unit
- **v2 module:** `subfleet/cli.py`, `subfleet/adapters/claude.py`, `subfleet/adapters/codex.py`
- **Rationale:** the three rows are one behaviour, "isolated review integrity" (29), and their
  rationales say why a sandbox is not enough: "`--add-dir` would otherwise load the reviewed repo's
  rules" (31) and "filesystem read-only does not govern hosted tools" (45). C-17.2 enumerates
  `run`'s complete flag list without `-I` or `-D`, C-12.4 names only `ANTHROPIC_API_KEY` among the
  environment removals, and C-12.3 enumerates Codex argv without the hosted-tool disables, so no
  test of an existing clause fails if the mode is dropped. The exact names are from
  `bin/subfleet-claude:135-141,778-791,797-806` and `bin/subfleet-codex:322-360`. See
  [Conflicts](#c-1) for `-I` and `-D` against C-17.2.

### P-23.3 — an isolated review is refused when operator policy could override it

> **P-23.3** An isolated review is refused with exit 7, naming the variable or layer, when the
> submission inherits `CLAUDE_CODE_MANAGED_SETTINGS_PATH`, `CLAUDE_CODE_REMOTE_SETTINGS_PATH`, or
> `CLAUDE_CODE_MOCK_REMOTE_SETTINGS`, or when the Codex home reports managed configuration
> requirements or a nonempty `system` or `project` configuration layer. Operator policy is never
> silently discarded to make an isolated review possible.

- **Ledger rows:** 30 (`keep`, safety-guard), 46 (`keep`, safety-guard)
- **Milestone:** 1
- **Acceptance owner:** unit
- **v2 module:** `subfleet/cli.py`, `subfleet/guard/preflight.py`
- **Rationale:** row 30's incident is that "a parent could aim trusted hooks at the reviewed
  checkout"; row 46's is that "managed policy can override CLI sandbox flags". C-6.5 lists the
  exit-7 refusals and neither is on it, so a C-6.5 test passes while the review runs under inherited
  policy. The Codex predicate is v1's: layers of type `sessionFlags` and `user` are ours or ignored,
  `system` and `project` are permitted only when empty, and a non-null `configRequirements/read`
  result refuses (`bin/subfleet-codex:368-472`). See [Conflicts](#c-2) for the addition to C-6.5.

### P-23.4 — isolation is prepared per attempt

> **P-23.4** An adapter prepares isolation from the attempt's own lane on every launch: the
> configuration layers, the managed-requirements check, and the MCP inventory are re-read for that
> lane, and no isolation state computed for a previous lane or attempt is reused.

- **Ledger rows:** 47 (`keep`, safety-guard)
- **Milestone:** 1
- **Acceptance owner:** unit
- **v2 module:** `subfleet/adapters/codex.py`
- **Rationale:** row 47's incident is "stale inventory from the previous account" after auto-lane
  rotation. C-12.1 comes close — `build_launch(job, attempt, lane, credential_env)` takes the lane
  per attempt, and under C-4.6 a new lane is a new attempt — but a module-level cache of the config
  read satisfies C-12.1's signature while serving lane A's inventory to lane B, so a C-12.1 test
  does not fail on the violation. The integrator may reasonably judge C-12.1 sufficient and drop
  this proposal; it is written because the cache defeats it.

### P-23.5 — how long a guard preflight verdict is good for

> **P-23.5** A guard preflight verdict is reused only while the installed Codex version, the lane
> home, the override string, and the fingerprint of the seeded configuration are all unchanged; any
> difference re-verifies, and a lane home the daemon has not verified under that key is verified
> before its first launch on that lane. A cached verdict older than 30 days is discarded. A
> re-picked lane whose preflight fails refuses that launch rather than launching unguarded.

- **Ledger rows:** 41 (`keep`, safety-guard), 52 (`keep`, safety-guard)
- **Milestone:** 1
- **Acceptance owner:** fake
- **v2 module:** `subfleet/guard/preflight.py`
- **Rationale:** the two rows are the same question — when does an old verdict still bind. Row 41's
  rationale is that "lane rotation changes the config layer"; row 52's is that "a `features.hooks=false`
  edit must re-verify". C-14.2 scopes the preflight to "the first executable Codex job", so not
  re-running on a re-picked lane is consistent with C-14.2 and a C-14.2 test passes. Code on this
  branch satisfies both by keeping no cache at all (`subfleet/guard/preflight.py:7`, "There is no
  persistent cache"), which the clause permits; the clause still has to exist, because a later cache
  would be unconstrained. See [Conflicts](#c-3) for the scope change to C-14.2.

### P-23.6 — the `write_stdin` hole is closable

> **P-23.6** A Codex launch reads `SUBFLEET_CODEX_UNIFIED_EXEC`; when it is `off` the launch carries
> `-c features.unified_exec=false`, leaving `shell_command` as the only shell tool. `doctor` reports
> which setting is in force, and the setting is recorded on the attempt's decision record.

- **Ledger rows:** 65 (`keep`, safety-guard)
- **Milestone:** 1
- **Acceptance owner:** unit
- **v2 module:** `subfleet/adapters/codex.py`
- **Rationale:** row 65's incident is "421 unguarded `write_stdin` calls in one sampled rollout" —
  `write_stdin` emits no `PreToolUse` payload, so text typed into a PTY session bypasses every rule
  of the guard hook. C-12.3 mirrors v1's argv construction but names no unified-exec switch, and
  section 14 does not mention the hole, so nothing in the contract fails if the switch disappears.
  v1's default is on, source-verified against codex-rs 0.144.0 and not live-verified
  (`bin/subfleet-codex:60-69`); the clause keeps the default a policy choice rather than fixing it.

### P-23.7 — reset credits are gifts, not purchases

> **P-23.7** The reset-credit client lists and consumes only gifted entitlements. Its request URLs
> are an allowlist that contains no purchase, checkout, or add-credit endpoint, and no verb, action
> kind, or alert offers one.

- **Ledger rows:** 170 (`keep`, safety-guard)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** row 170's rationale is that "upsell CTAs are ignored by design". C-19.1's action
  kinds are `merge`, `reset-credit`, and later `send`, which does not forbid a purchase call inside
  a `reset-credit` action, so a C-19.1 test passes while the client spends money. v1 states the rule
  in a docstring only (`subfleet/codex.py:395-399`, "this never calls purchase/add-credit paths"); a
  URL allowlist makes it assertable.

### P-23.8 — a gate approves an exact revision

> **P-23.8** An approval binds to a fingerprint the caller attests — `--expect-sha256` for a plan,
> `--expect-head` with `--expect-base` for a pull request — and the gate never infers approval from
> a fresh read of the artifact. The fingerprint is re-captured after each peer round returns, and a
> round whose artifact changed while the peer was reviewing blocks instead of counting.

- **Ledger rows:** 193 (`keep`, safety-guard), 196 (`keep`, safety-guard)
- **Milestone:** 7
- **Acceptance owner:** unit
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 193's rationale is that "main must review the exact revision it approves"; row
  196's is "moving-target review". They are one behaviour at two moments — before the round and
  after it. C-19.1 keys a merge action by head sha, which fixes what the action operates on but not
  what the approval attested, so a C-19.1 test passes while an approval is inferred from a later
  read.

### P-23.9 — what counts as a peer verdict

> **P-23.9** A peer verdict is exactly one sentinel-delimited JSON object naming the revision that
> was reviewed, with no text outside the sentinels; anything else is not a verdict and the round
> does not count. An `approve` verdict carries no findings and no notes, and a `changes-requested`
> verdict carries at least one finding.

- **Ledger rows:** 194 (`keep`, safety-guard), 195 (`keep`, safety-guard)
- **Milestone:** 7
- **Acceptance owner:** unit
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 194 guards "verdict forgery and drift"; row 195 keeps "approval must mean zero
  actionable findings". No clause of the contract describes the gate's wire format or its verdict
  semantics at all. Parser fixtures belong beside the C-12.7 corpus and are redacted the same way.

### P-23.10 — where a peer round runs and what reserves it

> **P-23.10** Each peer round is reserved under a lease held for the round's lifetime; output from a
> round whose lease is no longer held is discarded and never counted as an approval. The peer runs
> `read-only` from a neutral directory the gate allocates under `$SUBFLEET_HOME`, never inside the
> repository under review.

- **Ledger rows:** 198 (`keep`, safety-guard), 199 (`keep`, safety-guard)
- **Milestone:** 7
- **Acceptance owner:** fake
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 198's rationale is that "durable state can live inside a repo"; row 199's is
  "duplicate reviews and stale approvals". C-2.4 refuses a `/tmp` workdir, which is why the neutral
  directory is under `$SUBFLEET_HOME`, and C-14.4 proves the read-only sandbox — but neither states
  that the peer must run outside the repository, and no clause reserves a round. In v2 the round
  lease is a store row (C-6.3's lease machinery), not v1's file lock.

### P-23.11 — what a merge requires before it is attempted

> **P-23.11** A merge action runs its preflight immediately before the remote call and proceeds only
> when the pull request is open and not a draft, its head and base are the approved ones, its
> mergeability is clean, and its checks have reached a terminal green state. The merge command pins
> `--match-head-commit` to the approved head.

- **Ledger rows:** 201 (`keep`, safety-guard), 202 (`keep`, safety-guard)
- **Milestone:** 7
- **Acceptance owner:** unit
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 201's rationale is that "merge is the only built-in external action"; row 202's
  is that "GitHub's head guard is the atomic part" — the pin is the only part of the sequence GitHub
  itself makes atomic. C-19.1 covers the action row, its `op_key`, and its states, not the predicate
  or the argv, so a C-19.1 test passes while a draft PR with red checks is merged.

### P-23.12 — how a landing is verified

> **P-23.12** The gate verifies a landing against the merge commit's own parents — the approved
> base, and for method `merge` also the approved head — never against the base branch's current
> tip. Merge methods are `merge` and `squash` only; a rebase landing is refused. A landing whose
> parents do not match is recorded as a mismatch on the action, and the gate neither retries the
> merge nor reverts it.

- **Ledger rows:** 203 (`keep`, safety-guard), 204 (`keep`, safety-guard), 207 (`keep`, safety-guard)
- **Milestone:** 7
- **Acceptance owner:** unit
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 203's rationale is that "the base branch moves after approval", row 207's that
  "the gate cannot verify rebase parents", and row 204's that "a race must not be papered over".
  C-19.1 supplies the terminal `failed` state but says nothing about what verification compares or
  what happens when it disagrees, so a C-19.1 test passes while the gate retries a merge it could
  not verify.

### P-23.13 — only the holder publishes an action's result

> **P-23.13** An action moves to `executing` by recording the identity of the worker that holds it,
> and only that holder may write its result; a result offered by any other worker is discarded and
> recorded as an event. A `confirmed`, `failed`, or `unknown` action is never overwritten by a later
> result for the same `op_key`.

- **Ledger rows:** 206 (`keep`, safety-guard)
- **Milestone:** 5 for `reset-credit`, 7 for `merge`
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** row 206's rationale is "concurrent recovery paths". C-19.1's arrow
  `pending → executing → confirmed | failed | unknown` covers the second half — a transition out of
  a terminal state fails a C-19.1 test — but not the first: two workers both holding the same
  `executing` row race with no clause broken, which is exactly the recovery path v1's file lock
  guarded (`subfleet/consensus.py:775-784`). The unique `op_key` plus C-3.2's one transaction per
  transition replaces the file lock; the holder identity is what the store still needs.

### P-23.14 — what a handoff may carry

> **P-23.14** A handoff excerpt is scrubbed before it is written: private keys, JWTs, prefixed API
> tokens, and `Bearer` values are replaced, encoded binary is omitted, and the result of any tool
> call whose input matches a credential-reading pattern (`agent-secret get`, a keychain read, `env`
> or `printenv`, `auth.json`, `.env`, a credentials file) is omitted by pattern rather than
> redacted. Ordinary code, commands, and tool output are retained verbatim.

- **Ledger rows:** 208 (`keep`, safety-guard), 209 (`keep`, safety-guard)
- **Milestone:** 6
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/handoff.py`
- **Rationale:** row 208's rationale is that "lossy rewrites destroy continuity; credentials must
  not travel", and row 209's that "secrets leak through tool results" — suppression is keyed on the
  tool *input* so a secret never reaches the excerpt even unredacted. C-10.5 is the analogous
  milestone-1 rule and covers lane credentials in the launch environment only, so a C-10.5 test
  passes while a handoff carries a keychain read's output. The categories are v1's
  (`subfleet/handoff.py:51-160` for the patterns, `:100-110` for the tool-input suppression).

## capacity-truth

Eleven rows, six clauses.

### P-23.15 — transcript usage is summed once per message

> **P-23.15** Token usage read from a Claude transcript is summed once per `message.id`: a repeated
> assistant message replaces the record already held for that id and never adds to it. The count is
> a `readings` observation, not a job column.

- **Ledger rows:** 128 (`keep`, capacity-truth)
- **Milestone:** 2
- **Acceptance owner:** unit
- **v2 module:** `subfleet/adapters/claude.py`
- **Rationale:** row 128's rationale is that "transcripts repeat updated assistant messages", so a
  naive sum double-counts every streamed revision. C-6.4 names an optional `max_tokens_observed`
  cap, which implies a token observation exists but says nothing about how it is computed, so a
  C-6.4 test passes on a doubled figure — and a cap enforced on a doubled figure kills jobs early.

### P-23.16 — when a reset credit may be spent

> **P-23.16** A reset-credit redemption is attempted only when the usage endpoint reports the
> account `limit_reached` and the entitlements list holds a concrete `available` credit of type
> `codex_rate_limits`; neither condition alone admits it. Each consume carries a fresh UUID4
> `redeem_request_id` in the action's request JSON. Only a response with code `reset` and
> `windows_reset` greater than zero moves the action to `confirmed`.

- **Ledger rows:** 161 (`keep`, capacity-truth), 164 (`keep`, capacity-truth), 165 (`keep`, capacity-truth)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** the three rows are the precondition, the request, and the success predicate of one
  irreversible action. Row 161's rationale is "do not spend a scarce gift on a guess", row 164's is
  "idempotency for an irreversible action", and row 165's is the "upstream success enum". C-18.1
  adopts "reset-credit policy as an action (C-19) with v1's rule set" at interface level only, and
  C-19.1 owns the `confirmed` state without a success predicate, so a C-19.1 test passes while a
  credit is burned on a guess and any 200 is read as success. v2's durable double-spend guard is
  C-19.1's unique `op_key` (account key plus credit id); the UUID rides inside the request.

### P-23.17 — a confirmed consume is the authority

> **P-23.17** A `confirmed` consume is the later `provider` reading C-9.6 requires: it releases the
> lane's weekly closure, sets the weekly reset clock to now plus seven days with
> `clock_source: guessed`, and leaves the lane dispatchable even while the usage endpoint still
> reports the old window. It also clears the lane's `local-backoff` dispatch closure.

- **Ledger rows:** 167 (`keep`, capacity-truth), 169 (`keep`, capacity-truth)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** row 167's rationale is that "endpoint lag must not undo a real redemption"; row
  169's is that "the reset supersedes the 15-minute cooldown". C-9.6 says closures "expire by
  clock; nothing else releases a provider-limit closure except a later `provider` reading that shows
  the window reset" — the clause names the confirmed consume as that reading rather than carving an
  exception, which is why it is a proposal and not an amendment to C-9.6. Now plus seven days is a
  guessed clock under C-9.4, marked as one.

### P-23.18 — an unreadable count is not a small count

> **P-23.18** A fleet-wide credit total is rendered only when every lane's count was readable; if
> any lane's count is unknown the total is `null` and is displayed as unknown, never as a sum over
> the readable lanes.

- **Ledger rows:** 168 (`keep`, capacity-truth)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** row 168's rationale is that "unknown is rendered as unknown" — the failure it
  prevents is a false undercount that reads as "we are nearly out". C-9.1 forbids rendering a
  percentage without a `provider` reading but says nothing about aggregates, and a count is not a
  percentage, so a C-9.1 test passes on a silent undercount. This is amendment 15's rule
  ("admission-observed and local-backoff evidence render as words, never as a percentage") applied
  to a total rather than a rate.

### P-23.19 — when a five-hour window is open

> **P-23.19** A keepalive reading's observed-at instant is the moment the provider request is sent,
> not when the worker was queued or the credential was read. A lane whose window was opened by any
> request inside the last five hours — a keepalive, a completed attempt, or an attempt still
> running — is skipped and recorded `skipped-open`. An attempt that ended rc 5 without a recorded
> provider session did not open the window.

- **Ledger rows:** 172 (`keep`, capacity-truth), 173 (`keep`, capacity-truth), 174 (`keep`, capacity-truth)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/keepalive.py`
- **Rationale:** the three rows answer one question, when the lane's window opened. Row 172's
  rationale is that "the 5h window starts at the request", row 173's that "another ping only
  consumes usage", and row 174's that "a no-session rc=5 may mean no provider request happened".
  C-18.1 gives the 5 h 05 m cadence and nothing else; C-9.1's `admission-observed` label needs a
  success or a rejection, so an in-flight attempt — which has neither — has no label and no rule,
  and a C-18.1 test passes while a keepalive pings a lane that is already open.

### P-23.20 — revive measures the lane it is about to use

> **P-23.20** Revive admits a lane only on a `provider` reading taken in the same pass; a stored
> reading, a `stale-provider` reading, or the absence of a closure never qualifies a lane on its
> own.

- **Ledger rows:** 189 (`keep`, capacity-truth)
- **Milestone:** 6
- **Acceptance owner:** fake
- **v2 module:** `subfleet/sessions/tickle.py`
- **Rationale:** row 189's incident is 2026-08-25, when "three 'healthy' lanes were out of Fable".
  C-11.4 requires a probe before dispatching expensive work — writable, or tier `hard` — to an
  *unmeasured* lane; a revive is usually neither, and the lanes in the incident were measured but
  stale, so a C-11.4 test passes on exactly the case that failed. C-9.1 removed estimates from the
  label set, which is the other half of the fix and is already contracted.
