# Desktop org: routing follows the desktop app's own account (C-10.3, C-10.9)

D-DESKTOP-ORG1. Base `266c3ab8b`. Code, tests and contract are at `4ab996095`; this report is the commit on top of it.

**Branch.** `fix/desktop-org-217` is checked out in the caller's worktree, so `git switch` refused it and every commit here sits on a detached HEAD. The hub needs to move `fix/desktop-org-217` to the head named at the end of this report.

**Timing.** The contract commit landed at 2026-11-07 23:42:51 UTC. The session was then suspended until 2026-11-13 23:46 UTC, so the brief's 4-hour mark (about 01:50 UTC on 2026-11-08) passed while it was down. After resuming I ran only short, bounded checks: the eight mutants, the named scheduler, policy and alert files, and a final run of the new tests. Then I stopped and wrote this report. The broad group-2 sweep did not run (see "What didn't run").

## What changed

- **The hook report** (`subfleet/hooks.py`, `subfleet/desktop_app.py`).
  - Fires on `SessionStart` and `UserPromptSubmit` only, outside a Subfleet launch (`launched_by_subfleet()` is None), and only when `CLAUDE_CODE_ENTRYPOINT` is `claude-desktop`.
  - Reads exactly `CLAUDE_CODE_USER_EMAIL`, `CLAUDE_CODE_ACCOUNT_UUID` and `CLAUDE_CODE_ORGANIZATION_UUID`, and no other name.
  - Validates them: an email with exactly one `@`, a part on each side, no whitespace or control character and at most 254 characters; canonical uuids, lower-cased.
  - Sends `{email, account_uuid, org_uuid, session_id, observed_at}` by the new verb `desktop.report`. The session comes from the hook payload only.
  - Uses one round trip (`Client(..., retry_busy=False, timeout=1.0)`). Every failure is swallowed, so the hook's stdout, stderr, notices and exit code are what base gives.
  - Never logs, prints or stores the environment.
  - Registration: `desktop.report` is in `protocol.OPS` and in a new `descriptors.OBSERVATION_OPS`, which `read_only()` honours, so a report whose client has gone is not run (C-16.7). It is not folded into `notice.pending`.
  - `tests/conftest.py` pops `CLAUDE_CODE_ENTRYPOINT` at configure, so a suite started from a desktop terminal never reports.
- **The event** (`subfleet/daemon.py`).
  - `desktop.report` validates again. A malformed report is refused with `invalid-args` and writes nothing; a future `observed_at` is clamped to now.
  - Under one lock, it writes a `desktop.account` event with the report plus `source: "hook"`, but only when the email (case aside), account or org differs from the newest such event, or that event is over an hour old.
  - Readers take a bounded query (`LIMIT 4000`) on the events kind index and reparse only when `MAX(event_id)` for the kind moves. No schema change.
- **The app file reader** (`desktop_app.read_app_usage`, `home_paths.desktop_usage_path`).
  - Default path `~/Library/Application Support/Claude/plan-usage-history.json`; `SUBFLEET_DESKTOP_USAGE` overrides it. That name is registered in `SAFE_HOME_PATHS` as a test-only override, so tests point it into their own folder.
  - Reads with `read_regular` and a 4 MiB cap, and re-reads only when `stat` changes.
  - Version 2 only. Another version, a parse error, a non-regular, unreadable or missing file, or no usable sample is no answer, logged once per change of file state.
  - The answer is the newest sample: greatest `t`, equal times broken by org. It gives the org, the time, the weekly percent (`u.sd`) and when the trailing run naming that org began.
- **What it read from the copy.** I copied the hub's `plan-usage-history.copy.json` to the temp folder and ran the reader on it at the final head.
  - Answer: yes, version 2.
  - Newest sample: the app account's org (prefix left out: this repository is public), at `2026-11-07T23:34:56.429Z`.
  - The trailing run naming that org began at that same sample.
  - Weekly 6.0%. No log line.
  - This matches the design record's 23:34:56Z switch to an org at 6%.

## C-10.3's new rule and the fallback

While either signal answers (the usage file, or at least one `desktop.account` event), it alone decides which lanes are `desktop`:

- The *app's org* is the newest file sample's org, else the newest event's org (by `observed_at`, equal times by event id).
- An org's *email* is the email of the newest event naming that org, at any age.
- A lane is *linked elsewhere* when its email (`account_key` `claude:<email>`) appears in an event of another org and in none of the app's org.
- When the app's org has an email E, exactly the Claude lanes whose `account_key` equals `claude:E`, case aside, are `desktop`. If no lane has that account, none is.
- When it has no email, no lane is `desktop`, and every Claude lane not linked elsewhere gets `desktop_possible`.
- The profile probe and `~/.claude.json` then decide nothing for routing. `doctor` still shows them (its identity live checks and the new row) and says when they name another account than the app's.
- The in-use rule keeps its predicate and applies to the app's account. The contract now states the cost: while the signals answer, a terminal session on the CLI login is not protected by C-10.3 unless that login is the app's account.

**Fallback.** With neither signal, `resolve()` returns None. `DesktopIdentity.app` stays None, and the view, decisions and ranking equal base exactly (invariant 6).

**Ranking.** `rank_key` inserts `bool(detail.get("desktop_possible"))` right after `desktop` in both comparators, and nothing else moves. `judge_lane` sets the field, and `desktop_possible` joins `LANE_FACTS`.

**Status, doctor, alerts.**
- Status: one line, for example `desktop app: org <prefix> (app file) on claude-2; app file <time> (weekly 12%); hook <time>`. It reads "no lane" or "not linked yet" when apt, and is absent with neither signal. It appears in `daemon.status`, `cli.format_status`, `render.status` and the timers snapshot.
- Doctor row "desktop app's account linked to a lane": pass when an event links the app's org and the newest event is under 24 h old. Otherwise unknown, with the fix `subfleet daemon install --hooks`.
- Two warn alerts:
  - `desktop-app-unlinked:<prefix>`: the file's org has stayed unlinked for 10 minutes while the desktop login is in use, with unknown counting as use.
  - `desktop-app-disagree:<prefix>`: events newer than the file's newest sample have named another org for 10 minutes.

**Contract.**
- C-10.3 amended.
- C-10.9 added.
- C-11.3 now names `desktop_possible`.
- C-16.7's read-only list gains `desktop.report`.
- C-26.13 says neither event sends the app's account.
- A change-log line was added at the top of version 3's changes, without a PR number.

## Invariants and their tests

All tests use synthetic uuids and `example.com` emails. The property tests use Hypothesis (`derandomize=True`, `database=None`).

| # | Invariant | Tests |
|---|---|---|
| 1 | The app's org decides | `tests/unit/test_desktop_app.py::test_c10_3_invariant_1_and_2_the_apps_org_decides_and_never_guesses` (property) |
| 2 | No guess: an unlinked org marks no lane `desktop` | the same property test; `test_c10_3_an_unlinked_org_marks_no_lane_desktop_and_every_lane_not_linked_elsewhere_possible` |
| 3 | The file sets the org; no event moves it while the file answers | `test_c10_9_invariant_3_while_the_file_answers_no_event_moves_the_org` (property); `test_c10_9_the_file_sets_the_org_whatever_a_newer_event_says` |
| 4 | Only desktop sessions outside a launch report, and only the three names | `tests/unit/test_desktop_hook_report.py::test_c10_9_invariant_4_only_a_desktop_session_outside_a_launch_reports` (property); `test_c10_9_invariant_4_the_report_carries_exactly_three_values_and_the_session` (property); `test_c10_9_the_report_reads_only_its_names`; `test_c10_9_a_subfleet_launch_never_reports` |
| 5 | The hook is otherwise unchanged | `test_c10_9_invariant_5_stdout_stderr_and_exit_equal_the_base_hooks`: a differential of stdout, stderr and exit code against `tests/fixtures/hooks_266c3ab8b.py`; `test_c10_9_a_failed_report_never_changes_the_exit_code`, through `hooks.run` with each failure kind |
| 6 | Fallback unchanged | `tests/unit/test_desktop_routing.py::test_c10_9_the_frozen_copies_are_266c3ab8b` (SHA-256 of the frozen copies, below); `test_c10_9_invariant_6_without_the_signals_every_decision_equals_base` (property: full `build_view` output and `asdict(Decision)` equal frozen base); `test_c10_9_invariant_6_rank_key_orders_as_base_without_desktop_possible` (property); at the daemon, `tests/fake/test_desktop_account_daemon.py::test_c10_9_without_the_signals_routing_is_the_hints` |
| 7 | Ranking only, except the in-use refusal | `test_c10_9_invariant_7_the_signals_change_ranking_and_the_in_use_refusal_only` (property). See the note below. |
| 8 | Order-free; equal times break by event id | `test_c10_9_invariant_8_shuffled_events_decide_the_same`; `test_c10_9_invariant_8_the_samples_order_never_matters` (both property) |

**Invariant 7, stated exactly.** With and without the signals, `evaluate` refuses the same lanes for the same reasons and keeps the same candidates. The one exception is C-10.3's in-use refusal (`desktop`), which follows the desktop account:
- With the signals, it applies to exactly the Claude lanes the app's org's email names, while Claude Code uses the desktop login (or that is unknown) and the job lacks `allow_desktop`.
- Without the signals, it applies to the lanes the profile and hint rule names.

**Frozen base copies** (`git show 266c3ab8b:<path>`, SHA-256 checked before loading):

| File | SHA-256 |
|---|---|
| `tests/fixtures/capacity_266c3ab8b.py` | `1771e1d1ff6174fd0d41e68ea2c1d3cef7b1a2edd6b02be75201160276f792f6` |
| `tests/fixtures/scheduler_266c3ab8b.py` | `f6c9ad0cae11fd8b9d624f856b0782ddb7e155cbc4dfca1ab85ce141d1da7d54` |
| `tests/fixtures/hooks_266c3ab8b.py` | `f1f5b53be73fd77c7b5b75fda65ef06d4c28c5aa280beb978c3648615529b6ed` |

## Mutants and their killing tests

How each mutant ran:
- Applied alone by an exact string replace that had to match once.
- Run with `pytest -x`, putting the deterministic test first and the property test second.
- Restored from a pristine copy in `finally`. After each restore the file's SHA-256 equalled HEAD's blob, and `git diff --quiet` was clean.

Every mutant was killed by its first test, so `-x` skipped the second.

| # | Mutant | File | Killed by |
|---|---|---|---|
| 1 | The hook reports inside a launch (both launch gates removed) | `subfleet/hooks.py` | `tests/unit/test_desktop_hook_report.py::test_c10_9_a_subfleet_launch_never_reports[SUBFLEET_ATTEMPT]` |
| 2 | The report copies every `CLAUDE_CODE_*` name | `subfleet/desktop_app.py` | `tests/unit/test_desktop_hook_report.py::test_c10_9_a_desktop_session_reports_its_account_once[SessionStart]` |
| 3 | Routing keeps the `~/.claude.json` hint while the signals answer (`owns` falls back to the label hints) | `subfleet/capacity.py` | `tests/fake/test_desktop_account_daemon.py::test_c10_9_while_the_signals_answer_the_hint_decides_nothing` |
| 4 | An unlinked org's lanes become `desktop` | `subfleet/desktop_app.py` | `tests/unit/test_desktop_app.py::test_c10_3_an_unlinked_org_marks_no_lane_desktop_and_every_lane_not_linked_elsewhere_possible` |
| 5 | `desktop_possible` dropped from the rank key | `subfleet/scheduler.py` | `tests/unit/test_desktop_routing.py::test_c11_3_a_possible_lane_with_more_room_still_sorts_after_one_known_elsewhere` |
| 6 | Link through the oldest event instead of the newest | `subfleet/desktop_app.py` | `tests/unit/test_desktop_app.py::test_c10_3_an_org_is_linked_through_its_newest_event_not_its_oldest` |
| 7 | A newer event's org overrides the file's org | `subfleet/desktop_app.py` | `tests/unit/test_desktop_app.py::test_c10_9_the_file_sets_the_org_whatever_a_newer_event_says` |
| 8 | A failed report makes the hook exit 2 (`send_desktop_report` re-raises; `session_event` returns `Exit.INVALID_INPUT`) | `subfleet/hooks.py` | `tests/unit/test_desktop_hook_report.py::test_c10_9_a_failed_report_never_changes_the_exit_code[SessionStart-DaemonError0]` |

Each mutant also has a second-line test that `-x` didn't run:

| # | Second-line test |
|---|---|
| 1 | `test_c10_9_invariant_4_only_a_desktop_session_outside_a_launch_reports` |
| 2 | `test_c10_9_invariant_4_the_report_carries_exactly_three_values_and_the_session` |
| 3 | `tests/unit/test_desktop_app.py::test_c10_9_while_the_app_answers_the_hint_decides_nothing` |
| 4 | invariant 1 and 2 property test |
| 5 | `test_c11_3_desktop_possible_sorts_right_after_desktop_in_every_comparator` |
| 6 | invariant 1 and 2 property test |
| 7 | invariant 3 property test |
| 8 | invariant 5 differential |

## Commands and results

Every pytest run used the same settings:
- `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, with `PYTHONPATH` unset.
- `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 --basetemp <fresh>`.
- A private `UV_CACHE_DIR` in the temp folder.

Mutant runs used `.venv/bin/python -m pytest -x` instead.

| Step | Command | Result |
|---|---|---|
| Branch | `git switch fix/desktop-org-217` | Refused: checked out in the caller's worktree. Work committed on detached HEAD. |
| Setup | `uv sync --group dev` | Built the worktree's `.venv`. |
| Setup | `uv run --no-sync --with 'pytest-xdist>=3.6' ... -n 4` with the shared `$TMPDIR/uv-cache` | Failed: `unrecognized arguments: -n` (the shared overlay lacked xdist). Fixed with a private `UV_CACHE_DIR`. |
| Search | `grep -r` over `docs/` (188 MB) | Hit the 600 s cap and was stopped. Switched to `git grep`. |
| smoke1 | `test_hooks`, `test_protocol_registry`, `test_scheduler_weekly`, `test_doctor`, `test_doctor_identity`, `test_live_store_guard` | 502 passed (351 s) |
| new1 | `tests/unit/test_desktop_app.py` | 62 passed |
| new2 | `tests/unit/test_desktop_hook_report.py` | 35 passed |
| new3 | `tests/unit/test_desktop_routing.py` | 7 passed (224 s) |
| new4 | `tests/fake/test_desktop_account_daemon.py` | 14 errors: the lanes table's CHECK constraint needs `keychain-token`. Test fixed. |
| new4b | same | 2 failed, 12 passed: the autouse `no_desktop_login` fixture blanks the default login read. The test now names its own stand-in file. |
| new4c | same | 14 passed |
| g3 | `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py`, `tests/unit/test_handler_refusals.py`, `tests/unit/test_protocol_registry.py` | 427 passed (273 s) |
| Data | `desktop_app.read_app_usage(<temp copy>)` | Answer from version 2; newest sample: the app account's org, `2026-11-07T23:34:56.429Z`, weekly 6.0%; no log line |
| Mutants, first try | runner over all eight in one call | At the 600 s foreground cap the harness moved it to the background, against the brief's no-background rule. I interrupted my own runner with SIGINT inside the session; `finally` restored `subfleet/scheduler.py` and the worktree was clean. The results were lost because the runner printed only at the end. |
| Mutants 1–2 | one pytest process per mutant, recording each result as it landed | Both killed (62 s) |
| Mutants 3–8 | same | All killed (100 s) |
| Exact compares | `git grep` for `LANE_FACTS`, `desktop_possible`, `desktop_account` and key-set compares in `tests/` | One relevant compare: `tests/unit/test_scheduler_split.py:75` (`read <= set(LANE_FACTS)`) needs `desktop_possible` in `LANE_FACTS`, which it is. The other key-set compares (`test_status_payload.py:50`, `test_operator_notices.py:269`, `test_tick_prefilter.py:243`, `frontend/test_core_protocol.py:237`) are on records this change doesn't touch. |
| g2named, first try | named files passed through an unsplit zsh variable | `no tests ran` (rc 5): zsh passed the list as one argument |
| g2named | `test_scheduler_weekly`, `test_scheduler_split`, all eight `*policy*` files, `test_timers_alerts`, `test_operator_notices`, `test_codex_ca_bundle`, `test_conversation_service` | 757 passed (211 s) |
| final | the four new test files, `test_doctor`, `test_hooks`, `test_live_store_guard` at `4ab996095` | 570 passed (203 s) |

The last two rows include the two caplog-empty tests (`test_codex_ca_bundle`, `test_conversation_service`) that a new log line could have broken, and both pass.

## What didn't run

- **Most of group 2.** `git grep -l -E 'desktop|hook' -- 'tests/*.py'` matches 253 files outside `tests/fixtures/`.
  - 18 of them ran above. 235 did not: 151 under `tests/unit`, 58 under `tests/fake`, 7 e2e, 6 frontend, 3 process, plus helper modules and `tests/conftest.py`.
  - They didn't run because the 4-hour mark had already passed when the session resumed.
  - CI runs the whole suite on the PR.
- **The whole suite**, by the brief.
- **The second-line killing tests for each mutant**, skipped by `-x`. Unmutated, they pass in new1–new3 and in the final run.
- **Anything live.** No daemon, store, CLI or process outside the temp folder was started or read. The real usage file and `~/.claude.json` were not read; tests build their own environments. The reader ran only on the temp copy.
- **A real desktop session's report.** Not exercised. The hook's behaviour is proved by tests that build the environment.
- **Independent review.** None ran in this job. The merge gate's review belongs to the hub.

## Notes for review

- **Write frequency.** Dedupe compares a report with the newest event only. Two live sessions on different accounts that alternate prompts could therefore write two rows per prompt. This needs two app accounts with sessions open at once; the app restarts its sessions at an account switch. The hourly refresh bounds a single account.
- **Missing-file log.** On a machine without the desktop app, the reader logs "missing" once per path per daemon process.
- **The cost of the new rule.** Once the signals answer, a terminal Claude Code session on a different CLI login loses C-10.3's in-use protection. This is stated in C-10.3 by the brief's design.
- **Mergeability.** The base is `266c3ab8b`; check mergeability against the moved release line before opening or updating the PR.

Head built: `4ab996095` (code, tests, contract), plus this report's commit on top of it.
