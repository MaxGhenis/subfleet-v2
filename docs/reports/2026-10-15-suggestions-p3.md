# Suggested conversations: review r8 P3 follow-ups

Base: `2012bbed4926` on release/217 after #181. Read `~/reviews/subfleet-2110/suggested-conversations/review-r8.md` before editing. Work stayed in the assigned checkout, with no Git remote operations or sub-agents.

## Changes

- An existing secret-shaped folder is refused with checked `workspace_status=secret-shaped`, explaining that its path cannot be used because it looks like it holds a secret. The wire and CLI select the same fixed reason and repair without echoing the path.
- Only a checked `trust` guard verdict receives the SHA-256/TRUST/pinned-version hint. Environment, configuration and version verdicts receive their own fixed repairs.
- Checked `unreadable-spelling` context restores the searchable-directory repair in `workspace.check`, matching `conversation.create`.
- Both start and dismiss orphan tests now assert the exact person-only reason and unchanged `open` state. The adjacent no-terminal cases assert the reason too.
- The shared whole-text scrubber removes repr-escaped and unterminated quoted credential assignment values through their line or record boundary. One advancing line-boundary iterator preserves linear scanning. Complete ordinary quoted assignments retain their existing treatment; approval masking uses separate token-only rules.
- The bounded property covers eight quoting styles, credential-key families with generated prefixes, dummy secret values and arbitrary trailing text (40 examples per style). A real config-parser refusal is captured through the request responder into a temporary `daemon.log`.
- The retained-event secrecy fixture now supplies a fixed historical raw diagnostic, independent of the repaired scrubber.
- Amended C-25.7 (suggestion/handler refusal templates and local daemon-log scrubbing), C-29.14, C-26.10, C-14.2 and C-23.14, with a version-history entry citing #181 review r8.

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
