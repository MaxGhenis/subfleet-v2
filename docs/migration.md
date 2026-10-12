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
| `S/outbox.sqlite3` (`messages`: 6 rows, status, receipt), `S/outbox-attachments/` | The legacy desktop cockpit's message outbox (corrected 2026-09-24; it was listed as a notice outbox) | import at milestone 9 (C-30.4) | each message by itself: a `finished`, `error` or `cancelled` message of a Claude session whose transcript is found becomes a read-only history row, under its legacy id, of that session's one conversation in `conversations.sqlite3` (the one it has, whatever its origin, else a new `legacy` one); every other message keeps its legacy owner, with its whole session, as does a session the cockpit is using now; each message is in the report with its disposition; image snapshots stay in v1. The six notices an earlier pass made from this row stay as they are |
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

## Pruning redundant decision rows (one-off)

`python -m subfleet.prune_decisions` deletes the redundant `decisions` rows of terminal jobs — in practice the ones the admission pass wrote while a job waited for capacity. Nothing in the daemon or the CLI imports it; an operator runs it.

**When it is needed.** Until #16 the admission pass wrote a whole routing decision each time its verdict left a job waiting, once per re-check. #16 stopped the repeats: a wait that reaches the verdict it reached last time adds no row (C-6.10). The rows already written stay, and the dry run measures them: how many, over how many jobs, and how many bytes of `decision_json`. Retention does not reach them: its unit is a whole job (C-8.4), and the bytes it counts are `<state root>/jobs/<id>/` and an owned worktree, not the database file, so however large the `decisions` table grows it exerts no pressure on retention. This pass is the one-off that gives the space back.

**What is kept.** Every row with an `attempt_id` (C-11.5); per job the row `subfleet why` selects (`ORDER BY decision_id DESC LIMIT 1`) and the row offline `runs show` selects (`ORDER BY evaluated_at DESC, decision_id DESC LIMIT 1`), and the same two over the attempt-less rows alone; and every row of a job that is not terminal — a waiting job's history belongs to the daemon. Everything else is deleted. Nothing outside `decisions` is written but the one `events` row per delete batch that C-3.2 requires.

**Reader promises on release/217 (schema 7).**

| Reader | Answer after pruning |
| --- | --- |
| Daemon `why` (`daemon.py`, `_why_job`) | Unchanged stored `decision_json` and `evaluated_at` from the greatest `decision_id`; the new jobless prune event does not affect its refusal lookup. |
| Offline `runs show` (`offline.py`, `show_job`) | Unchanged `decision_json` from the greatest `(evaluated_at, decision_id)`, even when this is a different row from `why`. |
| Retention archive (`retention_archive.py`, `job_rows`) | Intentionally fewer rows for a pruned job, in the same `decision_id` order, with every surviving row unchanged. An archive made after the prune holds fewer decision rows than one made before. |
| `Store.list_decisions` (`store.py`) | Intentionally the retained subsequence rather than the original full history. On this line its callers are tests in `test_probe_turn`, `test_routing_end_to_end`, `test_admission_visibility`, and `test_capacity_wait_backoff`; no production caller uses it. |

The unchanged-reader promise covers persisted decision inputs. `why`'s live queue and disk information can change with time and daemon restart.

The full-history readers retain every attempt-linked decision, every row of an unfinished job, both latest-row choices, and both latest attempt-less choices. They do not promise the original row count. Existing archives are untouched and can still be verified, read (`rows.json`), and restored by the retention tools. Retention restore restores files and Git objects; it does not reinsert archived database rows into the live store. The pre-prune whole-store copy and gzip deleted-row backup preserve the original decision history for recovery.

Archives made after a prune contain no pruning marker or record of the original decision count. Their `rows.json` alone cannot establish the original history; keep the prune report and pre-prune backups for that provenance. This pass documents the caveat and does not add an archive marker.

Schema 7 has no foreign key targeting `decisions`. The pass enumerates all application tables from `sqlite_master`, including `job_owned_branches`, `job_pushes`, and `service_notices`; `decisions` and `events` have separate proofs. It holds the same exclusive `flock` on `daemon.lock` as this line's daemon and refuses while the daemon holds it. The audit kind `decisions.pruned` is valid JSON with no job id: generic event readers tolerate arbitrary kinds, while recovery and job-specific readers filter for their own kinds. A writing pass refuses schemas older than this build's schema 7 before `Store` can migrate or initialize them, including schema 0 (an absent or empty version record); newer schemas are also refused by `Store`. It also refuses missing expected tables, and opens the existing writer without applying schema DDL, so neither tables nor indexes are repaired before the baseline or backup.

**The commands.**

```
python -m subfleet.prune_decisions                       # dry run: the plan, nothing written
subfleet daemon stop                                     # the daemon is the store's writer (C-3.4)
launchctl unload ~/Library/LaunchAgents/com.subfleet.daemon.plist   # if installed: KeepAlive restarts it
python -m subfleet.prune_decisions --apply --i-understand-this-deletes-decisions --vacuum
launchctl load -w ~/Library/LaunchAgents/com.subfleet.daemon.plist  # or `subfleet daemon start`
subfleet doctor
```

The dry run is the default and computes the real keep and delete sets, the exact bytes, and the free-space verdict. A real pass holds `daemon.lock` for its whole length and refuses (exit 7) while a daemon holds it, with too little free space for the backups, or without both `--apply` and `--i-understand-this-deletes-decisions`. `--vacuum` is optional and belongs on the pass that deletes, which is the pass that took the backups: leave it off and the freed pages stay in the file as a freelist, which the report prints; a later pass has nothing to delete and rebuilds nothing. `--backup-dir` puts the backups on another volume. Every pass saves `<state root>/decisions-prune-report-<utc>.json`. A pass that cannot save it — a full volume, say — prints it in full on stderr instead and exits non-zero, still with its own error and fix, which names the copy to restore from once rows have been deleted; keep that printed copy.

**The backups, and restoring from them.** Before the first delete a real pass writes both of these to `<state root>/backups/` (0700, files 0600) and verifies them:

- `state-<utc>.sqlite3` — a whole `VACUUM INTO` copy of the store, checked with `PRAGMA integrity_check` and every table's row count and content digest against the planning snapshot. Canonical row hashing retains column names and tags every cell as NULL, INTEGER, REAL, TEXT, or BLOB. BLOBs use hex bytes; REALs use all IEEE-754 bits, including signed zero and infinities (SQLite stores NaN as NULL); TEXT preserves NULs and surrogate characters. The same encoder is used for planning, backup verification, and the after-proofs, including after VACUUM. The copy must be content-equal to that snapshot before any DELETE; its verified fingerprints are recorded under `backups.copy.fingerprints`. Restore it with the procedure below.
- `decisions-pruned-<utc>.jsonl.gz` — every deleted row as one JSON object per line with every column, re-read and checked against the row count and digest recorded while writing. Its SHA-256 is in the report (`shasum -a 256` confirms it later). To put rows back without restoring the whole store, `INSERT` them from the file.

After deletion, the proof also hashes the complete event table, including the new prune audit payload bytes. `--vacuum` must preserve that digest as well as the original append-only event history, other tables, retained decisions, and reader answers.

<!-- prune-restore:start -->
Stop the daemon with `subfleet daemon stop`. If its launchd job is
installed, unload it with
`launchctl unload ~/Library/LaunchAgents/com.subfleet.daemon.plist` so KeepAlive
cannot restart it. Set `state_root` to the store's directory, then run
`lsof -- "$state_root/state.sqlite3" "$state_root/state.sqlite3-wal" "$state_root/state.sqlite3-shm"`
and confirm that no process holds any of these files. Missing sidecars are OK;
resolve any other inspection error before proceeding.

Move `state.sqlite3`, `state.sqlite3-wal` and `state.sqlite3-shm` aside together
before copying the backup. Run this block in the same shell with `state_root`
set; replace the backup placeholder with the copy named in the pass report:

```sh
(
    set -eu
    backup='<backup copy>'
    aside=$(mktemp -d "$state_root/state-before-restore.XXXXXX")
    for file in state.sqlite3 state.sqlite3-wal state.sqlite3-shm; do
        if [ -e "$state_root/$file" ]; then
            mv "$state_root/$file" "$aside/$file"
        fi
    done
    cp "$backup" "$state_root/state.sqlite3"
    sqlite3 "$state_root/state.sqlite3" 'PRAGMA integrity_check'
)
```

Require `integrity_check` to return exactly `ok`. Compare each table's
`SELECT COUNT(*) FROM "<table>"` with the pre-prune counts in this pass's JSON
report: `decisions.total` for `decisions`, and
`fingerprints.before.<table>.rows` for `events` and every other table.
`fingerprints.before.decisions-kept.rows` is only the retained subset, not the
backup's decision count. An integrity check alone does not prove that the
backup's contents were restored. Only after integrity and all counts match,
restart with `launchctl load -w ~/Library/LaunchAgents/com.subfleet.daemon.plist`
if installed, or `subfleet daemon start`. Keep the displaced files and backup
until the restore is verified.
<!-- prune-restore:end -->

**What the pass proves before it reports success.** Every table other than `decisions` and `events` is counted and digested before and after and must match; the tables are enumerated from `sqlite_master`, so one added later is covered. `events` must hold its existing rows unchanged and gain exactly one `decisions.pruned` row per batch. The surviving `decisions` rows must be byte-identical to the rows the plan kept, `COUNT(DISTINCT job_id)` unchanged, and both latest-row reader queries must return identical stored fields for every job, including jobs without decisions (`why` also reads `evaluated_at`). The archive and store full-history readers intentionally return the unchanged retained subsequence. These proofs are repeated after `--vacuum`. `PRAGMA integrity_check` and `PRAGMA foreign_key_check` must be clean. Any mismatch exits non-zero, saves the report, and names the copy to restore from. A second pass is a no-op: the keep set is everything that is left.

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

`--claude-dir` names another `~/.claude` for the transcripts and its
`sessions/` registry, `--json` prints the report. A `--v1-state` that holds
none of the manifest's entries (missing, empty, another directory), or that is
a v2 state root, is refused, and so is a `--state-root` that looks like a v1
state directory (the two swapped), by the milestone pass too. A refused pass
touches neither directory, except that with a conversation store already
there (it may record a retirement) the pass takes `daemon.lock` and opens that
store before it refuses. Once the cockpit will never run again,
`--legacy-cockpit --cockpit-retired` lifts every legacy hold without reading
the v1 state and records the retirement, even before any conversation store
exists; later passes, milestone passes included, then read nothing from the
cockpit (neither its outbox nor its journal), and a legacy pass is not refused
once the v1 state is gone. Before a real pass, stop the daemon and quit the
cockpit: while its broker holds `S/broker.lock` every session is held, as it
is while the journal, the outbox or `S/native-workers.json` exists and cannot
be read, and a session with a live cockpit worker is held too. A session with
a live Claude process outside Subfleet gets no history that pass. Start the
daemon again only from this build or a later one: `legacy_hold` and
`legacy_sessions` are additive, so an older build opens the store and ignores
them.

The report lists every message and journal entry by id with its disposition.
`history` is written by this pass and `already-imported` by an earlier one.
These leave the message where it is, and a later pass imports it once its
reason is gone: `legacy-owned` (not terminal, or a journal entry),
`session-held-by-legacy-owner`, `transcript-not-found`,
`session-not-continuable` and `transcript-unreadable`. These leave it for good,
since no later pass changes them: `conversation-has-own-messages` and
`history-after-messages` (the session's conversation has messages of its own,
and history goes first), `message-id-conflict`, `unreadable-row` (a message
id, a payload or a timestamp), `bad-text` (a prompt over 1 MiB) and
`not-a-claude-session`. The outbox row also counts `absent`,
`unreadable-database` or `no-messages-table`, and the journal row
`unreadable-journal`.

Conversations appear too, by id: `bound-session-held` for each conversation
bound to a held session, whatever its origin, with the reason (its
`legacy_hold`), its own `blocked_by`, and its messages that are not settled;
`bound-session-released` for one whose session a pass finds settled. While a
conversation is held no turn runs in it. When the daemon starts it stops a
turn that kept running there, and a queued or waiting message runs once the
hold lifts; cancel one first (`message.cancel`, with the daemon running) if it
should not. A session held by the pass that is opened after it
(`conversation.open`) is bound held as well.
