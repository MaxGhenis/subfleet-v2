# Refusal-verbatim rebase (#104 onto release/217), build r1

Two sessions built this. The earlier one (2026-11-12, cut off by a usage limit) did the rebase. This session started at 2026-11-13T18:29:46Z, from 1c8a6bbb0, and wrote this report at 21:25Z, inside the 3-hour limit. Base: release/217 at b51f08f961e1b1dcfb93347eb080f9ddd9a9260d.

## Commits

The earlier session's two commits were checked, not taken on trust. Both tests it cited pass again here (43 passed).
- 45fd06991 replays 4fef714c3 with C-9.3 and claude.py conflicts resolved.
- 1c8a6bbb0 replays 954da538a cleanly.
- I found nothing wrong in either, but one gap they left: the enroll raise kept the reason `claude-org-disabled`, whose template still sent the old sentence to the client. d15a2ab70 through 7054721e5 fix that.

This session's commits:
- d15a2ab70: enrolment reads only the CLI's words.
- a04bd6b32: provider error text stays the CLI's.
- 7054721e5: the wire template (C-25.7).
- 46750072a: fixtures.
- ffc750577: invariant tests.
- 8a8575986 and c5d736962: the mutation tool.
- 23712c0a2: the contract.
- This report.

## Step 0 (cited on b51f08f96)

1. b51f08f96 exists (`git cat-file -e`). Its merge base with #104's tip is 581b07b01, 3,019 commits back.
2. `git grep` on b51f08f96 finds none of `SubscriptionRefusal`, `ORG_BLOCK_CAUSES`, `LINE_EXCERPT_MAX`, `claude-subscription-refused` or `_provider_error_texts`. Its enroll corpus is `summary.texts()` (claude.py:923), and its org-block raise comes after the init gate (936-942). So it doesn't carry the change, and I proceeded.
3. **The wire path** (my own check, line numbers as cited):
   - `dispatch` calls `_enroll_lane` (daemon.py:4318), which only takes a lock (1285-1287).
   - `_enroll_lane_locked` catches `AdapterError` and rewraps it as `ProtocolError(str, code, fix, reason, details)` (1359-1362).
   - `_answer` passes it to `protocol.refuse` and then to `refusals.log_detail` (12373-12375).
   - `refuse` renders `refusal_text` and `handler_refusal` by reason and checked details, and puts reason and details on the wire only for `task-hold-condition` (protocol.py:579-591).
   - `log_detail` writes `request <op> refused (<class>): <str(exc)>; fix: <fix>`, scrubbed (refusals.py:638-664).
   - `.enroll(` has one caller in subfleet/ (daemon.py:1359), and nothing catches the error before 1360.
   - The test `test_the_recorded_refusal_reaches_the_wire_whole_and_daemon_log_has_every_word` confirms that the words reach daemon.log.
4. **The CLI boundary.** `cli_texts()` admits a frame's text only when `cli_placeholder(raw)` is true, plus the result's `errors` and its text when `is_error` (claude_stream.py:332-346). `cli_placeholder` admits API-error-marked frames, frames with a missing or `<` model, and frames with an `AUTH_ERROR_KINDS` error (127-140). **The hub's claim is confirmed.** A frame from model `claude-opus-5-5` with `error: max_output_tokens` and no API-error marker gave `cli_texts() == ()` but `_provider_error_texts == ('MODEL PROSE: ...',)`. Fixed in a04bd6b32.
5. **The success gate** is kept: `refusal = None if successful else _subscription_refusal(cli_corpus, summary)` (claude.py:1719). `test_success_supersedes_cli_text_failures` passes (16 cases).
6. **The fixtures.** The recorded and synthetic org-block streams decode to the same sentence, error kind, api_error status, `is_error` success result, placeholder frame (`is_synthetic_api_error` and `cli_placeholder` both true), `cli_texts`, kinds, init and answered. The recorded one adds a real `commands_changed` frame and Claude Code 2.1.284's extra keys (`output_tokens_details`, `iterations`, `speed`, `inference_geo`, which are null on the placeholder), plus result extras, from an unsynthesized attempt. **It is kept.**
7. The PR test file's helpers (`_Runner`, `make_launch`, `make_lane`, `exit_info`, `QUIET`, `stage_case`, `load_expected`) still fit release/217's conftest.

## Conflicts (resolved by the earlier session; recorded in 45fd06991's message)

- **C-9.3:** release/217's clause is kept whole and the PR's sentence appended.
- **`enroll`:** release/217's post-init raise is dropped, because the PR raises before the gate.
- **`classify`:** release/217's single `cli_corpus` is kept, with its success gate.

## Changes

- **claude.py:978:** the enroll corpus is `"\n".join([stderr, *summary.cli_texts()])`.
- **claude.py:2154-2168:** an assistant frame's text joins the provider error text only when `cli_placeholder(m.raw)`.
- **claude.py:266:** a new `SUBSCRIPTION_REFUSAL_LINES` holds the 2026-09-30 sentence, with its provenance.
- **claude.py:2143:** a new `SubscriptionRefusal.details()`.
- **claude.py:988-989:** the raise passes `reason="claude-subscription-refused", details=refusal.details()`, and its message keeps the full statement.
- **refusals.py:66:** `CLAUDE_REFUSAL_CAUSES` is `ORG_BLOCK_CAUSES` word for word.
- **refusals.py:300 and 433:** the reason's four forms.
- **refusals.py:489-490:** the closed values `error_kind` (`AUTH_ERROR_KINDS`) and `claude_refusal` (`SUBSCRIPTION_REFUSAL_LINES`). Neither admits free text.
- **Fixtures:** make_fixtures.py:777 now reads "subscription access refused (error-kind)".
- **Contract:** C-9.3 is amended, C-25.7's closed-value list gains the two sets, and a dated entry heads "Changes in version 3".

## Wire text, before and after

Before, every enrolment refusal read `claude: the organisation has disabled Claude Code subscription access for this account`, with the fix `ask the account's admin to enable Claude Code access`.

After, each form below has `ORG_BLOCK_CAUSES` as its fix:
1. When the line is the recorded sentence and the kind is a credential kind: `claude: Claude Code refused subscription access for this account: "Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, or ask your admin to enable access" (error kind oauth_org_not_allowed)`.
2. When only the line is checked: the same, without `(error kind …)`.
3. When only the kind is checked (a reworded refusal, or one with no words): `claude: Claude Code refused subscription access for this account (error kind oauth_org_not_allowed)`.
4. When neither is checked: `claude: Claude Code refused subscription access for this account`.

The old reason now renders the generic fallback.

## Invariants and their tests

The PR file is tests/unit/test_claude_subscription_refusal.py; the new file is tests/unit/test_claude_refusal_boundaries.py ("bounds" below). "H" marks a Hypothesis test.

1. **Quote.** `test_any_matching_corpus_is_quoted_by_enroll` and `…_by_the_classifier` (H), extended to every CLI channel: stderr, errors, the CLI's own frame and an error result's text.
2. **No model prose.** `test_model_prose_never_refuses_and_is_never_quoted` (H, `st.from_regex(ORG_BLOCK_RE)`) and `test_a_model_quoting_the_refusal_does_not_refuse_enrolment`, both in bounds.
3. **CLI words only.** `test_every_quoted_line_is_the_cli_s` (H, mixed streams) and `test_a_model_frame_with_an_error_kind_is_not_the_provider_s_error_text`, in bounds.
4. **Success supersedes.** `test_a_verified_success_is_never_refused` (H, bounds).
5. **Template only.** `test_the_wire_reads_one_of_the_template_s_forms` (H, through `enroll`, the daemon's rewrap and its real `_answer`), plus the two wire and daemon.log tests, in bounds.
6. **No cause asserted, no outcome promised.** `test_the_template_s_fix_is_the_adapter_s_cause_line` (differential and literal) and `test_the_closed_values_are_the_recorded_line_and_the_credential_kinds` in bounds, with the PR's `test_the_shipped_cause_line_is_the_neutral_one`.
7. **No refusal invented.** `test_no_refusal_is_invented` (H, PR) and `test_no_refusal_is_invented_from_any_channel` (H, bounds).
8. **Excerpt.** `test_an_excerpt_always_holds_its_match` (H, PR), extended so that each cut end is marked exactly.
9. **Class unchanged.** `test_what_release_217_called_auth_dead_still_is` (H, bounds): a differential against b51f08f96's predicate, which also checks that enrolment still exits 5.

## Mutants

Each mutant was applied alone, restored from a pristine copy, and killed by the named test:
1. Brief 1 (`enroll` reads `texts()`): tool #18, killed by `test_a_model_quoting_the_refusal_does_not_refuse_enrolment`.
2. Brief 2 (no success gate): #19, `test_a_verified_success_is_never_refused`.
3. Brief 3 (every error frame is provider text): #20, `test_a_model_frame_with_an_error_kind_is_not_the_provider_s_error_text`.
4. Brief 4 (old template fix and old text): #21, `test_the_template_s_fix_is_the_adapter_s_cause_line`; #22, `test_the_recorded_refusal_reaches_the_wire_whole_and_daemon_log_has_every_word`.
5. Brief 5 (no `reason=`): #23, the same wire test.
6. Brief 6 (unchecked line): #24, `test_the_wire_reads_one_of_the_template_s_forms`.
7. Brief 7 (bystander first): #25, `test_unrelated_matching_text_is_never_quoted_as_the_refusal[stderr-enroll]`.
8. Brief 8 (`line[:300]`): #14, `test_an_excerpt_always_holds_its_match`.
9. Brief 9 (refusal after the init gate): #2, `test_enroll_quotes_the_refusal_without_system_init`.

**`tools/refusal_mutations.py`: 25 of 25 killed** (the PR's 17 plus 8 from the brief). It ran in 269 s, under `.venv/bin/python tools/refusal_mutations.py --basetemp-root …/mut-b`.

## Fixture regeneration

`python3 tests/fixtures/claude/make_fixtures.py` changed one file, `org-block-synthetic/expected.json`, by one line (`evidence.auth`).

## Commands and results

1. The PR file plus `test_success_supersedes_cli_text_failures`: 43 passed.
2. The PR file (extended), `-n 4`: 27 passed. The run printed one `PytestUnraisableExceptionWarning`, a KeyboardInterrupt inside Hypothesis's GC timing callback; no test failed.
3. bounds, `-n 4`: 12 passed.
4. Group 1 (`test_claude_adapter`, `test_claude_stream`, `test_claude_identity`): 269 passed.
5. Group 2 (`test_handler_refusals`, `test_lane_answers`, `test_limit_from_model_text`, `test_task_hold_cas_wire`, `test_task_holds`, `test_codex_probe`, `test_enrollment_runner`, `test_timers_busy_read`): 745 passed, with the same warning.
6. Group 3 (fake `test_lane_pilot`, `test_lanes_reenroll`, `test_lane_fault`, unit `test_empty_provider_results` and both refusal files): **inconclusive.** It printed 436 passing dots to 100%, with no F or E, then hung before pytest's summary line, and my 570 s deadline killed its own session.
7. Step 3 (`tests/fake/test_state_contract.py`, `test_test_module_names.py`, `test_invariants_index.py`, `test_handler_refusals.py`): 425 passed.
8. The mutation tool: the first slice errored on a missing basetemp root (fixed in c5d736962), and the full run killed 25 of 25.

All runs used `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` with a new `--basetemp` under `sf-rv-b1`.

Step 4's grep found the old words pinned only in the PR test's docstring and `OLD_ASSERTIONS`, which are intentional, and in descriptive comments. No exact compare needed an update beyond the fixture.

## Deviations

- `uv sync --extra api` fails: pyproject has no `api` extra. I ran `uv sync --group dev`.
- xdist isn't a dev dependency on release/217. I installed `pytest-xdist==3.8.0` into the job venv only (no repo files changed) and didn't check it is the latest release.
- The step-2 run was split into three `-n 4` groups to fit the 600 s command cap. The host's load average was 110-130, and mkdtemp took 6.6 s.
- My grep found 15 files. I added the brief's `test_lane_fault.py` and `test_empty_provider_results.py`.
- The tool ran under `.venv/bin/python`, not `uv run`. The tool gained per-mutant killers, `--only`, `--list` and `--basetemp-root`, and now mutates refusals.py as well.
- The template doesn't say "Claude Code's words are in daemon.log". The brief fixes the least specific form's text, and a stamped refusal with no words has none there. C-9.3 states the daemon.log rule instead.
- C-25.7's closed-value list was extended for the two new sets. Without that, the contract's closed list would contradict the template.
- The group 3 run is unconfirmed (above). The two refusal files in it passed separately.

## Scratch folders

Everything this session created is under `$TMPDIR/sf-rv-b1`:
- `copy`, a `git archive` export used for test runs;
- `bt-*`, `mut-a`, `mut-b`;
- the logs and `deadline.py`.

`$TMPDIR/uv-cache` (the brief's `UV_CACHE_DIR`) lies outside it, and I left it in place.

## For #104's body

Rebased onto release/217 (b51f08f96). Beyond the PR's two commits:
- Enrolment reads only the CLI's words (`stderr` plus `cli_texts()`), as `classify` does.
- `_provider_error_texts` no longer quotes a model-served frame that carries a non-credential error kind.
- Release/217 renders every handler refusal from its closed template (C-25.7), so the PR's verbatim message alone could never reach the client. The enrolment refusal is now `claude-subscription-refused`. Its template quotes Claude Code's line only when it is one a recorded stream shows Claude Code printing, names the kind only when it is a credential kind, and always gives `ORG_BLOCK_CAUSES` as the fix. The full statement goes to daemon.log.
- The fixture's evidence text is updated, and the PR's properties are extended.
- New boundary properties cover model prose, CLI-only quoting, success supersedes, template-only wire text, and a differential against release/217's auth-dead predicate.
- `tools/refusal_mutations.py` kills 25 of 25.
- Fix r1 (2026-11-15, after merging release/217 at c8b61c95f): every line a Claude detail or enrolment refusal quotes comes from its source scrubbed whole, so a label outside the excerpt still masks its value; the differential against release/217 covers blank and nonblank rc-0 results and asserts the class both ways; C-9.3 limits the success exclusion to classification. The tool now kills 29 of 29.

## Fix r1

Session start: 2026-11-15 04:36:20 UTC (`date -u`). The fix started from cfd0545ef, the head review r1 read. The host load average stayed between 50 and 183 throughout, so every run below is serial.

### Commits

| Commit | What |
|---|---|
| `8b929a13f` | Merge release/217 (`c8b61c95f`), on its own |
| `b0fbf0581` | The three findings: code, tests, C-9.3 and its change-log entry |
| `cdcda5ac8` | `tools/refusal_mutations.py`: new anchors and fix r1's four mutants |
| (this section's commit) | This report section |

### Merge of release/217

- `git merge --no-commit --no-ff c8b61c95f4f5…` conflicted only in `docs/acceptance-contract.md`. `subfleet/refusals.py` auto-merged: release changed only the `task-hold-condition` rendering of `since`.
- `python3 ~/reviews/subfleet-hub/host-runs/merge_contract.py HEAD c8b61c95f4f5772e3ce8c85365b4335e07309c34` exited 0 with "merged against 85769b826: 257 clauses, none duplicated". (Its regex skips ids with a letter suffix.)
- A separate check of the merged file found 260 clause ids, counting suffixed ones such as C-5.8a, with none duplicated. The change log holds every entry from both sides. It has 66 blank-line gaps between entries; none is new (each separated the same two entries on one side), and none was lost.
- No unmerged paths remained. `8b929a13f` has parents cfd0545ef and c8b61c95f.

### Findings and changes

**P1, excerpting could defeat log scrubbing.** Whether a run is a refusal is still read from the text as written, so the class is unchanged. The words quoted are now taken from each source after it has been scrubbed whole.
- `subfleet/adapters/claude.py:352-372` adds three pieces. `SCRUBBED_AWAY` is the statement used when the scrub removed every match. `_scrubbed(text)` is `sessions.handoff.scrub_secrets(text, whole=True)`, which keeps line breaks. `_quoted(text, match)` scrubs, searches the match's own pattern in the scrubbed text, and excerpts that with `_first_line_containing`, which itself is unchanged.
- In `_subscription_refusal` (`claude.py:2202`), the stamped frame, the provider error texts and the corpus are each scrubbed before ORG_BLOCK_RE or the first-line search runs. Step 4, which quotes the provider error text's first line, now runs only for stamped refusals (`claude.py:2249`). A text refusal whose match was scrubbed away therefore never quotes an unrelated line: `line` is None, and the statement reads `no words: the words that matched lie inside text the scrubber removes` (`claude.py:2160`).
- I also fixed the same defect at the other excerpt sites, which go beyond line 2194. The PR's window excerpt replaced release's prefix cut everywhere, so any of them can drop a label:
  - `_first_auth_phrase` (`claude.py:2131`), whose `claude-authenticate` message goes to daemon.log;
  - classification's `cli-too-old`, no-init `auth-dead`, credits, limit and transient details (`claude.py:1735, 1768, 1809, 1833, 1901`), which the attempt row stores and daemon.log logs.
- The client reply is unchanged. The recorded line is a fixed point of the scrub, and a test asserts this, so the closed check still passes. The least specific form is still sent when no checked value applies.
- Tests, all in `tests/unit/test_claude_refusal_boundaries.py`. Each P1 test runs the real `ClaudeAdapter.enroll`, rewraps the error exactly as `_enroll_lane_locked` does (`daemon.py:1365-1367`), answers through the daemon's real `_answer` (`wire_reply`) and writes daemon.log through the real `daemon_log.ScrubbingStreamHandler` (`written_to_daemon_log`, line 252). `_never_logged` (line 415) also checks the exception, the reply, and the `auth-dead` detail and evidence.
  - `test_log_scrubs_a_password_whose_label_is_outside_excerpt` (line 431) is r1's 600-character case, now permanent.
  - `test_log_scrubs_a_password_whose_label_follows_the_marker` (line 442), the label-after case, has five cuts: the line is quoted whole (48 words), the cut falls inside the value (51, 52), inside the label (54), or before the label (120).
  - `test_the_recorded_line_reaches_the_wire_whole_beside_a_credential` (line 451) shows the client reply is unchanged.
- Under the P1 mutant (no scrub), r1's case puts part of the value into daemon.log, the exception, and the `auth-dead` detail and evidence. Cuts 48, 51 and 52 put it into the exception, detail and evidence, though daemon.log's own scrub caught those. Cuts 54 and 120 hold no part of the value.
- The quote invariant now reads the scrubbed corpus, as the adapter does. `_assert_quotes` (`tests/unit/test_claude_subscription_refusal.py:477`) and `test_every_quoted_line_is_the_cli_s` (line 318) were changed accordingly.

**P2, the differential missed relevant regressions.**
- `_release_217_auth_dead` (`test_claude_refusal_boundaries.py:550`) writes out b51f08f96's predicate whole:
  - `successful` as release defined it: rc 0, a result not marked `is_error`, and a final text (the result's, else the last assistant frame's) that is not blank;
  - then the CLI version gate;
  - then ORG_BLOCK_RE on the CLI corpus, a credential kind, or, without `system/init`, AUTH_SIGNATURE_RE (the branch the old copy left out).
- `test_what_release_217_called_auth_dead_still_is` (line 586) no longer skips rc-0 results. It asserts that classification is `auth-dead` exactly when release's predicate says so; it used to check one direction only. The enrolment exit-5 check is kept.
- `mixed_streams(blank=True)` (line 171) draws blank text for non-error results. Other callers' draws are unchanged.
- Explicit examples cover:
  - blank rc-0 results: refusal text in stderr, in `errors`, or after a CLI frame;
  - nonblank rc-0 successes, one with `oauth_org_not_allowed`;
  - the no-init signature branch;
  - empty streams.

**P2, C-9.3's success sentence.**
- In `docs/acceptance-contract.md:561`, "a verified success has none" became: only classification excludes a verified success (exit 0, a result not marked `is_error`, a final text that is not blank), and a blank result is no verified success. Enrolment has no such exclusion: a stream that carries the refusal and classifies `ok` still refuses enrolment with exit 5.
- The clause also states P1's rule: a quoted line is taken from its source scrubbed whole, then excerpted, and a match the scrub removed is not quoted.
- A dated change-log entry is at line 9.
- `test_enroll_verified_success_contract` (`test_claude_refusal_boundaries.py:361`, refusal carried by stderr or by a retry kind) pins the new wording: `ok` from classification, `claude-subscription-refused` with exit 5 from enrolment.

**P3s.** R1 labelled none. I did close two smaller gaps it reported:
- Its surviving mutant (error-result text dropped from `_provider_error_texts`) is now killed by `test_a_wordless_stamp_quotes_a_reworded_error_result` (line 400).
- Its note that no example guaranteed an empty stream is closed by the differential's explicit `("", "")` and `("", "HTTP 401 Unauthorized")` examples.

### Mutants

Each mutant was applied alone by `tools/refusal_mutations.py`, run from a scratch git copy of `cdcda5ac8` (`git archive` of `subfleet`, `tests`, `tools` and `pyproject.toml`), never the worktree. The tool restores each file from its pristine text. After every slice I ran `cmp` on both mutated files against the worktree, and they were byte-identical every time.

| # | Mutant | Killed by |
|---|---|---|
| 26 | `_scrubbed` returns its text unscrubbed (undoes P1) | `test_log_scrubs_a_password_whose_label_is_outside_excerpt` |
| 27 | The refusal gate is `non_error_result`, not `successful` (undoes P2) | `test_what_release_217_called_auth_dead_still_is` (explicit blank-result example) |
| 28 | Enrolment lets a verified success supersede the refusal (makes the old broad C-9.3 sentence true) | `test_enroll_verified_success_contract[stderr]` |
| 29 | `_provider_error_texts` omits an error result's text (r1's survivor) | `test_a_wordless_stamp_quotes_a_reworded_error_result` |

The tool's full tally is **29 of 29 killed**: mutants 1-13, 14-17, 18-20, 21-25 and 26-29. Two of the earlier mutants need notes:
- Mutant 9 (the stamped frame is not looked at): its named killer passed. The tool then ran both files, and `test_classify_quotes_the_recorded_refusal_with_its_error_kind` killed it.
- Mutant 18: the 14-25 slice's `timeout 595` killed the tool partway through it, so mutant 18 was rerun in the 18-20 slice and killed there.

I also applied mutant 26 alone and ran every P1 test against it: 4 failed (r1's case and label-after cuts 48, 51, 52) and 3 passed (cuts 54 and 120, and the recorded-line test, which guards the reply rather than the scrub).

### Commands and results

Every pytest run used `.venv/bin/python -m pytest -q -p no:cacheprovider`, with `UV_CACHE_DIR=$TMPDIR/uv-cache` and a new `--basetemp` under `$TMPDIR/sf-rv-f1/` whose name contains `pytest`. Each ran with one worker: the dev group has no pytest-xdist.

| Run | What | Result | UTC | Load (start → end) |
|---|---|---|---|---|
| — | `git status --short --untracked-files=no` | timed out at 100 s (rc 124) | 04:43 | 183.01 |
| — | `uv sync --group dev --extra api` | error: no `api` extra at this head | 05:23 | 96.05 |
| — | `uv sync --group dev` | ok, CPython 3.15.2 | 05:24 | — |
| 1 | Both refusal files, P1 code only | 39 passed | 05:34:34-05:40:11 | 129.14 → 120.97 |
| 2 | Both refusal files, all new tests | 49 passed in 301.89 s | 05:45:10-05:50:27 | 49.79 → 134.37 |
| 3 | `test_claude_adapter`, `test_limit_from_model_text`, `test_empty_provider_results`, `test_claude_stream` | 669 passed in 262.29 s | 05:52:43-05:57:26 | 150.46 → 104.47 |
| 4 | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | 435 passed in 296.75 s | 05:57:38-06:02:57 | 111.96 → 108.75 |
| m1 | Mutants 26-29 | 4 of 4 killed | 06:04:32-06:06:05 | 132.32 → 100.55 |
| m2 | P1 tests under mutant 26 | 4 failed, 3 passed, in 16.59 s | 06:06-06:07 | 71.74 → 96.81 |
| m3 | Mutants 1-13 | 13 of 13 killed | 06:08:08-06:13:20 | 109.44 → 138.59 |
| m4 | Mutants 14-25 | 14-17 killed, then `timeout 595` stopped it during 18 (rc 124) | 06:13:46-06:23:44 | 141.94 → 83.57 |
| m5 | Mutants 18-20 | 3 of 3 killed | 06:24:27-06:27:16 | 82.27 → 122.15 |
| m6 | Mutants 21-25 | 5 of 5 killed | 06:27:28-06:28:17 | 124.23 → 123.09 |
| 5 | `tests/fake/test_lanes_enroll.py test_record_secrecy.py test_lane_fault.py test_lane_pilot.py` | 103 passed in 307.56 s | 06:28:51-06:34:03 | 129.23 → 117.58 |

Runs 1-5 and m1-m3, m5 and m6 finished. M4 did not finish; its unfinished mutants were rerun in m5 and m6. A check after m4 and after run 5 found no leftover process of mine. Every pytest run was serial. Other checks:
- All 29 of the tool's anchors applied exactly once before the run.
- `git diff --quiet` at the end: no tracked file is left changed.

### Deviations

- I called `merge_contract.py` with the release SHA rather than the name `origin/release/217`, which points at the same commit.
- `--extra api` no longer exists, so I synced the dev group only. The dev group has no pytest-xdist, so every run was serial.
- P1's fix reaches beyond `claude.py:2194`: to `_first_auth_phrase` and classification's other excerpt sites, for the reason given under P1. The fixture-backed tests in run 3 passed unchanged.
- The permanent r1 test is my own, written from r1's description. The reviewer's code is not available: their worktree is under `~/.subfleet`, which I may not read, and their scratch was removed. It copies `_enroll_lane_locked`'s three-line rewrap, as the file's existing `on_the_wire` helper does, rather than building a daemon to call `_enroll_lane_locked` itself.
- One commit holds all three findings, because `tests/unit/test_claude_refusal_boundaries.py` carries changes for both P1 and P2, and splitting it would need interactive staging.
- I lost time to two mistakes of mine:
  - My first merge-commit command began with a `cat >` that waited on stdin. I stopped it after about seven minutes. It had left an empty file, `$TMPDIR/sf-rv-f1-merge-msg.txt`, outside the scratch folder, which I removed with a guarded `os.remove` that asserted `sf-rv-f1` was in its path.
  - The m4 timeout killed the mutation tool before its own restore ran, leaving the scratch copy's `claude.py` mutated at line 1003. I restored it from the scratch commit and checked the bytes with `cmp` before m5.
- One probe of mine (where the value shows under mutant 26) made `TemporaryDirectory(prefix="sf-rv-f1-probe-")` folders under `$TMPDIR`. Its context manager removed them, not a guarded `shutil.rmtree`. `test_any_matching_corpus_is_quoted_by_the_classifier` also makes and removes its own temporary folders there.
- The runs made an untracked `.hypothesis/` in the workspace, which is not ignored, and ignored `__pycache__/` and `.venv/` folders. Nothing in them is committed.
- I ran no `tools/refusal_mutations.py` slice and no test against the worktree's own files while they were mutated, and ran no full suite.

### Scratch folders

`$TMPDIR/sf-rv-f1` (`TMPDIR=/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`) holds:
- the scratch git copy `tree/`;
- the pytest basetemps (`pytest-run1`-`5`, `pytest-mut-*`);
- `claude_b51.py`, the base adapter read from b51f08f96;
- `claude.pristine.py`;
- the commit-message files.

It is left in place. `$TMPDIR/uv-cache` is shared and also left in place. Outside the scratch folder I created only the empty file and the probe's temporary folders listed under Deviations; both are gone.

Head built: the commit that adds this section (its parent is `cdcda5ac8`).
