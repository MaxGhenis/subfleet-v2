# Resume request replay before pin refusal

Issue #344, D-EFFORT-ENV1 review r5. Work started at `2026-11-26T16:16:34Z` on detached HEAD `a4d21501e` for `fix/resume-replay-first-217`.

## Cause and changes

At `a4d21501e`, `daemon.py:549–564` selected the resume pin, and `daemon.py:3755–3774` recorded it with the source provider model ID. `Daemon.submit` detected an accepted request at line 3140, but its pin check at lines 3273–3291 raised before the digest at line 3350 and the request lookup at lines 3383–3392. `policy.py:939–960` lets a current key or retired alias resolve the source ID even when its entry has a different provider ID. An earlier daemon could accept such a request under the same policy hash. After the upgrade, the same pin raised code 7 before the caller could settle the lost acknowledgement.

- `subfleet/daemon.py` defers the existing pin refusal when the request ID is already held and the pin resolves to an entry at a different ID. The existing digest lookup returns the job for a match and names it in a code 2 refusal for a mismatch. If the lookup finds the job gone, the C-26.13 session check runs before the deferred pin refusal. Validation between pin resolution and lookup still runs.
- `tests/fake/test_resume_contract.py` adds the replay and missing-job cases and the three C-12.4 shadow cases. The replay fixture holds the daemon policy hash constant while simulating the original acceptance.
- `docs/acceptance-contract.md` updates C-6.2, C-12.4 and the version 3 change log. The invariant markers stay intact.
- `docs/reports/2026-11-26-resume-replay-first.md` records this work and its verification.

A retry can reach digest comparison only while a policy name resolves the source ID. A returned job with a mismatched model still stops at the `resume-identity-mismatch` launch check (base lines 10250–10255). Dispatch pins retain their pin-check-then-lookup order.

## Invariants

| Invariant | Tests in `test_resume_contract.py` |
| --- | --- |
| 1. A fresh resume with a missing source ID raises `unknown-pinned-model` (7), carries the `resume_pin` detail and `RESUME_PIN_FIX`, and stores nothing. | (a), the fresh refusal test under `no-alias`, `retired-shadow` and `key-shadow`. The helper verifies the exception type, message, fix and wire text. |
| 2. A matching retry returns its job and request IDs with `created: False`, preserves the stored row and adds no job. | R1 under both shadow types. Both reach admission and stop before the adapter. The launch receipts contain the identity-mismatch message. |
| 3. Another payload raises `different-payload` (2), names the existing job and adds no job. | R2, which changes the prompt while keeping the request ID. |
| 4. A held request with an unresolvable source ID raises the pin refusal and leaves the existing row and job set intact. | R3 with `no-alias`. |
| 5. A held request whose job disappears by lookup raises the pin refusal and stores nothing. | R4, with acceptance reported at the first check and no matching row at lookup. |
| 6. C-12.4 follows the entry at the source ID. | (a) refuses a shadow key with no entry at the ID. (b) preserves the imported source row and launches on `astra` at the source ID. (c) launches on `opus` at the source ID and its low effort. |

## Fail-first and mutation results

At the original daemon revision, both R1 variants and R2 failed at `daemon.py:3285` with `subfleet.adapters.base.AdapterError: unknown pinned model 'claude-haiku-4-5-20251001'` (`unknown-pinned-model`, code 7). R3, R4, and all three shadow cases passed. After the fix, all 49 resume module cases passed.

- **M0** reversed `779f34241` in `daemon.py`. Six cases failed, including (a) `key-shadow` and `retired-shadow`, (b), (c), the existing imported shadow case and the existing `id-rollover-retired-shadow` case. The `no-alias` case and the other three policy-change cases passed.
- **M1** raised at the pin on every request. Both R1 variants and R2 failed with `unknown-pinned-model`.
- **M2** omitted the raise after a lookup miss. R4 failed with `DID NOT RAISE AdapterError` as the request continued to job creation.
- **M3** also deferred the unresolved pin. R3 failed because the digest resolver's `PolicyError` surfaced as `ProtocolError` with reason `invalid-args`.

The optional M0 probe submitted the same payload directly in the retired-shadow state under the reversed build. The direct acceptance used a fresh request ID and left the rolled-over entry in place. Both its stored digest and the simulated acceptance's stored digest were `d1f87f93ec04663929cba9f2de2e9c3616ba8669155fc523b4cfb456b0aef9fa`. The temporary probe test was removed.

## Test results

The one 17-module run passed 1,115 cases and skipped 31. Its three failures all expected a workdir outside Git. The fallback put those fixtures under this checkout, so the policy-sandbox test accepted the build and the two CLI cases waited on accepted jobs until pytest's 540-second timeout. All three passed on a focused rerun using the sandbox-assigned temp directory outside Git. The invariant index, unique test module names, and handler refusal tests passed in the combined run.

`df -H /` and `uptime` were recorded before each run. The root filesystem reported 3,996 GB total and 14 GB used. Each verified basetemp was measured with `du -sk` and removed before the next run.

| Run | UTC start | Root free | Local time and load averages | Pass | Fail | Skip | Basetemp KiB |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| fail-first | 2026-11-26T16:20:01Z | 76G | 17:20, 40.36 52.78 60.86 | 46 | 3 | 0 | 30,656 |
| m0 | 2026-11-26T16:21:41Z | 76G | 17:21, 43.42 49.04 58.42 | 4 | 6 | 0 | 6,176 |
| after-change | 2026-11-26T16:23:32Z | 76G | 17:23, 62.31 53.71 59.08 | 49 | 0 | 0 | 30,732 |
| m1 | 2026-11-26T16:25:59Z | 76G | 17:26, 62.02 51.26 56.79 | 0 | 3 | 0 | 1,848 |
| m2 | 2026-11-26T16:26:33Z | 76G | 17:26, 59.53 51.85 56.82 | 0 | 1 | 0 | 616 |
| m3 | 2026-11-26T16:27:00Z | 76G | 17:27, 43.64 48.70 55.51 | 0 | 1 | 0 | 616 |
| full | 2026-11-26T16:27:50Z | 75G | 17:27, 42.07 47.10 54.50 | 1115 | 3 | 31 | 157,736 |
| outside-git | 2026-11-26T16:57:49Z | 75G | 17:57, 36.95 43.53 50.35 | 3 | 0 | 0 | 588 |
| probe | 2026-11-26T16:58:23Z | 75G | 17:58, 37.71 43.06 49.95 | 1 | 0 | 0 | 628 |

## Test commands

These are the pytest invocations with their common prefix factored out. The test environment used a local copy of the pre-existing uv cache. The last two runs used the inherited, sandbox-assigned temp directory's `rrf-build-r1` child for `TMPDIR` and basetemp.

```sh
outside_job="${TMPDIR%/}/rrf-build-r1"
export TMPDIR="$PWD/build/rrf-build-r1"
export UV_CACHE_DIR="$TMPDIR/uv-cache"

run() {
    run_name="$1"
    shift
    uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
        -n 4 --timeout=540 --basetemp "$TMPDIR/pytest-$run_name" -q "$@"
}
resume=tests/fake/test_resume_contract.py
run fail-first "$resume"
run m0 \
    "${resume}::test_resume_is_refused_unknown_pinned_model_when_no_entry_holds_its_source_model_id" \
    "${resume}::test_imported_resume_named_by_its_source_id_pins_the_entry_at_that_id_past_a_current_key" \
    "${resume}::test_resume_pins_the_entry_at_its_source_id_when_a_current_key_spelled_like_it_names_another" \
    "${resume}::test_imported_resume_pins_source_id_when_a_current_key_shadows_it" \
    "${resume}::test_resume_after_a_policy_change_pins_an_entry_at_its_source_model_id"
run after-change "$resume"
run m1 \
    "${resume}::test_resume_retry_gets_its_job_back_when_no_entry_holds_its_source_model_id" \
    "${resume}::test_resume_retry_with_another_payload_is_refused_as_a_different_payload"
run m2 "${resume}::test_resume_whose_accepted_job_is_gone_by_the_lookup_is_refused_at_its_pin"
run m3 "${resume}::test_resume_retry_is_refused_at_its_pin_when_no_policy_name_resolves_its_source_id"
run full "$resume" \
    tests/fake/test_state_contract.py \
    tests/fake/test_admission_route_isolation.py \
    tests/fake/test_policy_sandbox.py \
    tests/fake/test_mcp_job_contract.py \
    tests/fake/test_launch_provider_qos.py \
    tests/fake/test_sessions_end_to_end.py \
    tests/fake/test_daemon_contract.py \
    tests/fake/test_lost_acknowledgement.py \
    tests/fake/test_canonical_identity.py \
    tests/fake/test_review_needs_shell.py \
    tests/unit/test_daemon_refusals_cli.py \
    tests/unit/test_outcome_unknown.py \
    tests/unit/test_protocol_secrecy.py \
    tests/unit/test_invariants_index.py \
    tests/unit/test_test_module_names.py \
    tests/unit/test_handler_refusals.py
TMPDIR="$outside_job" run outside-git \
    tests/fake/test_policy_sandbox.py::test_d261_a_build_job_outside_a_repository_is_refused_with_the_way_out \
    tests/unit/test_daemon_refusals_cli.py::test_cli_writable_outside_git_names_the_policy_fix
TMPDIR="$outside_job" run probe "${resume}::test_m0_legacy_resume_digest"
```

## Workspace and deviations

- Git denied creation of the assigned worktree index lock. The test, code, contract and report commits could not be created. `git log --oneline a4d21501e..HEAD` is empty. The four files are left in the detached worktree for Subfleet salvage.
- The same denial prevented `git restore --source=HEAD -- subfleet/daemon.py`. After each reversal or mutant I restored the saved HEAD content, verified `git diff --quiet -- subfleet/daemon.py`, and reapplied the saved fix when applicable. The probe also restored the original test file bytes.
- The Darwin temp root denied direct creation of the job folder, and the pre-existing uv cache was read-only. The worktree fallback used a local cache copy and a venv created by `uv sync --group dev --offline`. The outside-Git rerun and probe used the allowed temp child to keep those fixtures outside the checkout. All dependency operations were offline.
- No individual basetemp exceeded 200 MB. Aggregate scratch briefly exceeded 200 MB with the cache copy present. The job folder measured 200,948 KiB during the combined run.
- The basetemps, cache copy, venv and job scratch were removed. No job-created files or folders were left outside the worktree. The pre-existing shared uv cache was left in place.
