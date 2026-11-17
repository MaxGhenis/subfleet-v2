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

## Fix r1

Fix round 1 for review r1 (`review-a1-r1.md`, head reviewed 7b92ad50) of PR #313, on `feat/review-verdict-217` from 316675986 (r1's head plus the hub's merge of release/217 at 82d3dd819). Started 2026-11-15 01:42:42 UTC (`date -u`, first command); completed 03:54:20 UTC, before the 05:12:42 UTC deadline. Line numbers below are at code head 0689f895b.

Commits, named files only, none pushed:

| Commit | What |
| --- | --- |
| bae350752 | Gate rounds out of the template and the parse; writable-only sentence; new parser; parse before the transaction, bounded; tests |
| fcdb4e52f | Revert the route-isolation stabilization (P3) |
| b15796347 | C-12.11, C-6.7, change-log line, README |
| 0689f895b | `review_result.py`: zero-width, dash and middle-dot characters written as `\u` escapes, not literal characters |
| 22f52e0ed | This section, as a draft |
| (the commit after it) | This section, completed with the long runs and the cleanup |

### Findings and what changed

**1. P1: gate rounds got the review preamble.** Fixed as r1 suggested.
- `subfleet/review_record.py:12–20`: `is_review_job` is now `task == "review"` alone. A gate round has kind `gate-review` and no task, so it gets neither the template nor the parse.
- `subfleet/daemon.py:3434` uses it at submit. A gate round is read-only, so it again has no `prompt.prepared.md` and no `preamble` key in its manifest, as before the PR.
- The views follow the same test: `daemon.py:4284` (online `list`; `show` goes through `stored_review_result`) and `subfleet/offline.py:214` select review evidence for `task = 'review'` only. A gate round's `review` is null everywhere.
- Publish denial is unchanged and still covers `gate-review`: `review_publish.publishing_denied` (`review_publish.py:68`), the host-push refusal at submit (`daemon.py:3014`) and `_publish_bundle` (`daemon.py:12396`).
- C-12.11 now says gate rounds are excluded and why (C-23.9's sentinel-only rule); C-6.7 says the same.
- Test: `tests/unit/test_review_contract_prompt.py::test_a_gate_round_gets_no_review_contract_and_its_gate_passes`. It drives the real gate CLI (`gate_cli.run`) through `FakeClient` (the real daemon dispatcher) with the `core` fixture and `finish`, captures the round's job on submit, and asserts: kind `gate-review`, task null, read-only, isolated; no `prompt.prepared.md` with the marker; no `preamble` in the manifest; the prompt sent has none of the contract's lines but has the gate's "Return exactly one sentinel-delimited JSON object with no text outside it."; the gate returns 0 and writes its certificate.
- Parse side: `tests/unit/test_review_result_storage.py::test_a_gate_round_is_never_parsed` finalizes an accepted `ok` attempt of a job whose kind is `gate-review` and whose task is null. The parser is not called, the evidence has no `review`, there is no `job.review_result` event, and all four views show null. `tests/unit/test_review_result_cli.py::test_offline_gate_rounds_show_no_review` checks that offline views ignore saved review evidence on a gate round. That test replaces `test_offline_review_task_and_gate_review_kind_expose_the_saved_result`, whose gate half asserted the old behavior.

**2. P2: read-only and isolated reviews were told they "may run tests and write scratch files".**
- `daemon.py:558`: `REVIEW_WRITABLE` holds that sentence. `REVIEW_PREAMBLE` (`:563`, used for writable reviews) keeps it. `REVIEW_PREAMBLE_READ_ONLY` (`:576`) is the same text without it.
- `daemon.py:3442–3447` picks the template from the job's resolved sandbox. The writable form goes to a workspace-write review, including one whose sandbox came from a `worktree-write` policy. Every read-only review gets the read-only form, and an isolated review is always read-only (C-23.3). Both forms keep "Do not commit, push or publish." Word counts (whitespace-separated, marker included): writable 126, read-only 114.
- C-6.7 now limits the sentence to writable reviews and cites #221 r1b P3-8.
- Tests:
  - `test_only_a_writable_review_is_told_it_may_run_tests_and_write_scratch[writable|read-only|isolated]` checks the sentence appears once, zero times and zero times.
  - `test_every_review_prompt_contains_the_contract_once` (48 cases), `test_no_preamble_cannot_omit_the_review_contract` and `test_real_provider_launch_receives_the_contract_once` now also assert the sentence's count against each job's actual sandbox.
  - `test_preamble_definitions_are_verbatim_and_under_130_words` covers both forms.

**3. P2: the finding parser was too wide and too narrow.** `subfleet/review_result.py` was rewritten around three compiled patterns.
- **Lines.** `:91` splits on `\n` only and drops one trailing `\r`. A form feed, U+2028, U+0085 or a bare `\r` is no longer a line break.
- **Code.** `:53` and `:100–108` skip fenced code blocks: a line of three or more backticks or tildes, after any spaces and `>`s, opens one, and a fence of the same character at least as long, with nothing after it, closes it. An unclosed fence runs to the end. A backtick fence whose info string holds a backtick is not a fence (CommonMark). Indented code is never a finding, because a finding must start within three spaces of indentation; four columns or a tab is code or nested content.
- **What counts (`_FINDING`, `:28`).** After at most three spaces, any run of containers:
  - `>`;
  - `-`, `*` or `+` and a space;
  - `1.` or `1)` and a space;
  - `#`s;
  - `**`, `*`, `__` or `_`.

  Then the level, an optional finding number (`P2-1`), optional closing emphasis and an optional parenthetical of up to 80 characters (`P2 (change)`, `P3-1 (UNPROVEN)`). Then the separator: `:`; `.` before a space (`**P3-1. …**`); or whitespace and an em dash, en dash or hyphen. That covers every shape r1 listed: `1. P1: …`, `1. **P2 — …**`, `### P2-1: …`, `- **P3-1:** …`, `> P1: …` and an en dash. Possessive repeats keep a run of marks linear, and only a line's first 256 characters (`FINDING_PREFIX_CHARS`, `:27`) are matched, so no single line costs more than that.
- **What doesn't count (`_finding_level`, `:57–74`).**
  - A tally: the text after the level is a count or `none`, `zero` or `no findings`, followed by the end of the line, a list separator or another level (`P0: 0`, `P1: 0, P2: 1`, `- **P0:** none`, `P3: 2.`). `P1: 2 tests fail` is still a finding.
  - A range: a dash followed by another level (`P0 – P1: none`).
  - An echoed definition.
- **How an echoed definition is told from a finding.** A levelled line is an echo when its text after the separator, emphasis stripped and case folded, opens with that level's own definition: `harm that is live now` (P0), `the change does something wrong` (P1), `right today but fragile` (P2), `style and nits` (P3). These are `DEFINITION_OPENINGS`, `:45`. Each is a clause of Max's definition that no real finding summary would open with. P1's opening is the full clause, because "The change does …" can open a real P1 summary. Another level's definition does not match: `P1: harm that is live now` counts. A test checks the openings against the preamble's own definition lines, so the two can't drift apart.
- **`FINDINGS: none`.** It counts only as a whole line within three spaces of indentation and outside a fence (`:54`).
- **BOM and zero-width characters (P3).** `:14`, `:94–99`: before the verdict comparison, a BOM and U+200B, U+200C, U+200D and U+2060 are stripped from the first non-empty line's start (and end). A line holding only those characters and whitespace counts as empty. A leading space still makes the verdict null, as before.
- **Tests.** Every case r1 lists is now a permanent parametrized case.
  - `test_real_finding_shapes_are_counted` has 15 shapes.
  - `test_code_separators_definitions_and_tallies_are_not_findings` has 28 texts: fences (backtick, tilde, in a list item, in a quote, unclosed), indented and tab-indented code, a four-column nested item, U+2028, form feed, U+0085, a bare `\r`, all four echoed definitions, tallies, ranges, and the comma, middle-dot and heading-only shapes.
  - `test_markdown_line_start_findings` has 16 prefixes × 8 separators × 4 levels; it adds ordered, `+`, `>`, `_` and en-dash forms.
  - Also: `test_the_preambles_own_definitions_echoed_are_not_findings`, `test_findings_none_inside_code_does_not_declare_none`, `test_a_fence_closes_only_on_its_own_marker`, `test_a_byte_order_mark_and_zero_width_characters_before_the_verdict_are_ignored` (12 cases), `test_zero_width_characters_do_not_rescue_a_wrong_first_line`.
- **Existing tests whose expectation changed.**
  - `test_markdown_line_start_findings` dropped the `"\t- **"` prefix, because a tab-indented line is code now.
  - `test_generated_counts_only_line_start_findings` dropped `"\r"` as a newline, because a bare `\r` is no longer a line break.

**4. P2: the parse ran inside the `attempt.accepted` write transaction.**
- `daemon.py:11971–11978` now parses before the transaction opens at `:11979`. It parses only when acceptance is still possible: a review job, not lost, outcome `ok`, a nonblank deliverable, and rc 0 (or a turn). The transaction can only downgrade that `ok` (a daemon signal, a cancel), never make one.
- Inside the transaction, `:12041–12044` only stores the result and emits `job.review_result`, and only when `accepted` (`ok and not cancel`). A cancel that vetoes an `ok` attempt therefore leaves the speculative parse unstored.
- **Bound.** `parse_review_deliverable` (`review_result.py:133`) reads at most `MAX_REVIEW_BYTES`, 4 MiB (`:10`). I chose 4 MiB because the largest hub review is 33,537 bytes, so the bound has about 125× headroom, and the brief's 2 MB fixture still parses whole.
- **What a larger deliverable records.** The daemon decodes and parses the bytes up to the last `\n` inside the bound, or the whole bound if there is none. It records the verdict and the counts from that prefix, sets `valid` false, and appends to `reason`: `deliverable is N bytes, over the 4194304-byte bound; only its first M bytes were read`. The round is invalid because findings past the bound were never read.
- C-12.11 states the bound, and that validity is also false past it. That amends I4 for this one case.
- Tests:
  - `test_review_result_storage.py::test_the_parse_runs_before_the_acceptance_transaction` wraps the parser and records `store._holds_writer()` during the call; it must be `[False]`.
  - `test_an_oversized_review_is_stored_bounded_and_invalid` covers the bound end to end through `_finalize`.
  - `test_review_result.py::test_a_deliverable_at_the_bound_is_read_whole`, `test_a_deliverable_over_the_bound_is_read_to_a_line_break_and_recorded_invalid` and `test_deliverable_parse_is_total_for_generated_bytes` (200 generated byte strings, equal to a fresh parse below the bound) cover the parser side.

**5. P2: test gaps.**
- A submitted gate-review round with its prompt read: the P1 test (finding 1).
- A resumed review with its prompt read: `test_review_contract_prompt.py::test_a_review_resume_gets_the_contract_again[read-only|workspace-write]`. It finalizes a review with a native session, submits `kind="resume"`, and checks the resume's kind, task and sandbox. The contract must appear once, with the scratch sentence only for the writable resume.
- A cancel that vetoes an `ok` attempt: `test_review_result_storage.py::test_a_cancel_that_vetoes_an_ok_review_stores_no_result`. The attempt ends `interrupted`, rc 0, class `ok`; the job ends `cancelled`, with no accepted attempt, no `review` in evidence, no event and null views.
- r1's three mutants are all killed (see Mutants below).

**6. P3s.**
- **C-6.7's resume sentence.** I corrected the text rather than the code: a resume keeps its source's choice of the *write* template, while a review's resume, a review too, gets the review template again (and the parse on its own accepted answer). This includes the resume of a pre-PR read-only review, whose source had no template.
- **PR-body sentence.** Suggested replacement for the line in `pr-body-313.md`:

  > `runs show` prints `review: REQUEST_CHANGES (P1 1, P2 2); round valid` on stderr. Its stdout keeps its shape (the metadata object, then `--- out.md ---` and the deliverable), but the bare form's metadata object gains a top-level `review` key: null for other jobs, gate rounds and older records, and null against an older daemon too, because the CLI sets it before printing (`subfleet/cli.py:1963`). `--out` and `--err` are unchanged. Online and offline list/show carry the accepted attempt's object; other jobs show null.
- **BOM and zero-width characters.** Fixed under finding 3, with tests.
- **Route-isolation stabilization.** Reverted on this branch (fcdb4e52f; `git checkout 82d3dd819 -- tests/fake/test_admission_route_isolation.py`), so the file now matches release/217. Its diff, for the hub to file separately (`git diff 82d3dd819 316675986 -- tests/fake/test_admission_route_isolation.py`):

```diff
@@ -564,9 +564,14 @@ def test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again(routi
     reservation, which reserved on that decision though its own evaluation rejects
     the lane (below the floor). The check decides every lane's override again at its
     own clock: codex-1's has ended, so codex-1 is judged again with its reading back
-    (review of d04b8b3: it used to refuse and evaluate the whole route again)."""
+    (review of d04b8b3: it used to refuse and evaluate the whole route again).
+    Hold the clock until the first pick: a loaded runner could otherwise spend
+    the override's two seconds in submission or admission before that pick."""
     service, harness = routing_state
-    now = datetime.now(timezone.utc)
+    HeldClock.at = now = datetime.now(timezone.utc).replace(microsecond=0)
+    monkeypatch.setattr(daemon_module, "datetime", HeldClock)
+    monkeypatch.setattr(scheduler, "datetime", HeldClock)
+    monkeypatch.setattr(service.timers, "now", lambda: HeldClock.at)
     service.store.add_reading(Reading("codex-1", "account", "seven_day", .99, after(86400),
                                       ReadingLabel.PROVIDER, "fixture", now.strftime("%Y-%m-%dT%H:%M:%SZ")))
     confirmed = (now - timedelta(days=7) + timedelta(seconds=2)).replace(microsecond=0)
@@ -574,7 +579,7 @@ def test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again(routi
     stamp = confirmed.strftime("%Y-%m-%dT%H:%M:%SZ")
     service.store.add_action(action_id="act-1", kind="reset-credit", op_key="codex:fake:op1", subject="codex-1",
                              state="confirmed", request_json="{}", created_at=stamp, updated_at=stamp)
-    assert service.timers.actions.confirmed_override("codex-1") is not None
+    assert service.timers.actions.confirmed_override("codex-1", now=HeldClock.at) is not None
     job_id = submit(service, harness, pinned_model="astra")
     real, early = service._pick, []
 
@@ -583,12 +588,13 @@ def test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again(routi
         assert not service.store._holds_writer()
         early.append((decision.chosen_lane, options["basis"]["overrides"].get("codex-1")))
         if len(early) == 1:
-            while datetime.now(timezone.utc) <= ends + timedelta(seconds=.2):
-                time.sleep(.05)                               # the override ends before the reservation
+            assert service.timers.actions.confirmed_override("codex-1", now=HeldClock.at) is not None
+            HeldClock.at = ends + timedelta(seconds=1)        # the override ends before the reservation
         return decision
     monkeypatch.setattr(service, "_pick", pick)
     service._admit()
     assert early == [("codex-1", ("act-1", ends.strftime("%Y-%m-%dT%H:%M:%SZ")))]  # held out, 99% did not count
+    assert service.timers.actions.confirmed_override("codex-1", now=HeldClock.at) is None
     assert service._route_evaluations == {**ZERO, "rechosen": 1, "rejudged": 1}   # judged again: below the floor
     assert not service.store.list_attempts(job_id)             # the lane is below the floor now
     assert service.store.get_job(job_id)["state"] == "waiting"
```

With the stabilization reverted, the route file failed here exactly as the build saw it: `1 failed, 92 passed`. The failure was in that test: `assert [(None, None)] == [('codex-1', ('act-1', …))]` at line 591, at load 152 to 114. The override had already ended before the first pick. It submits a non-review job, and this PR's code does not reach it.

### Corpus, before and after

Read-only over `/Users/maxghenis/reviews/subfleet-hub/`: every file named `review-r*.md` at any depth, except names containing `brief`. Each file was decoded as UTF-8 with replacement, the way the daemon decodes it. "Before" is `git show HEAD:subfleet/review_result.py` at 316675986; "after" is the committed parser at 0689f895b. Script: `corpus.py` in scratch.

I could not reproduce r1's 301 files. Today the pattern matches 334, and limiting it to depth 2 gives 311. Seven files changed after r1's review was written. Restricting to files no newer than `review-a1-r1.md` (the "as of r1" set) gives 327. Both sets are shown, measured the same way before and after:

| Set | Files | Verdict line, before → after | Valid, before → after | Levelled findings, before → after | `consistent` false, before → after |
| --- | ---: | --- | --- | --- | --- |
| All `review-r*.md` | 334 | 92 → 92 | 32 → 73 | 102 → 255 | 46 → 9 |
| As of r1's file | 327 | 85 → 85 | 28 → 68 | 98 → 250 | 46 → 9 |

On the full set:
- 41 reviews became valid and none became invalid.
- No verdict changed.
- Level totals went from P0 0, P1 25, P2 147, P3 109 to P0 0, P1 78, P2 446, P3 477.
- The 261 still invalid fail for these reasons: 183 lack only the verdict line, 59 lack the verdict and any findings declaration, and 19 have a verdict but no levelled finding or `FINDINGS: none`.
- Most hub reviews predate the preamble and write `VERDICT #NNN: …`, `**REQUEST CHANGES**` or `REQUEST CHANGES` alone, so their verdict line is correctly null.

### Spot-check: 20 valid, 20 invalid

Many hub files are byte-identical copies across `ctl-1023-*` folders. I sampled over distinct contents instead, deterministically, in path order: the first 20 of the 28 distinct valid reviews, and every eleventh of the 225 distinct invalid ones. A first pass over paths, which was mostly copies, was replaced by this one. For each sampled review I read the first line and every line whose first 60 characters name a level, with the parser's decision on each (`spot.py`).

**Valid (20 read).** Codex-bounce r2; ctl-1023-armr2 r2, r3, r5, r6, r8, r9, r10, r11, r11-summary, r12, r12-summary, r13, r14, r15, r17; ctl-1023-snapshot r18, r18-summary, r19, r19-answer.
- Verdicts are all right.
- Numbered (`1. **P2 — …**`), bold-colon (`- **P2:** …`), heading (`### P2-1: …`), plain (`P2: change, UNPROVEN. …`) and `P3-1:` findings are counted.
- Status tables (`| P1-1 order | Open |`), prose mentions (`No new P1/P3 finding`, `P1–P4 are useful …`) and closed items (`- **P2, stale expiry …: closed.**`) are not counted.
- Still wrong, in three files:
  1. `6. **P2, inherited — Delayed runner reactivates an ended window.**` (armr2 r5) and `- **P2, inherited follow-up — Shared seen is ignored.**` (armr2 r6) are open findings the parser misses: each review is one P2 short.
  2. `- **P2-1, pulse_arm_on_force.py:206–209:** …` and `- **P3-1, :160, 198–200:** …` (snapshot r18-summary) are missed. That review records P3 2 instead of P2 1 and P3 3, so it reads `consistent` false although it is consistent.

  I did not widen the parser to a comma after the level. In the same reviews, the same shape marks findings closed or fixed in a re-review (`- **P2, stale expiry ended an accepted extension: closed.**`, four such lines in r11 alone; `- **P2, runner.py:1062 …: fixed …**`). Counting it would count closed findings as open, which is worse for the round rule than missing an inherited one. The preamble asks every new review for `P2:` at the line start.

**Invalid (20 read).** adm3 r1; ci2-detach r3; ctl-1023-armr2 r7; daemon-log-scrub r4; flake-relay r2; kill3 r1; kill4-p3 r2; lane-scope r1, r7; lsg-p3b r2; probe-deadline r1; pulse-swap r4; retention-batch-fairness r1; retention-live r9; scrub-boundary r1; scrub-order r1; shell-downgrade r7; status-src r4; task-hold r2; unrun-worktrees r1.
- Each is invalid for the stated reason:
  - 19 have a non-contract first line (`VERDICT #235: APPROVE`, `VERDICT v10w: APPROVE`, `**REQUEST CHANGES**`, a `# Review r1: …` heading);
  - armr2 r7 has `VERDICT: APPROVE` but says "No new P1–P3 findings." in prose instead of a `FINDINGS: none` line.
- Tallies such as `**P1:** none.` are not counted, and `(hub question)` and `(comment)` parentheticals are.
- Still wrong in the counts, though not in validity:
  1. kill4-p3 r2: six re-check lines in its checks section, `- **P3-1:** the last-read landing test … passes …`, are counted as P3 findings. It records P3 8; its new findings are P2-1, P3-1 and P3-2. A line that confirms an earlier finding fixed has the same shape as a finding, and the parser cannot tell them apart without reading the sentence.
  2. retention-live r9 `**R9-F1 — P2:** …` and task-hold r2 `**F1 (P3). …**` (five findings) put an id before the level, so nothing is counted.
  3. pulse-swap r4 lists two findings under a `Nonblocking P3 findings:` line with no level on their own lines.

### Timings

Each shape ran in its own process, `before` as r1's daemon called it (`parse_review_result(payload.decode("utf-8", "replace"))`) and `after` as this daemon does (`parse_review_deliverable(payload)`). The machine was loaded at 156 to 171 (`timing.py`; 02:26:14 to 02:28:06 UTC, uptime load 170.81 then 160.91).

| Input | Before | After | Load (1 min) |
| --- | ---: | ---: | --- |
| 2 MB review (header, `- **P2 — …**`, then one 2 MB line) | 0.004 s | 0.088 s | 170.8 |
| 20 MB, one line of `*# \t` marks | 12.294 s | 0.058 s | 170.8 → 162.3 |
| 100 MB, one line of marks | 63.210 s | 0.192 s | 162.3 → 156.5 |
| 100 MB of `- **P2 — …**` finding lines | 30.944 s (2,231,012 P2, valid) | 2.980 s (89,239 P2 from the first 4 MiB, invalid with the size reason) | 156.5 → 160.9 |

- **Where the time is spent.** Before, all of this ran while the store's writer was held. After, none of it does (asserted by `test_the_parse_runs_before_the_acceptance_transaction`).
- **The 2 MB case.** I repeated it three times at load about 101: after 0.203, 0.152 and 0.186 s; before 0.004, 0.050 and 0.080 s. cProfile puts the after time in the first `str.split("\n")` of a fresh process, which copies the 2 MB line (0.251 s of 0.256 s). Repeated in one process, both parsers take 0.003 to 0.046 s. I did not change it: it is a one-time allocation outside the transaction.
- **Comparison with r1.** r1 measured 0.23 s, 11.8 s, 61.6 s and 14.7 s at load about 100.

### Mutants

Each mutant was applied alone to `SCRATCH/mut/repo`, a `git archive HEAD` copy, never the worktree. Its killer then ran there with the worktree's `.venv` Python; `subfleet` was imported from the copy (`…/sf-rv-f1/mut/repo/subfleet/__init__.py`). I ran the killer on the unmutated copy first, then on the mutant. The file was then restored from a pristine copy and its SHA-256 compared (`mutants.py`).

The final run was at head 0689f895b, from 02:58:06 to 03:00:26 UTC, at load 184.93 to 171.12. Pristine `daemon.py` 95281aa5…; `review_result.py` caf2820d…. Every mutant was killed, and every restore was byte-equal. An earlier run of all seven at b15796347 gave the same result; its `review_result.py` differed only in literal versus escaped characters.

| Mutant | Edit | Killer | Unmutated | Mutated |
| --- | --- | --- | --- | --- |
| Gate review back in the template (r1's first, reversed under the P1 fix) | `review = is_review_job(…) or args.kind == "gate-review"` | `test_review_contract_prompt.py::test_a_gate_round_gets_no_review_contract_and_its_gate_passes` | 1 passed | 1 failed (`'<!-- subfleet:review -->' is contained` in the prepared prompt) |
| No template for resumes | `… and args.kind != "resume"` | `test_review_contract_prompt.py::test_a_review_resume_gets_the_contract_again` | 2 passed | 1 failed (`assert 0 == 1`, marker count) |
| Store on `ok`, not `accepted` | `if ok and review_result is not None:` | `test_review_result_storage.py::test_a_cancel_that_vetoes_an_ok_review_stores_no_result` | 1 passed | 1 failed (`'review' not in` evidence) |
| Writable-only sentence for every sandbox | read-only branch uses `REVIEW_PREAMBLE` | `test_review_contract_prompt.py::test_only_a_writable_review_is_told_it_may_run_tests_and_write_scratch` | 3 passed | 1 failed, 1 passed (`assert 1 == 0` at read-only) |
| `splitlines` restored | `for line in text.splitlines():` | `test_review_result.py::test_code_separators_definitions_and_tallies_are_not_findings` | 28 passed | 1 failed, 8 passed (U+2028 case) |
| Fenced code counted again | `marker = None` | same | 28 passed | 1 failed (first fence case) |
| Parse moved back inside the transaction | pre-transaction parse removed; `evidence["review"] = review_result = parse_review_deliverable(review_result)` inside it | `test_review_result_storage.py::test_the_parse_runs_before_the_acceptance_transaction` | 1 passed | 1 failed (`assert [True] == [False]`) |

The build's five mutants were not rerun. Their killers still exist and passed in the runs below.

### Commands and results

All pytest runs:
- set `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache` and `PYTHONDONTWRITEBYTECODE=1` (from run `a` on), with `PYTHONPATH` unset;
- used `-p no:cacheprovider` and a new `--basetemp` under `$TMPDIR/sf-rv-f1/` whose name contains `pytest`;
- ran with `-n 4` (`uv run --no-sync --with 'pytest-xdist>=3.6'`), except `pytest-01` and `pytest-02`, which ran without and with `-n 4` respectively.

Files ran one at a time through `runfiles.py`, which records `uptime` before and after each file. Python was 3.14.7 for the scratch scripts; the venv is the one `uv sync` made.

| Command | Result |
| --- | --- |
| `date -u; git status --short; git log --oneline -3; uptime` | 01:42:42 UTC; clean; HEAD 316675986; load 130.18 |
| `uv sync --group dev` (02:11:56–02:12:07) | exit 0; 10.6 s; created `.venv` |
| `pytest -x --basetemp …/pytest-01 tests/unit/test_review_result.py` (02:15:51–02:17:22, load 69.69 → 115.50) | 619 passed in 68.77 s |
| `pytest -n 4 --basetemp …/pytest-02` storage file, CLI file and four new prompt tests (02:17:34–02:17:58, load 109.83 → 101.29) | 47 passed in 19.01 s |
| `git add` (8 named files); `git commit` | bae350752 |
| `git checkout 82d3dd819 -- tests/fake/test_admission_route_isolation.py`; `git commit` (named file) | fcdb4e52f |
| `git add README.md docs/acceptance-contract.md`; `git commit` | b15796347 |
| `git add subfleet/review_result.py`; `git commit` | 0689f895b |
| `python3 -I mutants.py` (twice: three mutants then four at b15796347; all seven at 0689f895b) | 7 of 7 killed each time; 14 of 14 restores byte-equal |
| `python3 -I corpus.py` (before; after twice, the second at 0689f895b) | table above; the two after runs agree |
| `python3 -I spot.py` (four batches of 10) | above |
| `python3 -I timing.py` (eight shapes × mode, then 2 MB × 3 × 2); cProfile of the 2 MB case | above |
| `uv run --no-sync pytest -q -p no:cacheprovider --collect-only --basetemp …/pytest-collect-<file>` for latency, liveness and stateful; `split -l 7/6/3 -d` | 14, 32 and 17 ids; 2, 6 and 6 chunks |
| `python3 -I runfiles.py WORKTREE SCRATCH <label> 570–575 <files or @ids>` (labels a to m) | one pytest per file or chunk; results below |

Test files, one per run, all finished (`label-NN` is the basetemp `pytest-label-NN`):

| Run | File | Result | rc | UTC | Load (1 min), start → end |
| --- | --- | --- | ---: | --- | --- |
| a-01 | `tests/unit/test_review_contract_prompt.py` | 117 passed in 65.32 s | 0 | 02:29:22–02:30:35 | 175.01 → 154.38 |
| a-02 | `tests/unit/test_review_result_storage.py` | 15 passed in 25.46 s | 0 | 02:30:35–02:31:06 | 154.38 → 141.88 |
| a-03 | `tests/unit/test_review_result_cli.py` | 24 passed in 26.77 s | 0 | 02:31:06–02:31:44 | 141.88 → 158.43 |
| a-04 | `tests/unit/test_gate_admission.py` | 24 passed in 19.43 s | 0 | 02:31:44–02:32:11 | 158.43 → 162.64 |
| a-05 | `tests/fake/test_gate_end_to_end.py` | 11 passed in 34.43 s | 0 | 02:32:11–02:32:54 | 162.64 → 125.51 |
| b-01 | `tests/fake/test_review_launch_permissions.py` | 35 passed in 30.73 s | 0 | 02:33:08–02:33:43 | 126.28 → 138.89 |
| b-02 | `tests/fake/test_policy_sandbox.py` | 25 passed in 31.59 s | 0 | 02:33:43–02:34:20 | 138.89 → 153.03 |
| b-03 | `tests/fake/test_prompt_seam.py` | 8 passed in 19.96 s | 0 | 02:34:20–02:34:43 | 153.03 → 171.02 |
| b-04 | `tests/unit/test_review_publish_guard.py` | 135 passed in 79.50 s | 0 | 02:34:43–02:36:07 | 171.02 → 171.30 |
| b-05 | `tests/fake/test_review_r185_adversarial.py` | 1 passed in 10.45 s | 0 | 02:36:07–02:36:21 | 171.30 → 163.35 |
| b-06 | `tests/fake/test_state_contract.py` | 37 passed in 29.89 s | 0 | 02:36:21–02:36:54 | 163.35 → 175.29 |
| b-07 | `tests/unit/test_offline.py` | 33 passed in 17.29 s | 0 | 02:36:54–02:37:14 | 175.29 → 183.50 |
| b-08 | `tests/unit/test_cli.py` | 256 passed in 36.12 s | 0 | 02:37:14–02:37:56 | 183.50 → 179.98 |
| b-09 | `tests/unit/test_daemon_verbs.py` | 20 passed in 25.90 s | 0 | 02:37:56–02:38:27 | 179.98 → 181.48 |
| c-01 | `tests/unit/test_test_module_names.py` | 1 passed in 13.02 s | 0 | 02:38:39–02:38:56 | 175.82 → 172.58 |
| c-02 | `tests/unit/test_invariants_index.py` | 25 passed in 12.04 s | 0 | 02:38:56–02:39:15 | 172.58 → 179.74 |
| c-03 | `tests/unit/test_handler_refusals.py` | 372 passed in 130.19 s | 0 | 02:39:15–02:41:31 | 179.74 → 169.48 |
| c-04 | `tests/fake/test_task_hold_admission.py` | 49 passed in 26.10 s | 0 | 02:41:31–02:42:00 | 169.48 → 177.07 |
| c-05 | `tests/fake/test_reset_credit_incident.py` | 19 passed in 27.16 s | 0 | 02:42:00–02:42:34 | 177.07 → 170.82 |
| c-06 | `tests/fake/test_unadmittable_pin_review.py` | 7 passed in 35.20 s | 0 | 02:42:34–02:43:17 | 170.82 → 185.42 |
| c-07 | `tests/fake/test_admission_properties.py` | 27 passed, 1 xfailed in 70.81 s | 0 | 02:43:17–02:44:38 | 185.42 → 182.09 |
| c-08 | `tests/fake/test_admission_visibility.py` | 43 passed in 62.41 s | 0 | 02:44:38–02:46:03 | 183.61 → 217.99 |
| c-09 | `tests/fake/test_capacity_wait_backoff.py` | 30 passed in 65.81 s | 0 | 02:46:03–02:47:14 | 217.99 → 153.84 |
| d-01 | `tests/fake/test_admission_route_isolation.py` (reverted file) | 1 failed, 92 passed in 72.92 s | 1 | 02:47:34–02:48:52 | 151.87 → 113.87 |
| d-02 | `tests/fake/test_probe_cancel.py` | 24 passed in 130.11 s | 0 | 02:48:52–02:51:06 | 113.87 → 109.27 |
| d-03 | `tests/fake/test_unrun_worktrees.py` | 98 passed in 108.26 s | 0 | 02:51:06–02:52:58 | 109.27 → 123.73 |
| d-04 | `tests/fake/test_salvage_finalization.py` | 47 passed in 54.99 s | 0 | 02:52:58–02:53:55 | 123.73 → 106.68 |
| e-01 | `tests/unit/test_review_result.py` (after the escape commit) | 619 passed in 34.42 s | 0 | 02:56:10–02:56:52 | 125.17 → 145.02 |
| e-02 | `tests/unit/test_review_result_storage.py` (after the escape commit) | 15 passed in 20.61 s | 0 | 02:56:52–02:57:22 | 145.02 → 175.92 |

The three long admission files could not run whole inside one tool call, which is capped at 600 s. I collected their test ids with `pytest -q --collect-only`: latency 14, liveness 32, stateful 17. I split the ids in collection order (`split -l 7`, `-l 6` and `-l 3`) and passed each chunk to one pytest run as `@<ids file>`. Every id ran exactly once. Later runs covered:
- every remaining file that names `gate-review`, `REVIEW_PREAMBLE` or `prompt.prepared`;
- the files the build left uninvoked.

| Run | File | Result | rc | UTC | Load (1 min), start → end |
| --- | --- | --- | ---: | --- | --- |
| f-01 | `tests/fake/test_admission_latency.py`, ids chunk-latency-00 (7) | 7 passed in 42.09s | 0 | 03:05:44–03:06:28 | 166.92 → 161.22 |
| f-02 | `tests/fake/test_admission_latency.py`, ids chunk-latency-01 (7) | 7 passed in 9.00s | 0 | 03:06:28–03:06:39 | 161.22 → 151.09 |
| g-01 | `tests/fake/test_admission_liveness.py`, ids chunk-liveness-00 (6) | 6 passed in 11.48s | 0 | 03:06:46–03:07:00 | 148.84 → 155.72 |
| g-02 | `tests/fake/test_admission_liveness.py`, ids chunk-liveness-01 (6) | 6 passed in 19.55s | 0 | 03:07:00–03:07:23 | 155.72 → 157.35 |
| g-03 | `tests/fake/test_admission_liveness.py`, ids chunk-liveness-02 (6) | 6 passed in 17.47s | 0 | 03:07:23–03:07:44 | 157.35 → 155.33 |
| g-04 | `tests/fake/test_admission_liveness.py`, ids chunk-liveness-03 (6) | 6 passed in 18.37s | 0 | 03:07:44–03:08:07 | 155.33 → 164.46 |
| g-05 | `tests/fake/test_admission_liveness.py`, ids chunk-liveness-04 (6) | 6 passed in 16.69s | 0 | 03:08:07–03:08:29 | 164.46 → 167.05 |
| g-06 | `tests/fake/test_admission_liveness.py`, ids chunk-liveness-05 (2) | 2 passed in 221.52s (0:03:41) | 0 | 03:08:29–03:12:16 | 167.05 → 173.38 |
| h-01 | `tests/fake/test_admission_stateful.py`, ids chunk-stateful-00 (3) | 3 passed in 125.23s (0:02:05) | 0 | 03:12:26–03:14:35 | 177.28 → 183.29 |
| h-02 | `tests/fake/test_admission_stateful.py`, ids chunk-stateful-01 (3) | 3 passed in 16.11s | 0 | 03:14:35–03:14:56 | 183.29 → 184.74 |
| h-03 | `tests/fake/test_admission_stateful.py`, ids chunk-stateful-02 (3) | 3 passed in 17.37s | 0 | 03:14:56–03:15:18 | 184.74 → 193.97 |
| h-04 | `tests/fake/test_admission_stateful.py`, ids chunk-stateful-03 (3) | 3 passed in 10.49s | 0 | 03:15:18–03:15:32 | 193.97 → 185.87 |
| h-05 | `tests/fake/test_admission_stateful.py`, ids chunk-stateful-04 (3) | 3 passed in 11.32s | 0 | 03:15:32–03:15:46 | 185.87 → 188.11 |
| h-06 | `tests/fake/test_admission_stateful.py`, ids chunk-stateful-05 (2) | 2 passed in 9.20s | 0 | 03:15:46–03:16:00 | 188.11 → 188.10 |
| i-01 | `tests/unit/test_capacity_steer_route.py` | 22 passed in 15.40s | 0 | 03:16:12–03:16:30 | 180.47 → 176.58 |
| i-02 | `tests/unit/test_admission_uncapped.py` | 27 passed in 243.83s (0:04:03) | 0 | 03:16:30–03:20:38 | 176.58 → 181.99 |
| i-03 | `tests/unit/test_host_push_intake.py` | 13 passed in 18.60s | 0 | 03:20:38–03:21:02 | 181.99 → 191.57 |
| i-04 | `tests/unit/test_launch_qos_property.py` | 10 passed in 24.33s | 0 | 03:21:02–03:21:31 | 191.57 → 186.54 |
| i-05 | `tests/unit/test_placement_ramp.py` | 26 passed in 19.58s | 0 | 03:21:31–03:21:54 | 186.54 → 185.28 |
| i-06 | `tests/unit/test_policy_sandbox_resolution.py` | 42 passed in 12.32s | 0 | 03:21:54–03:22:11 | 185.28 → 177.43 |
| i-07 | `tests/unit/test_retention_archive.py` | stopped at the call's budget after 200.5 s, no failure shown; rerun below | killed | 03:22:11–03:25:32 | 177.43 → 194.05 |
| j-01 | `tests/unit/test_retention_archive.py` | 109 passed in 189.93s (0:03:09) | 0 | 03:26:01–03:29:18 | 185.90 → 184.65 |
| j-02 | `tests/unit/test_scheduler_priority_callers.py` | 9 passed in 39.55s | 0 | 03:29:18–03:30:01 | 184.65 → 189.18 |
| j-03 | `tests/fake/test_launch_qos_review_r1.py` | 6 passed in 14.28s | 0 | 03:30:01–03:30:19 | 189.18 → 187.98 |
| j-04 | `tests/fake/test_reserve_authorization_validation.py` | 11 passed in 12.47s | 0 | 03:30:19–03:30:34 | 187.98 → 180.21 |
| k-01 | `tests/unit/test_daemon_refusals_cli.py` | 8 passed in 11.00s | 0 | 03:30:45–03:31:00 | 175.91 → 177.32 |
| k-02 | `tests/unit/test_mcp_cli_contract.py` | 6 passed in 11.45s | 0 | 03:31:00–03:31:15 | 177.32 → 174.44 |
| k-03 | `tests/unit/test_notice_rendering.py` | 40 passed in 12.09s | 0 | 03:31:15–03:31:33 | 174.44 → 173.19 |
| k-04 | `tests/unit/test_operator_notices.py` | 79 passed in 22.68s | 0 | 03:31:33–03:31:59 | 173.19 → 168.25 |
| k-05 | `tests/unit/test_outcome_unknown.py` | 58 passed in 18.39s | 0 | 03:31:59–03:32:26 | 168.25 → 178.19 |
| k-06 | `tests/unit/test_prune_decisions.py` | 48 passed in 53.62s | 0 | 03:32:26–03:33:27 | 178.19 → 193.28 |
| k-07 | `tests/unit/test_sessions_cli.py` | 142 passed in 31.42s | 0 | 03:33:27–03:34:06 | 193.28 → 207.44 |
| k-08 | `tests/unit/test_status_hold_source.py` | stopped at the call's budget after 359.1 s, no failure shown; rerun below | killed | 03:34:06–03:40:05 | 207.44 → 187.70 |
| l-01 | `tests/unit/test_status_hold_source.py` | 135 passed in 375.18s (0:06:15) | 0 | 03:40:14–03:46:33 | 187.41 → 182.40 |
| l-02 | `tests/unit/test_store_ids.py` | 27 passed in 11.84s | 0 | 03:46:33–03:46:48 | 182.40 → 182.42 |
| l-03 | `tests/unit/test_store_qos_migration.py` | 13 passed in 11.89s | 0 | 03:46:48–03:47:02 | 182.42 → 179.78 |
| m-01 | `tests/unit/test_census_boot_seeds.py` | 36 passed in 38.10s | 0 | 03:47:13–03:47:57 | 174.42 → 172.13 |
| m-02 | `tests/unit/test_hooks.py` | 81 passed in 16.16s | 0 | 03:47:57–03:48:18 | 172.13 → 172.19 |
| m-03 | `tests/unit/test_compat.py` | 1615 passed in 76.34s (0:01:16) | 0 | 03:48:18–03:49:36 | 172.19 → 124.58 |
| m-04 | `tests/fake/test_capacity_steer_daemon.py` | 72 passed in 26.93s | 0 | 03:49:36–03:50:05 | 124.58 → 114.12 |
| m-05 | `tests/fake/test_admission_ramp.py` | 19 passed in 31.64s | 0 | 03:50:05–03:50:38 | 114.12 → 122.31 |
| m-06 | `tests/fake/test_admission_ramp_retention.py` | 11 passed in 6.80s | 0 | 03:50:38–03:50:47 | 122.31 → 123.41 |

**Which runs finished.** Every run finished except i-07 and k-08, which the driver stopped at its per-call budget. Their process groups were killed, no failure had printed, and no process of mine remained. Both files were rerun whole and passed: j-01, 109 passed; l-01, 135 passed.

**Totals.**
- The 69 finished runs of the 71 covered 57 distinct test files, including the three chunked files, with 4,937 passed, 1 xfailed and 1 failed. These counts exclude the two runs outside the driver, `pytest-01` and `pytest-02`.
- The only failure is d-01, the route-isolation race described above, on the file as release/217 has it.
- Every file in the build rules' end list ran: capacity wait backoff, latency, properties, stateful, visibility, liveness, route isolation, task hold, reset credit, unadmittable pin, probe cancel, unrun worktrees, daemon verbs, state contract, test module names and the invariants index.
- So did every file the A1 build listed as uninvoked.

### Deviations

- **Corpus count.** r1's 301-file set could not be reproduced (see Corpus). I report the full set (334) and the as-of-r1 set (327), with the same rule before and after.
- **Spot-check sampling.** It is over distinct file contents, not paths, because of byte-identical copies. Only 28 distinct reviews are valid, so the valid sample is 20 of 28.
- **Beyond r1's list.** The parser also accepts `+` list items, `1)` items, `_` and `__` emphasis, a short parenthetical after the level, and `.` before a space. All appear in hub reviews as findings. It still does not accept a comma after the level, for the reason given in the spot-check.
- **Changed existing expectations.**
  - A tab-indented line is no longer a finding (`test_markdown_line_start_findings` lost its `"\t- **"` prefix).
  - A bare `\r` is no longer a line break (`test_generated_counts_only_line_start_findings`).
  - Gate rounds show null in every view (the offline CLI test was split).
  - `parse_review_result` remains the pure, unbounded parser. The daemon calls `parse_review_deliverable`, so the storage tests now patch that name.
- **I4 amended.** A deliverable over 4 MiB is invalid with a size reason (C-12.11 says so).
- **What counts as writable.** The writable-only sentence follows the resolved sandbox: workspace-write, from `-s` or a `worktree-write` policy. A read-only Codex review whose launch has a shell (capability `commands`) still does not get it, matching the removed comment's "writable reviews".
- **Suite lock.** Tests ran directly with `-n 4`, not under `suite_hold.sh`, as this brief's Tests rule says.
- **Load.** It was 57 to 218 throughout.
- **Route-isolation test.** With the stabilization reverted, the file fails its known race under load. That is release/217's state, not a change of this round.
- **Long files chunked.** Latency, liveness and stateful ran as id chunks, not as whole files, because of the tool's 600 s cap per call.
- **Two reruns.** Two runs (`test_retention_archive.py`, `test_status_hold_source.py`) hit the driver's budget, were stopped, and were rerun whole.
- **No full suite.** CI runs it.

### Cleanup

Scratch was `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-rv-f1` (`$(getconf DARWIN_USER_TEMP_DIR)sf-rv-f1`). During the work I removed its `mut/` copy once, with a guarded `shutil.rmtree`, before the final mutant run.

**Folders outside scratch.** These were made in the worktree by `uv sync` and the test runs. Each was renamed into `sf-rv-f1/cleanup/` and removed with `shutil.rmtree` after asserting `sf-rv-f1` was in its resolved path:
- `/Users/maxghenis/.subfleet/worktrees/20261114-190014-rv-a1-fix-r1/.venv`
- `…/.hypothesis`
- `…/subfleet/__pycache__`
- `…/subfleet/adapters/__pycache__`
- `…/subfleet/conversations/__pycache__`
- `…/subfleet/gate/__pycache__`
- `…/subfleet/guard/__pycache__`
- `…/subfleet/sessions/__pycache__`
- `…/tests/__pycache__`
- `…/tests/fake/__pycache__`
- `…/tests/unit/__pycache__`

**Scratch.**
- Removed whole (1.2 GB) by the same guarded `rmtree`.
- A first try stopped on a read-only directory a retention test's fixture had left under `pytest-j-01`. The retry set owner-only mode 0700 on two directories inside scratch, then deleted them. Its handler asserted every path was inside scratch.
- The helper scripts named above (`corpus.py`, `spot.py`, `timing.py`, `mutants.py`, `runfiles.py`) went with it. This section records their inputs, rules and edits.

**Left in place.**
- `$(getconf DARWIN_USER_TEMP_DIR)uv-cache`, the shared uv cache the Tests rule names. It existed before this session (created 2026-11-14 14:13 UTC) and other jobs use it.
- `/tmp/sf-failed`: empty, and nothing kept there by these runs.

**End state.** No process of mine remained (`ps` showed no pytest under these basetemps). The worktree holds no untracked or ignored files, and HEAD moved only by the commits listed at the top.

Code head: 0689f895b79cc11ea1db5111116751825dd53be5.


## Fix round 2

Round 2 of PR #313 (d2937, advisory phase), from checkpoint `952c7901923fe884c79f4046258bbfbe8c882eb2`. Started **2026-11-15 19:50:50 UTC**, from `date -u` in the first command; deadline **22:50:50 UTC**. This section supersedes the earlier reports' JSON-stream claim. Final results and cleanup are recorded below.

All three formal findings are fixed. The initial workspace contained an unstaged change to `tools/review_contract_mutations.py`, including the anchor repairs; those changes were preserved and validated. No other initial changes were reported by Git.

- **P1, list-item fences.** `_FENCE` recognized only whitespace and blockquotes before a fence. It missed `- ```text`, then mistook the closer for an opener and hid the actual finding. It now consumes bullet and ordered-list containers, including nested quotes, for both opening and closing fences (the closer behavior was incorrect and is corrected by fix round 3: list markers may precede opening fences only), preserving the same-character/length and backtick-info-string rules. `test_list_item_fences_hide_examples_and_close_before_real_findings` covers seven container prefixes and both fence characters. The exact review case is also in `test_review_regressions_keep_explicit_counts_in_every_view[list-fence]`: acceptance, evidence, the event, all four online/offline views and restart must retain P1=1, valid=true, consistent=false. All **15 cases failed before** the fix and **passed after**.
- **P1, numeric finding summaries.** `_TALLY.match` accepted a numeric/count-word prefix and stopped at punctuation without examining the rest of the line. `_TALLY.fullmatch` now requires a complete count expression; any continuation must be another complete level/count pair, with optional terminal punctuation. `test_numeric_or_count_word_summaries_are_findings` covers the three review summaries (`1,000 jobs…`, `500 (Internal Server Error)…`, `Zero (0) requests…`) and four further partial-tally/prose forms. The three review cases also pass through accepted evidence, event, every view and restart, using explicit expected counts rather than a fresh parse as the oracle. All **10 cases failed before** and **passed after**. Existing tally exclusions remain covered by the full parser file.
- **Quoted indented code, also observed in r2.** The finding container matcher consumed unlimited whitespace after `>`. `_INDENTED_CODE` now excludes four spaces after a quote's separator space, or a tab. `test_indented_code_in_a_quote_is_not_a_finding` covers single and nested quotes: **2 failed before, 2 passed after**.
- **P2, stale mutation anchors.** The preserved helper changes replace the old gate-review preamble anchor with `is_review_job(...)`, and the inline parse/storage anchor and mutant fallback with `review_result`. Static comparison finds zero matches for those two HEAD anchors and exactly one for every repaired anchor; all five replacements compile. The helper keeps source-byte restoration and named-test/exit-1 kill checks. Its existing scratch/cache changes were preserved; its private pytest plugin now also redirects `tempfile.tempdir` into each mutation directory so fixtures stay inside job scratch. All five execution results and killers appear below.

C-12.11 and a change-log line in `docs/acceptance-contract.md` now specify the container fences, quoted indentation, complete tallies and JSON behavior. No finding is left open.

The PR body's “leaving the JSON and artifact streams as they were” needs correction. Read-only comparison with the checkpoint's merged base (`952c79019^2`, `a4aded885`) and the list/show/wait/notice paths establishes:

- `runs --json` keeps one JSON object per job but adds `review` to every emitted row. Current online show adds the same value at top-level `review` and `job.review`; offline show stays flat and adds `review`. Bare show's metadata object gains those fields too, before its existing delimiter and artifact stream.
- The saved object has `verdict` (`approve`, `request_changes` or null), integer P0–P3 `levels`, boolean `valid`, string-or-null `reason`, and boolean `consistent`. It is selected from the accepted terminal `task=review` attempt. Other jobs, gate rounds, unfinished/unaccepted reviews and records without a saved dictionary return null. The CLI supplies null in list rows and at the top of show when an older daemon omits the field; it does not add `job.review` inside that older daemon's job object. Reads never reparse artifacts, and a CLI upgrade alone creates no stored results.
- The new review template changes hashes and sizes of existing `prompt-sent` artifacts, and of manifest artifacts where new preamble metadata is added (read-only reviews and reviews that previously omitted a preamble). There is no new prompt-prepared artifact row; the submitted prompt bytes, prompt path and digest input remain unchanged. Manifest preamble fields are not directly embedded in show metadata.
- Full show's existing `attempts[].evidence_json` string gains an encoded `review` member on accepted reviews. `wait --json` likewise changes `attempt.evidence_json`, with no new top-level `review`. Acceptance records one new `job.review_result` event whose `data_json` is the encoded result. This round corrects counts/validity/consistency in newly parsed reviews without adding further fields or rewriting saved results.
- `notices --json`, `notice.pending` and the notice arrays in show keep their JSON shapes and contents; notice text is unchanged. Bare show keeps its metadata/delimiter/artifact framing, with the expanded metadata and a readable review line on stderr. `--out` and `--err` retain their artifact behavior. The PR body itself was read only, for the hub to correct.

Sources: `subfleet/cli.py` list/show/wait/notices handlers; `subfleet/daemon.py` list/show, `_wait_answer`, `_finalize` and notice handlers; `subfleet/offline.py`; `subfleet/review_record.py`; `subfleet/store.py::notice_rows`; `subfleet/render.py::notice_text`. Existing JSON tests in `tests/unit/test_review_result_cli.py` exercise online/offline parity, nested show, bare metadata and old-daemon null handling.

### Round-two command ledger

Read-only discovery and repeated output polls are grouped by command family. Every test invocation is listed individually, in execution order. `S` below means `/private/tmp/sf-rv-fix2`.

| Command / family | Result |
| --- | --- |
| First `date -u`; `cat` the named review and PR report; `gh pr view 313 --repo MaxGhenis/subfleet-v2` and its `--json title,body,baseRefName,headRefName` form | exit 0; start above, review REQUEST_CHANGES with two P1s and one P2, stale PR-body JSON claim confirmed |
| `git status --short --branch`, `git rev-parse HEAD`, `cat .git`; confined `rg --files`, `rg -n`, `git grep -l`, `cat`, `sed`, `tail`, `wc`, `git show`, `git diff` | read-only discovery/audits; initial HEAD above; initial helper edit preserved. No AGENTS.md found (no-match exit 1). Parser importer search found exactly three files; no test imports the mutation helper (no-match exit 1). A stale local release-ref comparison was replaced by the checkpoint's merged-base comparison, isolating 13 PR files |
| `getconf DARWIN_USER_TEMP_DIR`; `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$(getconf DARWIN_USER_TEMP_DIR)uv-cache" uv sync --group dev` | getconf exit 0; sync exit 2, cache `sdists-v9/.git` denied |
| Python mkdir of `$TMPDIR/sf-rv-fix2` | PermissionError; no folder created; fallback `S` created |
| `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=S/uv-cache uv sync --group dev` | exit 0; CPython 3.15.2, 9 packages installed, `.venv` created |
| Python scratch runner/private-plugin creation, named `apply_patch` and Python file edits | exit 0; fixtures/pruning and Hypothesis storage confined to S; no environment snapshot written |
| AST checks / helper anchor extraction and replacement compilation | four Python files parse; both old anchors have 0 matches, all five repaired anchors have 1 and compile. An exploratory dataclass exec import failed on its synthetic namespace; AST literal extraction replaced it successfully, with no source effect |
| Named `git diff --check` | initial source/docs check passed; one report-draft EOF blank-line failure (exit 2) was corrected; subsequent checks passed |
| Named parser/contract `git add`; named-file `git commit -m 'Fix review finding counts in Markdown containers and numeric summaries' -- ...` | both exit 128, `index.lock: Operation not permitted`; no commit created |
| `git add tools/review_contract_mutations.py`; `git commit -m 'Repair review contract mutation anchors and scratch isolation' -- tools/review_contract_mutations.py` | both exit 128, index.lock denied; no commit |
| `git add docs/reports/2026-11-13-review-contract.md`; `git commit -m 'Document review verdict fix round two and verification' -- docs/reports/2026-11-13-review-contract.md` | both exit 128, index.lock denied; no commit |
| `python3 S/append_results.py`; `python3 S/cleanup.py`; final Python AST / `git diff --check` / status / HEAD checks | consolidated seven ordinary test runs and five mutants; cleanup and final checks recorded below |

All ordinary test commands set `TMPDIR` from getconf, `UV_CACHE_DIR=S/uv-cache` (sandbox fallback), `PYTHONDONTWRITEBYTECODE=1`, `HYPOTHESIS_STORAGE_DIRECTORY=S/hypothesis`, and `PYTHONPATH=S`. The private plugin sets `tempfile.tempdir=S/tmp` and `tests.failed_keep.ROOT=S/failed` before conftest starts. Each new basetemp contains `pytest`. The scratch runner expands `python3 S/run_tests.py LABEL TARGETS` to:

```sh
uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 \
  -p sf_rv_fix2_temp -p no:cacheprovider --timeout=180 --timeout-method=thread \
  --basetemp S/pytest-LABEL TARGETS
```

| LABEL / TARGETS | Result | Exit | Wall seconds |
| --- | --- | ---: | ---: |
| `fence-before`: `tests/unit/test_review_result.py::test_list_item_fences_hide_examples_and_close_before_real_findings tests/unit/test_review_result_storage.py::test_review_regressions_keep_explicit_counts_in_every_view` | 15 failed in 224.43s (0:03:44) | 1 | 366.5 |
| `numeric-quote-before`: `tests/unit/test_review_result.py::test_numeric_or_count_word_summaries_are_findings tests/unit/test_review_result.py::test_indented_code_in_a_quote_is_not_a_finding tests/unit/test_review_result_storage.py::test_review_regressions_keep_explicit_counts_in_every_view -k 'numeric or indented'` | 12 failed in 121.48s (0:02:01) | 1 | 148.46 |
| `regressions-after`: `tests/unit/test_review_result.py::test_list_item_fences_hide_examples_and_close_before_real_findings tests/unit/test_review_result.py::test_numeric_or_count_word_summaries_are_findings tests/unit/test_review_result.py::test_indented_code_in_a_quote_is_not_a_finding tests/unit/test_review_result_storage.py::test_review_regressions_keep_explicit_counts_in_every_view` | 27 passed in 180.38s (0:03:00) | 0 | 224.88 |
| `changed-files`: `tests/unit/test_review_result.py tests/unit/test_review_result_storage.py` | 661 passed in 383.60s (0:06:23) | 0 | 447.68 |
| `importer-cli`: `tests/unit/test_review_result_cli.py` | 24 passed in 216.14s (0:03:36) | 0 | 324.2 |
| `guards`: `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | 435 passed in 569.45s (0:09:29) | 0 | 663.85 |
| `mutant-baseline`: `tests/unit/test_review_contract_prompt.py` | 117 passed in 326.42s (0:05:26) | 0 | 367.39 |


The required order was preserved: new failing regressions, those regressions passing after the fix, complete changed test files, the remaining importing file, then the four guards. The unchanged prompt file supplies passing baselines for the prompt mutants. No required file was omitted and the whole suite was not run; CI owns it.

Mutation command: `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=S/uv-cache PYTHONDONTWRITEBYTECODE=1 python3 tools/review_contract_mutations.py --scratch-root S --timeout 600`. Each generated command is `uv --cache-dir S/uv-cache run --no-sync pytest -q -x -p sf_rv_review_mutation -p no:cacheprovider --tb=short KILLER --basetemp S/mutations/run-*/MUTANT/pytest`. Each mutant has a unique directory and private tempfile/pruning/plugin state; none runs concurrently with another test or source edit.

| Mutant | Named killer | Result | Seconds | Restored |
| --- | --- | --- | ---: | --- |
| `preamble-all-jobs` | `tests/unit/test_review_contract_prompt.py::test_non_review_prompts_never_get_the_review_contract` | exit 1, killed; 1 failed in 152.98s (0:02:32) | 239.438 | byte-equal SHA-256 |
| `level-anywhere-in-line` | `tests/unit/test_review_result.py::test_levels_in_prose_and_malformed_prefixes_are_not_findings` | exit 1, killed; 1 failed in 72.44s (0:01:12) | 129.73 | byte-equal SHA-256 |
| `valid-ignores-verdict` | `tests/unit/test_review_result.py::test_first_nonempty_line_must_be_a_verdict_even_with_findings` | exit 1, killed; 1 failed in 103.07s (0:01:43) | 205.711 | byte-equal SHA-256 |
| `first-attempt-review` | `tests/unit/test_review_result_storage.py::test_retry_reports_only_accepted_attempt_review` | exit 1, killed; 1 failed in 107.38s (0:01:47) | 189.984 | byte-equal SHA-256 |
| `p0-definition-shortened` | `tests/unit/test_review_contract_prompt.py::test_preamble_definitions_are_verbatim_and_under_130_words` | exit 1, killed; 1 failed in 94.13s (0:01:34) | 141.57 | byte-equal SHA-256 |

All five mutants failed in their named tests, not collection/setup, and all source bytes were restored. Run directory: `/private/tmp/sf-rv-fix2/mutations/run-078pcixx`.
Restored `subfleet/daemon.py` SHA-256: `95281aa5b65b8a655502766b7f096f9cc166dc67d127a2c24ed577f25545d6b8`.
Restored `subfleet/review_result.py` SHA-256: `b99f353c829313e226538faa21351062fc959e33c506c74a0df4bedc4e01b919`.

### Completion, deviations and cleanup

Post-fix checks: **1,264 passes including repeats** (1,237 distinct test cases across eight files), plus five expected mutant failures. Before the fix, all 27 added regression cases failed. No required file was omitted. No whole-suite, live-provider or CI-rerun check was run, and no CI-green claim is made.

The only scope/setup deviations are the preserved pre-existing helper edit, the additional two quoted-indentation regression cases from r2's observation, the sandbox-denied getconf scratch/cache requiring `/private/tmp/sf-rv-fix2` and its private cache, and denied commits. TMPDIR itself stayed the required getconf value; the pytest plugins redirected actual fixture allocations and pruning into scratch. One exploratory static import failed before the AST-based check; one redundant output poll addressed an already-closed tool session; one report EOF whitespace error was corrected. None affected production source or test results.

Named-file staging and commit attempts for the parser/contract step, mutation-helper step and final report step all exited 128 because Git could not create `index.lock`. No commit was created, no history rewritten and nothing pushed. All six changed files remain in this assigned workspace for hub collection; the caller's checkout was never written. The PR body was not changed.

Folders outside job scratch were recorded here **before deletion**:

- `/Users/maxghenis/.subfleet/worktrees/20261115-100032-rv-a1-fix-r2/.venv`

Cleanup completed: 1 outside-scratch folder moved into `sf-rv-fix2/cleanup` and deleted with guarded `shutil.rmtree`; job scratch, including logs, private caches and mutation copies, was also guardedly removed. Permission-repair callbacks: 0. The command ledger above preserves the results. All test and mutation commands returned before cleanup; no work that outlives this session was started.

Completed **2026-11-15 21:25:20 UTC**, before the **22:50:50 UTC** deadline. Final AST, whitespace, status and HEAD checks passed as recorded below. HEAD is unchanged because commits were denied; the tested changes are uncommitted.

Closeout verification: four Python AST checks and both restored-source SHA-256 checks passed; `git diff --check` passed; Git lists exactly the six named modified files and no untracked or ignored files; HEAD remains the checkpoint. All checks exited 0. Scratch and `.venv` are absent.

Head built: 952c7901923fe884c79f4046258bbfbe8c882eb2


## Fix round 3

PR #313, D-REVIEW-CONTRACT1 (d2937, advisory phase), from clean detached head `0c2995c04623cdda2c3f7c84a2af7ba06ba4959b`. Started **2026-11-17 13:37:46 UTC**, from `date -u` in the first command; deadline **16:37:46 UTC**. Completed **2026-11-17 13:57:41 UTC**, before the deadline.

All four assigned findings are fixed:

- **P1, list markers closing fences.** The shared opening/closing pattern accepted list markers inside a fence. `_FENCE_CLOSE` now preserves base's `[ \t>]*` prefix; list containers remain allowed only by the opener. Three new `test_review_regressions_keep_explicit_counts_in_every_view` cases all failed before and pass after: `diff-removed-indented-closer` expects request_changes/P1=1/valid/consistent; `list-marker-inside-md-fence` expects approve/P1=1/valid/inconsistent; `list-marker-inside-text-fence` expects approve/P1=0/valid/consistent. Each goes through `_finalize`, accepted attempt evidence, `job.review_result`, online/offline list/show, and a daemon restart. Expected counts are explicit; restart also rechecks evidence and event payloads. Round 2's 14 list-fence parser cases and existing fence cases pass. C-12.11 and both relevant change-log entries are corrected; round 2's report retains its original closer claim with an explicit correction annotation.
- **P2, parenthetical tally comments.** Complete-line matching rejected every trailing comment. `_TALLY` now allows one final parenthetical after a complete count expression, before optional terminal punctuation. `test_only_a_complete_count_with_one_final_parenthetical_is_a_tally` pins all six requested lines plus the bare `500 (Internal Server Error)` tally limit, punctuation, and a second-parenthetical finding. Four changed cases failed before; five prose/boundary controls passed before; all nine pass after. The added every-view `parenthetical-tally` APPROVE case failed before and passes after with P0=0 and consistent=true. Numeric summaries and `P1: 1. See below.` remain findings. C-12.11 states the rule and its limit.
- **P3, quoted tab separator.** The indented-code matcher consumed only a quote's separator space, treating its separator tab as code. It now consumes an optional space or tab before checking further indentation. The added `>\tP1:` parameter in `test_indented_code_in_a_quote_is_not_a_finding` failed before with P1=0 and passes after with P1=1. Both existing quoted-code controls remain P1=0. C-12.11 now describes the separator and four further columns.
- **P3, mutation scratch name.** `--scratch-root` is required and must resolve to a direct child of `$TMPDIR` whose name starts `sf-`. Documentation and the source-idle comment are round neutral. Missing root, a non-`sf-` root and a nested root each produce expected exit 2; the valid round-three path succeeds in the mutation run.

The third P3, the stale PR-body JSON claim, remains with the hub as instructed. The body was read only; round 2's corrected JSON description remains available for the hub's rewrite. No other finding is left open.

Every ordinary test used Python 3.15.2, a new `pytest-*` basetemp, and this command template (`S=/private/tmp/sf-rva1-fix3`):

```sh
python3 S/run_tests.py LABEL TARGETS
# expands to:
uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4 \
  -p sf_rva1_fix3_temp -p no:cacheprovider --timeout=180 --timeout-method=thread \
  --basetemp S/pytest-LABEL TARGETS
```

The runner sets `TMPDIR` from getconf, `UV_CACHE_DIR=S/uv-cache`, `PYTHONDONTWRITEBYTECODE=1`, `HYPOTHESIS_STORAGE_DIRECTORY=S/hypothesis` and `PYTHONPATH=S`. Its private plugin confines tempfile allocation and failed-test retention/pruning to S before conftest runs.

| Run / targets | Result | Exit | Pytest / command seconds |
| --- | --- | ---: | ---: |
| `regressions-before`: `tests/unit/test_review_result.py::test_only_a_complete_count_with_one_final_parenthetical_is_a_tally`, `tests/unit/test_review_result.py::test_indented_code_in_a_quote_is_not_a_finding`, `tests/unit/test_review_result_storage.py::test_review_regressions_keep_explicit_counts_in_every_view` | 9 expected assertion failures, 11 passed | 1 | 61.49 / 73.58 |
| `regressions-after`: same targets, plus `tests/unit/test_review_result.py::test_list_item_fences_hide_examples_and_close_before_real_findings` and `tests/unit/test_review_result.py::test_a_fence_closes_only_on_its_own_marker` | 35 passed | 0 | 73.28 / 79.10 |
| `importers`: `tests/unit/test_review_result.py`, `tests/unit/test_review_result_storage.py`, `tests/unit/test_review_result_cli.py` | 699 passed | 0 | 128.82 / 137.66 |
| `guards`: `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py`, `tests/unit/test_handler_refusals.py` | 435 passed | 0 | 199.13 / 224.37 |

The required order was preserved. Post-fix ordinary checks total **1,169 passes including repeats, 1,134 distinct cases across seven files**. All nine behavior-changing regression parameters failed before their fixes; the added controls intentionally passed before as well.

After ordinary tests, `python3 S/run_mutant.py` ran only:

```sh
TMPDIR=/private/tmp
UV_CACHE_DIR="$TMPDIR/sf-rva1-fix3/uv-cache" PYTHONDONTWRITEBYTECODE=1 \
  python3 tools/review_contract_mutations.py --scratch-root "$TMPDIR/sf-rva1-fix3" \
  --only list-markers-close-fence --timeout 600
```

The new mutant replaces the dedicated closer match with the list-capable opener match. Killer: `tests/unit/test_review_result_storage.py::test_review_regressions_keep_explicit_counts_in_every_view[diff-removed-indented-closer]`. Result: **1 assertion failure in 13.99 seconds**, pytest exit 1; helper exit 0; mutation 38.537 seconds, wrapper 41.77 seconds. This was a named-test kill, not collection/setup failure. The helper restored its pristine bytes; the wrapper independently compared parser and daemon bytes with copies saved before the ordinary importer run:

- `subfleet/review_result.py`: SHA-256 `e46058749ee041b1d8c5b8972685b1028901486f9a9269132521df3a7658fb44`.
- `subfleet/daemon.py`: SHA-256 `95281aa5b65b8a655502766b7f096f9cc166dc67d127a2c24ed577f25545d6b8`.

Command ledger, with read-only commands and repeated output polls grouped by family:

| Command / family | Result |
| --- | --- |
| First `date -u`, `pwd`, Git status/branch/HEAD; required review/report `cat` and report `sed`/`wc`; `gh pr view 313 --repo MaxGhenis/subfleet-v2` | exit 0; start/head above; initially clean; required sources read; stale PR-body claim confirmed |
| Confined `rg --files`, `rg -n`, `cat`, `sed`, `tail`, Git diff/status and `git grep -l` importer searches; output polls | successful reads; parser importers are exactly the three files run above; no mutation-helper test import (expected grep exit 1); no AGENTS.md in inventory |
| Required `getconf`; shared-cache `uv sync --group dev`; mkdir under required temp root | getconf succeeded; sync exit 2 (`sdists-v9/.git` denied); mkdir raised PermissionError |
| Fallback scratch creation; `UV_CACHE_DIR=S/uv-cache uv sync --group dev` | exit 0; `.venv` created; nine packages installed |
| Named `apply_patch`/Python source, docs, scratch runner/plugin/report/cleanup preparations | succeeded; synthetic fixtures only |
| Python AST checks, mutation-anchor audit, report/contract checks; helper CLI rejection probes | 3-file then 4-file AST checks passed; all six anchors unique and replacement sources compile; four wording assertions passed; three expected CLI exit-2 rejections |
| Named-file `git diff --check` checks | passed |
| Parser/tests/contract/report-correction named `git add` and `git commit`; helper named `git add` and `git commit` | all four commands exit 128: `index.lock: Operation not permitted`; no commits created |
| Report append; named report `git add` and `git commit`; guarded cleanup; final Python AST/hash/absence and Git checks | report written; both Git commands exit 128 (index.lock denied); cleanup exit 0; four ASTs, two hashes, whitespace and six-file status checks pass |

All required test files ran. The whole suite, live-provider checks, CI reruns, optional markdown-it-py comparison and the five existing mutants were not run. The scoped new mutant ran alone as requested.

Deviations are the sandbox-denied prescribed scratch/cache, requiring S and its private cache, and denied Git commits. Ordinary tests retained the prescribed getconf TMPDIR; the mutation command and helper argument probes used `/private/tmp` as TMPDIR so its explicit fallback root satisfies the new containment rule. No merges, history rewrites, pushes, other-branch changes or caller-checkout writes occurred. No live state or live process was inspected, and no forbidden write-shaped command was executed.

Folder outside scratch, recorded **before deletion**:

- `/Users/maxghenis/.subfleet/worktrees/20261117-083630-rva1-fix-r3/.venv`

Cleanup completed: the recorded folder was relocated into S/cleanup, its resolved destination was asserted to contain `sf-rva1-fix3`, and it and S were removed with guarded `shutil.rmtree` (exit 0; no permission-repair callbacks). The denied shared cache was left alone. All test/mutation processes returned before cleanup; no work that outlives the session was started.

Final report staging and commit were also denied (both exit 128, index.lock). No commits were created; all six named changed files remain for hub collection. Final verification passed: four Python ASTs, both restored-source SHA-256 values, `git diff --check`, exactly six modified files, no untracked or ignored files, and scratch/.venv absent. HEAD remains the starting checkpoint.

Head built: 0c2995c04623cdda2c3f7c84a2af7ba06ba4959b
