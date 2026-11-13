# D-FILL1 command and result ledger

This file records the build and replay commands and results. The main report contains the conclusions. All repository-relative paths refer to the assigned worktree. `$TMPDIR` below denotes the permitted task temp directory without reproducing its environment value. Each pytest invocation exported that permitted temp root and set `UV_CACHE_DIR="$TMPDIR/uv-cache"` before running the displayed command. Inline Python audits used only the copied offline export, opened with SQLite `mode=ro` and `PRAGMA query_only=ON`; their distinct operations and results are enumerated below.

## Read-only source inventory

The replay subtask read the following sources:

- `/Users/maxghenis/reviews/subfleet-hub/fill-first-2026-11-07/design.md` — hub design, requested ordering and measurement claim.
- `tools/replay_weekly_ranking.py` — existing replay model, snapshot reconstruction and comparator, then the edited implementation during review.
- `docs/reports/2026-10-03-rank-earliest-reset.md` — prior metrics, cost assumptions and uncalibrated-model caveat.
- `subfleet/scheduler.py` — original and new usage-ranking functions, comparator, preparation and candidate-detail semantics.
- `subfleet/capacity.py` — `fresh_provider` and its TTL/reset predicates.
- `tests/unit/test_scheduler_weekly.py` — generated-fleet conventions and existing ranking properties.
- `tests/unit/test_test_module_names.py` — test-module uniqueness rule.
- `tests/conftest.py` — isolated test homes, credential stripping and state guards.
- `pyproject.toml` — Python/dev dependency requirements and pytest configuration.
- `docs/reports/2026-11-08-fill-first-replay.md` — replay evidence draft, read again to construct this ledger.

Only file names were returned by the worktree-wide `rg` searches listed below. The caller checkout, live store, native app state and daemon were not read by these source-inspection commands.

| Command | Result |
| --- | --- |
| `pwd && sed -n '1,240p' tools/replay_weekly_ranking.py && cat /Users/maxghenis/reviews/subfleet-hub/fill-first-2026-11-07/design.md && cat docs/reports/2026-10-03-rank-earliest-reset.md` | Read assigned location, replay introduction, hub design and prior report. Tool display was truncated; later focused reads covered the replay implementation. |
| `sed -n '230,720p' tools/replay_weekly_ranking.py` | Read remaining cost attribution, snapshot lookup, comparator, event loop and report output. |
| `sed -n '550,780p' subfleet/scheduler.py && rg -n 'replay_weekly\|Replay\|Usage' tests/unit && cat pyproject.toml` | Read scheduler context, located related test references and read dependency configuration. |
| `rg -n '^def (rank_key\|ranking_usage\|ranking_reading_age)' subfleet/scheduler.py && sed -n '1,90p' tests/unit/test_scheduler_weekly.py && rg --files -g AGENTS.md -g '*replay*' -g '*names*'` | Located ranking functions, read weekly test helpers and inventoried matching worktree file names. |
| `sed -n '900,970p' subfleet/scheduler.py && sed -n '1250,1295p' subfleet/scheduler.py && sed -n '1,120p' tests/unit/test_test_module_names.py && git status --short` | Read base ranking usage/key and test naming requirements; inspected worktree status. |
| `rg -n '^def fresh_provider' subfleet/capacity.py && sed -n '175,200p' subfleet/capacity.py && sed -n '1,100p' tests/conftest.py` | Located fresh-reading predicate and read test isolation safeguards. |
| `sed -n '239,258p' subfleet/capacity.py` | Confirmed numeric finite provider evidence, 120-second TTL and future-reset freshness conditions. |
| `sed -n '955,1005p' subfleet/scheduler.py && git diff -- tools/replay_weekly_ranking.py && git status --short -- tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py docs/reports/2026-11-08-fill-first-replay.json docs/reports/2026-11-08-fill-first-replay.md` | Reviewed new production fill tie behavior and replay changes. Found and subsequently fixed replay's simultaneous-observation tie mismatch. |
| `sed -n '1,240p' docs/reports/2026-11-08-fill-first-replay.md` | Read completed replay evidence and command inventory while creating this ledger. |

## Offline copy and audit commands

| Command or inline Python operation | Result |
| --- | --- |
| `mkdir -p /private/tmp/subfleet-fill-first-replay && cp /Users/maxghenis/reviews/subfleet-hub/fill-first-2026-11-07/replay-input.sqlite3 /private/tmp/subfleet-fill-first-replay/replay-input.sqlite3`, followed in the same shell invocation by `python3 - <<'PY'` to hash the copy and inspect `sqlite_master` / `PRAGMA table_info` | Offline copy created: 55,074,816 bytes; SHA-256 `d32bef807da530aca0ff3cca9cc9161a089b2dedbe0f6066da89156c958eff06`. Confirmed `schema_version`, `lanes`, `readings`, `jobs`, `attempts`, `decisions` tables and expected lane/reading columns. |
| `python3 - <<'PY'`: group numeric provider/stale-provider readings by `(lane_id, scope, window)`, order by `(observed_at, reading_id)`, compare consecutive rows with identical known reset and right observation before reset | 40,972 pairs, zero decreases: Claude 982 (weekly 704; five-hour 278), Codex weekly 39,990. |
| `python3 - <<'PY'`: same grouping/reset equality without the open-window condition, plus `SELECT min(observed_at),max(observed_at) FROM readings` | 40,973 pairs, zero decreases. All-reading bounds were 2026-10-24T00:05:15Z through 2026-11-08T00:07:15Z; the first numeric row is six seconds later. |
| `python3 - <<'PY'`: count numeric readings by label and repeat exact-reset grouping using `(lane_id,scope,window)`, `(lane_id,window)` and `(lane_id,scope,window,label)` | All 41,803 numeric rows were `provider`; all three groupings yielded 40,973 equal-reset pairs and zero decreases. |
| `python3 - <<'PY'`: compare all consecutive pairs, preceding-reset-open pairs, current-reset-open pairs and both-open pairs, and inspect a few changed-reset examples by lane id only | 41,781 total consecutive pairs; 41,598 before the preceding reset; 41,780 before the current reset; 41,598 before both. The preceding-reset-only condition admitted 18 decreases where reset clocks changed, so the stated same-window measurement uses exact reset identity. Design's 41,399 count was not reproduced. |
| `python3 - <<'PY'`: inventory recorded candidate-detail field names using a `Counter` | First attempt raised `TypeError` because `Counter.update(d)` tried to add dictionary values including nulls. No file changed. |
| Corrected `python3 - <<'PY'` candidate-detail audit using `Counter.update(d.keys())` | Succeeded. Exported candidate details had no `desktop` field and zero such flagged candidates; historical comparator continues to use the supplied lane snapshot's desktop flag. Only field names and a count were printed. |
| `python3 - <<'PY'`: count equal observation-time pairs within each lane/scope/window and non-numeric/non-finite utilizations | Zero equal-timestamp pairs and zero non-numeric/non-finite utilization values. Conservative tie handling therefore does not change this export's replay results. |

The permanent `monotonicity` helper in `tools/replay_weekly_ranking.py` records the exact open-window audit in replay JSON for both the full export and the requested cutoff. Through 2026-11-07T23:51:40Z it counts 41,759 numeric rows and 40,930 qualifying pairs, with zero decreases. Full-export numeric bounds are 2026-10-24T00:05:21Z through 2026-11-08T00:07:15Z.

## New replay tests

All five runs used the permitted task temp root, its `uv-cache`, a fresh `--basetemp` and no xdist workers. The root agent had completed `uv sync --group dev` before these runs. This subtask did not run the legacy or whole test suite.

| Command | Result |
| --- | --- |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/unit/test_replay_weekly_fill.py --basetemp "$TMPDIR/replay-tests-1" -q` | 14 passed in 0.34 s. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/unit/test_replay_weekly_fill.py --basetemp "$TMPDIR/replay-tests-2" -q` | 15 passed in 0.45 s, after adding the six-experiment configuration check. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/unit/test_replay_weekly_fill.py --basetemp "$TMPDIR/replay-tests-3" -q` | 15 passed in 0.60 s, after strengthening the Codex overlapping-load check. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/unit/test_replay_weekly_fill.py --basetemp "$TMPDIR/replay-tests-4" -q` | 19 passed in 0.78 s, after adding conservative simultaneous-observation regressions. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/unit/test_replay_weekly_fill.py --basetemp "$TMPDIR/replay-tests-5" -q` | **20 passed in 0.39 s**, after removing live-store path resolution and adding its refusal regression. |

## Full replay runs and checks

Every replay used `/private/tmp/subfleet-fill-first-replay/replay-input.sqlite3`, `--days 7` and `--until 2026-11-07T23:51:40Z`. All six requested, deduplicated variants completed on every run; all 309 attempt snapshots were reconstructed and none was missing a decision. Only Claude's order/spread varied; Codex remained reset/spread 2.

| Command | Result |
| --- | --- |
| `python3 tools/replay_weekly_ranking.py /private/tmp/subfleet-fill-first-replay/replay-input.sqlite3 --days 7 --until 2026-11-07T23:51:40Z --output docs/reports/2026-11-08-fill-first-replay.json --progress` | First six-variant run completed. Loaded 20,814 readings and reconstructed 309 snapshots; 85 Claude and 224 Codex arrivals. |
| `python3 tools/replay_weekly_ranking.py /private/tmp/subfleet-fill-first-replay/replay-input.sqlite3 --days 7 --until 2026-11-07T23:51:40Z --output /private/tmp/subfleet-fill-first-replay/second.json > /private/tmp/subfleet-fill-first-replay/second.log`, followed by `python3 - <<'PY'` report equality, placement/provider/limit/account accounting, Codex equality and no-email-marker assertions | Second report equaled the first apart from the newly added snapshot fingerprint. All six variants passed accounting; Codex results were identical across variants; no email markers. The JSON with fingerprint was copied into the repository artifact. Printed the six-row Claude metric table. |
| `python3 tools/replay_weekly_ranking.py /private/tmp/subfleet-fill-first-replay/replay-input.sqlite3 --days 7 --until 2026-11-07T23:51:40Z --output docs/reports/2026-11-08-fill-first-replay.json > /private/tmp/subfleet-fill-first-replay/final.log`, followed by `python3 - <<'PY'` variant equality and no-email-marker assertions | Third run after generalizing hourly metadata reproduced all six preceding variant records exactly; no email markers. |
| `python3 tools/replay_weekly_ranking.py /private/tmp/subfleet-fill-first-replay/replay-input.sqlite3 --days 7 --until 2026-11-07T23:51:40Z --output docs/reports/2026-11-08-fill-first-replay.json > /private/tmp/subfleet-fill-first-replay/review-final.log`, followed by `python3 - <<'PY'` variant equality, actual-provider limit and no-email-marker assertions | Fourth run after conservative fill evidence ties and provider outcome counts again reproduced all six variant records. Confirmed actual recorded limits: Claude 3/85, Codex 0/224; no email markers. |
| `git diff --check -- tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py && python3 -m py_compile tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py && git diff --stat -- tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py` | Early whitespace and compilation checks passed; diff stat inspected. Untracked new files are absent from ordinary diff stat. |
| `python3 -m py_compile tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py` and `git diff --check -- tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py docs/reports/2026-11-08-fill-first-replay.md`, issued after the fourth replay in the same shell invocation | Passed. |
| Final `python3 -m py_compile tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py && git diff --check -- tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py docs/reports/2026-11-08-fill-first-replay.md`, followed by `python3 - <<'PY'` JSON no-email-marker and Codex consistency assertions | All final syntax, whitespace, JSON privacy and Codex consistency checks passed. |

The model is uncalibrated: today's-rule simulation predicts Claude 7/85 limits versus 3/85 actually recorded, and Codex 2/224 versus 0/224 recorded. The main report and replay artifact retain these limits on interpreting variant differences. Replay metrics remained unchanged after the final safety-only live-path-guard adjustment; that adjustment is covered by the twentieth test.

## Commit attempt

| Command | Result |
| --- | --- |
| `git add tools/replay_weekly_ranking.py tests/unit/test_replay_weekly_fill.py docs/reports/2026-11-08-fill-first-replay.json docs/reports/2026-11-08-fill-first-replay.md && git commit -m "Add offline Claude fill ranking replay and packing metrics"` | Exit 128 at named `git add`: shared Git metadata denied creation of `index.lock`. The commit command did not execute; files were preserved. The root agent authorized this attempt after reporting the same restriction. No alternate Git directory, history rewrite, stash or push was used. |

No daemon, provider API, native app, live-store connection, full-suite run or background process was started by this subtask. The root agent owns the combined report, final commit attempts and collection of preserved files.

Ledger-only check: `git diff --no-index --check /dev/null docs/reports/2026-11-08-fill-first-commands.md` produced no whitespace diagnostics; exit 1 reflects the new file's differences from `/dev/null`.


## Build reads and preparation

Source inspection used read-only `cat`, `sed`, `head` and `rg` over all required files: hub design; scheduler, policy/default JSON, picker, CLI, route-check; weekly tests; prior report and replay; C-11.3, C-10.3, C-23.37 and C-26.2. Follow-up reads covered capacity, store, daemon, timers/actions, test isolation/conftest, routing strategies, frozen references, policy/scheduler/turn/picker/route/horizon tests and new files. Searches stayed in permitted locations. AGENTS.md was absent. One exploratory `rg` returned exit 2 because `.agents`/`.codex` operands were absent; other source searches completed normally. Large tool displays were followed by focused reads.

| Command or operation | Result |
| --- | --- |
| `pwd`, `git status --short`, `git branch --show-current`, `git rev-parse HEAD`, `rg --files` inventories | Clean detached checkout at 266c3ab8b0be2548fbe0abc77273fa1ccd7fa9a9; test/dependency inventory read. |
| `git switch -c feat/fill-first-217` | Exit 128: branch exists. |
| `git switch feat/fill-first-217` | Exit 128: branch attached to caller worktree; no caller files accessed. |
| `git switch --ignore-other-worktrees feat/fill-first-217` | Exit 128: denied shared index.lock. Remained detached. |
| Set TMPDIR with `getconf DARWIN_USER_TEMP_DIR`, set UV_CACHE_DIR under it; `uv sync --group dev` | Exit 2: default cache denied by sandbox. |
| Set TMPDIR to permitted task child, UV_CACHE_DIR to its uv-cache; `uv --cache-dir "$UV_CACHE_DIR" sync --group dev` | Passed. Worktree .venv uses CPython 3.15.2, pytest 9.1.1, Hypothesis 6.168.1 and locked dev dependencies. Later tests used this permitted child/cache and a fresh basetemp, at most four workers per run. |
| `git show 266c3ab8b0be:subfleet/scheduler.py`; Python AST extraction and SHA-256 | Froze base evaluator and checked module/function hashes; runtime tests need no history. |
| `rg -n` each new decision key; `rg -l` exact candidate/detail/rejection/evaluation comparisons | Audited new fields and historical exact comparisons; explicit reset references and map/hash expectations updated. |
| Python AST call inventory, then regex union for rank_key/ranking_usage/evaluate and policy modules, with CLI/picker/capacity | Initial 65-file scope saved; launch-env policy added to final 66-file inventory. Retest inventory includes changed cases and direct users of the changed scheduler fixtures. |
| `python3 -m py_compile` edited modules/tests/tools, repeated scoped/whole `git diff --check`, status/diff/stat/numstat reads | Compilation/whitespace checks passed; diffs and new untracked files reviewed. |
| Read-only Python SQLite accessor benchmark on task-temp export copy; five calls/accessor and EXPLAIN | Existing138 rows/0.029042s median; fill66 rows/0.042736s. SQLite inlined provider CTE. |
| Python/apply_patch edits and report/inventory assembly | Files changed only in assigned workspace. One attempted ledger-write shell command was blocked by a text-matching shared-worktree hook on quoted documentation, before executing; reissued safely with simpler text. One apply_patch header edit missed a full-paragraph context and made no change; Python substring edit succeeded. |

## New tests and mutants

`UVPYTEST` denotes `uv --cache-dir "$UV_CACHE_DIR" run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4`, with `--basetemp "$TMPDIR/<name>"` and the listed files. Captured logs were inspected with read-only tail/rg. Replay-only commands above used no xdist.

| Command suffix / basetemp | Result |
| --- | --- |
| test_policy_fill.py / policy-fill-1 | 43 passed/3.10s. |
| test_scheduler_fill.py / fill-new-laws-1 | 16 passed,3 failed: test changed first sorted row (five-hour), corrected to seven-day. |
| Same / fill-new-laws-2 | 19 passed. |
| Same / fill-new-laws-3 | 32 passed,2 failed: test baseline already below custom reserve; corrected fixture. |
| Same / fill-new-laws-4 | 34 passed/7.43s. |
| Same / fill-new-laws-5 | 38 passed/10.88s. |
| test_fill_routing.py / fill-routing-1 | 4 passed,1 failed/5.34s: test expected a probe for read-only non-hard work. Corrected to compare original predicate and exercise workspace-write. |
| Same / fill-routing-2 | 4 passed,1 failed/1.62s: test passed dataclass tuples to JSON CLI helper. Corrected serialization. |
| Same / fill-routing-3 | 5 passed/8.10s. |
| test_fill_evidence.py test_policy_fill.py / fill-evidence-1 | 56 passed/6.40s. |
| All five new files: test_scheduler_fill.py test_policy_fill.py test_fill_routing.py test_fill_evidence.py test_replay_weekly_fill.py / fill-new-combined-1 | 118 passed/15.97s. All paths are under tests/unit/. |
| `uv --cache-dir "$UV_CACHE_DIR" run --no-sync --with 'pytest-xdist>=3.6' python tools/fill_rank_mutations.py --basetemp "$TMPDIR/fill-mutants-1" --output docs/reports/2026-11-08-fill-first-mutants.json` | 6/6 killed by assertions. Each child used `python -m pytest -q -x -p no:cacheprovider --basetemp <fresh-dir> tests/unit/test_scheduler_fill.py::<test>`. Pristine bytes restored/hash checked after each; artifact records exact child commands/tests/hashes. |
| Same five new files / fill-new-final-1 | 120 passed/11.68s, including final replay and direct per-provider ranking-map tests. |

## Requested regression and contract tests

New tests ran first, requested scheduler/caller/policy files second, five contract/registry modules last. The full suite was never invoked.

| UVPYTEST suffix / basetemp | Result |
| --- | --- |
| @docs/reports/2026-11-08-fill-first-test-files.txt (initial65-file inventory) / fill-requested-1 | 2611 passed,12 failed,2 skipped/346.85s. All failures were historical reset/frozen-reference assumptions and two bundled-policy hashes; test scope/default witnesses corrected. No production edits were needed. |
| @docs/reports/2026-11-08-fill-first-retest-files.txt / fill-requested-retest-1 | 356 passed/31.49s, including missing launch-env policy tests. |
| tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py tests/unit/test_protocol_registry.py / fill-contract-1 | 427 passed/26.52s. |

## Build staging attempts

All attempts named files and failed128 creating shared index.lock; commits never executed. Files were preserved. No alternative Git directory, caller edit, history rewrite or push was used.

| Named files/intended coherent step | Result |
| --- | --- |
| Scheduler, policy/default JSON, policy tests | Named staging denied. |
| CLI/picker/horizons/daemon/fill routing tests, then acceptance contract | Two named staging attempts denied. |
| tests/unit/test_scheduler_fill.py tests/unit/test_scheduler_weekly.py tests/fixtures/fill_first/scheduler_266c3ab8b.py; intended commit message test: prove fill ranking invariants against frozen release217 | Named staging denied; commit did not execute. |
| Auxiliary scheduler/policy/store/capacity/daemon/route-check pipeline and evidence tests | Named staging denied. |
| Replay tool/tests/JSON/integration draft | Named attempt detailed above denied. |
| Final source/tests/docs/artifacts (35 named files) | Exit128: shared index.lock denied. No commit executed; final files preserved. |


Final review found float epoch conversion could collapse distinct microsecond resets in very distant windows. Replaced only fill observation tie comparison with exact datetime/timedelta ordering, added a far-window witness, and repeated the ordered validation because production source changed. Four suspected skip-source cases were rerun with `UVPYTEST -rs` (two policy-sandbox path cases and two weekly-base differentials), basetemp fill-skip-audit-1: 4 passed/1.97s, so these did not explain the earlier skips. `git cat-file -e` for both historical commits64b4175f andf3bffcea exited0. The final requested run enables skip summaries.


| Final-source repeat / basetemp | Result |
| --- | --- |
| UVPYTEST all five new modules / fill-new-final-2 | 121 passed/5.58s. Exact far-window observation tie regression added; reset/fill invariants remain unchanged. |
| Same mutation command, fill-mutants-2 | 6/6 killed again; final-source pristine hashes verified and artifact refreshed. |
| UVPYTEST -ra @docs/reports/2026-11-08-fill-first-test-files.txt / fill-requested-final-1 | 2651 passed,2 skipped/231.78s. Both skipped importer real-daemon cases require sandbox-denied macOS boot/process inspection. |
| UVPYTEST -ra five specified final contract modules / fill-contract-final-1 | 427 passed/20.01s. |


Final read-only checks: AST/SHA extraction confirmed current ranking_usage source still matches its frozen-base hash. JSON assertions confirmed six replay variants, identical Codex metrics and no email markers. Mutation-artifact source hashes matched the final scheduler/policy bytes; command temp paths were normalized to $MUTATION_TEMP. The mutation runner's display-only normalization compiled successfully. Contract cross-references in C-11.7 and C-23.37 now name the configured reset/fill order; their predicates were not changed. The main report and ledger were read back, pending markers searched, and whole-worktree whitespace/status checks repeated.

Final named staging used the 35 paths reported by the final changed-file inventory; it again failed128 before any commit. Final status/head, changed-line/new-file whitespace, source compilation, report-link existence, JSON parsing/privacy and mutation source-hash checks passed. No pending task process remains.

A final whole-file whitespace assertion initially flagged pre-existing trailing spaces in test_policy.py. The corrected audit compared trailing-space lines against HEAD and checked every new file separately; inherited lines were preserved. The remaining final artifact assertions then passed.
