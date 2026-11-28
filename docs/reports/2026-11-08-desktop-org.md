# Desktop org: routing follows the desktop app's own account (C-10.3, C-10.9)

D-DESKTOP-ORG1. Base `266c3ab8b`. Code, tests and contract are at `4ab996095`; this report is the commit on top of it.

**Branch.** `fix/desktop-org-217` is checked out in the caller's worktree, so `git switch` refused it and every commit here sits on a detached HEAD. The hub needs to move `fix/desktop-org-217` to the head named at the end of this report.

**Timing.** The contract commit landed at 2026-11-07 23:42:51 UTC. The session was then suspended until 2026-11-13 23:46 UTC, so the brief's 4-hour mark (about 01:50 UTC on 2026-11-08) passed while it was down. After resuming I ran only short, bounded checks: the eight mutants, the named scheduler, policy and alert files, and a final run of the new tests. Then I stopped and wrote this report. The broad group-2 sweep did not run (see "What didn't run").

## What changed

- **The hook report** (`subfleet/hooks.py`, `subfleet/desktop_app.py`).
  - Fires on `SessionStart` and `UserPromptSubmit` only, outside a Subfleet launch (`launched_by_subfleet()` is None), and only when `CLAUDE_CODE_ENTRYPOINT` is `claude-desktop`.
  - Reads exactly `CLAUDE_CODE_USER_EMAIL`, `CLAUDE_CODE_ACCOUNT_UUID` and `CLAUDE_CODE_ORGANIZATION_UUID`, and no other name.
  - Validates them: an email with exactly one `@`, a part on each side and at most 254 characters, which NFKC leaves unchanged and which holds no whitespace, control (Cc), format (Cf) or surrogate (Cs) character; canonical uuids, lower-cased. *(Corrected in fix round 1: the build refused only whitespace and control characters.)*
  - Sends `{email, account_uuid, org_uuid, session_id, observed_at}` by the new verb `desktop.report`. The session comes from the hook payload only.
  - Sends one request and waits at most 1 s for its reply (`Client(..., retry_busy=False, timeout=1.0)`). After a timeout the client takes one look at the lock's holder, bounded by 5 s (`client.HOLDER_PROBE_BUDGET_S`), so a report can add up to about 6 s; review r1 measured 0.998 s to 1.039 s against a socket that never answered. Every failure is swallowed, so the hook's stdout, stderr, notices and exit code are what base gives. *(Corrected in fix round 1: this said "one round trip" of 1 s.)*
  - Never logs, prints or stores the environment.
  - Registration: `desktop.report` is in `protocol.OPS` and in a new `descriptors.OBSERVATION_OPS`, which `read_only()` honours, so a report whose client has gone is not run (C-16.7). It is not folded into `notice.pending`.
  - `tests/conftest.py` pops `CLAUDE_CODE_ENTRYPOINT` at configure, so a suite started from a desktop terminal never reports.
- **The event** (`subfleet/daemon.py`).
  - `desktop.report` validates again. A malformed report is refused with `invalid-args` and writes nothing; a future `observed_at` is clamped to now. Since fix round 1 a stamp past the calendar and a surrogate in the email are malformed too; before it, both raised out of the handler.
  - Under one lock, it writes a `desktop.account` event with the report plus `source: "hook"`, but only when the email (case aside), account or org differs from the newest such event, or that event is over an hour old. It compares with the newest event only, so two sessions on different accounts that alternate prompts write one event per prompt. A report stamped before the newest event that names another account is written on every call; the hook always stamps now. *(Fix round 1 added the last two sentences.)*
  - Readers take a bounded query (`LIMIT 4000`) on the events kind index and reparse only when `MAX(event_id)` for the kind moves. Since fix round 1 they also take, beside that window, the newest event of each (org, email) pair (`desktop_app.newest_links`). The daemon builds those pairs once per process from every row of the kind, then reads only the rows past the greatest event id it has read. Doctor reads every row each time. No schema change.
- **The app file reader** (`desktop_app.read_app_usage`, `home_paths.desktop_usage_path`).
  - Default path `~/Library/Application Support/Claude/plan-usage-history.json`. `SUBFLEET_DESKTOP_USAGE`, registered in `SAFE_HOME_PATHS`, moves it under pytest only, so tests point it into their own folder. Outside pytest the daemon reads the default path whatever that name holds. *(Corrected in fix round 1: this said the name overrides the path.)*
  - Reads with `read_regular` and a 4 MiB cap, and re-reads only when `stat` changes.
  - Only `version` 2, the integer, is read; `2.0` and `"2"` are refused. Another version, a parse error or any other failure in the parse, a non-regular, unreadable or missing file, or no usable sample is no answer, logged once per change of file state. *(Corrected in fix round 1: the build read `2.0`, and a failure outside its caught errors raised out of the reader.)*
  - The answer is the newest sample: greatest `t`, equal times broken by org. A sample stamped more than 5 minutes ahead of the clock is left out. It gives the org, the time, the weekly percent and when the trailing run naming that org began. The weekly percent is `u.sd` when that is a finite number from 0 to 100, else None. *(Fix round 1 added the future-sample and range rules. In the build, a `u.sd` of `10**400` raised `OverflowError` out of the reader, review r1's P1 1.)*
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
- *Corrected in fix round 1 (review r1's P1 2):* a Claude lane records its account in one of two key forms. Without profile scope the key is `claude:<email>`. A lane enrolled with profile scope (C-1.4) has the key `claude:<account uuid>:<org uuid>`. The build compared every key with `claude:<email>`, so a uuid-keyed lane was never `desktop`. Each form is now matched by its own kind (`desktop_app.lane_account`), and only Claude lanes match.
- An email-keyed lane is *linked elsewhere* when its email appears in an event of another org and in none of the app's org. A uuid-keyed lane's key names its org, so it is the app's or elsewhere, and never `desktop_possible`.
- When the app's org has an email E, two kinds of lane are `desktop`. One is an email-keyed lane whose email is E (compared with `lower()`, never `casefold`). The other is a uuid-keyed lane whose org is the app's and whose account is the account of the newest event naming that org. If no lane has that account, none is.
- When it has no email (the usage file alone), every uuid-keyed lane whose key names the app's org is `desktop`, whatever its account. Every email-keyed lane not linked elsewhere gets `desktop_possible`.
- *Added in fix round 1 (review r1's P2 1):* before each reservation try, the reservation reads the app's answer again, off the lock. If a `desktop.account` event was written since that read, the route counts as moved. A route that moved is evaluated again with the refreshed answer.
- The profile probe and `~/.claude.json` then decide nothing for routing. `doctor` still shows them (its identity live checks and the new row) and says when they name another account than the app's.
- The in-use rule keeps its predicate and applies to the app's account. The contract now states the cost: while the signals answer, a terminal session on the CLI login is not protected by C-10.3 unless that login is the app's account.

**Fallback.** With neither signal, `resolve()` returns None. `DesktopIdentity.app` stays None, and the view, decisions and ranking equal base exactly (invariant 6).

**Ranking.** `rank_key` inserts `bool(detail.get("desktop_possible"))` right after `desktop` in both comparators, and nothing else moves. `judge_lane` sets the field, and `desktop_possible` joins `LANE_FACTS`.

**Status, doctor, alerts.**
- Status: one line, for example `desktop app: org <prefix> (app file) on claude-2; app file <time> (weekly 12%); hook <time>`. It names the lanes whose key holds the app's account whenever there are any. Otherwise it reads "not linked yet" when no session has reported the org's email, else "no lane". It is absent with neither signal. It appears in `daemon.status`, `cli.format_status`, `render.status` and the timers snapshot, which carry the org's 8-character prefix only. *(Corrected in fix round 1: the build's line said "not linked yet" even when a uuid-keyed lane held the org, and its summary carried the full org uuid as `org`.)*
- Doctor row "desktop app's account linked to a lane", which matches lanes as routing does:
  - Fail when an event names the org's email and no lane holds that account; the fix is `subfleet lanes enroll <credential>`.
  - Pass when a lane holds it and the newest event is under 24 h old.
  - Pass when no email is known and a uuid-keyed lane names the app's org.
  - Otherwise unknown, with the fix `subfleet daemon install --hooks`.
  - *(Corrected in fix round 1: the build never failed, and passed with no lane holding the account.)*
- Two warn alerts:
  - `desktop-app-unlinked:<prefix>`: the file's org has stayed unlinked for 10 minutes while the desktop login is in use, with unknown counting as use. Since fix round 1 the alert also needs that no lane's key names the org.
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
| 2 | No guess: an unlinked org marks no email-keyed lane `desktop` (since fix round 1, a uuid-keyed lane whose key names the org is `desktop` by its key) | the same property test, whose oracle since fix round 1 reads each lane's recorded account under either key form; `test_c10_3_an_unlinked_org_marks_no_lane_desktop_and_every_lane_not_linked_elsewhere_possible` |
| 3 | The file sets the org; no event moves it while the file answers | `test_c10_9_invariant_3_while_the_file_answers_no_event_moves_the_org` (property); `test_c10_9_the_file_sets_the_org_whatever_a_newer_event_says` |
| 4 | Only desktop sessions outside a launch report, and only the three names | `tests/unit/test_desktop_hook_report.py::test_c10_9_invariant_4_only_a_desktop_session_outside_a_launch_reports` (property); `test_c10_9_invariant_4_the_report_carries_exactly_three_values_and_the_session` (property); `test_c10_9_the_report_reads_only_its_names`; `test_c10_9_a_subfleet_launch_never_reports` |
| 5 | The hook is otherwise unchanged | `test_c10_9_invariant_5_stdout_stderr_and_exit_equal_the_base_hooks`: a differential of stdout, stderr and exit code against `tests/fixtures/hooks_266c3ab8b.py`; `test_c10_9_a_failed_report_never_changes_the_exit_code`, through `hooks.run` with each failure kind |
| 6 | Fallback unchanged | `tests/unit/test_desktop_routing.py::test_c10_9_the_frozen_copies_are_266c3ab8b` (SHA-256 of the frozen copies, below); `test_c10_9_invariant_6_without_the_signals_every_decision_equals_base` (property: full `build_view` output and `asdict(Decision)` equal frozen base); `test_c10_9_invariant_6_rank_key_orders_as_base_without_desktop_possible` (property); at the daemon, `tests/fake/test_desktop_account_daemon.py::test_c10_9_without_the_signals_routing_is_the_hints` |
| 7 | Ranking only, except the in-use refusal | `test_c10_9_invariant_7_the_signals_change_ranking_and_the_in_use_refusal_only` (property). See the note below. |
| 8 | Order-free; equal times break by event id | `test_c10_9_invariant_8_shuffled_events_decide_the_same`; `test_c10_9_invariant_8_the_samples_order_never_matters` (both property) |

**Invariant 7, stated exactly.** With and without the signals, `evaluate` refuses the same lanes for the same reasons and keeps the same candidates. The one exception is C-10.3's in-use refusal (`desktop`), which follows the desktop account:
- With the signals, it applies to exactly the Claude lanes that hold the app's account (`AppAccount.owns`), while Claude Code uses the desktop login (or that is unknown) and the job lacks `allow_desktop`. Those are the email-keyed lanes with the org's email and the uuid-keyed lanes in the app's org, with the newest event's account when an event names the org. *(Corrected in fix round 1: this said "the lanes the app's org's email names". The property's oracle now derives them from each lane's recorded account, not from `owns`.)*
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

- **Write frequency.** Dedupe compares a report with the newest event only. Two live sessions on different accounts that alternate prompts could therefore write two rows per prompt. This needs two app accounts with sessions open at once; the app restarts its sessions at an account switch. The hourly refresh bounds a single account that the hook reports. A report stamped before the newest event that names another account is written on every call. Only a local client can send one, since the hook stamps now. *(Fix round 1 added the last two sentences.)*
- **Missing-file log.** On a machine without the desktop app, the reader logs "missing" once per path per daemon process.
- **The cost of the new rule.** Once the signals answer, a terminal Claude Code session on a different CLI login loses C-10.3's in-use protection. This is stated in C-10.3 by the brief's design.
- **Mergeability.** The base is `266c3ab8b`; check mergeability against the moved release line before opening or updating the PR.

Head built: `4ab996095` (code, tests, contract), plus this report's commit on top of it.

## Fix round 1

This round fixes review r1 of #318, taken at `5a86e3a7e`. It ran as job `dorg-fix-r1`, starting 2026-11-28 02:15:52Z. Every commit sits on a detached HEAD, so the hub needs to move `fix/desktop-org-217` to the head named at the end. Nothing was pushed, and nothing changed on GitHub.

### Step 0

- R is `a4d21501e`, release/217's head as this worktree's objects have it. The brief allowed no fetch. It was merged into `5a86e3a7e` as `7b68a8cab`.
- The step 0 run passed 198 tests at `7b68a8cab`. Its first try put the basetemp inside the worktree, and 4 gate tests failed their neutral-directory check. I moved the scratch folder to `$TMPDIR/sf-dorg-fix1-b`, and the rerun passed.

### Commits

| Commit | What |
|---|---|
| `7b68a8cab` | Merge release/217 at `a4d21501e` |
| `42ac607f5` | Tests for every fix, committed before the fixes |
| `12e00cf17` | Fixes for rulings 1 to 4 and the P3s |
| `7858b313d` | Contract and texts; C-26.2's test |
| `c22d0c390` | Test fix: readings dated 30 s back, so mutant 17's test turns on `LANE_FACTS` |
| `e4207eccb` | Test fix: the stand-in daemons of `test_legacy_hold` and `test_gate_admission` carry the new desktop attributes |
| `3b0718013` | A moved route is evaluated again with the refreshed answer (P2 1) |
| `929c56761` | Late test: no unlinked alert while a lane's key names the app's org |
| this commit | This section and the corrections above |

### Findings

**P1 1 (ruling 1): a usage-file number raised out of `read_app_usage`.** Fixed.
- *Cause.* `float(u.sd)` on `10**400` raised `OverflowError`. The reader didn't catch it, so admission, `status`, `lanes` and `pick` stopped.
- *Change.*
  - `_percent` gives None for a bool, a non-number, an overflow, a non-finite number, or a value outside 0 to 100.
  - `version` must be the integer 2.
  - A sample stamped more than 5 minutes ahead of the clock is left out.
  - Any other exception in the parse means no answer. It is recorded for that file state and logged once.
  - `Daemon._desktop_app_read` also takes a failure as no answer, so the events still count.
- *Tests.*
  - `test_c10_9_no_number_in_the_usage_file_raises_out_of_the_reader`: each odd number at each place.
  - `test_c10_9_any_failure_in_the_parse_is_no_answer_recorded_and_logged_once`: injected errors.
  - `test_c10_9_any_json_value_anywhere_gives_a_sample_or_none`: a Hypothesis property.
  - At the daemon, `test_c10_9_a_usage_number_too_large_for_a_float_stops_nothing`.

**P1 2 (ruling 2): a lane keyed by its identity never matched.** Fixed.
- *Cause.* `owns` and "linked elsewhere" compared every key with `claude:<email>`. A lane enrolled with profile scope has the key `claude:<account uuid>:<org uuid>`. Such a lane was never `desktop`, even when it held the app's account, and it could rank `desktop_possible`.
- *Change.* One key parser, `desktop_app.lane_account`, reads Claude lanes only and returns the email kind or the uuid kind.
  - An email-keyed lane matches by email, compared with `lower()`.
  - A uuid-keyed lane matches when its org is the app's and, if an event names that org, its account is that event's account.
  - A uuid-keyed lane is never `desktop_possible`.
  - `AppAccount` now carries `account`.
- *Tests.*
  - Unit: `test_c10_3_a_verified_lane_is_the_apps_by_its_account_and_org`, `test_c10_3_with_the_file_alone_a_verified_lane_in_the_apps_org_is_desktop`, `test_c10_3_a_verified_lane_in_another_org_is_never_possible`, `test_c10_3_a_codex_lane_is_never_desktop_or_possible`, `test_c10_3_owns_and_elsewhere_read_one_key_parser` and `test_c10_3_case_aside_is_lower_case_not_casefold`.
  - Daemon: `test_c10_3_a_verified_lane_stays_desktop_on_the_report_that_names_its_account` and `test_c10_3_the_usage_file_alone_makes_a_verified_lane_in_its_org_desktop`.
  - The oracles of invariants 1, 2 and 7 now take the app's lanes from each lane's recorded account, under either key form, and never from `owns`.

**P2 1 (ruling 3): the reservation judged the desktop lane by the evaluation's answer.** Fixed, with one window left open and stated in the contract.
- *Cause.* `_route_rows` marked the lanes with the identity read when the pass began. C-6.3's check therefore missed a report or a file change that landed between the evaluation and the reservation.
- *Change.*
  - Before each try, the reservation loop reads the app's answer again, off the lock (`_desktop_app_read`).
  - `_route_rows` marks the lanes with that answer. It gives `moved` when the newest `desktop.account` event id differs from the id read with the answer.
  - A moved route is evaluated again with the refreshed answer (`3b0718013`).
  - The admission-probe path judges by an answer refreshed before its transaction. When an event was written since, its look ends and the job is evaluated again (`_DesktopMoved`).
  - The usage file is never read inside the transaction (C-3.3). A file the app rewrites between the refresh and the transaction is judged at the next evaluation; C-10.3 states this window.
- *Tests.* `test_c10_3_the_reservation_judges_the_desktop_lane_by_the_newest_report`, `test_c10_3_a_report_after_the_refresh_moves_the_route` and `test_c10_9_desktop_possible_alone_changing_moves_the_route` (mutant 17).
- *Untested.* The probe path's refresh and the moved re-evaluation in `3b0718013`. No test drives either, and no mutant ran on them.

**P2 2 (ruling 4): an org's link fell out of the 4,000-row window.** Fixed by keeping the link, as the hub prefers.
- *Cause.* Readers took only the newest 4,000 rows. Each write adds two rows, so that is 2,000 events. Once enough other events followed, the org's email was forgotten. Its lanes then stopped being `desktop`, and its email-keyed lanes ranked as possible.
- *Change.* The newest event of each (org, lower-cased email) pair is kept beside the window (`newest_links`, `with_links`).
  - The daemon builds the pairs once per process from every row of the kind. After that it reads only the rows past the greatest event id it has read.
  - Doctor reads every row each time.
  - Over the window plus these pairs, `resolve` gives the same org, email, account, linked-elsewhere set and linked count as over every event.
  - Cost: retention never deletes this kind, so the first read in a process covers every row ever written. C-10.9 states this.
- *Tests.*
  - `test_c10_9_an_orgs_link_outlives_the_row_window`, with `EVENT_ROWS` set to 8, through the daemon and through doctor's offline read.
  - `test_c10_9_the_hook_alone_names_the_newest_reports_org_past_the_window`.
  - `test_c10_9_disagreement_is_dated_by_the_newest_events_past_the_window`.

**P3s (ruling 5).** All fixed except the two under "Left for an issue".

| P3 | Change | Tests |
|---|---|---|
| A stamp past the calendar raised `OverflowError` out of `desktop.report` | `_instant` catches it, so the report is refused with `invalid-args` | `test_c10_9_a_stamp_past_the_calendar_is_malformed`; `test_c10_9_a_report_past_the_calendar_or_with_a_surrogate_email_is_refused` |
| A surrogate in the email raised `UnicodeEncodeError`, and `valid_email` passed characters that NFKC changes and format characters | `valid_email` refuses whitespace, Cc, Cf and Cs characters, and any value NFKC changes | `test_c10_9_an_email_is_refused_when_nfkc_changes_it_or_it_holds_a_control_format_or_space`; `test_c10_9_a_plain_address_still_passes`; `test_c10_9_the_hook_drops_a_surrogate_email`; the daemon refusal test above |
| Status and the snapshot carried the full org uuid | `summary` carries `org_prefix` only | `test_c10_9_status_carries_the_org_prefix_only`; `test_c10_9_status_and_the_snapshot_carry_no_full_org` |
| Doctor never failed, even with no lane holding the app's account | It fails then, with the fix `subfleet lanes enroll <credential>`, and matches lanes as routing does | `test_c10_9_doctor_fails_when_no_lane_holds_the_apps_account` |
| The status line and the unlinked alert ignored uuid-keyed lanes | The line names any lane that holds the account; the alert also needs that no lane does | `test_c10_3_the_usage_file_alone_makes_a_verified_lane_in_its_org_desktop` (line); `test_c10_9_no_unlinked_alert_while_a_lane_key_names_the_apps_org` (alert, written after the fix) |
| `version` 2.0 was read | The integer 2 only | `test_c10_9_only_an_integer_version_2_is_read` |
| A `u.sd` outside 0 to 100 was kept | It reads as None | `test_c10_9_a_weekly_percent_outside_0_to_100_is_none` |
| A sample from the future became the newest | Left out when more than 5 minutes ahead | `test_c10_9_a_sample_from_the_future_is_left_out` |
| `casefold` joined distinct addresses | `lower()` everywhere | `test_c10_3_case_aside_is_lower_case_not_casefold` |
| Texts: the hook's 1 s, the dedupe's "only when the account changes", the override, C-26.2's order | C-10.3, C-10.9, C-26.2, and the hook and descriptor texts corrected | `test_c26_2_desktop_possible_outranks_a_turns_affinity` for C-26.2 |

### Left for an issue

- **The usage file alone, and dropped reports.** Not fixed. What follows is inferred from the code and not tested.
  - With the file alone, when no event names the app's org, every uuid-keyed lane in that org is `desktop`, whichever account it holds. Two seats of one org enrolled with profile scope are both kept off. This errs safe.
  - The app can move between two accounts of the same org. If the new account's report is dropped (the hook drops a report on any failure, its 1 s wait included, or when the daemon is down), the newest event naming the org still names the old account. The file records only the org, so it cannot tell the two accounts apart. The old account's lanes stay `desktop`, and the new account's lanes go unprotected until a report lands at the next prompt or session start. This errs unsafe.
  - A fix needs either an account in the usage file or a report the daemon acknowledges.
- **C-26.2's "each move is recorded with its reason".** Not checked for a turn that moves off a `desktop_possible` lane. The new C-26.2 test checks only the order.

### Mutants

Method (`$SF/mutants.py`):
- Each mutant was applied alone by an exact string replace that had to match once.
- Its named test ran with `.venv/bin/python -m pytest -x -p no:xdist`.
- The file was restored from a pristine copy in `finally`, and `git hash-object` then had to equal HEAD's blob. Every restore passed.
- Mutants 13, 15 and 17 carry review r1's numbers; 21 to 38 are this round's.

| # | Mutant | File | Result | Test |
|---|---|---|---|---|
| 13 | The window query ascending | `daemon.py` | killed | `test_c10_9_disagreement_is_dated_by_the_newest_events_past_the_window` |
| 13h | The same mutant | `daemon.py` | survives | `test_c10_9_the_hook_alone_names_the_newest_reports_org_past_the_window`. The kept pairs make the hook-alone org exact whatever the window holds, so only the dating depends on the window's order. |
| 15 | The report lock removed | `daemon.py` | killed | `test_c10_9_concurrent_identical_reports_write_one_event` (16 threads) |
| 17 | `desktop_possible` dropped from `LANE_FACTS` | `scheduler.py` | survived at `12e00cf17`; killed at `c22d0c390` | `test_c10_9_desktop_possible_alone_changing_moves_the_route`. Readings stamped now passed their clock horizons, so every lane was judged again. With readings dated 30 s back, only a changed fact marks a lane. |
| 21 | `_percent` without its `OverflowError` catch | `desktop_app.py` | killed | `test_c10_9_no_number_in_the_usage_file_raises_out_of_the_reader[10**400-u.sd]` |
| 22 | The reader catches only `OSError`, `UnicodeError`, `ValueError` and `RecursionError` | `desktop_app.py` | killed | `test_c10_9_any_failure_in_the_parse_is_no_answer_recorded_and_logged_once[error0]` |
| 22p | The same mutant | `desktop_app.py` | survives | The JSON property and the odd-numbers tests (19 passed). The mutant is equivalent for every file content those tests generate; only an injected failure tells them apart. |
| 23 | A uuid-keyed lane never owns | `desktop_app.py` | killed | `test_c10_3_a_verified_lane_is_the_apps_by_its_account_and_org` |
| 24 | A uuid-keyed lane in another org is possible | `desktop_app.py` | killed | `test_c10_3_a_verified_lane_in_another_org_is_never_possible` |
| 25 | `_route_rows` ignores the refreshed answer | `daemon.py` | killed, at `12e00cf17` and again at `c22d0c390` | `test_c10_3_the_reservation_judges_the_desktop_lane_by_the_newest_report` |
| 26 | No event-id check in `_route_rows` | `daemon.py` | killed | `test_c10_3_a_report_after_the_refresh_moves_the_route` |
| 27 | The daemon reads the window without the kept pairs | `daemon.py` | killed | `test_c10_9_an_orgs_link_outlives_the_row_window` |
| 28 | No NFKC check | `desktop_app.py` | killed | `test_c10_9_an_email_is_refused_when_nfkc_changes_it_or_it_holds_a_control_format_or_space[a＠b@example.com]` |
| 29 | `_instant` catches only `ValueError` | `desktop_app.py` | killed | `test_c10_9_a_report_past_the_calendar_or_with_a_surrogate_email_is_refused[broken0]` |
| 30 | The summary carries `org` | `desktop_app.py` | killed | `test_c10_9_status_and_the_snapshot_carry_no_full_org` |
| 31 | Doctor never fails | `doctor.py` | killed | `test_c10_9_doctor_fails_when_no_lane_holds_the_apps_account` |
| 32 | `casefold` for email-keyed lanes | `desktop_app.py` | killed | `test_c10_3_case_aside_is_lower_case_not_casefold` |
| 33 | Future samples kept | `desktop_app.py` | killed | `test_c10_9_a_sample_from_the_future_is_left_out` |
| 34 | The version checked with `isinstance`, so 2.0 passes | `desktop_app.py` | killed | `test_c10_9_only_an_integer_version_2_is_read` |
| 35 | No 0 to 100 range | `desktop_app.py` | killed | `test_c10_9_a_weekly_percent_outside_0_to_100_is_none[-5]` |
| 36 | Doctor reads the window without the kept pairs | `doctor.py` | killed | `test_c10_9_an_orgs_link_outlives_the_row_window` |
| 37 | The status line ignores owned lanes when the org is unlinked | `render.py` | killed | `test_c10_3_the_usage_file_alone_makes_a_verified_lane_in_its_org_desktop` |
| 38 | The unlinked alert ignores owned lanes | `desktop_app.py` | killed | `test_c10_9_no_unlinked_alert_while_a_lane_key_names_the_apps_org` |

### The tests commit

- The tests commit's tree (code at `7b68a8cab`) ran just before it was committed.
  - In `test_desktop_app`, `test_desktop_routing` and `tests/fake/test_desktop_account_daemon`, 44 tests failed and 106 passed (t0c).
  - Invariant 1 and 2's property, run alone, failed (t0d).
- Every failure was one of these:
  - an assertion or `DID NOT RAISE`;
  - the review's exception: `OverflowError` (3 from int to float, 4 from the calendar) or `UnicodeEncodeError` (the surrogate);
  - an error the parse-failure test injects (`ArithmeticError`, `KeyError`, `MemoryError`, `TypeError`).
  - None was an import error or a missing name.
- Five new tests passed there, as intended: each guards behaviour base already had, or carries a mutant. They are the Codex guard, plain addresses, the 16-thread lock test (mutant 15), and the two past-the-window tests for the hook alone and the dating (mutant 13).
- Tests changed after the tests commit:
  - `12e00cf17`:
    - The huge-number test's `pick` now names its family. At `7b68a8cab` it failed on the review's `OverflowError`. After the fix it failed on `pick: model belongs to a different provider`.
    - The future-sample test dates its sample 60 s back, so the event is newer.
    - Two reservation tests call `_desktop_app_read()` after `_record_desktop_use()`, as the reservation loop does (the brief's named exception).
  - `c22d0c390`: readings dated 30 s back (mutant 17).
  - `e4207eccb`: the two stand-in fixtures.
  - `929c56761`: the alert test, written after its fix.

### Runs

Every pytest run went through `$SF/pt.sh`, with these settings:
- `TMPDIR` is the user temp folder, `PYTHONPATH` is unset and `UV_CACHE_DIR` is `$TMPDIR/uv-cache`.
- The command is `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -p no:cacheprovider --basetemp $SF/pytest-<label>`, with at most `-n 4`. The basetemp is fresh and is removed after the run.
- `df -H /` and `uptime` print before each run, and the script refuses to start with under 56 GiB (60 GB) free.
- Free disk was 73 to 74 GB in the runs whose readings appear below, and the guard never refused a run. Other jobs drove the load average as high as 117.

| Run | Head | Files | Result |
|---|---|---|---|
| Step 0, first try | `7b68a8cab` | step 0's files | 4 gate tests failed: the basetemp was inside the worktree |
| Step 0 | `7b68a8cab` | step 0's files | 198 passed |
| t0c | the tests commit's tree | `test_desktop_app`, `test_desktop_routing`, `tests/fake/test_desktop_account_daemon` | 44 failed, 106 passed, as expected |
| t0d | the same | invariant 1 and 2's property | 1 failed, as expected |
| f1 | the fix commit's tree | the same three files | 2 failed, 148 passed: the two test bugs above |
| f2, f3 | the same | the two tests | 2 passed; 1 passed |
| s4 | `c22d0c390` | step 4's files | 24 failed, 242 passed: the stand-in daemons lacked `_desktop_links` (`AttributeError`) |
| s4b, s4c, s4d | from `e4207eccb`'s tree (s4b ran just before that commit) to `3b0718013`; ended 03:44Z, 03:51Z and 03:54Z | step 4's files again, in parts | 80, 210 and 56 passed |
| s5 | `3b0718013` | step 5's files | 447 passed |
| s6 | `3b0718013` | `test_doctor`, `test_status_json`, `test_route_check`, `test_render`, `test_timers_alerts`, `test_scheduler_split` | 167 passed in 898 s (73 GB free, load 12.26) |
| f4 | `3b0718013` plus the late test | the late test and the existing unlinked-alert test | 2 passed |
| z1 | `929c56761` | `test_desktop_app`, `test_desktop_hook_report`, `tests/fake/test_desktop_account_daemon` | 179 passed in 29 s (73 GB free, load 22.96) |
| z2 | `929c56761` | `test_desktop_routing`, `test_hooks`, `test_legacy_hold`, `test_gate_admission` | 169 passed in 15 s (73 GB free, load 28.94) |

- The s4b to s5 logs keep pytest's output only, not their file lists, so I don't restate those lists here.
- z1, z2 and s6 cover every touched module's own test files at the final code.
- s6's log carries one ignored `PytestUnraisableExceptionWarning`: a `KeyboardInterrupt` inside Hypothesis's GC callback. No test failed, and I did not trace its source.

### What didn't run

- The whole suite, by the brief. CI runs it.
- The probe path's refresh and the moved re-evaluation: neither has a test or a mutant.
- An independent review of this round.
- A real desktop session or the real usage file, by the brief.
- C-26.2's "recorded with its reason" check.
- Mergeability against today's release/217. The brief allowed no fetch, so R is `a4d21501e` as this worktree's objects have it.

### The PR body's false sentences

These sentences come from the PR body read with `gh pr view` (read only) at head `5a86e3a7e`. None was changed on GitHub.

| Sentence | At this head |
|---|---|
| "uses one 1-second round trip" | False. The hook sends one request and waits 1 s for the reply, then takes one holder look bounded by 5 s: up to about 6 s. |
| "The daemon writes a `desktop.account` event only when the account changes, or once an hour." | False. Two sessions on different accounts that alternate prompts write one event per prompt. A report stamped before the newest event that names another account is written on every call. |
| "(format version 2 only)" | False at `5a86e3a7e`, which read `2.0`. True at this head, which reads the integer 2 only. |
| "exactly the lanes whose account is the app's" | False at `5a86e3a7e`, where a uuid-keyed lane never matched. Overstated at this head: with the file alone, every uuid-keyed lane in the app's org is `desktop`, whichever account it holds. |
| "With an org but no known email, no lane is `desktop`, and lanes not linked elsewhere rank as `desktop_possible`." | False at this head. Uuid-keyed lanes in the org are `desktop`, and only email-keyed lanes can be possible. |
| "the app's org has stayed unlinked … each for 10 minutes" | Incomplete at this head: the unlinked alert also needs that no lane's key names the org. |
| "Merged release/217 at adf66f2e5." | Stale. The branch merged release/217 five more times: `a4ea67e0e`, `82d3dd819`, `4aed7a3c9`, `53a7bac93` and `a4d21501e` (`7b68a8cab`). |
| "1,064 passed, 0 failed on the merged code" and "54 passed" | These describe the `adf66f2e5` merge's head, not this one, and were not rerun. |
| "Independent review (follows CI)", under "Not yet run" | Stale: review r1 ran. |
| "Full design, invariants, eight mutants and runs" | Incomplete: this round adds the mutants and runs above. |

### Passages corrected in place

1. "What changed", the hook report: the email rule gains NFKC and the Cf and Cs categories.
2. "What changed", the hook report: "one round trip" becomes a 1 s wait plus one holder look, about 6 s in all.
3. "What changed", the event: refusals of calendar stamps and surrogates.
4. "What changed", the event: the dedupe's alternating and back-dated cases.
5. "What changed", the event: readers take the kept pairs, with the first read's cost and doctor's read.
6. "What changed", the app file reader: `SUBFLEET_DESKTOP_USAGE` moves the path under pytest only.
7. "What changed", the app file reader: the integer version 2, and any failure in the parse.
8. "What changed", the app file reader: future samples, the range of `u.sd`, and P1 1's cause.
9. "C-10.3's new rule": the two key forms, "linked elsewhere", the email and no-email rules, and the reservation's refreshed answer (added).
10. "Status, doctor, alerts": the status line and the prefix, doctor's fail and pass rules, and the unlinked alert's lane condition.
11. Invariants table, row 2.
12. "Invariant 7, stated exactly".
13. "Notes for review", write frequency: the back-dated case.

### Folders

- **Created and deleted.**
  - `$TMPDIR/sf-dorg-fix1-b`, the scratch folder (logs, `mutants.py`, pristine copies, `pt.sh`, the PR body's copy; 1.3 MB). Deleted at the end by a guarded `shutil.rmtree`.
  - `.hypothesis/` in the worktree, deleted at the end.
  - `build/` in the worktree, used briefly and removed earlier.
  - Each run's basetemp under `$SF`, removed after its run.
- **Created and left.** `.venv/` and the `__pycache__` folders in the worktree, all gitignored.
- **Left, not this job's.**
  - `$TMPDIR/sf-dorg-fix1`, an older job's folder, untouched.
  - `$TMPDIR/uv-cache`, which is shared; the `--with pytest-xdist` overlay lives there.
  - 101 `$TMPDIR/sf-test-homes-*` folders (792 KiB) date from this job's span. The suite's conftest makes them, but other jobs run tests on this machine too, so I can't tell which are mine. They are left in place.

### Deviations

- The scratch folder is named `sf-dorg-fix1-b`, because `$TMPDIR/sf-dorg-fix1` already existed from an older job. The cleanup's guard ("sf-dorg-fix1" in the path) still holds.
- `build/` was used briefly in the worktree, then removed.
- Tests were edited after the tests commit, as listed above, and one test was written after its fix (`929c56761`).
- s6 ran past the 595 s foreground cap, so the harness moved it to the background. I polled its log in the foreground and started no other test run meanwhile.

Head built: `929c56761` (code, tests, contract), plus this report's commit on top of it.
