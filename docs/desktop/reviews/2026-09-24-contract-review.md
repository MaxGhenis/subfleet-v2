# Contract review, 2026-09-24

Five independent reviewers read design revision 1 and contract sections 24 to
30 through different lenses (durability, v2 integration, security, provider
protocol, product) and returned 58 findings. A second pass of 51 skeptics
tried to refute every blocking and major finding; 50 reported before the
session restarted. Two findings were settled by probes run this session:

- **F7 refuted.** A real `codex app-server` turn (scratch home, no model
  call) persisted `"client_id":"cid-123"` on the rollout's `UserMessage`
  item, so Codex reconciliation by client id is sound.
- **SEC-4 confirmed and fixed.** A writable `thread/start` with a `cwd`
  added `[projects."<cwd>"] trust_level = "trusted"` to the home's
  `config.toml`; a read-only thread with a writable turn policy added
  nothing.

Revision 2 of the design (commit `1bbfbe3`) adopted most findings; 15
skeptic verdicts say so explicitly ("already fixed"). The skeptics' refinements
to revision 2 became the implementation requirements below. Each is cited
where it is implemented; "open" means not yet built.

## Dispositions by lens

| Lens | Findings | Blocking | Disposition |
|---|---|---|---|
| Durability (F1–F11) | 11 | F1 (SIGTERM leaves Claude turns resumable) | All adopted: D-13 stop escalation and unfinished-turn block, §4 dispatch rules, D-3 watermarks, D-22 outbox order, §9 retention; F7 refuted by probe |
| v2 integration (F-01–F-14) | 14 | F-01 (Codex turns unclassifiable), F-02 (prose read as lane faults) | All adopted: §7 turn classification; F-02 fixed for every Claude job by PR #39; D-4 separate store answers F-11 |
| Security (SEC-1–SEC-9) | 9 | SEC-1 (agents could act as the person), SEC-2 (workspace settings disable hooks) | All adopted: D-8 person-only ops, D-9 `disableAllHooks:false`, D-2 relay peer check, D-9 read-only Codex threads |
| Provider protocol (P1–P9) | 9 | P1 (no pre-acceptance limit window) | All adopted: D-6 labelled continuation, D-15 background work, D-19 model identity, D-20 questions |
| Product (U-F1–U-F17) | 17 | none | All adopted: D-23 to D-27, ledger rows M-4a/b, M-10b, P-10 to P-13, R-7 to R-10 |

## Implementation requirements from the skeptics

| IR | Requirement | Source | Where |
|---|---|---|---|
| 1 | The dispatcher looks up `turn:<message>:<turn_seq>` first and binds it; never inserts another while one is not terminal; a turn's payload digest is the message digest | F2, F-07 | `dispatch.py` |
| 2 | Cancel atomicity lives in the job store: `message.cancel` sets the job's cancel only while no attempt row exists; `_launch` re-reads the cancel flag inside the `attempt.starting` transaction | F3 | `service.py`, `daemon.py` |
| 3 | Stop order: control interrupt, then SIGINT through a new relay `signal` op the guardian applies to its own unreaped child, then close stdin, then containment; each step bounded | F1 | `relay.py`, `guardian.py`, `runner.py` |
| 4 | Every daemon-initiated stop of a turn (wall limit, operator kill, approval timeout) escalates as IR-3, with reasons `wall-limit`, `operator-kill`, `approval-timeout` | F4, U-F4 | `runner.py`, `daemon.py` |
| 5 | The unfinished-turn block fires on delivery (user frame written), not only acknowledgement | F1 | `reconcile.py` |
| 6 | Compaction only after the attempt is terminal; reset exactly when the cursor is below the floor | F6 | `store.py`, `service.py` |
| 7 | Withdrawing a never-received message leaves a tombstone so a late submit of that id is cancelled | F10 | `service.py` |
| 8 | Turn wall limit and approval expiry end with named reasons and withdrawn approvals | F4, F-05 | `runner.py` |
| 9 | Claude classification reads provider-authored text only | F-02 | PR #39 |
| 10 | Codex turn classification: `usageLimitExceeded`/`rateLimitExceeded` limited with a closure from the latest `account/rateLimits/updated`; `unauthorized` unknown plus a usage probe, never auth-dead on its own | F-01 | `classify.py` |
| 11 | The socket `submit` op refuses `kind:"turn"`, `turn:` request ids and turn-only fields; binding requires the manifest's `turn` block to name the message | F-12 | `daemon.py`, `dispatch.py` |
| 12 | Turns are exempt from submit-time write-target refusals; contention is the admission `worktree:` lease | F-06 | `daemon.py` |
| 13 | A turn's `jobs.sandbox` is `read-only` for read-only turns and `workspace-write` otherwise | F-06 | `dispatch.py` |
| 14 | `allow_main` applies at submit, admission and launch | F-06 | `daemon.py` |
| 15 | Sessions under `/tmp` show `continue_blocker:"tmp-workspace"` unless a person sets `allow_tmp` | F-06 | `catalog.py` |
| 16 | Subfleet's own processes are never "live elsewhere" | F-06 | `catalog.py`, `dispatch.py` |
| 17 | No notice row for turn jobs; retention budgets split by kind; a notice with no session never pins | F-10, F8 | `daemon.py`, `retention.py` |
| 18 | `status.json` rows carry `kind`; `live`/`recent`/`counts` exclude turns; a `conversations` section | U-F14 | `status_json.py`, `conversations/store.py` (`status_summary`), `timers.py` (`publish_status`); C-18.2, C-29.6; tests `unit/test_status_json.py`, `unit/test_conversation_status.py`, `unit/test_timers_probe.py` |
| 19 | `list`/`runs` exclude turns unless asked | U-F14 | `daemon.py`, `cli.py` |
| 20 | Approval display masks value-shaped secrets only, never a span with `$(`, a backtick, a pipe, a separator, a redirection or a newline; reveal is person-only; no one-tap allow while masked | SEC-5 | `redact.py`, app |
| 21 | `message.submit` may not widen the conversation's permission; `conversation.create` above `ask` is person-only with `confirm_widen` | SEC-1 | `service.py` |
| 22 | Resolutions and decisions store the daemon-verified peer (pid, start, boot, executable) | SEC-1 | `service.py` |
| 23 | Claude Fast: fail `fast-unavailable` before sending when `initialize` reports it off or disabled; a turn may then be re-admitted elsewhere; `fast-mode-overage-rejected` is a served warning | U-F9 | `claude_turn.py`, `dispatch.py` |
| 24 | `EnterPlanMode` and `ExitPlanMode` are disallowed in writable Claude turns; plan approval is listed as unsupported | P5 | `claude_turn.py` |
| 25 | `<synthetic>` rows that are not API errors (for example "No response requested.") are ignored | P6 | `claude_turn.py` |
| 26 | Claude fixtures use observed catalog values (`default`, `opus[1m]`, `claude-fable-5-1[1m]`, `sonnet`, `haiku`) | P6 | tests |
| 27 | Relay frame cap advertised; Claude messages whose frame would exceed it are refused at submit; bounded resends; a relay status handshake before replay | F5 | `relay.py`, `service.py` |
| 28 | `conversation.handoff` op: pending messages move with the handoff, withdrawn from the source under the cancel guard | U-F11 | `service.py` |
| 29 | Each client has one watch and one events poll; a new one supersedes the old; abandoned polls end within 250 ms | U-F3 | `events.py` |
| 30 | Catalog indexes everything, with pinned exclusion predicates and archived handling | U-F5 | `catalog.py` |
| 31 | Stage 3: continue a Codex-app thread by copying its rollout into the lane that shares the app's account and calling `thread/fork` | U-F2 | live |
| 32 | Writable Codex policy refused until the live never-rules test is recorded | SEC-3 | `service.py` |
| 33 | Ledger R-4 cites C-26.11, C-14.2, C-14.3, C-23.6; M-14 cites C-29.6, C-18.1 | SEC-3, U-F7 | `ledger.json` |
| 34 | `status.json` windows keyed by (scope, window), never replacing the account window | U-F7 | `status_json.py` (`scoped_windows`, `claude_earliest_reset`); C-29.6; tests `unit/test_status_json.py`, `frontend/test_status_model.py` |
| 35 | Load test: 4 concurrent turns at 50 deltas/s keep commits batched and admission latency bounded | F-09 | tests |
