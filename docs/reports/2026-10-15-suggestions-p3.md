# Suggested conversations: review r8 P3 follow-ups

Base: `2012bbed4926` on release/217 after #181. Read `~/reviews/subfleet-2110/suggested-conversations/review-r8.md` before editing. Work stayed in the assigned checkout, with no Git remote operations or sub-agents.

## Changes

- An existing secret-shaped folder is refused with checked `workspace_status=secret-shaped`, explaining that its path cannot be used because it looks like it holds a secret. The wire and CLI select the same fixed reason and repair without echoing the path.
- Only a checked `trust` guard verdict receives the SHA-256/TRUST/pinned-version hint. Environment, configuration and version verdicts receive their own fixed repairs.
- Checked `unreadable-spelling` context restores the searchable-directory repair in `workspace.check`, matching `conversation.create`.
- Both start and dismiss orphan tests now assert the exact person-only reason and unchanged `open` state. The adjacent no-terminal cases assert the reason too.
- The shared whole-text scrubber removes repr-escaped and unterminated quoted credential assignment values through their line or record boundary (round 2: a repr-escaped value that closes now stops at its matching quote). One advancing line-boundary iterator preserves linear scanning. Complete ordinary quoted assignments retain their existing treatment; approval masking uses separate token-only rules.
- The bounded property covers eight quoting styles, credential-key families with generated prefixes, dummy secret values and arbitrary trailing text (40 examples per style). A real config-parser refusal is captured through the request responder into a temporary `daemon.log`.
- The retained-event secrecy fixture now supplies a fixed historical raw diagnostic, independent of the repaired scrubber.
- Amended C-25.7 (suggestion/handler refusal templates, and scrubbing of the refusal details the request responder writes to daemon.log; not every daemon.log writer, see round 2), C-29.14, C-26.10, C-14.2 and C-23.14, with a version-history entry citing #181 review r8.

## Commits

The original worktree's shared Git metadata is outside the authorized writable roots. Commits are on `fix/suggestions-p3-217` in workspace-local `.git-local`, with read-only alternates for base history; the caller's index and checkout were preserved. Inspect them with `git --git-dir=.git-local log`. The report is committed separately after verification.

- `8fb56b938`: test: cover review r8 refusals and daemon log secret scrubbing
- `58867081a`: test: capture local diagnostic logs and vary credential keys
- `25ec81981`: fix: retain checked workspace and guard refusal repairs
- `95e16da9e`: fix: scrub repr-escaped and incomplete credential assignments
- `e4bfc4c7a`: test: exercise supported brief-file input in CLI regression
- `63ede6603`: docs: amend refusal and scrubbing contracts for review r8

## Regressions and final verification

At the base revision, the existing-folder wire regression, both real non-trust guard cases, and the protected-path repair regression failed. The scrubber property failed for repr-escaped single/double quotes and unterminated single/double quotes; its line-boundary regression failed too. The actual responder's config-error log visibly retained the dummy `SECRET7-left` value. Test-fixture corrections (INFO capture, actual config-error wording, and supported CLI brief-file input) were committed without rewriting history.

Final full-file results below count each case once per file, despite the required repeated holds. Every runnable case passed. Runs used Python 3.14.7t with `PYTHON_GIL=1`, one foreground pytest at a time under its own `lockf -k` hold, `-p no:cacheprovider`, Hypothesis's `ci` profile, and task-local Hypothesis/uv cache directories. No deep profile or xdist workers were used. Each measured hold was under 20 minutes; waiting for the shared lock was unbounded.

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/e2e/test_suggestions_e2e.py` | 0 | 2 |
| `tests/e2e/test_suggestions_r1b_peer_e2e.py` | 0 | 8 |
| `tests/fake/test_record_secrecy.py` | 44 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 |
| `tests/unit/test_conversation_redact.py` | 9 | 0 |
| `tests/unit/test_conversation_service.py` | 98 | 0 |
| `tests/unit/test_daemon_log_scrubbing.py` | 10 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 |
| `tests/unit/test_handler_refusals.py` | 313 | 0 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 |
| `tests/unit/test_round10_refusal_context.py` | 46 | 0 |
| `tests/unit/test_sessions_handoff.py` | 112 | 0 |
| `tests/unit/test_suggest_cli.py` | 7 | 0 |
| `tests/unit/test_suggest_hint.py` | 10 | 0 |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 0 |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 |
| `tests/unit/test_suggestions.py` | 29 | 0 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |

Unique cases: **1177 passed, 10 skipped**, across 26 files.

| Hold | Passed | Skipped | Pytest seconds |
| --- | ---: | ---: | ---: |
| touched | 691 | 8 | 142.06 |
| protocol | 322 | 0 | 8.43 |
| refusals | 321 | 0 | 117.02 |
| record-secrecy | 44 | 0 | 111.96 |
| suggestions | 177 | 10 | 507.67 |
| round10 | 46 | 0 | 3.74 |
| conversation-service | 98 | 0 | 186.90 |
| state-contract | 37 | 0 | 10.28 |
| module-names | 1 | 0 | 0.28 |
| shared-scrubber | 121 | 0 | 55.83 |

## Mutations

Each mutation ran in its own foreground, shared-lock hold, failed exactly the named test, and restored production code in `finally`:

| Mutation | Named failing test |
| --- | --- |
| Remove the secret-shaped producer status | `tests/unit/test_round10_refusal_context.py::test_existing_secret_shaped_workspace_explains_refusal_on_wire_and_cli` |
| Add trust-pin advice for every checked verdict kind | `tests/unit/test_round10_refusal_context.py::test_real_guard_nontrust_verdict_gives_its_own_repair[environment-binaries]` |
| Drop the unterminated-quote redaction rule | `tests/unit/test_daemon_log_scrubbing.py::test_daemon_log_scrubs_config_errors_with_unterminated_repr_quotes` |

All three mutations were caught. After restoration, **14 targeted regression/property cases passed**.

## Unrun and cleanup

The 10 native e2e cases in the two suggestion e2e files skipped because the sandbox cannot read macOS boot identity for their required ps/sysctl inspection. This includes both strengthened orphan cases; their guards were not bypassed. The full repository suite, Python 3.12, and the three frontend suggestion probes outside `tests/**/test_suggest*.py` were not run.

Removed the task scratch directory, including temporary XML/JSON evidence and Hypothesis/uv caches. No .hypothesis folder remains, and all foreground test runners have finished. No logs, JSON evidence or bundles are staged, and nothing is pushed.

## Round 2: review r1 of #225

Head before this round: `f0d410f00` (CI 10 of 10). Review: `~/reviews/subfleet-2110/suggestions-p3/review-r1.md`, REQUEST_CHANGES with two P2s and five P3s. One P3, that several daemon.log writers bypass the scrubber, is out of scope here; the hub opens it as a separate follow-up. This round narrows the PR's claims to match it.

### Merge

`origin/release/217` at `f305f6214` (#198, #224) is merged as `db35e17f9`. There was one conflict, in the contract's "Changes in version 3": both sides added a 2026-10-15 bullet at the top of the list (#181 review r8 here, D-ST1 #198 round 8 on release). Both bullets are kept, this PR's first. `service.py` and `refusals.py` auto-merged in disjoint hunks.

### Changes

1. **A repr-escaped quoted key (P2).** `_ASSIGN_SEPARATOR_RE` is now `(?:\\*["'])?\s*[:=]\s*`, so a run of backslashes may escape a key's closing quote: a repr's `\'password\'`, or `\"password\"` and `\\\"password\\\"` from JSON encoded two or three times. `_neutral_break` and `_key_ends_at(..., quoted=True)` skip the same backslashes, so an excerpt is never cut between such a key and its value.
   - The review's two configparser inputs reach a temporary daemon.log through `host_push._workspace_refused` and the real request responder. They no longer carry `SECRET7quotedkey9`.
   - Nor do `{\"password\": \"…\"}` or JSON encoded two and three times.
2. **Excerpt merge (P2).** In excerpt mode, `_scrub_named_values` now masks the union of overlapping quoted candidates, as whole-text mode does.
   - `handoff.clean("export PASSWORD='SECRET7aa TOKEN='SECRET8bb", 4000)` is `export PASSWORD='[REDACTED]'`.
   - `password='SECRET7aa token='SECRET8bb' ok` is `password='[REDACTED]' ok`, in all three modes, and in a long text's head and tail excerpts.
   - A union spans a line break only inside a separator, between a key and its value. `_neutral_break` never cuts there, so no union crosses an excerpt cut.
   - The scrubber commit also closes three adjacent shapes, each leaked at base (the third in excerpts only) and clean at head in all three modes:
     - a key inside a quoted match whose value follows on the next line (`password='SECRET7a token:\n SECRET8b`);
     - a plain value that ends in a key (`password=xtoken = SECRET8c`);
     - in excerpts, a quoted value whose key a token rule would take first (`Bearer okPASSWORD='SECRET7d'`). Excerpts now mask quoted values before the token rules, as whole texts do.
3. **`conversation.create` and C-26.10 (P3).** `ConversationService.respond` gives a non-suggestion op's `ConversationError` that carries a checked `workspace_status` the checked template (`suggestions.scrub_error`) and repair (`suggestions.error_fix`). It writes the error's own words to daemon.log only, through `refusals.log_detail`.
   - `conversation.create` now answers an `EACCES` spelling of `<HOME>/.claude` with `protected-workspace: the folder cannot be checked because a protected path's spelling cannot be read`, and the repair "make every directory above it searchable, or choose Ask or read-only".
   - Neither the path nor the errno reaches the wire.
4. **A repr value that closes keeps its line (P3).** In one scan, `_escaped_quote_ends` indexes each backslash-escaped quote's next quote of the same kind, escaped by a run of the same length. A repr-escaped value closes there, and the redaction falls back to the line end only when no such quote exists.
   - `config error: password=\'SECRET7x\' at policy.json line 4; expected a string` keeps `at policy.json line 4; expected a string` in all three modes.
   - A location on the next line also survives.
   - **Not done as briefed:** the brief asked that both of the review's location-bearing inputs keep their location text. The review's other one, `invalid token='abc at /etc/subfleet/policy.json:12 (line 3)`, has no closing quote at all, and its quote is not escaped. Under the brief's own fallback (the line end only when no matching quote exists) and C-23.14 ("a missing closing quote never leaves the value visible"), it still redacts through its line. Keeping that location would mean guessing where an unterminated value ends, and a value with spaces would then show. `test_a_truly_unterminated_value_still_redacts_through_its_line` pins this trade.
5. **Guard repair table (P3).** `refusals.GUARD_REPAIRS` is the one table. `_choices()["guard_kind"]` is its keys, and `render` reads `GUARD_REPAIRS.get(kind, …)`, whose fallback is the generic "rerun subfleet doctor". A test adds a fifth kind to `_choices` and renders it; every admitted kind is rendered with and without the by-policy flag.
6. **Test gaps (P3).** These tests are added:
   - `("protected-workspace", "unreadable-spelling")` in `test_recorded_workspace_refusal_keeps_its_reason_and_repair`;
   - a round trip of that status through `response_reason`, `response_details`, `stored_error` and the CLI's `DaemonError`;
   - a real preflight test for the `version` verdict, `test_real_guard_version_verdict_asks_for_the_supported_version`.
7. **Claims narrowed.**
   - C-25.7's daemon.log sentence now covers the refusal details written through `refusals.detail_line`: the request responders', a job's refusal and a turn submit's. It says this is not a guarantee for every daemon.log line.
   - C-23.14 states the key quoting, the escaped-close rule and the union rule.
   - C-26.10 states `create`'s template.
   - The round-1 bullets above are annotated, and a version-history entry is added.

### Commits

These are committed on the detached job checkout, in the shared Git metadata. Nothing is pushed.

- `db35e17f9`: Merge release/217 (#198, #224) into fix/suggestions-p3-217
- `2e6410f0e`: test: cover review r1 of #225 scrubber and refusal gaps
- `349542110`: fix: scrub repr-quoted keys and close repr values at their match
- `d84bb3efb`: fix: build guard kinds from one repair table, never raise rendering
- `9670e4eca`: fix: answer an unreadable protected spelling with the checked template
- `d4a243ba2`: docs: amend scrubbing, daemon.log and folder refusal contracts for #225 r1
- this report's commit

### Regressions at base

The new tests import modules that #198 added, so `f0d410f00`'s own tree can't import them. The base is therefore `db35e17f9`: `f0d410f00` merged with release/217, without this round's fixes. Release changed none of `sessions/handoff.py`, and only disjoint hunks of `service.py` and `refusals.py`. Each case ran as a direct call (see Tests run). At base, 48 of the 101 cases fail; at head, all 101 pass.

| Item | Regression | Base | Head |
| --- | --- | --- | --- |
| 1 | `test_daemon_log_scrubbing.py::test_daemon_log_scrubs_a_repr_escaped_quoted_key` (2) | 2 failed | 2 passed |
| 1 | `test_daemon_log_scrubbing.py::test_daemon_log_scrubs_json_encoded_more_than_once` (3) | 3 failed | 3 passed |
| 1 | `test_daemon_log_scrubbing.py::test_every_key_quoting_separator_case_and_value_style_is_scrubbed` | failed | passed |
| 1 | `test_daemon_log_scrubbing.py::test_credential_assignment_scrubbing_never_keeps_the_value` (60) | 30 failed: every repr-single, repr-double and double-encoded key | 60 passed |
| 1 | `test_sessions_handoff.py::test_a_long_text_is_never_cut_between_a_repr_quoted_key_and_its_value` (3) | 3 failed | 3 passed |
| 2 | `test_sessions_handoff.py::test_an_excerpt_hides_a_second_assignment_whose_quote_closed_the_first` (2) | 2 failed | 2 passed |
| 2 | `test_daemon_log_scrubbing.py::test_two_assignments_of_one_kind_on_a_line_both_lose_their_values` (15) | 4 failed: `'` and `"` with the first unclosed | 15 passed |
| 3 | `test_round10_refusal_context.py::test_create_answers_an_unreadable_protected_spelling_with_the_checked_template` | failed | passed |
| 4 | `test_daemon_log_scrubbing.py::test_a_repr_escaped_value_that_closes_keeps_the_rest_of_its_line` | failed | passed |
| 5 | `test_round10_refusal_context.py::test_a_guard_kind_without_a_repair_renders_the_generic_one` | failed (`KeyError: 'fifth'`) | passed |

These coverage cases pass at base as well as at head:

- `test_a_truly_unterminated_value_still_redacts_through_its_line`;
- `test_scrubbing_any_text_twice_changes_nothing_more`;
- the `DaemonError` round trip;
- the recorded `unreadable-spelling` refusal;
- the eight cases of `test_every_guard_kind_the_checks_admit_renders_its_repair`.

### Mutations

Each mutation was applied to a scratch copy of head's `subfleet/` and checked by the same direct calls.

| Mutation | Named failing tests |
| --- | --- |
| Restore the old separator, `["']?\s*[:=]\s*` | 39 of 101: `test_daemon_log_scrubs_a_repr_escaped_quoted_key` (both), the three JSON cases, the cross product, the 30 repr- and double-encoded-key property cases, `test_a_long_text_is_never_cut_between_a_repr_quoted_key_and_its_value` (all three) |
| Drop the excerpt-mode merge (`if quoted:` back to `if quoted and multiline:`) | 12: `test_an_excerpt_hides_a_second_assignment_whose_quote_closed_the_first` (both), and the ten neither/second cases of `test_two_assignments_of_one_kind_on_a_line_both_lose_their_values` |
| Drop the repr close-at-matching-quote rule (an escaped opening's `closing = None`) | 4: `test_a_repr_escaped_value_that_closes_keeps_the_rest_of_its_line`, and the three cases of `test_daemon_log_scrubs_json_encoded_more_than_once` |

### Properties

All are bounded and derandomized:

- **The scrubber property:** key quoting (bare, single, double, repr-single, repr-double, double-encoded) × value style (10). Each of the 60 cases draws 12 examples of the key family and its case (as written, lower, upper, title), the separator (`=`, `: `, ` = `), and arbitrary leading and trailing text. No secret survives in the whole text, the excerpt, the daemon.log line or the daemon.log words, and scrubbing the whole text, the excerpt or the daemon.log line again changes nothing. An exhaustive cross product (6 × 3 × 4 × 10 = 720 texts) backs it.
- **Two same-kind assignments on one line:** five opening quotes (`'`, `"`, `\'`, `\"`, `\\\"`) × three closings (neither, second, both), 15 examples each, in every mode.
- **Idempotence for any text:** 200 examples, in each of the three modes.

### Seeded fuzz

These are outside pytest, at head and at base `db35e17f9`, and count (text, mode, secret) leaks.

- **Modelled on the review's fuzz:** 3,000 texts, seed 225. Keys come in six quotings, with six separators (two of them span a line break) and twelve value forms, plus filler. Head has 0 leaks; base has 10,141 across 2,193 texts. Neither has an idempotence failure.
- **An extended shape:** 4,000 texts, whose fillers add `Cookie: `, `Bearer `, `--token `, ` `, reminder tags and more backslash runs. Head has 18 leaks in 8 texts; base has 12,506 in 2,903.
  - Every head leak is a `Cookie:` header or a `Bearer` taking the key before the assignment rules see it.
  - Minimal inputs, each leaking at base too: `Cookie: x_auth-token :\n SECRET7c` (whole and excerpt; head's daemon.log line is clean), `Bearer PASSWORD=SECRET7b`, and `Bearer \rclient-secret: SECRET7r`.
  - One text leaks at head and not at base. Its double-encoded value now closes at its match, and the `Bearer \rclient-secret: …` after it is the third minimal input. Base hid it only by redacting the rest of the line.
  - These are pre-existing rule-order leaks, outside this round's items: a follow-up below.

### Linear time

This is the changed scanner, CPU time (`thread_time`), best of 3, at n = 4k / 16k / 64k repeats, in whole-text mode unless marked. Load average was about 23 to 35.

| Input (repeated n times) | Head (ms) | Base `db35e17f9` (ms) |
| --- | --- | --- |
| `password='x ` | 6.8 / 28 / 115 | 8.6 / 38 / 135 |
| repr `password=\'x ` | 7.6 / 30 / 123 | 5.8 / 22 / 110 |
| `password='` | 5.8 / 22 / 93 | 4.7 / 22 / 84 |
| `password='x' token='` | 44 / 180 / 719 | 43 / 181 / 704 |
| daemon.log path, `password='x' token='` | 107 / 438 / 1,907 | 87 / 357 / 1,828 |
| repr key `\'password\': \'x ` | 11 / 49 / 203 | 18 / 76 / 295 |
| daemon.log path, repr key `\'password\': \'x ` | 13 / 53 / 239 | 36 / 143 / 589 |
| `password=\\\"x ` (a run of three) | 8.8 / 32 / 136 | 5.8 / 25 / 109 |
| `password=` + 50 backslashes + `x ` (n / 10 times) | 6.2 / 25 / 103 | 4.4 / 18 / 70 |
| `token=` | 11 / 38 / 179 | 7.3 / 28 / 115 |
| `a=xtoken\n= y ` | 31 / 133 / 565 | 28 / 105 / 421 |
| `\'password\'\n:` | 13 / 41 / 161 | 12 / 48 / 206 |
| `password='` then `token=` n times | 3.0 / 12 / 49 | 2.5 / 16 / 51 |

- Both trees grow about 4× for each 4× of input; head's ratio from 16k to 64k is 3.9× to 4.7×.
- Head is within 1.6× of base, and faster on the repr-key shapes.
- In excerpt mode, a text over 262,144 characters has only its head and tail excerpts scrubbed (2 to 40 ms here at 64k repeats). Below that bound, excerpt mode tracks the whole-text times.

### Tests run

- **Since this round, per the hub:** the hub's host run at `89b9653ac` passed 1,273 tests in all the brief's groups (212, 748 and 313), and CI passed 10 of 10 checks at `b2dc9946b`. What follows is what this round's own session could run.
- **Pytest: none ran in this round's session.** I made ten attempts on the shared suite lock between 14:15 and 15:44, 61 minutes of waiting in all. Each was a foreground `lockf` wait of 330 to 470 s, bounded so that the hold would fit in a 600 s tool call; none acquired the lock. The lock had 6 to 12 waiters, and one holder had been running pytest for over 11 minutes. The last attempt began at 15:38:08, just past the 4 h 30 m mark (15:37:52), and none was started after it.
- **Direct calls:** this round's 101 new or changed cases ran as direct calls, not under pytest, in plain Python processes outside the lock. Each test function was called with its parameters, a temporary `tmp_path` and pytest's `MonkeyPatch`; the `svc` fixture was built as `test_conversation_service.py` builds it. The cases were 84 in `test_daemon_log_scrubbing.py`, 5 in `test_sessions_handoff.py` and 12 in `test_round10_refusal_context.py`.
  - Head: 101 passed.
  - Base: 53 passed, 48 failed.
  - Mutations: 39, 12 and 4 failed.
- **Probes:** the review's inputs and the minimal leak inputs above, in all three modes, at head and at base.

| File | Pytest this round | Direct calls at head |
| --- | --- | --- |
| `tests/unit/test_daemon_log_scrubbing.py` | not run | 84 passed |
| `tests/unit/test_sessions_handoff.py` | not run | 5 of its cases passed (the two new tests); the other cases not run |
| `tests/unit/test_conversation_redact.py` | not run | none |
| `tests/unit/test_handler_refusals.py` | not run | none |
| `tests/unit/test_daemon_refusals_cli.py` | not run | none |
| `tests/unit/test_round10_refusal_context.py` | not run | 12 passed; `test_real_guard_version_verdict_asks_for_the_supported_version` and the older cases not run |
| `tests/unit/test_protocol_secrecy.py` | not run | none |
| `tests/fake/test_record_secrecy.py` | not run | none |
| `tests/**/test_suggest*.py` | not run | none |
| `tests/unit/test_conversation_service.py` | not run | none |
| `tests/fake/test_state_contract.py` | not run | none |
| `tests/unit/test_test_module_names.py` | not run | none |

This round's session ran none of the existing cases of the files the brief lists; the hub's run and CI, above, ran them before #225 merged.

### Follow-ups

- **Review r1's P3: several daemon.log writers bypass the scrubber.** The hub opens this one: a handler-level `logging.Filter` running `scrubbed_line`, plus scrubbing `sys.excepthook` and `threading.excepthook`. Done as D-LOG1: `docs/reports/2026-10-17-daemon-log-scrub.md`.
- **A `Cookie:`/`Authorization:` header or `Bearer` takes a key whose value follows it.** Examples: `Cookie: x_auth-token :\n SECRET7c`, `Bearer PASSWORD=SECRET7b`, `Bearer \rclient-secret: SECRET7r`. This leaks at base and at head, in whole-text and excerpt mode, and in daemon.log for the `Bearer` forms. It is a rule-order question, since the token and header rules run before the assignment rules, and it is outside this round's items.

### Cleanup

I removed both attempts' scratch folders, including fuzz and timing evidence and the Hypothesis storage. There is no `.hypothesis` folder, and no process of mine is running. No logs, JSON evidence or bundles are staged, and nothing is pushed.
