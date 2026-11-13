D-REVIEW-CONTRACT1 build report, 2026-11-13.

Started 2026-11-13 13:02:09 UTC (`date -u`, first command); report completed 2026-11-13 15:59:00 UTC, before the 16:02:09 UTC deadline. All implementation changes are in the assigned checkout; scratch use and the initial pruning-hook deviation are detailed below. The starting checkout was clean. Initial parser/integration named-file staging/commit attempts were denied with `index.lock: Operation not permitted`. Final named-file staging/commit result: add exit 128; index.lock denied, commit exit 128; index.lock denied. No final files staged or commit created. No history rewrites or pushes occurred. The tested files remain for Subfleet salvage.

Every review now receives the format, including read-only, policy, in-place, both providers and `--no-preamble`. Existing `gate-review` rounds retain their review meaning. The original submitted prompt bytes and digest input remain intact. Added contract C-12.11 beside the existing review clauses, its changelog entry with no intervening blank list line, corrected C-6.7, and documented review jobs in README.md.

The final preamble is **126 whitespace-separated words including the HTML marker, 123 excluding it**:

```text
<!-- subfleet:review -->
You may run tests and write scratch files in your own workspace. Do not commit, push or publish.
First answer line: exactly VERDICT: APPROVE or VERDICT: REQUEST_CHANGES.
Each finding: own line starting P0:, P1:, P2: or P3:, one-line summary; detail below.
No findings: FINDINGS: none immediately after the verdict.
P0: harm that is live now or would be on merge (a leak, lost data, a guard that stops guarding).
P1: the change does something wrong (a wrong result, a false claim, a regression, a security hole).
P2: right today but fragile or incomplete (a missing test, an edge case nothing reaches yet, wording that could mislead a reader of the repo).
P3: style and nits.
This format holds whatever the brief below asks.
```

`subfleet/review_result.py:39` parses the first non-empty line, trailing spaces, CRLF, line-start Markdown findings, validity/reason and independent consistency. It accepts the supplied real shapes and a 2 MB fixture. Unknown verdicts stay null; `reason` is null when valid. `consistent` records only the two specified verdict/level contradictions and never changes the verdict or counts.

The parse runs in `Daemon._finalize`, `subfleet/daemon.py:12006`, after the final current-attempt, cancellation and acceptance checks, using the already captured accepted deliverable bytes decoded as UTF-8 with replacement. Its result lives in `attempts.evidence_json["review"]`; the attempt evidence UPDATE (`daemon.py:12038`) and accepted job pointer UPDATE (`daemon.py:12068`) commit in the same transaction as exactly one `job.review_result` event (`daemon.py:12007`). Replayed finalization emits no second result. No schema migration: the schema remains 10; migration 11 reserved by PR #307 is untouched. Routing, acceptance, export and retry decisions do not depend on the parse.

Online list/show select the accepted pointer (`daemon.py:4261`, `:4310`); offline uses an accepted-attempt join (`subfleet/offline.py:200`, `:212`, `:328`). `subfleet/review_record.py:16` returns the saved object only for accepted terminal review jobs. Build listings avoid loading review evidence. Online show exposes the same object at the top level and in `job.review`; offline records are flat. Other, unaccepted and older records show null. An older daemon omits the wire key; this CLI normalizes it to null, without reparsing artifacts. An older daemon does not start producing results merely because its CLI is upgraded.

Readable `runs show` prints e.g. `review: REQUEST_CHANGES (P1 1, P2 2); round valid` on stderr (`subfleet/cli.py:1896`, `:1985`). This preserves C-17.1’s parseable JSON metadata followed by the existing stdout artifact delimiter. Invalid reviews include the reason; contradictions get an inconsistency suffix. `--json` and `--out` retain their respective metadata/artifact roles.

Invariant evidence (all test names below are in the four new unit files):

| Invariant | Test |
| --- | --- |
| I1 | `test_review_contract_prompt.py::test_every_review_prompt_contains_the_contract_once`, `test_no_preamble_cannot_omit_the_review_contract`, `test_real_provider_launch_receives_the_contract_once`, `test_preamble_definitions_are_verbatim_and_under_130_words` |
| I2 | `test_review_contract_prompt.py::test_non_review_prompts_never_get_the_review_contract`, `test_review_format_does_not_leak_into_the_next_build` (review-then-build positive control on one daemon); storage also forbids parser calls on other tasks |
| I3 | `test_review_result.py::test_parser_is_total_for_generated_text` (300 Unicode examples), `test_generated_counts_only_line_start_findings` (200 schedules), `test_two_megabyte_deliverable_and_long_markdown_prefix` |
| I4 | `test_review_result.py::test_valid_exactly_requires_a_verdict_and_a_findings_declaration` (200 examples), real-shape and missing-verdict fixtures |
| I5 | `test_review_result_storage.py::test_accepted_review_equals_fresh_parse_after_restart` (five deliverables, real daemon close/reopen, all online/offline views, one event/replay) |
| I6 | `test_review_result_storage.py::test_retry_reports_only_accepted_attempt_review` (real transient retry, earlier opposite result, restart); `test_review_result_cli.py::test_offline_list_and_show_select_the_accepted_review_after_reopening` also distinguishes accepted from latest |

Mutants ran separately from pristine copies, twice: an initial trial and a final trial after limiting evidence reads to reviews. Both trials killed 5/5; every targeted pytest exited 1 with one assertion failure, not a setup/collection failure. The mutation runner itself exited 0. Each run restored all source bytes and checked SHA-256 after every mutant. Reusable command: `python3 tools/review_contract_mutations.py`; final evidence: `/private/tmp/sf-rv-a1/mutations/run-xruipwuo/results.json`.

| Mutant | Killer | Final result |
| --- | --- | --- |
| M1: preamble-all-jobs | `tests/unit/test_review_contract_prompt.py::test_non_review_prompts_never_get_the_review_contract` | 1 failed in 52.50s; bytes restored |
| M2: level-anywhere-in-line | `tests/unit/test_review_result.py::test_levels_in_prose_and_malformed_prefixes_are_not_findings` | 1 failed in 19.81s; bytes restored |
| M3: valid-ignores-verdict | `tests/unit/test_review_result.py::test_first_nonempty_line_must_be_a_verdict_even_with_findings` | 1 failed in 34.03s; bytes restored |
| M4: first-attempt-review | `tests/unit/test_review_result_storage.py::test_retry_reports_only_accepted_attempt_review` | 1 failed in 34.49s; bytes restored |
| M5: p0-definition-shortened | `tests/unit/test_review_contract_prompt.py::test_preamble_definitions_are_verbatim_and_under_130_words` | 1 failed in 11.39s; bytes restored |

After those recorded trials, only the mutation harness was tightened for future runs: each case generates `sf_rv_review_mutation.py`, sets `tests.failed_keep.ROOT` to its private `case_dir/failed`, loads that plugin with `-p`, and prepends `case_dir` and the repository to PYTHONPATH. Mutant definitions and daemon/parser source are unchanged. No pytest mutation rerun followed this harness-only fix; AST validation, a generated-plugin smoke check with stdlib stubs, and named `git diff --check` passed. Its smoke directory was guardedly deleted inside primary scratch.

Final restored hashes: daemon `2781bc2de46cef8824df5ae613a57d6bc75de04f388ac50002ff1d62466bbac4`; parser `a00c74f9adc5a97ba134eb8859afc0fe5dfed842e86feec591455e09660c7c3d`. Final source bytes matched those pristine copies.

Command/results ledger. Read-only discovery and scratch preparation are grouped by command family; consequential runs are named below:

| Command or command family | Result/count |
| --- | --- |
| `date -u`; `pwd`; `git status --short`; `git rev-parse HEAD` | Start/head recorded above; starting status empty; exit 0 |
| `cat` required external Rules brief, `pyproject.toml`, `.git`, `.gitignore`; targeted `rg --files`, `rg -n/-l` and `sed -n` over worktree source/tests/docs | Read-only discovery. Absent-file probes for job_view.py, job_records.py, records.py, conversations/dispatch.py and tests/fake/harness.py (rg exit 2), and unmatched job* shell globs, failed as discovery probes; alternate real paths were used. Harness was found in tests/fake/conftest.py; fresh UUID request IDs were confirmed for the new I2 control. No AGENTS.md found. |
| Python scratch helper preparation and AST validation | Prepared final_git_attempt.py and the final test/resume/report/cleanup helpers under sf-rv-a1; final AST validations passed |
| Read-only failed-fixture retention audit | state_daemon/launch_state do not call failed_keep; 11 storage-01 and 8 prompt-adapter-tests-01 distinct state roots verified under owned basetemps. Targeted retention rg returned no matches (exit 1); no global sf-failed listing or process inspection during audit |
| Python report-draft append/write commands | Persistent draft notes and paths were recorded in the assigned worktree before cleanup; the closeout report is regenerated from the evidence files |
| `find` confined to own `/private/tmp/sf-rv-a1/f09` for `.lock`/`.pid` files | No matches; no live-process or live-state inspection |
| Python filesystem `stat().st_dev` for primary scratch, assigned worktree and `.venv` | All three returned 16777233; cleanup relocation is on the same filesystem |
| `uv sync --group dev` with default cache, then required getconf temp cache | Both exit 2: cache sdists-v9/.git denied. No tests ran. |
| `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`; scratch cache fallback; `uv --cache-dir /private/tmp/sf-rv-a1/uv-cache sync --group dev` | Exit 0; nine packages installed, CPython 3.15.2 |
| Prompt-agent getconf-cache pytest preflight | Exit 2 before pytest; same denied cache |
| Initial `uv ... run --no-sync pytest -q tests/unit/test_review_result.py --basetemp .../parser-01` | Exit 0; 260 passed in 81.53 s (standalone REQUEST CHANGES fixture later adds one) |
| Initial prompt file, basetemp `prompt-tests-02` | Exit 0; 101 passed in 450.07 s; eight adapter cases added afterward |
| Adapter selection `-k real_provider_launch`, basetemp `prompt-adapter-tests-01` | Exit 1; 6 failed, 2 passed, 101 deselected in 27.03 s: recording Popen fixture lacked the unrelated hook subprocess context manager |
| Same adapter selection, `-p no:cacheprovider`, basetemp `prompt-adapter-tests-02` | Exit 0; 8 passed, 101 deselected in 14.57 s after stubbing only that unrelated fixture seam |
| Initial CLI/offline file, basetemp `pytest-ledger-01` | Exit 0; 22 passed in 49.44 s |
| Initial storage file, basetemp `storage-01` | Exit 1; 6 failed, 5 passed in 57.24 s: Store.add_event emitted an extra audit event |
| Storage rerun, basetemp `storage-02` | Exit 0; 11 passed in 80.88 s after one SQL event insert inside acceptance |
| `python3 tools/review_contract_mutations.py` (two trials) | Both exit 0; ten expected mutant failures total, ten byte-equal restorations |
| Post-trial mutation isolation harness validation | AST passed; stdlib-stub generated-plugin smoke exit 0; named git diff --check exit 0; smoke scratch guardedly deleted. No pytest rerun after harness-only fix |
| AST parse checks; `git diff --stat`; named-file `git diff --check` | AST checks of nine, then ten, finally 11 named Python files passed (`/private/tmp/sf-rv-a1/final-source-check.json`); whitespace checks passed |
| Scratch-only Python edits to report/cleanup scripts | One exit 1 SyntaxError (before execution) and one exit 1 edit-assertion failure; corrected, final scripts AST-parse passed, no production effect |
| Prior full prompt file, basetemp `z01` | Exit 0; all 109 then-existing cases passed in 631.97 s; command wall duration 690.36 s. The added review-then-build case has a separate targeted followup below |
| Exact-compare `rg` audits for REVIEW_PREAMBLE/subfleet:review, review keys, payload_digest and prompt snapshots | 10 preamble lines/2 files, 20 review-key lines/7 files, 7 digest lines/3 files. Existing prose checks retain their phrases; no old runs JSON key-set snapshot or digest pins the old preamble. |
| `python3 /private/tmp/sf-rv-a1/final_checks.py` | Stopped on the stateful file at its 1800-second bound; the resumer records that incomplete run as exit 124 |
| `python3 /private/tmp/sf-rv-a1/resume_checks.py --primary-exited-after-timeout` | Prioritized all four new test files, then the repaired route file, before remaining required files; state finished; 35/51 primary files invoked |
| Named `git add` + `git commit` for initial parser/integration steps | All initial staging and commit attempts denied index.lock (exit 128); no files staged or commits made by those attempts |
| `python3 /private/tmp/sf-rv-a1/cleanup_workspace.py` | Result recorded in cleanup.json: 12 generated directories removed, secondary scratch removed; final test coverage counts retained |
| `python3 /private/tmp/sf-rv-a1/write_report.py` | Current report generation from recorded evidence; includes final Git attempt results |
| `python3 /private/tmp/sf-rv-a1/final_git_attempt.py` | Final named-file staging/commit result: add exit 128; index.lock denied, commit exit 128; index.lock denied. No final files staged or commit created. |
| `python3 /private/tmp/sf-rv-a1/review_i2_followup.py --resumer-stopped-and-reaped` | Exit 0; 1 passed in 24.89 s, command wall 65.78 s |
| `python3 /private/tmp/sf-rv-a1/cli_targeted_rerun.py --other-checks-stopped` | Exit 0; 2 passed in 25.50 s, command wall 48.92 s |
| `python3 /private/tmp/sf-rv-a1/final_verify.py` | Recorded verification: 4 new files / 404 passing tests; 5 mutants killed/restored; 11 AST checks; source hashes, cleanup, stopped/reaped runner, report contract markers and ending HEAD verified. |

Successful pytest invocations first set `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and use the sandbox fallback `TMPDIR=/private/tmp/sf-rv-a1`, `UV_CACHE_DIR=/private/tmp/sf-rv-a1/uv-cache`. Each basetemp is new. Final checks also set `PYTHONDONTWRITEBYTECODE=1` and `HYPOTHESIS_STORAGE_DIRECTORY` under scratch. No full suite was run. No new refusal was introduced. Guard self-tests receive commands as JSON data; no write-shaped git/gh command was executed as a probe.

The audited 51-file manifest covers every mandatory file, review-preamble marker match and affected runs-show output test. Seven broader search matches were inspected and do not exercise the changed behavior: `test_launch_provider_qos.py`, `test_wall_cancel_races.py`, `test_wall_inspection_order.py`, `test_qos_review_r1.py`, `test_host_push.py`, `test_hold_note.py`, `test_prune_217.py`; they are optional and outside that manifest.

Final one-file-at-a-time command template (BASE and FILE are recorded in each command; continuation commands use new zNN basetemps):

```sh
uv --cache-dir /private/tmp/sf-rv-a1/uv-cache run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 -p sf_rv_temp -p no:cacheprovider --basetemp BASE FILE
```

Primary final pass and continuation: **35/51 files invoked; 3 failed, 1529 passed, 21 skipped, 1 xfailed; 4 nonzero exits**. Detailed commands, logs and durations are in `/private/tmp/sf-rv-a1/final/results.json`.

| Order | FILE | Basetemp | Result | Exit |
| ---: | --- | --- | --- | ---: |
| 01 | `tests/e2e/test_codex_turn_completed_e2e.py` | `/private/tmp/sf-rv-a1/f01` | 3 skipped in 16.97s | 0 |
| 02 | `tests/e2e/test_milestone1_codex.py` | `/private/tmp/sf-rv-a1/f02` | 9 skipped in 8.95s | 0 |
| 03 | `tests/e2e/test_milestone2_claude.py` | `/private/tmp/sf-rv-a1/f03` | 7 skipped in 13.96s | 0 |
| 04 | `tests/e2e/test_recovery.py` | `/private/tmp/sf-rv-a1/f04` | 2 skipped in 25.35s | 0 |
| 05 | `tests/fake/test_admission_latency.py` | `/private/tmp/sf-rv-a1/f05` | 14 passed in 501.47s (0:08:21) | 0 |
| 06 | `tests/fake/test_admission_liveness.py` | `/private/tmp/sf-rv-a1/f06` | 32 passed in 1518.31s (0:25:18) | 0 |
| 07 | `tests/fake/test_admission_properties.py` | `/private/tmp/sf-rv-a1/f07` | 27 passed, 1 xfailed in 64.70s (0:01:04) | 0 |
| 08 | `tests/fake/test_admission_route_isolation.py` | `/private/tmp/sf-rv-a1/f08` | 1 failed, 92 passed in 138.97s (0:02:18) | 1 |
| 09 | `tests/fake/test_admission_stateful.py` | `/private/tmp/sf-rv-a1/f09` | Timed out at the original driver's 1800-second bound; no completed pytest result. | 124 |
| 10 | `tests/unit/test_review_contract_prompt.py` | `/private/tmp/sf-rv-a1/z01` | 109 passed in 631.97s (0:10:31) | 0 |
| 11 | `tests/unit/test_review_result.py` | `/private/tmp/sf-rv-a1/z02` | 261 passed in 117.38s (0:01:57) | 0 |
| 12 | `tests/unit/test_review_result_cli.py` | `/private/tmp/sf-rv-a1/z03` | 22 passed in 47.52s | 0 |
| 13 | `tests/unit/test_review_result_storage.py` | `/private/tmp/sf-rv-a1/z04` | 11 passed in 118.12s (0:01:58) | 0 |
| 14 | `tests/fake/test_admission_visibility.py` | `/private/tmp/sf-rv-a1/z06` | 43 passed in 112.99s (0:01:52) | 0 |
| 15 | `tests/fake/test_capacity_wait_backoff.py` | `/private/tmp/sf-rv-a1/z07` | 30 passed in 158.71s (0:02:38) | 0 |
| 16 | `tests/fake/test_codex_turn_completed_fake.py` | `/private/tmp/sf-rv-a1/z08` | 109 passed in 323.92s (0:05:23) | 0 |
| 17 | `tests/fake/test_notice_delivery.py` | `/private/tmp/sf-rv-a1/z09` | 15 passed in 217.33s (0:03:37) | 0 |
| 18 | `tests/fake/test_policy_sandbox.py` | `/private/tmp/sf-rv-a1/z10` | 25 passed in 96.03s (0:01:36) | 0 |
| 19 | `tests/fake/test_probe_cancel.py` | `/private/tmp/sf-rv-a1/z11` | 24 passed in 159.07s (0:02:39) | 0 |
| 20 | `tests/fake/test_prompt_seam.py` | `/private/tmp/sf-rv-a1/z12` | 8 passed in 76.24s (0:01:16) | 0 |
| 21 | `tests/fake/test_record_secrecy.py` | `/private/tmp/sf-rv-a1/z13` | 44 passed in 163.25s (0:02:43) | 0 |
| 22 | `tests/fake/test_reset_credit_incident.py` | `/private/tmp/sf-rv-a1/z14` | 19 passed in 61.17s (0:01:01) | 0 |
| 23 | `tests/fake/test_review_launch_permissions.py` | `/private/tmp/sf-rv-a1/z15` | 35 passed in 47.45s | 0 |
| 24 | `tests/fake/test_review_r185_adversarial.py` | `/private/tmp/sf-rv-a1/z16` | 1 passed in 34.82s | 0 |
| 25 | `tests/fake/test_review_r223_wall_probes.py` | `/private/tmp/sf-rv-a1/z17` | 23 passed in 169.62s (0:02:49) | 0 |
| 26 | `tests/fake/test_salvage_finalization.py` | `/private/tmp/sf-rv-a1/z18` | 47 passed in 80.46s (0:01:20) | 0 |
| 27 | `tests/fake/test_state_contract.py` | `/private/tmp/sf-rv-a1/z19` | 37 passed in 33.09s | 0 |
| 28 | `tests/fake/test_task_hold_admission.py` | `/private/tmp/sf-rv-a1/z20` | 49 passed in 49.29s | 0 |
| 29 | `tests/fake/test_unadmittable_pin_review.py` | `/private/tmp/sf-rv-a1/z21` | 7 passed in 39.25s | 0 |
| 30 | `tests/fake/test_unrun_worktrees.py` | `/private/tmp/sf-rv-a1/z22` | 98 passed in 191.74s (0:03:11) | 0 |
| 31 | `tests/fake/test_wall_running_time.py` | `/private/tmp/sf-rv-a1/z23` | 35 passed in 48.78s | 0 |
| 32 | `tests/unit/test_capacity_steer_route.py` | `/private/tmp/sf-rv-a1/z24` | 22 passed in 50.89s | 0 |
| 33 | `tests/unit/test_census_boot_seeds.py` | `/private/tmp/sf-rv-a1/z25` | 36 passed in 71.67s (0:01:11) | 0 |
| 34 | `tests/unit/test_cli.py` | `/private/tmp/sf-rv-a1/z26` | 2 failed, 254 passed in 90.08s (0:01:30) | 1 |
| 35 | `tests/unit/test_compat.py` | `/private/tmp/sf-rv-a1/z27` | Timed out after 25 seconds; captured output is not a completed result. | 124 |

Incomplete timed-out files are not passing test results: `tests/fake/test_admission_stateful.py` (exit 124, 1800-second bound); `tests/unit/test_compat.py` (exit 124, 25.083-second bound). The original stateful timeout interrupted its driver; continuation children are killed and reaped as owned process groups before another file starts. No completed pytest count is claimed for a timeout.

**16 required files remain uninvoked at the time limit**; the requested end-of-job verification is incomplete:

- `tests/unit/test_daemon_refusals_cli.py`
- `tests/unit/test_daemon_verbs.py`
- `tests/unit/test_gate_admission.py`
- `tests/unit/test_hooks.py`
- `tests/unit/test_invariants_index.py`
- `tests/unit/test_mcp_cli_contract.py`
- `tests/unit/test_notice_rendering.py`
- `tests/unit/test_offline.py`
- `tests/unit/test_operator_notices.py`
- `tests/unit/test_outcome_unknown.py`
- `tests/unit/test_prune_decisions.py`
- `tests/unit/test_sessions_cli.py`
- `tests/unit/test_status_hold_source.py`
- `tests/unit/test_store_ids.py`
- `tests/unit/test_store_qos_migration.py`
- `tests/unit/test_test_module_names.py`

The primary run exposed one pre-existing real-time fixture race: `test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again` allowed only about one to two seconds for submission and admission before its first pick. The override had already expired there. It submits a non-review job; the new preamble path is not taken. Only that test was stabilized: existing HeldClock holds time through the initial pick, then advances past expiry before the reservation check. Both the active and expired states remain asserted. Production routing was untouched.

The primary `test_cli.py` run reported 2 failed and 254 passed. `test_wait_backs_off_when_the_long_poll_returns_at_once` measured gaps about 1.04 s then 0.0058 s, violating its monotonic-gap assertion; `test_ping_open_socket_is_bounded` took about 1.46 s against a 0.75 s limit. These tests exercise unchanged wait/ping paths, but their cause was not proven pre-existing. Rerun status: `test_wait_backs_off_when_the_long_poll_returns_at_once`: passing rerun (exit 0: 2 passed in 25.50s); `test_ping_open_socket_is_bounded`: passing rerun (exit 0: 2 passed in 25.50s).

After the full 109-case prompt run, one test was added: `test_review_format_does_not_leak_into_the_next_build` submits a review and then a build on the same daemon, proving the review format does not leak across jobs. This addition changes only tests. Targeted followup exit 0: 1 passed in 24.89s.

| Followup command | Result | Exit |
| --- | --- | ---: |
| `uv --cache-dir /private/tmp/sf-rv-a1/uv-cache run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 -p sf_rv_temp -p no:cacheprovider --basetemp /private/tmp/sf-rv-a1/z05 tests/fake/test_admission_route_isolation.py` | 93 passed in 277.71s (0:04:37) | 0 |
| `uv --cache-dir /private/tmp/sf-rv-a1/uv-cache run --no-sync --with 'pytest-xdist>=3.6' pytest -q -p sf_rv_temp -p no:cacheprovider --basetemp /private/tmp/sf-rv-a1/i2-tqiazd81/p tests/unit/test_review_contract_prompt.py::test_review_format_does_not_leak_into_the_next_build` | 1 passed in 24.89s | 0 |
| `uv --cache-dir /private/tmp/sf-rv-a1/uv-cache run --no-sync pytest -q -p sf_rv_temp -p no:cacheprovider --basetemp /private/tmp/sf-rv-a1/cl-2vvrhrbz/pytest tests/unit/test_cli.py::test_wait_backs_off_when_the_long_poll_returns_at_once tests/unit/test_cli.py::test_ping_open_socket_is_bounded` | 2 passed in 25.50s | 0 |

The full repaired route file was invoked sequentially after the four new files were prioritized in the continuation; its recorded exit is 0. The initial route failure remains visible in the primary ledger and is superseded only if this followup completed successfully.

The optional old-daemon baseline differential was not run; no baseline equivalence or baseline byte-restoration result is claimed. The wall-clock diagnosis follows from the existing fixture and its captured (None, None) early decision, not from a baseline test.

The sandbox denies the process inspection required by the e2e and real-daemon fixtures; those cases skip before provider launch. Any existing strict expected failures are reported as xfailed, rather than silently treated as new passes. The four new files exercise their contract through deterministic state and real adapter construction without provider processes.

Scratch/cleanup and deviations: `/private/tmp/sf-rv-a1` holds caches, socket roots, logs and evidence. C-2.4 workdir fixtures require a path outside /tmp, so the test-only `sf_rv_temp` plugin supports such fixtures by redirecting into secondary scratch `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/sf-rv-a1`, records any created path in this report before deletion, and guards shutil.rmtree with an assertion that sf-rv-a1 is in the resolved path. It also confines hard-coded /tmp mkdtemp calls and failed-test retention to this job. No production or checked-in fixture behavior was changed by the plugin.

Limitation: initial focused runs and mutation trials used the existing `tests/conftest.py` session-start `failed_keep.prune` hook before the scratch plugin confined its ROOT. Any global read or removal by that hook was unobserved. The failed new fixtures used `state_daemon`/`launch_state`, which do not call retention; this audit did not list global `/tmp/sf-failed` or inspect processes. The final-pass plugin confined ROOT to this job scratch.

Dependency sync and initial tests created the following generated directories outside temp scratch. They were recorded before cleanup, moved into `/private/tmp/sf-rv-a1/cleanup`, and removed with guarded shutil.rmtree asserting sf-rv-a1 in the target path:

- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/.pytest_cache`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/.hypothesis`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/.venv`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/subfleet/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/subfleet/adapters/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/subfleet/sessions/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/subfleet/conversations/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/subfleet/gate/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/subfleet/guard/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/tests/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/tests/unit/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-080028-rv-a1-verdict-build/tests/fake/__pycache__`

Cleanup result: 12 generated directories removed; secondary scratch removed; 59 C-2.4 fixture folders recorded, 59 removed by guarded normal teardown and 0 removed by guarded final cleanup. Initial cache denials and initially unconfined session-start prune hooks, the corrected duplicate-event and launch-fixture failures, corrected scratch-edit SyntaxError and assertion failures with no production effect, the stabilized existing clock race, the two CLI timing assertion failures (resolved only if a recorded rerun passes), the stateful timeout and any further timeouts or uninvoked required files listed above, the optional baseline differential when not run, the mutation isolation harness fix validated without a pytest rerun, sandbox skips, stderr placement for the readable line, and initial denied commits are the only deviations/limitations; the final Git attempt state is reported above. No schema, routing, acceptance, export or retry-policy deviation.

Head built: 85769b8269162806513030094b362cb3554cbf5a
