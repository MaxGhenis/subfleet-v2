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
