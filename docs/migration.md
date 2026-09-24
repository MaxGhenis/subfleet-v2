# Migration and cutover

Plan B rev 4 "Migration and cutover" as amended by plan amendment 8 (ownership per account as well as per capability) and 12 (store upgrade path). This document is the import manifest and the transfer procedure for milestone 4. Every v1 store found on 2026-09-05 is classified; nothing is imported by guesswork.

The historical shadow-week procedure below was superseded for this installation
by Max's September 19 direct-cutover decision in `plan.md`. Native completion
retains the public command spellings without executing the v1 binary. `pick`,
operator maintenance, diagnostics, login instructions, and the attached-runner
hook now use v2; `_record-*` and private `_tickle` callbacks refuse explicitly.
The tracked `bin/codex` shim and native PreToolUse entry replace the final v1
runtime dependencies. Preserve the old stores, receipts, settings backup, and
shim for rollback evidence; do not run both schedulers. See
`reports/2026-09-21-native-completion.md` for the current verification scope.

## Principles

1. **One owner per account and per capability.** During the shadow period every account has `owner: v1` or `owner: v2` in `lanes.json`; v2 never dispatches, probes for dispatch, redeems, or keeps alive on a v1-owned account, and v1's roster loses an account the moment v2 takes it. Capabilities (dispatch and notices, timers, sessions, gates) also transfer one at a time, each fenced before the next is enabled.
2. **Import facts, not estimates.** Provider readings older than the TTL import as `stale-provider`. Learned percentages, token-sum ratios, and derived window boundaries are never imported as quota.
3. **Still-running v1 work is external.** A v1 run that is live at import time is recorded as a job with `kind: dispatch`, state `running`, and an `imported_external: true` flag in `manifest.json`; v2 never adopts, kills, or finalizes it. It becomes terminal when v1 finalizes it and a later import pass reads the rc.
4. **Idempotent and incremental.** The importer keeps a cursor per store (last id, last mtime, last line) in `events` and re-running it changes nothing already imported.
5. **Rollback never leaves two schedulers or two redeemers alive on one account.**

## Import manifest

Paths are relative to `~/chief-of-staff/state/subfleet/` (v1 state root, "S") or `~/.local/state/delegate/` ("D"). Counts are from the 2026-09-05 listing.

| Store | What it is | Disposition | v2 destination and rules |
|---|---|---|---|
| `~/chief-of-staff/subfleet/claude-accounts.json`, `codex-accounts.json` | The roster: enrolled accounts, keychain item names, Codex homes | import | `lanes` rows with `owner: v1` initially; credential refs copied, never values; `desktop` set from `~/.claude.json` `oauthAccount`; each Codex home's account key read from its `auth.json` at import |
| `S/runs/` (500 dirs, `meta.json`, `prompt.md`, `out.md`, `err.log`, `lane.log`) | The v1 ledger | import read-only | one `jobs` row and one `attempts` row per directory; `job_id` keeps the v1 id; `request_id` = `v1:<id>`; state from rc (0 `succeeded`, 4 or 5 `failed`, -9/143/killed `interrupted`, never finalized `lost`); `artifacts` rows point at the v1 paths (not copied); `imported: true`; live entries per principle 3 |
| `S/runs-out/` (15 files: `<name>.err.log`, `<name>.MODEL_ATTESTED`) | Side files from named runs | retain read-only | not imported; referenced by the v1 rows they belong to if a run names them |
| `S/notices/` (152 per-session JSONL files) | Parked completion notices per caller session | import | `notices` rows with `state: pending` for entries v1 marked unsurfaced, `surfaced` otherwise; `session_id` from the file name |
| `S/outbox.sqlite3` (`messages`: 6 rows, status, receipt), `S/outbox-attachments/` | The legacy desktop cockpit's message outbox (corrected 2026-09-24; it was listed as a notice outbox) | import at milestone 9 (C-30.4) | each message by itself: a `finished`, `error` or `cancelled` message of a Claude session whose transcript is found becomes a read-only history row, under its legacy id, of that session's one `legacy` conversation in `conversations.sqlite3`; every other message keeps its legacy owner, with its whole session; each message is in the report with its disposition; image snapshots stay in v1. The six notices an earlier pass made from this row stay as they are |
| `S/gates/` (117 entries) | Gate state directories | retain read-only until milestone 7 | active gates finish in v1 by default; at milestone 7 a gate imports only if subject fingerprint, approvals, peer evidence, and action state all verify (plan A step 6); never rebuilt from a summary |
| `S/tickles/` (5,823 files), `S/revive/` (244 logs), `S/revive-lane.json`, `S/session-locks/` (10), `S/session-continuations.lock`, `S/native-workers.json` | Sessions kit state | retain read-only until milestone 6 | tickle dedup records import as `events` of kind `tickle` with their timestamps so the sessions kit does not re-nudge; revive logs are not imported; `native-workers.json` (two claude worker ids) imports as `events` of kind `native-worker` for the twin check |
| `S/history.jsonl` (2,400 lines), `S/lane-usage.jsonl` (1,733 lines) | Rolling token sums and burn history | drop for quota; retain file | never imported as readings or percentages; the file stays for the compare script and for `docs/reports/B-capacity.md`-style audits |
| `S/capacity-live-cache.json` (`probed_at`, `accounts`) | Last Codex wham probe results | import | `readings` rows per account and window classified by duration (C-9.7), label `provider` if `probed_at` within `READING_TTL_S`, else `stale-provider` |
| `S/claude-oauth-raw.json` (`checked_at`, `raw`) | Last desktop OAuth usage payload | import | `readings` for the desktop account, label by age as above; per-model scoped limits in the payload become `admission-observed` rows for those models. Two payload windows the dry run found have their own dispositions: `extra_usage` is dropped (overage is parsed independently and is never admission or quota evidence, C-9.8); `nimbus_quill` is retained un-imported until the operator names the model or plan it belongs to |
| `S/claude-statusline.json`, `claude-statusline-history.jsonl`, `claude-statusline-invoked.json` | Statusline tap | drop | the tap is dead for the desktop app (plan B "What gets dropped") |
| `S/snapshot.json`, `S/rollout-scan-cache.json`, `S/rollout-scan-memo.json`, `S/refresh-probes.json` | Derived caches | drop | regenerated by the daemon's probe cycle |
| `S/keepalive.json` (`lanes`, `last_run`) | Keepalive pings per lane | import | `readings` with label `admission-observed`, scope the Haiku model id, source `keepalive`, observed at the ping time; never a window reset |
| `S/reset-policy.json` (`last_redeemed_at`, `credit_id`, `last_redemptions`) | Reset-credit redemption history | import | `actions` rows of kind `reset-credit`, state `confirmed`, `op_key` = account key plus credit id, so the one-at-a-time rule and the minimum interval respect history |
| `S/alerts.json` (latch keys such as `claude-limit:<ts>`, `codex-revoked:<home>`) | Alert latches for transition-only alerting | import at milestone 5 | `events` of kind `alert-latch`; the timer reads them before its first cycle so it does not re-alert |
| `D/cooldowns.json` (keys per Codex home and per Claude email) | Active cooldowns from the delegate | import | `closures` with scope `account` for legacy unscoped holds, the model scope when recorded, `until_at` from the entry, `clock_source: reported` when the entry came from a provider reset and `guessed` otherwise, `source_event: v1-cooldown`; expired entries skipped |
| `D/decisions.jsonl` (16,710 lines), `D/rotation.json` | Routing decision journal and last-used rotation | retain read-only | inputs to the shadow-week compare script; not imported |
| `S/prompts/` (17), `S/briefs/` (26), `S/dispatch/` (1) | Prompt and brief files named by runs | retain read-only | referenced by imported v1 artifacts where a run names them |
| `S/integration-events/v1/` | Traycer events spool, schema v1 | retain; keep writing | the daemon emits `run.started`, `run.bound`, `run.finished`, `handoff.created` to the same spool from milestone 5 |
| `S/cockpit-client/` (`pending-messages.json`, `images-<uuid>/`) | The legacy cockpit app's journal of unacknowledged sends and their image snapshots | retain read-only; reported at milestone 9 (C-30.4) | every journal entry keeps its legacy owner and is reported, with the outbox status for its id, and holds its session's history back; nothing is sent |
| `S/composer-attachments/` (empty), `S/iariw-drain.json`, `S/iariw-drain.log`, `S/autopick.log`, `S/brief.md` | Job-specific or regenerated | drop after inspection | `brief.md` is regenerated from the store; the drain files belong to one finished campaign |
| `S/*.lock`, `S/broker.lock`, `S/broker.sock`, `S/.integration-events.salt.lock`, `S/keepalive.json.lock`, `S/reset-policy.json.lock`, `S/revive.lock`, `S/rollout-scan.lock`, `D/cooldowns.json.lock` | File locks and the v1 broker socket | drop | SQLite and `daemon.lock` replace them; never copied |
| `S/integration-events.salt` | Salt for event ids | import | copied to `$SUBFLEET_HOME/integration-events.salt` so event ids stay stable across the cutover |

Anything found at import time that is not in this table is reported by the importer and left alone; the manifest is extended before it is imported.

## Shadow week

1. Build v2 under `~/.subfleet/` with the CLI installed as `sf2`. v1 keeps running unchanged.
2. Import per the manifest. Every account is `owner: v1`.
3. Transfer one Codex account to v2 as the canary (`sf2 lanes transfer codex-<n> --to v2 --i-understand-v1-edit`): the home `~/.codex-<n>` is relocated to `~/.subfleet/lanes/codex-<n>/`, so v1's directory glob no longer finds it from any caller (CLI, gates, watchdog, keepalive, reset policy); v1's roster records it under `transferred_to_v2` with a backup beside the file; the transfer is refused while v1's ledger shows a run live on that home. Verified by v1's `subfleet status` omitting the lane and `sf2 lanes` showing `owner: v2`. v2 then runs 100 canary jobs on it (release gate). Rollback is `sf2 lanes transfer codex-<n> --to v1 --i-understand-v1-edit` after v2's attempts on the lane are terminal; it moves the home back.
4. Every v1 dispatch from one chosen session is mirrored to `sf2 run --dry-run --json`; a `compare` script (never shipped) diffs the two decisions nightly against `D/decisions.jsonl` and writes `docs/shadow-diffs/<date>.md` with every difference explained.
5. Capability transfer order, each fenced before the next is enabled: dispatch and notices (milestone 4: v1 `subfleet run` becomes a shim to v2 for v2-owned accounts), timers including reset credits, keepalive, alerts, and mirror (milestone 5: v1 plists booted out one at a time), tickle and revive (milestone 6), gates (milestone 7). v1 gates call `delegate_main` directly, so they keep working on v1-owned accounts until milestone 7 and are never repointed by the symlink.
6. Accounts transfer in batches after the canary passes; a transfer records an `events` row and edits both rosters in one step.

## Cutover checklist (milestone 4)

- `subfleet` symlink repointed to v2 with the compatibility parser in place (every v1 invocation accepted; C-17.1, C-17.2).
- Hooks reinstalled by `sf2 daemon install` (PreToolUse guard unchanged; SessionStart and UserPromptSubmit surfacing from the v2 store).
- CLAUDE.md model-routing section reviewed: the five-line contract is unchanged by construction.
- Skills `tickle`, `muster`, `codex-accounts` repointed at milestone 6, not before.
- `bin/codex` shim kept, calling `subfleet lanes pick codex`.
- Memory files updated with the new state root and the ownership map.
- Findings from the compatibility lane (2026-09-05), each a checklist item:
  - `PYTHONPATH` on this machine points at the v1 checkout and outranks an installed package, so a v2 console script in a v2 virtualenv imports v1's package. Unset it (or repoint it) before the symlink flip; `doctor` fails on this until it is done.
  - The v2 compat layer delegates every unported v1 verb (`gate`, `sessions`, `handoff`, `tickle`, `muster`, `revive`, `pick`, `login`, `reset`, `errors`, `watch`, `keepalive`, `brief`, the hidden `_` verbs) to the v1 binary with its exit code unchanged, because v1's runners call `_record-run` and friends back through `$SUBFLEET_RUN_SUBFLEET` on every record. The v1 binary therefore stays installed at its known path until milestones 6 and 7 land; the flip repoints only the front door.
  - `--independent` changes meaning: v1's abbreviation of `--independent-review`; v2's child-survives-parent flag (C-7.3). v2 wins. Say so in the cutover announcement, with `-I -D` as the isolated-review spelling.
  - `subfleet codex -d` and `subfleet claude -d`, which v1's guard allowed, are refused unconditionally in v2 (front-door rule). The refusal names `subfleet run`.
  - `runs show <id>` keeps its spelling but v1 printed metadata and the deliverable together; v2 prints one thing per form. Restore v1's bare-form shape in `cmd_runs_show` before the flip (follow-up, not compat's job).
  - v1's `gate` is `consensus.py` behind v1's own `subfleet`; there is no separate `subfleet-gate` binary in v1, so delegation targets the v1 `subfleet` entry point.

## Rollback

In this order: stop v2 admissions; stop the daemon's timers; restore the v1 hook configuration; reconcile every v2 attempt (wait, kill with tree verification per C-5.6, or hand it to v1 as externally owned); transfer accounts back one at a time; re-bootstrap the v1 plists; repoint the symlink. Results, notices, and confirmed actions created under v2 stay recorded in the v2 store; rollback never erases evidence of an action that happened.

## Store upgrade (v2 to v2)

Stop admission; drain or re-adopt attempts; `VACUUM INTO '<state root>/backups/state-<utc>.sqlite3'`; apply additive migrations; `PRAGMA integrity_check`; resume. A CLI newer than the daemon refuses to write and prints both versions (C-3.5).

## Dry run against the real v1 state, 2026-09-05 16:21 EDT

`SUBFLEET_HOME=~/.subfleet-dryrun uv run python -m subfleet.importer --dry-run`, read-only toward v1, 0.7 s. Counts are what a real pass would write.

| Store | Seen | Imported | Skipped and why |
|---|---|---|---|
| roster | 23 | 23 | 3 accounts noted as not enrolled in v1 |
| runs | 500 | 500 | 2,233 artifact references; 9 live v1 runs imported as external, never adopted; some rows carry no model recorded by v1 |
| notices | 1,119 | 203 | 916 name runs no longer in the 500-run ledger (17 pending, 186 surfaced imported) |
| outbox | 6 | 6 | all six acknowledged |
| capacity-live-cache | 14 | 6 | 8 rows are learned percentages, not live readings, and never become quota |
| claude-oauth-raw | 3 | 3 | two payload windows without a manifest row left alone (`extra_usage`, `nimbus_quill`) |
| cooldowns | 34 | 17 | 15 expired; 2 name an account with no lane; all 17 imported carry `clock_source: guessed` |
| keepalive | 14 | 14 | |
| reset-policy | 3 | 3 | |
| salt | 1 | 1 | |
| alerts, sessions kit | 0 | 0 | staged for milestones 5 and 6 by the manifest |
| gates | 0 | 0 | retained until milestone 7 |

Not in the manifest, left alone: `~/.local/state/delegate/cooldowns.json.bak-2026-09-04`. Nothing under `~/chief-of-staff` changed.

Open from this run: the two OAuth payload windows (`extra_usage`, `nimbus_quill`) need a manifest decision (import as readings with their own scope, or drop); the 916 orphaned notices are v1's own retention gap and stay out.

The outbox row above was the notice mapping; C-30.4 replaced it on 2026-09-24 (next section).

## The legacy cockpit, milestone 9

`outbox.sqlite3` turned out to be the legacy desktop cockpit's message outbox,
not a notice outbox: one row per message the cockpit sent into a native
session, keyed by the client's UUID (`subfleet/conversations/legacy.py` records
what the cockpit's code writes, with citations). C-30.4 classifies each message
by itself into conversation history, and reports the rest:

```sh
# what a pass would do: nothing under the state root changes but the report
uv run python -m subfleet.importer --legacy-cockpit --v1-state ~/chief-of-staff/state/subfleet --dry-run
# the pass itself, with the daemon stopped (it is conversations.sqlite3's writer)
uv run python -m subfleet.importer --legacy-cockpit --v1-state ~/chief-of-staff/state/subfleet
```

`--claude-dir` names another `~/.claude` for the transcripts, `--json` prints
the report. The report lists every message and journal entry by id with its
disposition: `history`, `already-imported`, `legacy-owned`,
`session-held-by-legacy-owner`, `transcript-not-found`,
`session-not-continuable`, `conversation-has-own-messages`,
`message-id-conflict`, `not-a-claude-session` or `unreadable-row`. Every
disposition but the first two leaves the message where it is, and a later pass
imports it once its reason is gone.
