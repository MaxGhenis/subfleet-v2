<!-- Lane report D-surface · run 20260905-063314-sfplan-d-surface-sonnet · Sonnet (max@rulesfoundation.org after a re-pick), 13m · read-only lane dispatched by the planning session 2026-09-05; verbatim -->

# subfleet surface audit (assignment D)

Scope note: this is delivered directly as the final message per the lane brief's headless-execution rule ("your final message is the deliverable... do not try to write it to a file"). Every code claim below cites `path:line` from files read in full this turn: `subfleet/cli.py` (1638 lines), `subfleet/delegate.py` (1315), `subfleet/consensus.py` (1528), `subfleet/handoff.py` (665), `subfleet/resume_codex.py` (231), `subfleet/run_ledger.py` (1032), `subfleet/notify.py` (664), `subfleet/render.py` (407), `subfleet/hooks.py` (222), `subfleet/tickle.py` (1085), `subfleet/paths.py` (166), `subfleet/login.py` (89), `subfleet/__init__.py` (38), `bin/subfleet-claude` (899), `bin/subfleet-codex` (600), `bin/subfleet-hook` (92), `bin/subfleet` (18), `bin/codex` (56), `bin/carpool` (5), `bin/subfleet-watch` (9), `bin/subfleet-keepalive` (7), `bin/subfleet-login-watch` (first 40 of a longer file), `bin/subfleet-guard` (first 60 of a longer file), `README.md` (700), `docs/guard.md` (217), `claude-accounts.json`, `codex-accounts.json`, `~/.claude/cc-mirror.json`, the hook/statusLine block of `~/.claude/settings.json`, `~/.claude/CLAUDE.md:45-102`, and the SKILL.md files for tickle, muster, codex-accounts, and gpt-pro. `subfleet/codex.py`, `subfleet/capacity.py`, `subfleet/claude.py`, `subfleet/watchdog.py`, `subfleet/keepalive.py`, `bin/subfleet-mirror`, and `bin/subfleet-statusline` were only grepped, not read line by line; claims about them are marked "not fully read" below. `state/subfleet/traycer-profiles.json` does not exist (confirmed by Glob against `state/subfleet/`), consistent with it belonging to the unmerged `feat/subfleet-traycer-port` branch rather than today's shipped surface.

One correction to the shared context: it states "rc 75 = queued." Nothing in `consensus.py` uses 75; the queued-merge code is 5 (`consensus.py:907-911`, `consensus.py:1448-1452`), documented identically in `README.md:696-699`. Flagging rather than repeating it.

## 1. Verb inventory

Caller key: **Max** = typed by Max, **Agent** = dispatched by an agent in a session, **Launchd** = one of the four scheduled jobs, **Hook** = `bin/subfleet-hook` / Claude Code hook machinery, **Verb** = called by another subfleet verb, not directly.

| Verb | Key flags | Does | Exit codes | Called by | Verdict |
|---|---|---|---|---|---|
| `status` (default) | `--json`, `--cached` | Per-account table/JSON (`cli.py:75-83`) | 0 | Max, agents, app | core |
| `capacity` | `--json` | Cached cross-family headroom (`cli.py:86-89`) | 0 | Max, agents, `login.py` skill | overlaps `status`; ops |
| `pick codex\|claude` | `--json --all --model --min-headroom --handicap` | Best lane on stdout (`cli.py:92-194,326-413`) | 0/1 | `run`, both runners, `bin/codex` shim, `resume_codex.py` (indirectly), Max diagnostics | should be internal |
| `run` | `--task/--tier`, `-t`, `-m`, `-a/-H`, `-C -o -n`, `-d/--attach`, `--json --dry-run --why --status`, `-x/--exclude`, `--reuse-out` | Dispatch front door: classify, pick capacity, launch a runner (`delegate.py:790-1311`) | 0,2,3,6,7,124,125,127 (see section 2) | Max, agents, `handoff.py`, `consensus.py` (peer rounds) | core |
| `codex` (pass-through) | see `bin/subfleet-codex` | execs `bin/subfleet-codex` (`cli.py:1486-1492,1584-1591`) | see runner | `run`, launchd lane scripts, Max scripts | should be internal (PreToolUse guard already blocks it from a session, `bin/subfleet-hook:39-69`) |
| `claude` (pass-through) | see `bin/subfleet-claude` | execs `bin/subfleet-claude` | see runner | same as `codex` | should be internal, same reason |
| `mirror` (pass-through) | `--quiet --list --dry-run` | execs `bin/subfleet-mirror` (not fully read) | unknown (not read) | launchd (60s) | ops |
| `runs` | `--last --json --mine --running` | List ledger rows (`cli.py:691-733`, `run_ledger.py:998-1031`) | 0,2 | Max, agents | core |
| `runs show <id>` | `--err` | Print one run's meta+out(+err) (`cli.py:692-711`) | 0,1 | Max, agents | core |
| `runs reap` | `--dry-run --grace` | Finalize dead-pid RUNNING entries as rc=-9 (`cli.py:712-717`, `run_ledger.py:841-867`) | 0 | Max, watchdog (implied), agents | ops |
| `wait` | ids, `--mine --last --timeout --interval --cat` | Block for ledger completion (`cli.py:740-783`) | 0,2,124,125,pass-through | Max, agents | core |
| `kill <id>` | `--grace` | SIGTERM/SIGKILL the run's process group (`cli.py:786-799`, `run_ledger.py:870-938`) | 0,1 | Max, agents | core |
| `resume-codex <id> [PROMPT]` | `-o` | Resume a Codex thread on its original home (`cli.py:1033-1035`, `resume_codex.py`) | 0,2,3,127,pass-through | Max, agents | core |
| `handoff` | `SESSION_ID\|--last`, `--to`, `-C` | Cross-provider continuation of a Claude session (`cli.py:1038-1045`, `handoff.py:607-664`) | 0,1,2,pass-through from `run` | Max, agents | core |
| `gate pr\|plan\|continue` | `--peer --main-approve --expect-* --brief --max-rounds --json --dry-run --on-agreement --merge-method --response` | Durable main/peer review loop (`cli.py:1048-1049,1350-1396`, `consensus.py`) | 0-5 (documented, `README.md:696-699`) | Max, agents | core, best-documented part of the surface |
| `login codex <N\|app>` | `--no-watch --no-open` | Stage a re-login, arm the watcher (`cli.py:1052-1056`, `login.py`) | 0,1 | Max only (docs say Claude never clicks) | core, Max-only |
| `reset codex <N\|all>` / `--policy` | `--dry-run` | Consume a gifted reset credit (`cli.py:1112-1275`) | 0,1,2 | Max, watchdog, `pick codex` (indirectly, `cli.py:106` `reset_policy.run`) | ops |
| `enroll <email>` | none | Store a Claude setup-token, probe it, clear cooldowns (`cli.py:468-525`) | 0,1,2 | Max only | core, Max-only |
| `errors` | `--hours --json` | Observed limit/auth errors both providers (`cli.py:416-438`) | 0 | Max, agents | ops, overlaps `status` |
| `watch` | `--dry-run` | One watchdog cycle (`cli.py:441-444`) | 0 | launchd (30 min) | ops, not for Max/agents |
| `keepalive` | `--dry-run --family` | Keep idle Claude windows rolling (`cli.py:447-459`) | 0,1 | launchd (5h05m) | ops |
| `brief` | none | Morning-brief markdown section (`cli.py:462-465`) | 0 | launchd/skill consumer | ops |
| `sessions` | `--all --json` | List live Claude sessions with an inbox (`cli.py:802-818`) | 0 | Max, agents, muster/tickle diagnostics | core (diagnostic) |
| `notify [--session] TEXT` | `--force --mode --json` | Push a message into a session inbox (`cli.py:821-836`) | 0,1,2 | Max, agents, other tools | core, but name collides with the unrelated CoS `bin/notify` (see section 5) |
| `hooks install\|uninstall\|status` | `--dry-run` | Manage `~/.claude/settings.json` entries (`cli.py:839-847`, `hooks.py`) | 0,1 | Max (one-time setup) | ops |
| `tickle` | `--session --transcript --all --dry-run --force --json` | Resume-nudge sessions cut off by a restart (`cli.py:966-1009`, `tickle.py`) | 0,1 | Max (`/tickle` skill), hook (auto), agents | core |
| `muster` | `--dry-run` | Broader roll call: tickle + nudge idle-but-recent sessions (`cli.py:928-963`) | 0 | Max (`/muster` skill) | core, overlaps tickle (see section 5) |
| `revive` | `--model --no-fallback --dry-run --max` | Headlessly resume cold (process-dead) sessions (`cli.py:900-925`, `tickle.py:903-1084`) | 0 | launchd (`com.maxghenis.cos.subfleet-revive`, ~2 min) | core, ops-adjacent |
| `_session-hook <event>` | positional `event` | Backend for `bin/subfleet-hook`'s session-start/user-prompt (`cli.py:850-891`) | 0 | `bin/subfleet-hook` only | should be internal (already `argparse.SUPPRESS`, `cli.py:1417`) |
| `_tickle` | `--session --transcript --delay --force` | Detached nudge worker (`cli.py:894-897`, spawned `tickle.py:457-473`) | 0,1 | `tickle.py:spawn`, `bin/subfleet-hook` indirectly | should be internal |
| `_canonical-model <model>` | positional | Resolve alias/retired pin to canonical id (`cli.py:539-543`) | 0 | `bin/subfleet-claude:188` | should be internal (pure function, no need for a subprocess round trip) |
| `_api-lane-check <home>` | positional | rc=7 refusal for API-key Codex homes (`cli.py:528-536`) | 0,7 | `bin/codex:50-51` | should be internal |
| `_record-lane-run` | `--email --model --session-id --rc --workdir --err-file --raw-file` | Best-effort per-attempt accounting hook (`cli.py:546-568`) | 0 | `bin/subfleet-claude:581-592` | should be internal |
| `_record-run` | `--phase start\|adopt\|update\|finish` + many | Ledger start/adopt/update/finish (`cli.py:571-676`) | 0,1 | both runners, `resume_codex.py` | should be internal |
| `_record-codex-cooldown` | `--home --minutes` | Write a lane cooldown for the rollout-scan propagation gap (`cli.py:1278-1282`) | 0 | `bin/subfleet-codex:573` | should be internal |
| `carpool` (top-level binary) | forwards all argv | Prints a deprecation notice, execs `bin/subfleet` (`bin/carpool:1-6`) | pass-through | any script not yet renamed | legacy, overdue: README (`README.md:3-4`) promised "one week" from 2026-08-23; today is 2026-09-05, 13 days over |

Env-driven legacy aliasing (`CARPOOL_*` -> `SUBFLEET_*`, `CLAUDE_LANE_CARPOOL` -> `CLAUDE_LANE_SUBFLEET`, `DELEGATE_CARPOOL` -> `DELEGATE_SUBFLEET`) is duplicated as near-identical shell loops in **six** files (`bin/subfleet:7-11`, `bin/codex:17-21`, `bin/subfleet-hook:23-27`, `bin/subfleet-claude:81-85`, `bin/subfleet-codex:76-80`, `bin/subfleet-login-watch:8-12`) plus a seventh Python copy (`subfleet/__init__.py:24-37`). Same verdict as `carpool`: legacy, should be deleted as one unit.

## 2. Exit-code map: `run`, the two runners, `wait`, `gate`, `resume-codex`

| Code | `subfleet run` (`delegate.py`) | `subfleet-claude` (bash) | `subfleet-codex` (bash) | `wait` (`cli.py`) | `gate` (`consensus.py`) | `resume-codex` |
|---|---|---|---|---|---|---|
| 0 | dispatched/`--dry-run`/attach-wait succeeded (`:1306-1311`) | OK (`:824`) | OK (`:565`) | all runs finished (worst=0) or nothing to wait for (`:755`) | agreement/completion/no-op repeat (`:964-966,899-903`) | inner runner returned 0 (pass-through, `:220`) |
| 1 | not produced by `run` itself; a codex-family **sync** dispatch can pass this through raw from `codex exec` | unclassified failure; also rc=0-with-empty-envelope remapped here (`:895-898`) | unclassified failure; rc=0-with-empty-out remapped here (`:598`) | not produced directly (only pass-through) | operational error, e.g. `gh`/git subprocess failure (`:172,193,197,202,206`) | not produced directly |
| 2 | `argparse.error()` on bad flags (`:791-807`) | usage/argument errors: missing `-a/-A`, `-C`, `-p`, `-o`, bad `-s`, missing `jq`/`uuidgen` (`:121-156`) | usage/argument errors, **and** guard preflight/override failure (`:109,144,489-490`), conflating "bad flags" with "security guard refused" | `--mine` with no `CLAUDE_CODE_SESSION_ID` (`:745-746`), **and separately** an unknown/missing run id (`:766-767`), two unrelated meanings under one code | invalid input, the `GateError` **default** code (`:48-50`); bad `--peer`, `--max-rounds<1`, bad workdir, missing `--main-approve`, unknown gate id | run not found / still RUNNING / missing thread-id, home, model, or workdir / missing dirs (`:82,89,111,116,119`), same number as `subfleet-codex`'s own usage-error code |
| 3 | no dispatchable lane (`:1174,1223`), `-o` collision with a live writer (`:306`), Fable-floor unavailable (`:1051-1052`), **and** Claude retries-exhausted or `-a`-pinned hard limit is unconditionally converted from 4/5 here (`:1302-1303`) | not produced (Claude runner uses 4/5, not 3) | content-filter block, not retried (`:570`) | not produced | changes requested, gate stays open for another round (`:1230`, also `:1019-1022`) | original lane cooled/limited/exhausted, thread cannot migrate (`:130`) |
| 4 | never surfaces as `run`'s own final code (always converted to 3, `:1302-1303`); **but** an auto-picked (no `-H`) dispatch that lands on a Codex API-key home would surface rc=7 raw, since only 4/5 are intercepted, an unhandled asymmetry | hard lane limit (`:877`) | not used (a Codex usage limit re-picks internally, `:571-589`, with no distinct terminal code of its own if repick fails; asymmetric with Claude) | not produced by `wait` itself; pass-through of a run's own rc is possible | blocked / invalid review / max rounds / stale lease / merged-revision mismatch on completion-check, at least 7 distinct call sites (`:301,781,783,790,816,951,956,1230,1233`), by far the most overloaded code in `gate` | not used |
| 5 | never surfaces (converted to 3); side effects (30-day cooldown, re-enroll ritual) still fire first (`:1276-1277`) | auth failure, dead token or org-blocked (`:858`) | not used | not produced | merge action failed, unverified, or queued (README documents "queued" and "failed" as the same code, `:839,846,890,898,911,919,931`) | not used |
| 6 | passed through raw from a Claude-family sync dispatch (CLI too old); `run` does not intercept it | Claude CLI too old for the requested model (`:837`) | not used | not produced | not used | not used |
| 7 | passed through raw from a Codex-family sync dispatch (API-key home refusal); not intercepted at all for auto-picked dispatch | not used | API-key lane refused, subscription-only policy (`:306`) | not produced | not used | not used |
| 97 | not produced | **undocumented**: `cd "$WORKDIR" \|\| exit 97` inside the launch subshell (`:794`); not listed in the usage header (`:45-79`) and not remapped by the final catch-all (which only remaps 0 and 4/5, `:895-896`) | not used | not produced | not used | not used |
| 124 | `--attach` inline wait timed out (`:1144-1145`) | not used | not used | a waited run is still RUNNING at `--timeout` (`:762-763`) | not used | not used |
| 125 | `--attach` inline wait found an orphaned runner (`:1147-1148`) | not used | not used | a waited run is orphaned, no finish record (`:768-769`) | not used | not used |
| 127 | detached launch `OSError` (`:1124-1127`) | not applicable at this layer | not applicable | not produced | not used | inner launch `OSError` (`:225`) |
| 130/143 | not intercepted, pass-through possible | explicit `trap 'exit 130' INT` / `trap 'exit 143' TERM` (`:431-432`) | **no explicit INT/TERM trap** (only `trap on_exit EXIT`, `:544`); relies on bash's default 128+signal behavior, same numeric result but implicit rather than documented | not produced | not used | not used |

**Collisions worth fixing first:** rc=2 means "you typed it wrong" in five different binaries and, in `subfleet-codex`, also means "the security guard refused to arm"; rc=3 means four unrelated things across `run`/`gate`/`resume-codex` (no capacity, output collision, changes-requested, parked-thread); rc=4/5 exist for Claude but have no Codex equivalent (a Codex usage-limit exhaustion is indistinguishable from an ordinary transient failure at the exit-code level); rc=97 is real, reachable, and appears nowhere in `bin/subfleet-claude`'s own usage header.

## 3. Env var and config inventory

Five prefixes appear in the shipped source (`.venv/`, `.pytest_cache/`, and `.claude/worktrees/*` excluded as stale duplicates, not shipped surface): `SUBFLEET_*` (current, ~30 distinct names), `CLAUDE_LANE_*` (carpool-era, runner-scoped, ~14 names), `DELEGATE_*` (pre-carpool `ai-quota`/`delegate` era, 4 names), `CARPOOL_*` (any name, aliased away, not read directly anywhere), `CODEX_*` (2 names, both guard-specific, post-dating `subfleet-guard`).

| Name | Read at | Default | Generation | Still needed? |
|---|---|---|---|---|
| `SUBFLEET_STATE_DIR` | `paths.py:48` | `~/chief-of-staff/state/subfleet` | subfleet | yes, canonical state root |
| `SUBFLEET_CODEX_HOMES` | `paths.py:23,26` | `~/.codex-1..9` (existing dirs) | subfleet | yes |
| `SUBFLEET_CODEX_APP_HOME` | `paths.py:35,37` | `~/.codex` | subfleet | yes |
| `SUBFLEET_CLAUDE_DIR` | `paths.py:67` | `~/.claude` | subfleet | yes |
| `SUBFLEET_CLAUDE_JSON` | `paths.py:71` | `~/.claude.json` | subfleet | yes |
| `SUBFLEET_NOTIFY` | `paths.py:88` | `~/chief-of-staff/bin/notify` | subfleet | yes, but the name collides with `subfleet notify` (section 5) |
| `SUBFLEET_CLAUDE_ACCOUNTS` | `claude.py:55` (grepped, not fully read) | `claude-accounts.json` (inferred) | subfleet | duplicate purpose with `DELEGATE_ACCOUNTS_FILE` below, one file, two override names |
| `SUBFLEET_CODEX_ACCOUNTS` | `codex.py:139-140` (grepped) | `codex-accounts.json` (inferred) | subfleet | yes |
| `SUBFLEET_CODEX_BIN` | `codex.py:458,464` (grepped) | resolved `codex` binary | subfleet | yes |
| `SUBFLEET_ATTACHED_OK` | checked as literal command text by `bin/subfleet-hook:46`, not read via `os.environ` anywhere in Python or the runners | n/a | subfleet | works only as a hook-bypass string match, not a real runtime switch; misleading to call it an "env var" in docs |
| `SUBFLEET_ALLOW_API_LANE` | `cli.py:531`, `codex.py:87` (grepped), `bin/codex:50`, `bin/subfleet-codex:301,304` | unset/refuse | subfleet | yes, deliberate override |
| `SUBFLEET_NO_AUTOPICK` | `bin/codex:39` | unset/autopick | subfleet | yes |
| `SUBFLEET_RUN_DETACH` | `delegate.py:722,729,731,733` | unset, auto-detect via `CLAUDECODE`/`CLAUDE_CODE_SESSION_ID` | subfleet | yes |
| `SUBFLEET_RUN_ID` | `delegate.py:1013,1112`; runners `subfleet-codex:181,186`, `subfleet-claude:379,382`; `resume_codex.py:200` | unset | subfleet | yes, internal handshake |
| `SUBFLEET_RUN_LANE_LOG` | `delegate.py:1004,1110`; runners `subfleet-codex:156,190,201-202`, `subfleet-claude:231,395` | derived from `-o` | subfleet | yes, internal |
| `SUBFLEET_RUN_ORIGINAL_OUT` | `delegate.py:1012`; runners `subfleet-codex:148`, `subfleet-claude:128` | `$OUT_FILE` | subfleet | yes, internal |
| `SUBFLEET_RUN_OWNED_PROMPT` | `delegate.py:1111`; runners `subfleet-codex:225`, `subfleet-claude:241,748`; `resume_codex.py:202` | unset | subfleet | yes, internal |
| `SUBFLEET_RUN_RESUMED_FROM` | `resume_codex.py:201`; `bin/subfleet-codex:85` | unset | subfleet | yes, internal |
| `SUBFLEET_RUN_CALLER_JSON` | `cli.py:588`; written `delegate.py:1015,1017` | falls back to `notify.caller_context()` | subfleet | yes, internal |
| `SUBFLEET_RUN_DECISION_JSON` | `cli.py:613`; written `delegate.py:1005-1009` | none | subfleet | yes, internal |
| `SUBFLEET_RUN_SUBFLEET` | `bin/subfleet-codex:173` | `$HERE0/subfleet` | subfleet | test/override seam, keep |
| `SUBFLEET_CODEX_PICK` | `bin/subfleet-codex:262-263` | `$HERE0/subfleet pick codex` | subfleet | test/override seam, keep |
| `SUBFLEET_NOTIFY_MODE` | `notify.py:32,342,347` | unset, recipient's own mode | subfleet | yes |
| `SUBFLEET_TICKLE` | `tickle.py:31,272,291` | "on" | subfleet | yes |
| `SUBFLEET_TICKLE_MAX_AGE_S` | `tickle.py:33,278` | 28800 (8h) | subfleet | yes |
| `SUBFLEET_MUSTER_MAX_AGE_S` | `tickle.py:366,561` | 7200 (2h) | subfleet | yes |
| `SUBFLEET_REVIVE` | `tickle.py:629,908,916` | "on" | subfleet | yes |
| `SUBFLEET_REVIVE_MODELS` | `tickle.py:741`; `cli.py:1438` | `claude-fable-5-1,claude-opus-5` | subfleet | yes |
| `SUBFLEET_AGENT_SECRET` | `tickle.py:637` | `~/bin/agent-secret` | subfleet | yes |
| `SUBFLEET_SESSION_STORE` | `tickle.py:795` | `~/Library/Application Support/Claude/claude-code-sessions` | subfleet | test-only override |
| `SUBFLEET_CLAUDE_SETTINGS` | `hooks.py:43` | `~/.claude/settings.json` | subfleet | yes |
| `SUBFLEET_CODEX_GUARD` | `bin/subfleet-codex:485` | "on" | subfleet, post-guard (2026-08-19) | yes |
| `SUBFLEET_CODEX_GUARD_CACHE` | `bin/subfleet-guard:44,230` | `${XDG_CACHE_HOME:-~/.cache}/subfleet-codex` | subfleet, post-guard | yes |
| `SUBFLEET_CODEX_UNIFIED_EXEC` | `bin/subfleet-codex:318` | "on" | subfleet, post-guard | yes, closes a real bypass when set off |
| `CODEX_GUARD_PREFLIGHT_TIMEOUT` | `bin/subfleet-guard:224-225` | 60 | post-guard, **not** `SUBFLEET_`-prefixed despite being subfleet-owned | inconsistent naming, should be `SUBFLEET_CODEX_GUARD_PREFLIGHT_TIMEOUT` |
| `CODEX_GUARD_LOG` | `bin/subfleet-guard:176`, `bin/subfleet-guard-hook:52,104` | `~/.cache/subfleet-codex/guard-denials.log` | post-guard, same inconsistency | same fix |
| `CLAUDE_LANE_CLAUDE` | `paths.py:150,159`, `bin/subfleet-claude:164` | `~/.local/bin/claude` if executable, else `claude` on PATH | carpool era (`CLAUDE_LANE_` prefix predates subfleet, still the runner's own name) | yes, but should be `SUBFLEET_CLAUDE_BIN` |
| `CLAUDE_LANE_AGENT_SECRET` | `claude.py:78` (grepped), `bin/subfleet-claude:172` | `$HOME/bin/agent-secret` | carpool era | duplicate of `SUBFLEET_AGENT_SECRET` above (different modules, same purpose) |
| `CLAUDE_LANE_CLAUDE_MODEL` | `bin/subfleet-claude:173` | `$(dirname "$0")/claude-model` | carpool era | rename candidate |
| `CLAUDE_LANE_SUBFLEET` | `bin/subfleet-claude:175` | `$REPO_ROOT/bin/subfleet` | carpool era (was `CLAUDE_LANE_CARPOOL`) | rename candidate |
| `CLAUDE_LANE_PICK` | `bin/subfleet-claude:176` | unset | carpool era | rename candidate |
| `CLAUDE_LANE_BACKOFF` | `bin/subfleet-claude:177` | 60 | carpool era | rename candidate |
| `CLAUDE_LANE_MODEL_CHECK_RETRIES` | `bin/subfleet-claude:178` | 4 | carpool era | rename candidate |
| `CLAUDE_LANE_MODEL_CHECK_BACKOFF` | `bin/subfleet-claude:179` | 1 | carpool era | rename candidate |
| `CLAUDE_LANE_TMPDIR` | `bin/subfleet-claude:285,357` | `${TMPDIR:-/tmp}` | carpool era | rename candidate |
| `CLAUDE_LANE_DETACHED` | `bin/subfleet-claude:265,297`, `keepalive.py:163` | unset | carpool era | internal marker, keep under any name |
| `CLAUDE_LANE_OWNED_PROMPT` | `bin/subfleet-claude:266-267,298,361`, `keepalive.py:164` | unset | carpool era | internal, keep |
| `CLAUDE_LANE_DETACHED_START_DELAY` | `bin/subfleet-claude:434-435` | unset | carpool era | test-only |
| `CLAUDE_LANE_AUTH_PROBE` | `bin/subfleet-claude:498-499` | unset, real probe | carpool era | test-only |
| `CLAUDE_LANE_CURL` | `bin/subfleet-claude:501` | `curl` | carpool era | test-only |
| `DELEGATE_STATE_DIR` | `delegate.py:115`, `paths.py:63` | `~/.local/state/delegate` | pre-carpool (`ai-quota`/`delegate` module name) | should collapse into `SUBFLEET_STATE_DIR`, a second independent state root today |
| `DELEGATE_ACCOUNTS_FILE` | `delegate.py:119` | `claude-accounts.json` | pre-carpool | duplicate of `SUBFLEET_CLAUDE_ACCOUNTS` |
| `DELEGATE_SUBFLEET` | `delegate.py:216,378,399` | `_repo_bin("subfleet")` | pre-carpool (was `DELEGATE_CARPOOL`) | rename candidate |
| `DELEGATE_CODEX_RUN` | `delegate.py:1178` | `_repo_bin("subfleet-codex")` | pre-carpool | rename candidate; note the module is literally named `subfleet-codex` but the override variable is still `DELEGATE_*` |
| `DELEGATE_CLAUDE_LANE` | `delegate.py:1229` | `_repo_bin("subfleet-claude")` | pre-carpool | same |
| `CARPOOL_*` (any) | never read directly; aliased to `SUBFLEET_*` by six shell copies + one Python copy (section 1) | n/a | carpool era, superseded 2026-08-23 | drop entirely, 13 days past its own stated one-week transition |

**Config files:**

- `claude-accounts.json` (`claude-accounts.json:1-38`): `{_comment, enrolled: {email: keychain-secret-name}, accounts: [email...]}`. 14 enrolled of 17 known accounts (`:3-18` vs `:19-37`).
- `codex-accounts.json` (`codex-accounts.json:1-12`): `{_comment, protected_account: {email, account_id}, auto_reset: {enabled, headroom_floor_pct, min_interval_min}}`.
- `~/.claude/settings.json`: three hook entries and one statusLine entry, all pointing at subfleet binaries (`PreToolUse[Bash] -> bin/subfleet-hook pre-bash`, `UserPromptSubmit -> ... user-prompt`, `SessionStart -> ... session-start`, `statusLine -> bin/subfleet-statusline`, confirmed at settings.json lines 112,182,201,209-211 via grep).
- `~/.claude/cc-mirror.json` (`cc-mirror.json:1-4`): `{dead_home, archive}`, a two-key sidecar for `bin/subfleet-mirror` (not fully read).
- `~/.local/state/delegate/cooldowns.json`, `rotation.json`, `decisions.jsonl` (`delegate.py:183-200,279-283`): a second, independently-rooted state tree parallel to `state/subfleet/`.
- `state/subfleet/reset-policy.json` (`paths.py:103-105`), `state/subfleet/gates/<id>/gate.json` (`consensus.py:116-138`), `state/subfleet/runs/<id>/meta.json` (`run_ledger.py:84-90`), `state/subfleet/notices/<session>.jsonl` (`notify.py:402-408`), `state/subfleet/tickles/<session>.json` (`tickle.py:250-256`): five more independent JSON/JSONL stores under the one state root, none sharing a schema.
- `state/subfleet/traycer-profiles.json`: does not exist (Glob confirmed). Not part of today's surface.

## 4. The agent contract

Under 40 lines, using only today's surface:

```
1.  Dispatch: subfleet run --task <task> --tier <tier> -C <dir> -p prompt.md -o out.md
    (task: lookup/research/sweep/review/build/authored-prose/strategy/adjudication;
     tier: trivial/easy/standard/hard). Inside a session this returns immediately
     with a run id; the provider outlives the session.
2.  Never call subfleet codex / subfleet claude / bare codex exec directly from a
    session; the PreToolUse guard blocks it and names this replacement.
3.  Never hold the dispatch open or sleep-and-poll. To block: subfleet wait <id>
    (safe under run_in_background; if the waiter dies, the run still finishes and
    a completion notice still arrives).
4.  subfleet wait --mine = every unfinished run this session dispatched.
    subfleet runs --mine = status table. subfleet runs show <id> = output.
    subfleet kill <id> = cancel (its EXIT trap salvages and finalizes).
5.  After a restart: run `subfleet runs --mine --running` BEFORE re-dispatching
    anything. Detached runs survive account switches and may already be done.
6.  Read the ledger output at the path `subfleet runs show <id>` prints, not the
    completion notice text (the notice carries only metadata, never the body).
7.  For cross-provider continuation of THIS Claude session: subfleet handoff
    --last --to sol|terra|astra|opus -C <dir>.
8.  For continuing a finished Codex thread: subfleet resume-codex <run-id> [PROMPT].
9.  For a main/peer agreement loop: subfleet gate pr|plan ... --peer <peer>
    --main-approve --expect-<field> <value>; on changes_requested, fix and repeat
    subfleet gate continue <gate-id> --main-approve --expect-<field> <new-value>.
10. Every headless Claude-lane brief this session AUTHORS for another lane must
    include an explicit instruction not to end the turn "standing by" or poll;
    nothing downstream enforces this.
```

That is 32 lines of instruction (10 numbered items). What it does not protect against, and whether the surface could make each mistake impossible instead of merely documented:

| Gotcha | Protected today? | Could the surface make it impossible? |
|---|---|---|
| Headless lane "standing by" burning a window (2026-08-22 terra sweep) | No. `PREAMBLE_WRITE`/`PREAMBLE_AUDIT` (`delegate.py:103-105`) cover commit hygiene and audit framing, nothing about turn-ending behavior | Yes: a HEADLESS block could be appended unconditionally by `delegate.py`'s own preamble builder (`:1068-1075`) instead of relying on the calling session to remember it |
| `-o` overwrite by a second live dispatch | Partially. `_output_path_guard` (`delegate.py:286-315`) refuses a live collision for `subfleet run` callers, but a direct runner invocation (launchd scripts, `--reuse-out`) bypasses it entirely | Yes for the `run` path already; could be made unconditional (no `--reuse-out` escape) and pushed down into `run_ledger.start_run` itself so even a bypassing caller is refused |
| Read-only review lane cannot write `-o` inside `-C` | No. `-s read-only` (`bin/subfleet-claude:769-807`, `bin/subfleet-codex` sandbox arg) makes the write fail at runtime; nothing checks upfront that `-o` sits outside `-C` before launch | Yes: `delegate.py:839` already computes `sandbox`; a one-line check there could refuse the dispatch instead of letting it fail after the lane has burned budget |
| Twins: a headless revive racing a still-live session of the same id | No structural guard. `cold_sessions()` excludes live ids (`tickle.py:564,582`) but `_auto_revive_pass` only re-checks *other* detached revives before launch (`tickle.py:1060-1069`), not whether the target session itself came back alive during the probe window | Yes: re-run the same `cold_sessions()` live-id check immediately before `revive_session()`, under the same lock, not just the detached-revive census |
| Second live instance of one session id after a restart | No. `find_session()` explicitly documents "a restarted session leaves an old row behind for a while" and only ranks which one to notify (`notify.py:138-172`); it never kills or deregisters the stale process | Only partially: killing a process this module did not launch is a much bigger blast radius than anything else here; more realistically, the guard hook could deny a git-mutating command from a session whose registry row is not the winning one |
| `pgrep -f <model-id> \| kill -9` killing unrelated runs | No. `subfleet kill <id>` is scoped and safe (`run_ledger.py:927-938`, signals the pid's own process group), but nothing stops an out-of-band `pkill`/`kill -9` by substring | Yes in spirit: the never-rules guard (`bin/subfleet-guard-hook`, `docs/guard.md`) already denies unscoped `find`/`rg`; a tenth rule denying `pkill -f`/`kill -9 $(pgrep ...)` patterns would close this the same way |

## 5. Naming and vocabulary audit

| Term | Current meaning(s) | Verdict |
|---|---|---|
| lane | dispatch identity bound to one account (`README.md:10-12`) | canonical, keep |
| home | the filesystem directory backing a Codex lane (`paths.py:15-30`); also names `~/.codex`, the one directory that is explicitly *not* a lane | overloaded within Codex only; demote to an internal field, never a user-facing synonym for "lane" |
| account | the provider-side identity a lane or login is bound to (`claude-accounts.json`, `codex-accounts.json`) | canonical, keep |
| login | (noun) the desktop app's currently signed-in identity, explicitly opposed to "lane" (`README.md:10`); (verb) the CLI command that stages re-auth (`subfleet login codex 3`) | noun/verb overload; split in a rebuild |
| profile | not used anywhere in the audited surface; appears only in the unmerged Traycer branch context | not real yet; do not introduce without a concrete referent |
| run | (noun) a durable ledger entry (`run_ledger.py`); (verb) `subfleet run`, the dispatch front door | dual use, acceptable (matches common `docker run`/`ps` pattern) but worth naming explicitly in docs |
| dispatch | the act of sending a prompt to a lane, verb-only, no matching noun/listing command | synonym of "run" as a verb; fold into one word in a rebuild |
| attempt | one internal retry of a runner's provider call (`bin/subfleet-claude` attempt counter, `bin/subfleet-codex` same); invisible at the ledger level, only in `.err.log`/`.lane.log` text | real gap: promote to a structured ledger field instead of leaving it as unstructured log text |
| session | exclusively a Claude Code interactive process (`notify.py:111-172`); also silently covers headless lane runs, distinguished only by a boolean (`notify.py:194-195`, `lanes.is_lane_session`) | overloaded: "session" should mean interactive-only; headless dispatches should use "attempt"/"run" |
| task | the semantic work-type enum (`delegate.py:76-79`) | canonical, keep |
| tier | the capability-level enum (`delegate.py:80`) | canonical, keep |
| class | the legacy coarse task classifier (`-t`, `delegate.py:126-142,321-322`) **and**, unrelatedly, the permission-attestation category on a notice (`mode_class`, `notify.py:32,51,102,342,355-357`) | damaging overload, same word for two unrelated concepts; rename one (permission category to something like "trust level") |
| gate | the main/peer agreement flow (`consensus.py`) | canonical, keep; note near-collision with "guard" (the never-rules hook) even though the code never conflates them |
| peer | the reviewing agent in a gate (`consensus.py:36`) | canonical, keep, scoped to gate only |
| handoff | cross-provider continuation of a Claude session via a redacted brief (`handoff.py`) | canonical, keep, mechanically distinct from resume/revive |
| revive | headless restart of a cold (process-dead) Claude session, internally built on the CLI's own `--resume` (`tickle.py:819-841`) | keep the mechanism, but "resume" and "revive" as sibling English words obscure that they serve different providers (`resume-codex` = Codex thread, `revive` = Claude session) |
| tickle | a nudge (message push, no new process) for a session whose process is alive but idle after a restart (`tickle.py:1-38`) | keep the mechanism; the metaphor is good once explained but not discoverable from the CLI verb list alone |
| muster | the superset of tickle that also nudges completed-but-recently-idle sessions (`tickle.py:344-384`) | real functional superset, but muster/tickle/revive are three unrelated English roots for one family of "wake a session" behaviors, documented as such by the muster skill itself |
| salvage | the git commit-tree snapshot on runner exit (`bin/subfleet-claude:319-354`, `bin/subfleet-codex:495-530`) | canonical, keep, single clean meaning |
| notice | a parked/pushed completion message for a dispatching session (`notify.py:402-473,575-647`) | confirmed collision: `subfleet notify` (push into a session inbox) and the unrelated CoS-wide `bin/notify` (Telegram/email, `paths.py:87-88`) share the identical verb name for two different programs |
| inbox | the Claude Code harness's own cross-session socket/registry (`notify.py:1-19`), which subfleet is a client of, not the owner of | correctly documented as harness-owned today; keep the distinction explicit in a rebuild too |

Canonical vocabulary for a rebuild: **lane, account, task, tier, gate, peer, salvage, session (interactive only), attempt (promoted to a ledger field)**. Retire as separate CLI-facing words: **home** (Codex-internal field), **class** (rename the permission one), **login** (split noun/verb), **tickle/muster/revive** (collapse into one verb family, see section 6), **notify** (rename to avoid the `bin/notify` collision).

## 6. Proposed minimal surface for a rebuild

**One config file**: `state/subfleet/fleet.json`, replacing `claude-accounts.json`, `codex-accounts.json`, `~/.local/state/delegate/cooldowns.json`, `rotation.json`, and `reset-policy.json` with one schema (`accounts: {claude: [...], codex: [...]}`, `auto_reset: {...}`, `cooldowns: {...}`). `~/.claude/settings.json` stays separate; it is owned by the Claude Code harness, not by subfleet, and cannot be folded in.

**One env var prefix**: `SUBFLEET_*` only. Every `CLAUDE_LANE_*`, `DELEGATE_*`, and `CARPOOL_*` name is either dropped or renamed under `SUBFLEET_`. `CODEX_GUARD_*` becomes `SUBFLEET_CODEX_GUARD_*`.

**One state root**: `SUBFLEET_STATE_DIR`, absorbing `DELEGATE_STATE_DIR`'s contents.

**Exit codes** (one scheme, reused everywhere a code is returned): `0` ok, `1` operational error, `2` invalid input, `3` blocked/no capacity now (retry later), `4` blocked, needs a human or config action (subsumes today's "changes requested" and today's "blocked", since both mean "a person must act before this proceeds"), `5` action failed or unverified (subsumes merge failure and queued), `124`/`125` reserved fleet-wide for `wait`-style timeout/orphan, never reused for anything else.

**Verbs** (13, versus today's 26 public + 7 hidden):

| Current verb | Rebuild verb | Notes |
|---|---|---|
| `status`, `capacity`, `errors` | `status [--json] [--errors] [--cached]` | folded into one |
| `run` | `run` | unchanged, absorbs `pick`'s routing internally |
| `codex`, `claude` (pass-through) | dropped (why: never a legitimate direct entry point; already hook-blocked from sessions; a rebuild should make them unreachable, not just discouraged) | |
| `pick codex\|claude` | dropped (why: pure internal routing seam for `run`; exposed today only for diagnostics, which `run --dry-run --why` already covers) | |
| `runs`, `runs show`, `runs reap` | `runs [show\|reap]` | unchanged |
| `wait` | `wait` | unchanged |
| `kill` | `kill` | unchanged, made to guarantee "kill takes the tree" always, not opt-in via `--grace` |
| `resume-codex` | `resume <run-id>` | provider-generic name; still Codex-only mechanically today |
| `handoff` | `handoff` | unchanged |
| `gate pr\|plan\|continue` | `gate` | unchanged, the best-designed part of today's surface |
| `login codex <N\|app>` | `reauth codex <N\|app>` | renamed to resolve the login noun/verb overload |
| `reset codex` | `reset` | unchanged |
| `enroll` | `enroll` | unchanged |
| `sessions` | `sessions` | unchanged |
| `notify` | `ping <session>` | renamed to stop colliding with the CoS-wide `bin/notify` |
| `hooks install\|uninstall\|status` | `hooks` | unchanged, unavoidable Claude Code integration |
| `tickle`, `muster`, `revive` | `continue [--scope interrupted\|idle\|dead] [--session ID\|--all]` | one verb, default scope = today's `muster` behavior (the superset) |
| `watch`, `keepalive`, `mirror`, `brief` | `ops watch\|keepalive\|mirror\|brief` | moved under one namespace, never documented to agents, launchd-only |
| `_session-hook`, `_tickle`, `_canonical-model`, `_api-lane-check`, `_record-lane-run`, `_record-run`, `_record-codex-cooldown` | dropped as CLI subcommands (why: these are ledger/accounting RPCs, not verbs; a rebuild should expose them as in-process library calls or a private local socket, not `argparse.SUPPRESS`d subcommands of the same public binary an agent can discover via shell history or `--help` archaeology) | |
| `carpool` (top-level alias) | dropped (why: 13 days past its own one-week transition promise) | |

This drops the public surface from 26 top-level verbs (plus 7 hidden) to 13, collapses three env-var generations into one, collapses six independent state/config files into one, and collapses eight distinct exit-code meanings for "3" and "4" alone into two.
