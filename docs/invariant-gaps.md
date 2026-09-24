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

> Folded into `docs/acceptance-contract.md` as section 23 on 2026-09-05 13:50 EDT: every `P-23.n` below is now clause `C-23.n`. This file stays as the provenance record (ledger rows, rationale, incidents) and the conflict list with the integrator's dispositions.

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
  result refuses (`bin/subfleet-codex:368-472`). v1 exits 2 on both refusals; the clause says 7
  because C-17.3 gives exit 7 one meaning, "refused (message names the rule and the fix)", and the
  disposition makes the same constant change on row 51. See [Conflicts](#c-2) for the addition to
  C-6.5.

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
> which setting is in force.

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

## ops-hygiene

Fifteen rows, nine clauses. Row 218 is the sixteenth `ops-hygiene` gap row and is covered by
P-23.40 under provenance/attestation.

### P-23.21 — how a provider binary is found

> **P-23.21** A provider binary is resolved to an absolute path before launch, never left to
> `PATH` alone. Claude resolves `SUBFLEET_CLAUDE_BIN`, then `~/.local/bin/claude`, then `PATH`;
> Codex resolves `SUBFLEET_CODEX_BIN`, then `PATH`, then its known install locations. The resolved
> path goes into the launch argv, is recorded on the attempt, and is printed by `doctor`.

- **Ledger rows:** 26 (`keep`, ops-hygiene), 158 (`keep`, ops-hygiene)
- **Milestone:** 2
- **Acceptance owner:** unit
- **v2 module:** `subfleet/adapters/claude.py`, `subfleet/adapters/codex.py`
- **Rationale:** the same failure on both providers. Row 26's incident is 2026-09-02, when "launchd
  PATH puts `/opt/homebrew/bin` first" and a Homebrew cask frozen at 2.1.87 shadowed the real CLI;
  row 158's is 2026-08-13, when "the auto-heal raised FileNotFoundError for a week" because
  "launchd's PATH lacks `~/bin`" and `.codex-3` latched failed. No clause states provider-binary
  resolution at all: C-12.3 and C-12.4 begin at `codex` and `claude` as bare names. Under a daemon
  that launchd starts, that bare name is the whole bug. The clause keeps the two orders apart
  rather than unifying them, because v1's differ and each difference is load-bearing:
  `~/.local/bin` before `PATH` is the whole point on Claude, where `claude update` maintains the
  native launcher a Homebrew cask on `PATH` shadows (`subfleet/paths.py:147-165`), while Codex has
  no `~/.local/bin` candidate at all and falls back past `PATH` to the bun global install, `~/bin`,
  and the two Homebrew prefixes (`subfleet/codex.py:454-480`). v1's override variables were
  `CLAUDE_LANE_CLAUDE` and `$SUBFLEET_CODEX_BIN`; row 219 collapses the prefixes to `SUBFLEET_*`,
  which is why the clause names the new spellings.

### P-23.22 — accounting never changes an outcome

> **P-23.22** Usage accounting runs synchronously inside finalization, before the notice is written,
> and never changes a job's class, rc, deliverable, or state. A hook failure or a parse failure
> records an `events` row and finalization continues, exactly as a failed export does under C-8.3.

- **Ledger rows:** 35 (`keep`, ops-hygiene), 129 (`keep`, ops-hygiene)
- **Milestone:** 1 for the finalization ordering, 2 for the parser
- **Acceptance owner:** fake
- **v2 module:** `subfleet/daemon.py`
- **Rationale:** row 35's rationale is that "retries would overwrite err/raw before parsing" — the
  hazard per-attempt directories (C-2.3) remove — and row 129's is the "monitoring sidecar rule". The
  nearest clauses are C-8.4, which puts probe and keepalive results in `readings` and `events` rather
  than `jobs`, and C-8.3, which states the same containment for the `-o` export; neither mentions
  accounting, so a test of either passes while a JSON parse error fails an otherwise successful job.

### P-23.23 — how the guard preflight probes

> **P-23.23** The guard preflight never writes a lane's provider home: it seeds a scratch home under
> `$SUBFLEET_HOME` with copies of the lane home's `config.toml` and `hooks.json`, copies no
> credential or session store, and probes there. The probe carries a deadline: it fails at once if
> the app-server exits without answering, and at the deadline it SIGTERMs the probe's process group,
> waits, then SIGKILLs it.

- **Ledger rows:** 50 (`keep`, ops-hygiene), 53 (`keep`, ops-hygiene)
- **Milestone:** 1
- **Acceptance owner:** process
- **v2 module:** `subfleet/guard/preflight.py`
- **Rationale:** row 50's incident is dated: "the first preflight (2026-08-18 23:53) rewrote
  `~/.codex-5/config.toml` via personality migration" — the check corrupted the thing it checked.
  Row 53's is that "the npm launcher forwards TERM; a KILL would orphan the binary". C-14.2 states
  the preflight's inputs (version, hash, override) and nothing about how it runs, and C-2.1 bars
  writes only outside `$SUBFLEET_HOME`, which leaves a lane home under `lanes/<lane id>/` (C-2.2)
  writable. C-5.6 is the escalation shape the second sentence reuses, but C-5.6 governs an attempt's
  recorded process group, not a probe the daemon spawns, so it does not cover the row. Both halves
  are implemented on this branch (`subfleet/guard/preflight.py:210-219` for the scratch home,
  `:94-106` and `:134-142` for the deadline) — see [Conflicts](#c-4) for where that code puts the
  scratch home.

### P-23.24 — a guard check writes nothing

> **P-23.24** The guard check verb evaluates a payload against the copied hook and prints the
> verdict without writing the hook's denial log, the preflight's cache, or any other guard
> telemetry.

- **Ledger rows:** 56 (`keep`, ops-hygiene)
- **Milestone:** 8 (guard parity)
- **Acceptance owner:** unit
- **v2 module:** `subfleet/guard/preflight.py`
- **Rationale:** row 56's rationale is that "manual checks must not pollute telemetry" — a denial log
  that records rehearsals cannot be read as a record of what agents actually attempted. C-19.1's
  last sentence is the analogue ("`status`, `why`, and `--dry-run` never create or advance an
  action") but it is scoped to actions, and C-17.1's verb list has no guard check verb at all, so no
  test fails when the check logs. See [Conflicts](#c-5) for the verb.

### P-23.25 — installing a hook into a shared settings file

> **P-23.25** Before changing `~/.claude/settings.json` the installer writes a timestamped backup
> beside it, and the change preserves every key the installer does not own. A second install of the
> same version makes no further change and writes no further backup.

- **Ledger rows:** 70 (`keep`, ops-hygiene)
- **Milestone:** 4
- **Acceptance owner:** unit
- **v2 module:** `subfleet/hooks.py`
- **Rationale:** row 70's rationale is that "settings.json is shared with the user" — an installer
  that rewrites it wholesale destroys configuration subfleet never owned. C-2.1 does not permit the
  write at all: `~/.claude/settings.json` is outside `$SUBFLEET_HOME` and is none of its three
  exceptions, so the milestone-4 hook installer needs C-2.1 amended before it needs this clause. See
  [Conflicts](#c-6).

### P-23.26 — secondary records are pruned by age, never by a caller's window

> **P-23.26** The maintenance pass deletes notices in state `surfaced` or `acknowledged` whose row is
> older than 14 days, and never prunes a `pending` or `offered` notice by age. A provider scan-cache
> entry is dropped only when its file no longer exists or its record is more than seven days old; a
> narrower scan window never evicts an entry.

- **Ledger rows:** 80 (`keep`, ops-hygiene), 141 (`keep`, ops-hygiene)
- **Milestone:** 2 for the scan cache, 4 for notices
- **Acceptance owner:** unit
- **v2 module:** `subfleet/retention.py`, `subfleet/adapters/codex.py`
- **Rationale:** both rows are age-based pruning of a sidecar store, the C-8.4 analogue for records
  C-8.4 does not reach. Row 80's rationale is "file growth"; row 141's is that "narrower callers
  would force rescans" — an eviction rule keyed on the current call's window makes every scan pay
  for the last caller's narrowness. C-8.4 covers job retention and protects unread notices from *job*
  pruning; it neither prunes notices nor knows the scan cache exists, so a C-8.4 test passes while
  notices accumulate forever and the cache thrashes. C-10.3 makes `~/.codex` observed and never a
  lane, which is the home this cache is usually built over.

### P-23.27 — one monitoring cycle, one verdict

> **P-23.27** A cycle heals before it persists: `status.json`, the readings the cycle writes, its
> alert conditions, and its history all see post-heal verdicts. At most one refresh probe runs per
> provider home per cycle, and two probes of the same home are at least twenty minutes apart. A
> cycle in which every provider probe failed with a network error is recorded `offline` and emits no
> alert of any kind, including scoped-limit conditions.

- **Ledger rows:** 152 (`keep`, ops-hygiene), 153 (`keep`, ops-hygiene), 154 (`keep`, ops-hygiene)
- **Milestone:** 5
- **Acceptance owner:** fake
- **v2 module:** `subfleet/timers.py`, `subfleet/alerts.py`
- **Rationale:** three rules of the same cycle. Row 153's rationale is "mixed-state reporting" — a
  snapshot half taken before the heal and half after describes a fleet that never existed. Row 152's
  is that "back-to-back manual runs would hammer a dead lane", and row 154's is that "offline must
  not cry wolf". C-18.1 gives the probe cycle's cadence (`probe_interval_s` 300, one probe per idle
  lane per window) and the alert cadence (on transition, at most every 6 h while persisting), but the
  heal probe is not the probe cycle and C-18.1 states no ordering within a cycle and no offline
  suppression, so a C-18.1 test passes on all three failures. The ledger records a caveat on row 154:
  v1 lets non-silent scoped-limit conditions past its own offline check, so the v2 test asserts a
  wholly silent cycle, which is why the clause says "of any kind".

### P-23.28 — mirror health is a state file, not a quiet log

> **P-23.28** Mirror health is judged from the mirror's per-pass state sidecar, never from log
> recency: a pass the sidecar records as in flight is healthy until thirty minutes after its
> recorded start, and only then is the mirror `stalled`.
>
> Health is not reach: the desktop app lists a session folder into its sidebar only when it loads
> that folder (at launch, on an account or org switch, and at the first login after a logout), so a
> record the mirror copies into the loaded folder afterwards is missing from the running app's
> sidebar until its next load, and a flag the mirror writes there is overwritten by the app's next
> save of that record. The mirror therefore puts each new session whose transcript exists into every
> folder within about `mirror_hot_interval_s` (2 s, a `policy.json` cap under C-6.4) of the write
> while no full pass holds its worker (title, flag and setting changes spread with the full pass);
> it does not count an app's re-save of a value it never saw as a user's change; and it reports,
> from a journal of its own writes and the app's log, how many of its copies into the loaded folder
> postdate that load and still wait for a relaunch (`sessions mirror --status`, `sessions list`,
> `doctor`).

- **Ledger rows:** 159 (`keep`, ops-hygiene)
- **Milestone:** 6
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/mirror.py`
- **Rationale:** row 159's incident is "2026-08-19 07:08 false 'stalled' from the quiet log; an
  8.5-minute pass observed 2026-08-18" — the health signal was log writes, so a pass that was working
  quietly looked dead, and the cutoff has to exceed a real long pass. No clause mentions the mirror.
- **Amended 2026-09-24 (health is not reach):** a switch at 16:38 ET loaded a folder without 19
  sessions last active under other accounts; the mirror, whose passes were then taking up to two
  hours, copied them at 17:13-17:14, and the running app listed none until it relaunched at
  17:24:47. The app bundle (2.7032.0) reads a session folder only in `doInitialize`; nothing
  watches it. A healthy mirror can therefore still leave sessions out of the sidebar, so the
  clause now requires spreading new sessions within seconds, not mistaking an app's stale re-save
  for a user's flag change, and reporting the copies a relaunch would list.
  Evidence: `docs/reports/2026-09-24-mirror-load-gap.md`.

### P-23.29 — a keepalive pass is bounded

> **P-23.29** A keepalive pass runs at most `keepalive_workers` (4) lanes concurrently with a
> per-lane deadline of `keepalive_timeout_s` (60), both `policy.json` caps under C-6.4. A lane that
> exceeds its deadline is recorded as timed out and is not retried inside the same pass.

- **Ledger rows:** 177 (`keep`, ops-hygiene)
- **Milestone:** 5
- **Acceptance owner:** fake
- **v2 module:** `subfleet/keepalive.py`
- **Rationale:** row 177's rationale is "bounded concurrency for a launchd job" — an unbounded pass
  over fourteen lanes is fourteen simultaneous provider calls from a timer nobody is watching. C-16.4
  gives queued workers with deadlines, but it is scoped to the daemon's socket request handlers, not
  to a timer, so a C-16.4 test passes while a keepalive pass fans out without limit. v1 wrote its
  pass state under a flock; v2 drops the flock for store transactions (C-3.2), so only the two caps
  survive as numbers.

## session-continuity

Fourteen rows, seven clauses.

### P-23.30 — which registry row speaks for a session

> **P-23.30** When the session registry holds more than one row for a session, the row used is the
> one whose recorded pid is live, then the one whose socket is present, then the newest by start
> time.

- **Ledger rows:** 72 (`keep`, session-continuity)
- **Milestone:** 4
- **Acceptance owner:** unit
- **v2 module:** `subfleet/notices.py`
- **Rationale:** row 72's rationale is that "restarts leave stale `<pid>.json` rows" — after a
  restart the newest row is not always the live one, and delivering to the wrong row loses the
  notice silently. C-15.2 names the delivery layers in reliability order and C-15.3 the notice
  states; neither says which row a layer addresses, so a test of either passes while every notice
  goes to a dead pid.

### P-23.31 — a headless lane run is not a session

> **P-23.31** A headless lane run is never a `ping` or notice target, never appears in a session
> listing unless lanes are explicitly included, and is never revived or continued. A request naming
> one is refused with the reason.

- **Ledger rows:** 76 (`keep`, session-continuity), 187 (`keep`, session-continuity)
- **Milestone:** 4 for notices, 6 for the listing and revive
- **Acceptance owner:** unit
- **v2 module:** `subfleet/notices.py`, `subfleet/sessions/tickle.py`
- **Rationale:** both rows are the same category error with two dated incidents. Row 76: "2026-09-04:
  the Sol to Astra routing broadcast turned two finished lanes' outputs into 'Acknowledged'" — a
  fleet notice reached three running lanes and overwrote their deliverables. Row 187: "2026-09-04:
  the sweep revived five dead `claude -p` lanes, burning windows with no reader". C-15.4 states the
  prohibition for `wait` alone ("`wait` never wakes headless lanes and never targets a lane
  session"), so a C-15.4 test passes while `ping`, the notice push, the session listing, and revive
  all treat a lane as a session.

### P-23.32 — the importer never rewrites what it imported

> **P-23.32** An imported v1 record is never rewritten. A legacy Codex run whose thread id was not
> recorded resolves its resume identity in memory, from the saved `err.log` and the rollout name, at
> the moment a resume needs it.

- **Ledger rows:** 95 (`keep`, session-continuity)
- **Milestone:** 8 (cutover)
- **Acceptance owner:** unit
- **v2 module:** `subfleet/importer.py`
- **Rationale:** row 95's rationale is that "older entries lacked the fields" — backfilling them
  would edit history to match a schema that did not exist when the run happened, and a wrong backfill
  is indistinguishable from a real record. v2 records the thread id at launch (C-12.3), so only
  imported rows need the lazy path, and no clause covers the importer.

### P-23.33 — when a session may be nudged

> **P-23.33** A nudge is sent only for a `SessionStart` whose source is `startup` or `resume`, never
> `compact` or `clear`, and only when the interruption is younger than `nudge_max_age_s` (8 h). A
> session is nudged at most once per interruption point and no more often than `nudge_cooldown_s`
> allows; both are `policy.json` caps under C-6.4.

- **Ledger rows:** 179 (`keep`, session-continuity), 180 (`keep`, session-continuity), 181 (`keep`, session-continuity)
- **Milestone:** 6
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/tickle.py`
- **Rationale:** three eligibility rules for one action. Row 179's rationale is that "compaction is
  not a restart", row 180's that "an abandoned turn is not resumed because a tab reopened", and row
  181's is "restart storms". C-15.2 mentions `SessionStart` only as a layer that surfaces pending
  notices, never as a trigger that sends a session new work, so no test fails when every compaction
  wakes every session.

### P-23.34 — the worker decides against the transcript it can see

> **P-23.34** The hook records the wake and decides nothing: dedupe, cooldown, and eligibility are
> re-decided by the worker against the transcript as it reads after the nudge delay. A session whose
> last real turn changed during the delay is skipped, and a sweep started by hand requires a longer
> quiet window than a `SessionStart` wake. The transcript reader recognises the app's synthetic
> resume stub and judges the turn beneath it.

- **Ledger rows:** 182 (`keep`, session-continuity), 183 (`keep`, session-continuity), 184 (`keep`, session-continuity), 185 (`keep`, session-continuity)
- **Milestone:** 6, with the hook half in milestone 4's `subfleet/hooks.py`
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/tickle.py`
- **Rationale:** four rows about one race — the transcript at hook time is not the transcript at
  nudge time. Row 185's incident is dated: "2026-08-24: the stub lands about 0.7 s after the hook, so
  'already nudged' blocked a fresh restart". Row 184's is "observed four times in one session on
  2026-08-23". Row 182 skips when "the CLI's own `--resume` or a typed '.' already continued", and
  row 183 adds quiet for manual sweeps because "outside SessionStart an 'interrupted' tail can be a
  long tool call". Nothing in the contract describes the tickle path, so no test fails on any of the
  four.

### P-23.35 — which sessions revive admits

> **P-23.35** Revive admits only a session whose recorded permission mode is `bypassPermissions` and
> whose retirement flag is unset. Retirement is a durable session flag set by the operator; a retired
> session is absent from every session listing and is never a revive candidate until the operator
> clears it.

- **Ledger rows:** 188 (`keep`, session-continuity), 191 (`keep`, session-continuity)
- **Milestone:** 6
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/tickle.py`
- **Rationale:** two candidate filters on the same list. Row 188's rationale is that "a headless run
  would deny its own tools" — reviving a session that will refuse every tool call spends a window to
  produce nothing. Row 191's is "operator control over resurrection". No clause covers revive.

### P-23.36 — a handoff is bounded and points at its source

> **P-23.36** Every section of a handoff is bounded by an explicit per-section character cap. The
> handoff records the absolute path of the source transcript, which stays the durable record.

- **Ledger rows:** 210 (`keep`, session-continuity)
- **Milestone:** 6
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/handoff.py`
- **Rationale:** row 210's rationale is "unbounded context and lossy DB rewrites" — a handoff that
  tries to carry everything carries nothing usable, and one that replaces the transcript loses what
  it dropped. v1's caps are per section and explicit (`subfleet/handoff.py:29-44`). No clause covers
  handoffs.

## routing-policy

Three rows, three clauses.

### P-23.37 — stranded capacity is spent first

> **P-23.37** A lane under an unexpired closure scoped to a model above the one the job needs, with
> no `account` closure and a free slot, is model-stranded. The Claude comparator orders
> model-stranded lanes ahead of unstranded ones and then applies C-11.3's order within each group.

- **Ledger rows:** 108 (`keep`, routing-policy)
- **Milestone:** 3
- **Acceptance owner:** unit
- **v2 module:** `subfleet/policy.py`
- **Rationale:** row 108's rationale is Max's, 2026-08-26: "stranded capacity versus shared windows".
  A lane that can no longer serve Fable can still serve Sonnet, and its Fable window is already lost;
  spending an unstranded lane on Sonnet work strands a second window for nothing. C-11.3's Claude
  comparator has no stranded term, and because a stranded lane's worst window is by definition
  exhausted, headroom ordering ranks it last — the exact opposite of the rule — so C-11.3 does not
  merely omit the row, it contradicts it. See [Conflicts](#c-7).

### P-23.38 — which lane a reset credit is spent on

> **P-23.38** Reset-credit redemption orders candidate lanes by weekly reset furthest out first, then
> fewest in-flight attempts, then lowest lane number. This ordering governs redemption only; C-11.3
> continues to order routing candidates.

- **Ledger rows:** 162 (`keep`, routing-policy)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** row 162's rationale is "Max's rule, 2026-08-22". C-18.1 adopts "v1's rule set" for
  reset credits by reference and never states it, so nothing fails when the order is inverted. The
  order is deliberately the opposite of C-11.3's routing order, and in-flight count is a key here
  where C-11.3 forbids it as one — both because the question is different: routing asks which lane
  recovers soonest, redemption asks which lane a reset buys the most from. See [Conflicts](#c-8).

### P-23.39 — revive keeps the session's own tier

> **P-23.39** Revive launches on the tier recorded in the session's registry row. A different tier is
> used only when the operator passes `--model`, and the substitution is recorded.

- **Ledger rows:** 186 (`keep`, routing-policy)
- **Milestone:** 6
- **Acceptance owner:** unit
- **v2 module:** `subfleet/sessions/tickle.py`
- **Rationale:** row 186's rationale is Max's, 2026-08-26: "a fable-grade session on Opus is worse
  than a parked one" — a revive that silently changes model resumes someone else's work in the
  session's name. The tier comes from the session record, not from a `policy.json` chain, so C-11.2's
  chain walk does not reach it.

## provenance/attestation

Five rows, four clauses. Row 218 is an `ops-hygiene` row filed here because P-23.40 is the clause
that covers it.

### P-23.40 — finding the transcript attestation reads

> **P-23.40** The transcript for a session uuid is looked for at `<projects>/<session id>.jsonl` and
> `<projects>/*/<session id>.jsonl` and nowhere deeper; no search walks a workdir or a worktree.
> Attestation waits for a transcript that has not landed yet at most `transcript_wait_tries` (4)
> times with a one-second backoff, and then the verdict is `unattested`.

- **Ledger rows:** 19 (`keep`, provenance/attestation), 218 (`keep`, ops-hygiene)
- **Milestone:** 2
- **Acceptance owner:** unit
- **v2 module:** `subfleet/adapters/claude.py`
- **Rationale:** both rows bound the same search. Row 19's rationale is that "transcripts land
  slightly after the run", so some wait is necessary and an unbounded one hangs finalization; row
  218's is that the alternative is "an unbounded recursive walk of the worktree". C-12.5 says the
  transcript is "located by session uuid under `~/.claude/projects/`" and requires exactly one match,
  which a recursive walk also satisfies, and it says nothing about waiting — so a C-12.5 test passes
  on both violations. The depth bound is implemented on this branch
  (`subfleet/adapters/claude.py:1142-1163`, two globs, no recursion); the wait is not.

### P-23.41 — a nested submission mints its own identity

> **P-23.41** A submission made from inside a running attempt takes no identity from its environment:
> the daemon mints a fresh job id and, absent `--request-id`, a fresh request id. An inherited
> `SUBFLEET_JOB` is read only as the default `--parent`, and an inherited `SUBFLEET_ATTEMPT` — which
> C-5.1 and C-5.5 require the child to keep — is never an identity claim.

- **Ledger rows:** 32 (`replace`, provenance/attestation)
- **Milestone:** 1
- **Acceptance owner:** fake
- **v2 module:** `subfleet/daemon.py`
- **Rationale:** row 32's rationale is "id hijack by a child dispatch". v1 answered it by unsetting
  every `SUBFLEET_RUN_*` variable after recording the run; v2 cannot, because C-5.1 puts
  `SUBFLEET_ATTEMPT` in the child environment and C-5.5 enumerates containment by reading it back
  out of `ps -axEww`. The obligation therefore moves from scrubbing the environment to refusing to
  trust it, and no clause says the daemon distrusts an inherited id — C-6.2's request-id rule assumes
  the id came from the caller. C-7.3 is the secondary clause: a parent's cancel reaches children, so
  `--parent` has to keep working from the inherited value.

### P-23.42 — a notice says what the recipient may do

> **P-23.42** Every notice envelope declares the recipient session's own recorded permission class.
> `SUBFLEET_NOTIFY_MODE` in the recipient's environment overrides the recorded value, and the
> override is recorded on the notice.

- **Ledger rows:** 74 (`keep`, provenance/attestation)
- **Milestone:** 4
- **Acceptance owner:** unit
- **v2 module:** `subfleet/notices.py`
- **Rationale:** row 74's rationale is the "inbox attestation contract" — the recipient acts on what
  the envelope claims about it, so the claim has to be the recipient's own recorded class and not the
  sender's guess. C-15.1 enumerates the notice *text* (job id, class, rc, paths, summary,
  uncertainty) and C-15.3 the states; neither describes an envelope, so a test of either passes on an
  envelope that declares nothing.

### P-23.43 — an unattested round is not a verdict

> **P-23.43** A peer round counts only when its attestation is `attested` for the model the gate
> asked for. A round whose attestation is `mismatch` or `unattested`, or which carries a downgrade
> record, is discarded and re-run; it is never counted as agreement or as changes requested.

- **Ledger rows:** 197 (`keep`, provenance/attestation)
- **Milestone:** 7
- **Acceptance owner:** unit
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 197's rationale is "silent model downgrade during adjudication" — the whole
  value of a peer round is that a different, named model looked at it. C-12.5 produces the verdict
  and forbids a false positive, which is necessary but not sufficient: a gate that ignores an
  `unattested` verdict breaks no C-12.5 test. This is C-12.5's result being made load-bearing at the
  one place a wrong model is worth the most.

## identity

Eight rows, four clauses.

### P-23.44 — what an auth-dead lane costs

> **P-23.44** An `auth-dead` result disables the lane at once. A disabled lane is not a routing
> candidate, not a keepalive target, and not probed — no request is sent to it — until
> `subfleet lanes enroll` rebinds its credential, which records a new lane id under C-1.3; the CLI
> exits 5 and names that command. The lane's auth-dead detail is logged at most once a day.

- **Ledger rows:** 116 (`replace`, identity), 175 (`keep`, identity)
- **Milestone:** 2 for the disable, 5 for the keepalive and log cadence
- **Acceptance owner:** fake
- **v2 module:** `subfleet/credentials.py`
- **Rationale:** row 175's rationale is "repeated dead-token pings and alert spam"; row 116's ledger
  verdict is `keep-simplified` because v1's 30-day cooldown became severe once row 23 made rc 5
  probe-confirmed — the 2026-09-03 incident, "six live lanes parked 30 days on a phrase match". C-9.3
  sets the evidence bar for `auth-dead`, C-17.3 gives exit 5, and C-1.3 gives the new lane id on
  rebind, but no clause says the lane is disabled or that nothing may talk to it while it is, so a
  test of all three passes while a keepalive pings a dead token every five hours.

### P-23.45 — one account, one enabled lane

> **P-23.45** At most one lane is canonical for an account key. Enrolment and each probe cycle
> detect a second lane bound to an account key another lane already holds, mark the later binding
> non-canonical, and raise a critical alert naming both homes.

- **Ledger rows:** 136 (`keep`, identity)
- **Milestone:** 2 for detection, 5 for the alert
- **Acceptance owner:** unit
- **v2 module:** `subfleet/credentials.py`
- **Rationale:** row 136's incident is "2026-07-11: revoked refresh token from same-account-in-two-homes"
  — two homes refreshing one account's token race, and the loser's rotation revokes the winner's. That
  is the most expensive failure in the ledger, because it costs an operator login. C-1.4 defines the
  account key and C-10.1 makes a lane an immutable binding, but neither forbids two lanes holding the
  same key, so a test of either passes while the fleet is set up to revoke itself.

### P-23.46 — what shadowing changes and what it does not

> **P-23.46** A lane whose account is a provider desktop app's current login is recorded as shadowed;
> that fact alone never changes its dispatch order. Reset-credit redemption excludes a shadowed lane
> while any unshadowed lane holds a concrete `available` credit, and redeems on a shadowed lane only
> when none does.

- **Ledger rows:** 138 (`keep`, identity), 163 (`keep`, identity)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/actions.py`
- **Rationale:** two halves of one rule — shadowing is metadata for dispatch and a preference for
  redemption. Row 138 was "observed 2026-08-04, 08-13, 08-17"; row 163's rationale is that "shadowed
  lanes can be revoked by the app", so a credit spent there can evaporate. C-10.3's `desktop` flag is
  the adjacent clause but not the same thing: it is the Claude desktop login and it bars the lane from
  candidacy outright, where this row's Codex app shadow must *not* change dispatch. A C-10.3 test
  therefore passes while redemption picks the lane most likely to lose the credit. See
  [Conflicts](#c-9).

### P-23.47 — the auth store belongs to the provider CLI

> **P-23.47** subfleet never writes a provider auth store and never refreshes a token in process. A
> lane whose Codex token looks merely expired gets exactly one automatic heal — a minimal
> `codex exec` turn under that lane's home, so the CLI refreshes and persists its own `auth.json` —
> followed by a re-probe. A `refresh token was revoked` result latches the lane: it is probed no
> further until the credential epoch of C-10.1, the home's `auth.json` `last_refresh`, changes.

- **Ledger rows:** 139 (`keep`, identity), 150 (`keep`, identity), 151 (`keep`, identity)
- **Milestone:** 2 for the prohibition, 5 for the heal and the latch
- **Acceptance owner:** fake
- **v2 module:** `subfleet/credentials.py`, `subfleet/alerts.py`
- **Rationale:** one rule and its two consequences. Row 139's rationale is that "an unpersisted
  refresh rotation is the revocation trap" — refreshing in process rotates the token upstream while
  the file still holds the old one, which is how row 136's incident ends. Row 150 is the sanctioned
  way out, "added 2026-08-12 after `~/.codex-2` sat auth-suspect for 14 h": let the CLI do the
  refresh. Row 151 stops the loop when it is genuinely over. C-2.1 covers only part of the first
  sentence — a lane home under `$SUBFLEET_HOME/lanes/<lane id>/` (C-2.2) is inside the state root, so
  writing `auth.json` there breaks no C-2.1 test — and C-9.3 sets the `auth-dead` bar without saying
  what may be attempted before it.

## UX-contract

Eight rows, six clauses.

### P-23.48 — what the session hook counts as a launch

> **P-23.48** The session hook blocks a provider CLI or a v1 runner only in command position; a
> mention inside a quoted string, a heredoc, or an inspection command such as `bash -n` is not a
> launch. A runner's own detach flag passes, and `SUBFLEET_ATTACHED_OK=1` in the tool call's
> environment is the explicit one-off override.

- **Ledger rows:** 68 (`keep`, UX-contract), 69 (`keep`, UX-contract)
- **Milestone:** 4
- **Acceptance owner:** unit
- **v2 module:** `subfleet/hooks.py`
- **Rationale:** both rows bound the block of P-23.54 so it stays usable. Row 68's rationale is
  "false blocks on inspection commands" — a hook that cannot be reasoned about gets disabled, which
  costs more than it saves. Row 69's is that "detached launches already survive", so the thing the
  block exists to prevent is not present. C-17.6 makes `run` detached by default inside a Claude
  session, which is the replacement the hook points at, but no clause states the hook's matching
  rules, so a test of C-17.6 passes on a hook that blocks `cat bin/subfleet-claude`.

### P-23.49 — one envelope per notice

> **P-23.49** A notice is delivered as exactly one envelope. Any sequence in the body that would
> close the envelope early is neutralised before the envelope is written.

- **Ledger rows:** 75 (`keep`, UX-contract)
- **Milestone:** 4
- **Acceptance owner:** unit
- **v2 module:** `subfleet/notices.py`
- **Rationale:** row 75's rationale is that "recipient parses only single-envelope messages" — a
  deliverable that happens to contain the closing tag splits the notice and the recipient acts on
  half of it. C-15.1 fixes the notice's *content* and C-16.1 the socket framing between CLI and
  daemon, which is a different wire; nothing describes the envelope a session receives, so a test of
  either passes on a notice that terminates inside its own summary line.

### P-23.50 — the push waits behind a live waiter

> **P-23.50** The best-effort socket push is skipped while the job has a `wait` or `--attach` waiter
> whose recorded identity is still live, and is attempted once that waiter is no longer live. The
> push is never the reason a caller learns about a job twice.

- **Ledger rows:** 79 (`keep`, UX-contract)
- **Milestone:** 4
- **Acceptance owner:** fake
- **v2 module:** `subfleet/notices.py`
- **Rationale:** row 79's rationale is "duplicate reports". C-15.2 orders `wait` above the
  best-effort push in reliability but does not say the higher layer suppresses the lower one, and
  C-15.3 makes a repeated offer explicitly harmless ("a notice may be offered more than once"), so a
  test of either passes while every attached run reports itself twice. The fall-through matters as
  much as the skip: a waiter that died must not swallow the notice.

### P-23.51 — a running job never falls off the list

> **P-23.51** `runs --last N` bounds terminal jobs only. Every job in a live state is listed whatever
> N is and however old it is, and `--json` output carries the same set.

- **Ledger rows:** 89 (`keep`, UX-contract)
- **Milestone:** 1
- **Acceptance owner:** unit
- **v2 module:** `subfleet/cli.py`
- **Rationale:** row 89's incident is the most recent in the ledger: "2026-09-05: a 7 h lane fell
  below the newest-20 window and a poller lost it; commit c465456". A long job is exactly the job a
  poller is waiting on, and it is exactly the one a newest-N window drops. C-17.1 names `--last N`
  without stating its semantics, so a C-17.1 test passes on the failure. Code on this branch does not
  implement it: `subfleet/offline.py:207-224` applies the `LIMIT` to live and terminal rows alike.

### P-23.52 — what an alert says and when a recovery is one

> **P-23.52** subfleet never performs a provider login; every alert about a credential names the
> exact command the operator must run. A recovery notice is emitted only when a condition has cleared
> and no other condition on the same home is active; a move from one condition to another is reported
> as the new condition, not as a recovery.

- **Ledger rows:** 149 (`keep`, UX-contract), 156 (`keep`, UX-contract)
- **Milestone:** 5
- **Acceptance owner:** unit
- **v2 module:** `subfleet/alerts.py`
- **Rationale:** row 149's incident is the "2026-07-11 postmortem" and its rule is that logins are
  operator-only; row 156's rationale is that "revoked to no-auth is a state change, not a recovery" —
  an alert stream that says "recovered" while the home is still broken trains the operator to ignore
  it. C-17.3's exit 7 "names the rule and the fix" is the analogous rule for the CLI and does not
  reach alert bodies. The ledger notes v1's own prefix list omitted some conditions, so v2 defines
  recovery per condition rather than by prefix.

### P-23.53 — a gate stops

> **P-23.53** A gate stops after `gate_max_rounds` (4) peer rounds without agreement and reports the
> blocker, naming the last verdict. The cap is a `policy.json` cap under C-6.4.

- **Ledger rows:** 200 (`keep`, UX-contract)
- **Milestone:** 7
- **Acceptance owner:** unit
- **v2 module:** `subfleet/gate.py`
- **Rationale:** row 200's rationale is "bounded loops" — a gate that can iterate forever spends
  provider windows arguing. Four is v1's `DEFAULT_MAX_ROUNDS`. No clause covers gates beyond
  C-19.1's action row.

## process-survival

Three rows, two clauses.

### P-23.54 — one dispatch path

> **P-23.54** Every provider launch is a `subfleet run` submission, including subfleet's own —
> a handoff is dispatched detached through the ordinary submit path so it inherits routing, the
> guard, salvage, the ledger, and notices. The session hook blocks a provider CLI or a v1 runner
> invoked directly from a session's Bash tool and names `subfleet run` as the replacement.

- **Ledger rows:** 67 (`keep`, process-survival), 211 (`keep`, process-survival)
- **Milestone:** 4 for the hook, 6 for handoff
- **Acceptance owner:** fake
- **v2 module:** `subfleet/hooks.py`, `subfleet/sessions/handoff.py`
- **Rationale:** one rule, enforced outward and inward. Row 67's incident is "2026-08-23, observed
  three times" — a provider launched from a session's Bash tool dies with the session, which is the
  failure C-5.1's guardian exists to prevent and which no amount of guardian helps if the launch
  never reaches it. Row 211's rationale is "one dispatch path". C-5.1 owns what happens once a launch
  is the daemon's; nothing says every launch must become the daemon's, so a test of C-5.1 passes
  while a session or a subfleet subsystem shells out directly. P-23.48 bounds what the hook counts.

### P-23.55 — one live revive per session

> **P-23.55** A session has at most one live revive: the revive lease `session:<session id>:revive`
> is taken in the transaction that admits the attempt, and a session that already holds it is skipped
> rather than launched again. The census the sweep skips on is the lease rows, read inside the
> admitting transaction, not a snapshot taken at the start of the pass.

- **Ledger rows:** 192 (`keep`, process-survival)
- **Milestone:** 6
- **Acceptance owner:** fake
- **v2 module:** `subfleet/sessions/tickle.py`
- **Rationale:** row 192's incident is "2026-09-04: a headless revive twin ran alongside a live
  session and re-dispatched its lanes" — the twin did not merely waste a window, it re-issued the
  original session's work. C-6.5 refuses "a writable job for a session id that already has one
  running from another instance", which is the same shape but scoped to writable jobs; a revive need
  not be writable, so a C-6.5 test passes on the incident. v2's lease rows (C-6.3) replace v1's pass
  lock, and taking the lease inside the admitting transaction is what makes the stale-census race
  impossible rather than unlikely.

## Conflicts

Nine places where a proposal changes something the contract or `plan.md` already says, and two
where the contract and the plan already disagree with each other in a way these proposals depend
on. Each gives both texts and a recommendation. None of them is resolved here: section 23 is a
proposal until the integrator folds it in, and every one of these is the integrator's call.

> **Dispositions (integrator, 2026-09-05 13:55 EDT):** all eleven resolved as recommended and applied to the contract in the same commit that folded section 23 in; see "Changes in version 2" at the top of `docs/acceptance-contract.md`.

<a id="c-1"></a>
### C-1 — `-I` and `-D` against C-17.2's flag list (P-23.2, P-23.3)

- **C-17.2** enumerates `run`'s flags — "`--task`, `--tier`, `-m`, `-a EMAIL`, `-H CODEX_HOME`,
  `-C DIR`, `-p PROMPTFILE`, `-o OUT`, `-n NAME`, `-s SANDBOX`, `-x EMAIL` (repeatable),
  `--allow-desktop`, `--allow-tmp`, `--in-place`, `--independent`, `--parent JOB`, `--request-id ID`,
  `--wait`/`--attach`, `-d`/`--detach`, `--json`, `--dry-run`, `--why`, `--no-preamble`" — plus three
  deprecated flags. There is no `-I` and no `-D`.
- **P-23.2 and P-23.3** describe a mode reached by `-I` with `-D`, because ledger rows 29, 30, 45,
  46, and 47 all govern it and all five are `keep`.
- **Recommendation:** add `-I` and `-D <review root>` to C-17.2 in the same commit that folds in
  P-23.2 and P-23.3. The alternative — dropping the mode — is a sixth verdict change against the
  ledger and belongs in `docs/invariants.md`'s Dropped section with a reason, not in a flag list by
  omission. Note that the five rows are the only place isolated review is specified at all: if the
  mode goes, the gate lane loses the mechanism P-23.10 relies on for running a peer read-only outside
  the repository.

<a id="c-2"></a>
### C-2 — P-23.3 adds to C-6.5's refusal list

- **C-6.5** reads "Refusals are exit 7 with the fix named:" and then enumerates six. It is written as
  a closed list.
- **P-23.3** adds two: an inherited managed-settings variable, and a Codex home with managed
  requirements or a nonempty system or project config layer.
- **Recommendation:** fold P-23.3's refusals into C-6.5 rather than keeping them as a separate
  clause, so the exit-7 list stays one list. The same is true of any later refusal: C-6.5 is more
  useful as the index of refusals than as a sample of them.

<a id="c-3"></a>
### C-3 — P-23.5 widens C-14.2's preflight scope

- **C-14.2** — "Before the first executable Codex job, `doctor` and the daemon run the trust
  preflight". One preflight, once.
- **P-23.5** — a verdict is good only for the version, home, override, and seeded-config fingerprint
  it was taken under, so a re-picked lane with a different home is verified again.
- **Recommendation:** replace C-14.2's "the first executable Codex job" with "the first executable
  Codex job on each lane home", and let P-23.5 state the key. Ledger row 41 is a `keep` and its
  failure mode — launching unguarded on a rotated lane — is the one C-14.2 exists to prevent, so the
  narrower reading is very likely an accident of drafting rather than a decision.

<a id="c-4"></a>
### C-4 — where the preflight's scratch home lives (P-23.23)

- **C-2.1** — "Nothing is written outside [`$SUBFLEET_HOME`] except: job workdirs and worktrees the
  caller named or the daemon allocated under `$HOME`, salvage refs inside the job's git repository,
  and the `-o` export path."
- **Code on this branch** — `subfleet/guard/preflight.py:210` creates the scratch home with
  `tempfile.TemporaryDirectory(prefix="subfleet-guard-preflight-")`, which resolves under `TMPDIR`,
  outside the state root and not one of C-2.1's three exceptions.
- **P-23.23** puts the scratch home under `$SUBFLEET_HOME`.
- **Recommendation:** move the scratch home under `$SUBFLEET_HOME` rather than add a fourth exception
  to C-2.1. C-2.4 already refuses a `/tmp` workdir at submission, so a guard component probing from
  `/tmp` is the one part of the system exempt from the rule the rest of it enforces. This is a
  one-line change in `preflight.py` and belongs to whichever lane owns that file, not to this one.

<a id="c-5"></a>
### C-5 — a guard check verb C-17.1 does not have (P-23.24)

- **C-17.1** enumerates the verbs and their aliases. There is no guard verb.
- **P-23.24** requires a guard check that never writes the denial log, from ledger row 56.
- **Recommendation:** spell it `doctor --guard-check <payload>` rather than a new top-level verb.
  `doctor` already owns the guard preflight under C-14.2, the check is diagnostic, and amendment 1
  makes v1 *verb* spellings permanent — `subfleet-guard check` was a separate binary in v1, not a
  `subfleet` verb, so nothing is owed the old spelling.

<a id="c-6"></a>
### C-6 — the hook installer writes outside the state root (P-23.25)

- **C-2.1** names three exceptions and `~/.claude/settings.json` is none of them.
- **P-23.25** governs how that file is edited, and C-14.3 already assumes the global Claude hook
  exists in it ("a Claude launch relies on the global hook and `doctor` reports if it is missing from
  `~/.claude/settings.json`").
- **Recommendation:** add a fourth exception to C-2.1 for `~/.claude/settings.json`, written only by
  an explicit `daemon install` or hook install, never by a job or a timer. C-14.3 already reads the
  file, so the contract has half-adopted it; leaving the write unstated means the milestone-4
  installer will land in violation of C-2.1 and the violation will be discovered by a test rather
  than by a decision.

<a id="c-7"></a>
### C-7 — stranded lanes against C-11.3's Claude comparator (P-23.37)

- **C-11.3** — "Claude comparator: eligible by the same floor on the worst window; ordered by
  worst-window headroom descending, then in-flight ascending, then lane id."
- **P-23.37** — model-stranded lanes sort ahead of unstranded ones, and C-11.3's order applies within
  each group.
- **This is a contradiction, not an omission.** A model-stranded lane's worst window is by definition
  exhausted, so "worst-window headroom descending" ranks it last — exactly inverted from ledger row
  108, which is a `keep` and is Max's rule of 2026-08-26.
- **Recommendation:** amend C-11.3's Claude comparator to sort on the stranded term first.
  `plan.md` amendment 14 already records that v1's own sort "puts `not fable_stranded` before
  `active`", so the plan of record knows about the term and the contract lost it in condensation. The
  eligibility floor is unaffected: a stranded lane is eligible only for models it can still serve,
  which the closure scope already decides.

<a id="c-8"></a>
### C-8 — redemption order runs opposite to routing order (P-23.38)

- **C-11.3** — Codex lanes are "ordered by `seven_day` reset ascending (soonest first), then lane
  id. In-flight counts never reorder Codex lanes."
- **P-23.38** — redemption candidates are ordered by weekly reset *furthest out* first, then *fewest
  in-flight*, then lane number.
- **Not a contradiction once the scope is stated**, which is why P-23.38 says so in its own text: the
  two orders answer different questions. Routing asks which lane recovers soonest; redemption asks
  which lane a reset buys the most window on.
- **Recommendation:** keep both, and keep P-23.38's second sentence when folding it in. Without that
  sentence the two clauses read as a defect, and someone will "fix" one of them.

<a id="c-9"></a>
### C-9 — two kinds of shadowing (P-23.46)

- **C-10.3** — "A lane whose account is the desktop app's current login (`~/.claude.json`
  `oauthAccount`, re-read each probe cycle) is `desktop` and is never a candidate unless the job has
  `allow_desktop`."
- **P-23.46** — a shadowed lane's dispatch order is unchanged by the shadow, and only redemption
  prefers away from it.
- These are different facts about different providers wearing one word. C-10.3's `desktop` is the
  Claude desktop login and it bars candidacy; ledger rows 138 and 163 are the Codex app shadow and it
  must not.
- **Recommendation:** keep two names. Reserve `desktop` for C-10.3's Claude login and call the Codex
  one `app_shadowed`, or the first reader will conclude one of the two clauses is wrong. The
  disposition already flags the collision on row 138.

<a id="c-10"></a>
### C-10 — C-17.1's verb list against plan amendment 1 (P-23.31, P-23.35, P-23.53, P-23.54)

This one is not caused by a proposal; it is a disagreement the proposals run into.

- **Amendment 1** — "Every v1 verb spelling that appears in the README or in any agent's CLAUDE.md is
  permanent, not transitional: `run`, `runs [...]`, `runs show <id> [...]`, `runs reap`, `wait`,
  `kill`, `status`, `capacity`, `resume-codex`, `handoff`, `gate`, `notify`, `enroll`."
- **C-17.1** lists `subfleet`, `status`, `run`, `runs`, `runs show`, `runs reap`, `wait`, `kill`,
  `resume`, `lanes [...]`, `why`, `daemon`, `doctor`, `ping`, with `jobs`, `show`, `capacity`,
  `notify`, and `resume-codex` as aliases. **`handoff` and `gate` are absent**, and `enroll` survives
  only as `lanes enroll`. Ledger row 76 also cites a `subfleet sessions` listing, which appears in
  neither list.
- The contract's own preamble says "Where this file and `plan.md` disagree, this file wins", so as
  written C-17.1 deletes two verbs amendment 1 calls permanent.
- **Recommendation:** add `handoff`, `gate`, and `sessions` to C-17.1 when section 23 lands, since
  P-23.31, P-23.35, P-23.36, P-23.53, and P-23.54 all describe behaviour reached through them. If the
  integrator instead means to route them through subcommands, amendment 1 needs correcting in the
  same commit — the plan says the sentence "keeps working unchanged" is "literally true", and it is
  not.

<a id="c-11"></a>
### C-11 — a disabled lane against C-18.1's probe cycle (P-23.44, P-23.47)

- **C-18.1** — "Probe cycle every `probe_interval_s` (300) per lane, one probe per idle lane per
  window".
- **P-23.44** and **P-23.47** stop probing a lane entirely: an `auth-dead` lane until re-enrolment, a
  revoked-token lane until its credential epoch changes.
- A disabled lane is arguably not an "idle lane", so this may be a reading rather than a conflict.
- **Recommendation:** when C-18.1 is expanded for milestone 5, say "one probe per idle, enabled,
  unlatched lane per window" explicitly. Ledger row 175 exists because v1 pinged dead tokens on a
  timer for weeks, so the exclusion is worth stating rather than inferring.

## Index: ledger row to proposed clause

Every row that read `GAP` in version 1 of `invariants.md`, in ledger order, with the clause
that now covers it. Fifty-five clauses over ninety-two rows.

| row | invariant | class | clause | milestone |
|---|---|---|---|---|
| 8 | Delete only an owned prompt whose basename is `delegate-prompt-*.md` and which is the exa… | safety-guard | [P-23.1](#p-231--a-callers-files-are-read-only-to-subfleet) | 1 |
| 19 | Bound retries for delayed transcript persistence to a small default (4 tries, 1 s backoff… | provenance/attestation | [P-23.40](#p-2340--finding-the-transcript-attestation-reads) | 2 |
| 26 | Resolve the Claude binary through an explicit override, then `~/.local/bin/claude`, then… | ops-hygiene | [P-23.21](#p-2321--how-a-provider-binary-is-found) | 2 |
| 29 | Require `-s read-only` and `-D` for `-I`, expose sources through `--add-dir`, and allow o… | safety-guard | [P-23.2](#p-232--an-isolated-review-inherits-no-context-and-holds-no-hosted-capability) | 1 for the CLI and Codex halves, 2 for the Claude environment |
| 30 | Refuse an isolated review that inherits `CLAUDE_CODE_MANAGED_SETTINGS_PATH` or the remote… | safety-guard | [P-23.3](#p-233--an-isolated-review-is-refused-when-operator-policy-could-override-it) | 1 |
| 31 | In read-only mode, unset the memory and CLAUDE.md inheritance variables before Claude ini… | safety-guard | [P-23.2](#p-232--an-isolated-review-inherits-no-context-and-holds-no-hosted-capability) | 1 for the CLI and Codex halves, 2 for the Claude environment |
| 32 | Unset every `SUBFLEET_RUN_*` identity variable after recording the run so nested dispatch… | provenance/attestation | [P-23.41](#p-2341--a-nested-submission-mints-its-own-identity) | 1 |
| 35 | Call the accounting hook synchronously and let no hook failure change the run outcome. | ops-hygiene | [P-23.22](#p-2322--accounting-never-changes-an-outcome) | 1 for the finalization ordering, 2 for the parser |
| 41 | Re-run the guard preflight on a re-picked lane and stop rather than launch unguarded. | safety-guard | [P-23.5](#p-235--how-long-a-guard-preflight-verdict-is-good-for) | 1 |
| 45 | Disable apps, plugins, hooks, multi-agent, browser, memories, shell snapshots, history pe… | safety-guard | [P-23.2](#p-232--an-isolated-review-inherits-no-context-and-holds-no-hosted-capability) | 1 for the CLI and Codex halves, 2 for the Claude environment |
| 46 | Refuse an isolated Codex review when managed requirements or nonempty system or project c… | safety-guard | [P-23.3](#p-233--an-isolated-review-is-refused-when-operator-policy-could-override-it) | 1 |
| 47 | Re-prepare isolation before every attempt, since lane rotation changes the config and the… | safety-guard | [P-23.4](#p-234--isolation-is-prepared-per-attempt) | 1 |
| 50 | Never write the lane's `CODEX_HOME`: probe a scratch home seeded with copies of `config.t… | ops-hygiene | [P-23.23](#p-2323--how-the-guard-preflight-probes) | 1 |
| 52 | Key the preflight cache on codex version, home, override, and seeded-config fingerprint,… | safety-guard | [P-23.5](#p-235--how-long-a-guard-preflight-verdict-is-good-for) | 1 |
| 53 | Bound the preflight: fail immediately if the app-server dies unanswered, and TERM then KI… | ops-hygiene | [P-23.23](#p-2323--how-the-guard-preflight-probes) | 1 |
| 56 | Make the guard's `check` verb a dry run that never writes the real denial log. | ops-hygiene | [P-23.24](#p-2324--a-guard-check-writes-nothing) | 8 (guard parity) |
| 65 | Document the `write_stdin` PTY guard bypass and provide `SUBFLEET_CODEX_UNIFIED_EXEC=off`… | safety-guard | [P-23.6](#p-236--the-write_stdin-hole-is-closable) | 1 |
| 67 | Block provider runners launched straight from a session's Bash tool and name the `subflee… | process-survival | [P-23.54](#p-2354--one-dispatch-path) | 4 for the hook, 6 for handoff |
| 68 | Count only a runner in command position; `bash -n bin/subfleet-claude` or a heredoc menti… | UX-contract | [P-23.48](#p-2348--what-the-session-hook-counts-as-a-launch) | 4 |
| 69 | Let the runners' own `-d` pass, and accept `SUBFLEET_ATTACHED_OK=1` as the explicit one-o… | UX-contract | [P-23.48](#p-2348--what-the-session-hook-counts-as-a-launch) | 4 |
| 70 | Make hook install idempotent, preserve other settings, and write a timestamped backup bef… | ops-hygiene | [P-23.25](#p-2325--installing-a-hook-into-a-shared-settings-file) | 4 |
| 72 | Rank duplicate session-registry rows by live pid, then present socket, then newest start. | session-continuity | [P-23.30](#p-2330--which-registry-row-speaks-for-a-session) | 4 |
| 74 | Declare the recipient's own permission class on the notice envelope, with `SUBFLEET_NOTIF… | provenance/attestation | [P-23.42](#p-2342--a-notice-says-what-the-recipient-may-do) | 4 |
| 75 | Emit exactly one envelope per notice and neutralize a closing tag appearing inside the body. | UX-contract | [P-23.49](#p-2349--one-envelope-per-notice) | 4 |
| 76 | Refuse a lane session as a notify target and hide lane sessions from `subfleet sessions`… | session-continuity | [P-23.31](#p-2331--a-headless-lane-run-is-not-a-session) | 4 for notices, 6 for the listing and revive |
| 79 | Skip the push while an inline `--attach` waiter is still alive, and fall through if that… | UX-contract | [P-23.50](#p-2350--the-push-waits-behind-a-live-waiter) | 4 |
| 80 | Prune surfaced notices older than 14 days. | ops-hygiene | [P-23.26](#p-2326--secondary-records-are-pruned-by-age-never-by-a-callers-window) | 2 for the scan cache, 4 for notices |
| 89 | Never let an in-flight run age out of `subfleet runs`; `last` bounds only finished rows. | UX-contract | [P-23.51](#p-2351--a-running-job-never-falls-off-the-list) | 1 |
| 95 | Resolve historical Codex resume identity lazily from the saved `err.log` and rollout name… | session-continuity | [P-23.32](#p-2332--the-importer-never-rewrites-what-it-imported) | 8 (cutover) |
| 108 | Spend Fable-exhausted lanes first for non-Fable work. | routing-policy | [P-23.37](#p-2337--stranded-capacity-is-spent-first) | 3 |
| 116 | On an auth-dead result, cool the account and print the exact re-enrolment ritual. | identity | [P-23.44](#p-2344--what-an-auth-dead-lane-costs) | 2 for the disable, 5 for the keepalive and log cadence |
| 128 | Count each `message.id` once when summing transcript usage. | capacity-truth | [P-23.15](#p-2315--transcript-usage-is-summed-once-per-message) | 2 |
| 129 | Never let usage accounting fail a run; record a parse failure as an error record instead. | ops-hygiene | [P-23.22](#p-2322--accounting-never-changes-an-outcome) | 1 for the finalization ordering, 2 for the parser |
| 136 | Detect the same account bound in two homes, mark the non-canonical duplicate, and alert c… | identity | [P-23.45](#p-2345--one-account-one-enabled-lane) | 2 for detection, 5 for the alert |
| 138 | Treat app shadowing as metadata that does not change dispatch order but excludes a lane f… | identity | [P-23.46](#p-2346--what-shadowing-changes-and-what-it-does-not) | 5 |
| 139 | Never write any auth store and never refresh a token in-process. | identity | [P-23.47](#p-2347--the-auth-store-belongs-to-the-provider-cli) | 2 for the prohibition, 5 for the heal and the latch |
| 141 | Prune a Codex scan-cache entry only when its file is gone or is a week stale, never becau… | ops-hygiene | [P-23.26](#p-2326--secondary-records-are-pruned-by-age-never-by-a-callers-window) | 2 for the scan cache, 4 for notices |
| 149 | Never auto-login, and name the exact heal command in every alert. | UX-contract | [P-23.52](#p-2352--what-an-alert-says-and-when-a-recovery-is-one) | 5 |
| 150 | Allow exactly one automatic heal, a tiny `codex exec` turn that lets the CLI refresh and… | identity | [P-23.47](#p-2347--the-auth-store-belongs-to-the-provider-cli) | 2 for the prohibition, 5 for the heal and the latch |
| 151 | Latch `refresh token was revoked` until `auth.json` changes, and probe no further. | identity | [P-23.47](#p-2347--the-auth-store-belongs-to-the-provider-cli) | 2 for the prohibition, 5 for the heal and the latch |
| 152 | Attempt at most one refresh probe per home per cycle, spaced twenty minutes apart. | ops-hygiene | [P-23.27](#p-2327--one-monitoring-cycle-one-verdict) | 5 |
| 153 | Heal before persisting, so the snapshot, brief, history, and conditions all see post-heal… | ops-hygiene | [P-23.27](#p-2327--one-monitoring-cycle-one-verdict) | 5 |
| 154 | Treat a cycle in which every Codex probe is a network error as offline and stay silent. | ops-hygiene | [P-23.27](#p-2327--one-monitoring-cycle-one-verdict) | 5 |
| 156 | Send a recovery notice only when no other condition for the same home is active. | UX-contract | [P-23.52](#p-2352--what-an-alert-says-and-when-a-recovery-is-one) | 5 |
| 158 | Resolve the codex binary explicitly so a stripped launchd PATH cannot break the call. | ops-hygiene | [P-23.21](#p-2321--how-a-provider-binary-is-found) | 2 |
| 159 | Judge mirror health from its per-pass state sidecar, tolerate a long in-flight pass, and… | ops-hygiene | [P-23.28](#p-2328--mirror-health-is-a-state-file-not-a-quiet-log) | 6 |
| 161 | Redeem a reset credit only when the server confirms `limit_reached` and a concrete `avail… | capacity-truth | [P-23.16](#p-2316--when-a-reset-credit-may-be-spent) | 5 |
| 162 | Order redemption candidates by furthest-out weekly reset first, then lowest in-flight, th… | routing-policy | [P-23.38](#p-2338--which-lane-a-reset-credit-is-spent-on) | 5 |
| 163 | Prefer an unshadowed lane for redemption and use a shadowed one only when no unshadowed c… | identity | [P-23.46](#p-2346--what-shadowing-changes-and-what-it-does-not) | 5 |
| 164 | Send a fresh UUID4 `redeem_request_id` with every consume. | capacity-truth | [P-23.16](#p-2316--when-a-reset-credit-may-be-spent) | 5 |
| 165 | Accept a consume as successful only for code `reset` with `windows_reset` greater than zero. | capacity-truth | [P-23.16](#p-2316--when-a-reset-credit-may-be-spent) | 5 |
| 167 | Treat a confirmed consume as authoritative while the usage endpoint is stale: set the res… | capacity-truth | [P-23.17](#p-2317--a-confirmed-consume-is-the-authority) | 5 |
| 168 | Report fleet credits remaining as null whenever any lane's count is unreadable, never as… | capacity-truth | [P-23.18](#p-2318--an-unreadable-count-is-not-a-small-count) | 5 |
| 169 | Clear the lane's dispatch cooldown after a successful redemption. | capacity-truth | [P-23.17](#p-2317--a-confirmed-consume-is-the-authority) | 5 |
| 170 | List and consume only gifted entitlements, and provide no purchase or add-credit path. | safety-guard | [P-23.7](#p-237--reset-credits-are-gifts-not-purchases) | 5 |
| 172 | Stamp the five-hour window at the moment the provider request is sent, not when the keych… | capacity-truth | [P-23.19](#p-2319--when-a-five-hour-window-is-open) | 5 |
| 173 | Skip a lane that made any request within the last five hours, recording it as `skipped-op… | capacity-truth | [P-23.19](#p-2319--when-a-five-hour-window-is-open) | 5 |
| 174 | Count a running lane entry as a recent request, and never count a session-less rc 5 as one. | capacity-truth | [P-23.19](#p-2319--when-a-five-hour-window-is-open) | 5 |
| 175 | Mark a 401/403 lane auth-dead, skip it without a request, log the detail at most daily, a… | identity | [P-23.44](#p-2344--what-an-auth-dead-lane-costs) | 2 for the disable, 5 for the keepalive and log cadence |
| 177 | Run keepalives with at most four workers and a 60 s per-lane timeout, writing state under… | ops-hygiene | [P-23.29](#p-2329--a-keepalive-pass-is-bounded) | 5 |
| 179 | Nudge only from SessionStart sources `startup` and `resume`, never `compact` or `clear`. | session-continuity | [P-23.33](#p-2333--when-a-session-may-be-nudged) | 6 |
| 180 | Cap the age of an interruption eligible for a nudge at eight hours by default. | session-continuity | [P-23.33](#p-2333--when-a-session-may-be-nudged) | 6 |
| 181 | Nudge once per interruption point and enforce a per-session cooldown. | session-continuity | [P-23.33](#p-2333--when-a-session-may-be-nudged) | 6 |
| 182 | Re-check the transcript after the nudge delay and skip when the real last turn has changed. | session-continuity | [P-23.34](#p-2334--the-worker-decides-against-the-transcript-it-can-see) | 6, with the hook half in milestone 4's `subfleet/hooks.py` |
| 183 | Require additional transcript quiet before a manual sweep nudges a session. | session-continuity | [P-23.34](#p-2334--the-worker-decides-against-the-transcript-it-can-see) | 6, with the hook half in milestone 4's `subfleet/hooks.py` |
| 184 | Recognize the app's synthetic resume stub and judge the turn underneath it. | session-continuity | [P-23.34](#p-2334--the-worker-decides-against-the-transcript-it-can-see) | 6, with the hook half in milestone 4's `subfleet/hooks.py` |
| 185 | Defer the hook's dedupe and cooldown verdicts to the worker, which re-decides against the… | session-continuity | [P-23.34](#p-2334--the-worker-decides-against-the-transcript-it-can-see) | 6, with the hook half in milestone 4's `subfleet/hooks.py` |
| 186 | Make cross-tier revive an explicit choice: without `--model`, revive on the session's own… | routing-policy | [P-23.39](#p-2339--revive-keeps-the-sessions-own-tier) | 6 |
| 187 | Never revive a headless lane run as a continuation. | session-continuity | [P-23.31](#p-2331--a-headless-lane-run-is-not-a-session) | 4 for notices, 6 for the listing and revive |
| 188 | Revive only sessions whose recorded permission mode is `bypassPermissions`. | session-continuity | [P-23.35](#p-2335--which-sessions-revive-admits) | 6 |
| 189 | Probe a lane live before reviving rather than trusting the lane ledger's estimates. | capacity-truth | [P-23.20](#p-2320--revive-measures-the-lane-it-is-about-to-use) | 6 |
| 191 | Never list or revive a session the operator retired. | session-continuity | [P-23.35](#p-2335--which-sessions-revive-admits) | 6 |
| 192 | Refresh the live-revive census under the pass lock before every launch and skip a session… | process-survival | [P-23.55](#p-2355--one-live-revive-per-session) | 6 |
| 193 | Bind approval to a caller-attested fingerprint and never infer it from a fresh read of th… | safety-guard | [P-23.8](#p-238--a-gate-approves-an-exact-revision) | 7 |
| 194 | Require exactly one sentinel-delimited JSON verdict, bound to the same revision, with no… | safety-guard | [P-23.9](#p-239--what-counts-as-a-peer-verdict) | 7 |
| 195 | Reject an approval that carries findings or notes, and a changes-requested with no finding. | safety-guard | [P-23.9](#p-239--what-counts-as-a-peer-verdict) | 7 |
| 196 | Block the round if the artifact revision changed while the peer was reviewing. | safety-guard | [P-23.8](#p-238--a-gate-approves-an-exact-revision) | 7 |
| 197 | Require a positive Fable attestation and the absence of a downgrade marker before a Fable… | provenance/attestation | [P-23.43](#p-2343--an-unattested-round-is-not-a-verdict) | 7 |
| 198 | Run the peer read-only and isolated, from a neutral temporary directory outside the repos… | safety-guard | [P-23.10](#p-2310--where-a-peer-round-runs-and-what-reserves-it) | 7 |
| 199 | Reserve each round under a live lease so abandoned output never counts as a new approval. | safety-guard | [P-23.10](#p-2310--where-a-peer-round-runs-and-what-reserves-it) | 7 |
| 200 | Stop after four peer rounds by default. | UX-contract | [P-23.53](#p-2353--a-gate-stops) | 7 |
| 201 | Preflight a merge for an open, non-draft PR with unchanged head and base, clean mergeabil… | safety-guard | [P-23.11](#p-2311--what-a-merge-requires-before-it-is-attempted) | 7 |
| 202 | Merge with `--match-head-commit` pinned to the approved head. | safety-guard | [P-23.11](#p-2311--what-a-merge-requires-before-it-is-attempted) | 7 |
| 203 | Verify the landing against the immutable merge commit's parents, not the moving base tip. | safety-guard | [P-23.12](#p-2312--how-a-landing-is-verified) | 7 |
| 204 | Report a post-merge mismatch as a mismatch; never retry and never auto-revert. | safety-guard | [P-23.12](#p-2312--how-a-landing-is-verified) | 7 |
| 206 | Let only the currently reserved action publish its result, and never overwrite a completion. | safety-guard | [P-23.13](#p-2313--only-the-holder-publishes-an-actions-result) | 5 for `reset-credit`, 7 for `merge` |
| 207 | Support merge and squash only, and refuse to verify a rebased landing. | safety-guard | [P-23.12](#p-2312--how-a-landing-is-verified) | 7 |
| 208 | Scrub credentials and encoded binary from a handoff while retaining ordinary code, comman… | safety-guard | [P-23.14](#p-2314--what-a-handoff-may-carry) | 6 |
| 209 | Suppress the results of credential-reading tool calls (`agent-secret get`, keychain reads… | safety-guard | [P-23.14](#p-2314--what-a-handoff-may-carry) | 6 |
| 210 | Bound every handoff section with explicit character caps and keep the source transcript p… | session-continuity | [P-23.36](#p-2336--a-handoff-is-bounded-and-points-at-its-source) | 6 |
| 211 | Dispatch handoffs detached through `subfleet run` so they inherit routing, guard, salvage… | process-survival | [P-23.54](#p-2354--one-dispatch-path) | 4 for the hook, 6 for handoff |
| 218 | Keep `~/.claude` project transcript lookups to at most one directory below `projects`. | ops-hygiene | [P-23.40](#p-2340--finding-the-transcript-attestation-reads) | 2 |
