# Frontend state-root cleanup for PR #71

Base and final HEAD are `f2fa1a6d67d39adf07d3579a265c579467b6bbca`. No commit. Git exited 128 at each named staging step because the sandbox denied creation of the linked worktree's `index.lock` under `/Users/maxghenis/subfleet-v2/.git/worktrees/20261128-095532-fixtures-rebase-r1/`. The source and report are in the assigned worktree for salvage.

## Port

| Ruling | File and final lines | Change |
| --- | --- | --- |
| 1 | `tests/frontend/conftest.py:91` | Owns the short root. Uses `failed_keep_dir(request.node.name)` and `setup_or_call_failed(request.node)`. An exception inside the block keeps the root even before reports exist. A skip, xfail or `unittest.SkipTest` uses the reports. A keep replaces the previous copy in the same process, omits sockets and preserves the original and the test error on copy failure. |
| 2 | `tests/frontend/conftest.py:127` | Ports the PR's autouse `mkdtemp` recorder and its passed-call leak check. The report hook and comment at lines 39–46 and the probe fixtures, including `queue_tray_view_probe` at line 85, retain the base implementation. |
| 3 | `tests/frontend/test_core_protocol.py:41`, `test_core_diff.py:35`, `test_core_timeline.py:27`, `test_core_store.py:37` | Each fixture closes its service inside the root context. |
| 3 | `tests/frontend/test_core_outbox.py:27`, `test_core_client.py:234` | Close the server, then the service, before the root context exits. The client refusal test at line 185 also owns its inline home through its assertions. |
| 3 | `tests/frontend/test_core_suggestions.py:30`, `test_core_suggestions_r1b.py:39`, `test_core_notifications.py:54` | Manage the two suggestions roots and the notifications root. |
| 3 | `tests/frontend/test_core_approval_reach.py:642` | Closes the harness before the assertions. The outer state-root context encloses all of the assertions. |
| 4 | `tests/frontend/test_core_steer.py:56,66,107`, `test_core_steer_properties.py:172,282` | Substitute `state_root` for the existing root contexts while preserving teardown order and the fixture comments. Extend the #265 cases to check retained evidence and pass the property test's request into its inner test. |
| 5 | `tests/frontend/daemon_harness.py:78,384,396`, `tests/frontend/test_core_client.py:91` | `ServiceHarness.close`, `ServiceServer` and `short_dir` retain the base implementation. The shared keep helper, top-level/e2e/fake conftests, Swift compilation, CI, `app/` and `subfleet/` retain the base implementation. |
| 6 | `tests/unit/test_failed_keep_layout.py:26` | Adds frontend conftest to discovery's required harnesses. |
| 7 | `docs/lanes/README.md:23,25` | Documents fixture and inline cleanup, PID-scoped copies, notifications and the leak check. |
| 8 | Base census below | The remaining paths are accounted for before the passed-call leak check. |

| PR commit | Port |
| --- | --- |
| `74834d0cf` | Root context, leak check, six service fixtures, client inline root, scratch tests and lane docs. The base keep helper supplies the path. |
| `873553ba8` | Skip and xfail handling with scratch cases. |
| `2a4558f11` | `unittest.SkipTest` and skip-after-failed-call coverage. Both setup and call reports inform retention. |
| `5709b1c49` | The merge brought the older base forward. This port preserves the current report hook and appends the support code. |
| `b36d51e23` | Suggestions, steer, steer properties and approval reach. The steer code retains the current teardown shape. |
| `a85d2ced8` | Keeps C-27.5 assertions inside the outer root context, after closing the harness. |

The PID collision regression is `test_two_pytest_processes_keep_their_own_evidence` (`test_state_roots.py:320`). It runs the same failing test in two processes and checks both PID directories and their distinct contents. The dependent setup regression is `test_a_failed_dependent_fixture_setup_keeps_the_root` (line 305). It checks the failed setup report, the absence of a call report and the earlier fixture's evidence. `test_a_copy_failure_preserves_the_test_error_and_the_root` (line 344) checks the original assertion, stderr and the source root, then removes the synthetic root it made.

## Base census

`git grep -n -F -e mkdtemp -e TemporaryDirectory -e 'ServiceServer(' -- tests/frontend` returned 67 lines. Paths in this table are under `tests/frontend/`. Line numbers are from the base and include the recorder and comment hits.

| File | Matching lines | Lifetime |
| --- | --- | --- |
| `daemon_harness.py` | 384 | `ServiceServer.close` removes its socket and directory at 396–405. A constructor bind/listen error can leave the directory before the caller can close it. That cannot be a passed call and the brief preserves this code. |
| `test_core_approval_reach.py` | 55, 644 | Converted the inline service root. The probe-input context at 55 already exits. |
| `test_core_client.py` | 93, 186, 239–240 | Converted the inline home and watch fixture. `short_dir` already removes its directory. |
| `test_core_diff.py` | 36 | Converted the changes fixture. |
| `test_core_live.py` | 48 | Fixture closes at 76–79. Success removes the original at 86–90, and the existing failure keeper runs at 93–115. |
| `test_core_live_failure.py` | 18 | The temporary context exits at 22 before the assertions. |
| `test_core_notifications.py` | 43, 54, 242 | Converted the service fixture. The two probe-input contexts already exit. |
| `test_core_outbox.py` | 28–29 | Converted the daemon fixture. |
| `test_core_protocol.py` | 43 | Converted the protocol fixture. |
| `test_core_queue_engine.py` | 103, 391, 612, 624, 626, 635, 764, 850 | Temporary contexts exit on teardown. The daemon fixture closes server and harness at 630–631 within its context. |
| `test_core_queue_tray.py` | 93, 111, 121, 332, 497, 1578, 1604 | Contexts exit by teardown. The contexts at 332 and 497 use explicit `finally` cleanup at 339 and 508. |
| `test_core_steer.py` | 56, 66, 69, 79, 110, 112, 117 | Converted the fixture contexts. The socket cleanup order stays in place. The last four hits are comments or recorder code. |
| `test_core_steer_properties.py` | 134, 173, 176, 286, 288, 298 | Converted the inline service root. The other context already exits, and the last three hits are recorder code. |
| `test_core_store.py` | 38 | Converted the store fixture. |
| `test_core_suggestions.py` | 33 | Converted the suggestions fixture. |
| `test_core_suggestions_r1b.py` | 41 | Converted the R1b fixture. |
| `test_core_timeline.py` | 29 | Converted the timeline fixture. |
| `test_pr124_recovery_properties.py` | 45, 48 | `/private/tmp` context exits after the server, thread and world close at 117–120. |
| `test_queue_tray_view.py` | 374, 380 | `/tmp` context exits after closing server and harness at 423–424. |
| `test_review_r219_fix4.py` | 51, 101, 199, 222 | History context exits. Servers close in `finally` at 132, 215 and 244. Their state uses `tmp_path`. |
| `test_review_r219_probes.py` | 28, 151, 193, 240, 242, 278, 280, 289, 373, 403, 430 | Temporary contexts exit, and servers close at 266, 284 and 378. The server at 373 uses `tmp_path` for state. |

## Regression evidence

Warm passed both probe cases. M0 passed all 230 base cases before the test edits. M1 passed all 246 frontend cases with the full port. The end-of-job unit run passed all 39. Those four runs had zero failures, errors or skips.

The tests were written before the implementation. `red-layout` ran the extended layout test at the base. `red1` ran the new frontend module and both extended #265 cases with base implementations. `red2` ran the new module after the conftest edit and before the fixture edits. Git denied staging, so each proposed commit boundary was verified in the working tree.

| Case | Base | Conftest only | Full port |
| --- | --- | --- | --- |
| Protocol, diff, timeline, store, outbox, client watch and notifications fixture cases | Missing `no_leftover_temporary_directories` in scratch conftest | Seven behavior failures. Each passed inner call left its original root and received a teardown error. The failed inner calls also left their originals and had no keeper. | 7 passed |
| Both steer fixtures and both suggestions fixture cases | Same missing import | Four behavior failures. Originals were removed and the expected evidence files were absent. | 4 passed |
| Inline/raising fixture lifecycle, pass-only leak check, dependent setup failure, PID isolation and copy failure | Same missing import | 5 passed | 5 passed |
| `test_the_steer_fixtures_leave_no_root_after_teardown` | Behavior. Original roots were removed and the expected kept `work` directory was absent. | Outside `red2` | Passed |
| `test_the_property_harness_leaves_no_root_after_each_example` | Signature. The inner test rejected `request`. | Outside `red2` | Passed |
| `test_every_harness_that_keeps_roots_asks_the_helper_for_the_path` | Behavior. Discovery lacked `tests/frontend/conftest.py`. | Outside `red2` | Passed |

`red-layout` had 1 expected failure and 4 passes. `red1` had 18 expected failures. `red2` had 11 expected failures and 5 passes. Each count is for the outer pytest run. Deliberately failing or skipping inner tests are assertions' inputs.

### Mutations

Each mutation was applied in isolation. The frontend run selected `test_state_roots.py` and both #265 tests. The separate unit run selected `test_failed_keep_layout.py`. All six mutants were caught.

| Mutation | Frontend pass / fail | Detecting test and failure | Layout pass / fail |
| --- | ---: | --- | ---: |
| 1. Keep without PID | 2 / 16 | `test_two_pytest_processes_keep_their_own_evidence` found neither expected PID folder. The other retention checks also missed their expected copies. | 4 / 1. `test_every_harness_that_keeps_roots_asks_the_helper_for_the_path`. |
| 2. Call report only | 17 / 1 | `test_a_failed_dependent_fixture_setup_keeps_the_root` found the original gone and evidence absent. | 4 / 1. `test_every_harness_that_keeps_roots_keeps_a_failed_setup`. |
| 3. Copy outside the handler | 17 / 1 | `test_a_copy_failure_preserves_the_test_error_and_the_root` received `NotADirectoryError` in place of the inner assertion. | 5 / 0 |
| 4. Leak checker records nothing | 17 / 1 | `test_a_passing_test_that_leaves_a_directory_behind_errors` saw the leaking case finish without a teardown error. | 5 / 0 |
| 5. Remove failed root without a copy | 1 / 17 | `test_a_fixture_removes_its_root_after_a_pass_and_keeps_it_after_a_failure` found missing evidence in all 11 cases. The other keep checks also failed. | 4 / 1. `test_every_harness_that_keeps_roots_asks_the_helper_for_the_path` lost the known keeper. |
| 6. Revert the steer `harness` fixture to `TemporaryDirectory` | 16 / 2 | The steer-harness fixture case and `test_the_steer_fixtures_leave_no_root_after_teardown` found missing evidence. | 5 / 0 |

### End-of-job counts

| Run | Module | Pass | Fail | Error | Skip |
| --- | --- | ---: | ---: | ---: | ---: |
| m1 | `test_core_approval_reach` | 41 | 0 | 0 | 0 |
| m1 | `test_core_client` | 18 | 0 | 0 | 0 |
| m1 | `test_core_diff` | 7 | 0 | 0 | 0 |
| m1 | `test_core_notifications` | 46 | 0 | 0 | 0 |
| m1 | `test_core_outbox` | 13 | 0 | 0 | 0 |
| m1 | `test_core_protocol` | 10 | 0 | 0 | 0 |
| m1 | `test_core_steer` | 44 | 0 | 0 | 0 |
| m1 | `test_core_steer_properties` | 3 | 0 | 0 | 0 |
| m1 | `test_core_store` | 14 | 0 | 0 | 0 |
| m1 | `test_core_suggestions` | 7 | 0 | 0 | 0 |
| m1 | `test_core_suggestions_r1b` | 4 | 0 | 0 | 0 |
| m1 | `test_core_timeline` | 23 | 0 | 0 | 0 |
| m1 | `test_state_roots` | 16 | 0 | 0 | 0 |
| unit-end | `test_failed_keep` | 2 | 0 | 0 | 0 |
| unit-end | `test_failed_keep_layout` | 5 | 0 | 0 | 0 |
| unit-end | `test_failed_keep_prune` | 25 | 0 | 0 | 0 |
| unit-end | `test_failed_keep_teardown` | 6 | 0 | 0 | 0 |
| unit-end | `test_test_module_names` | 1 | 0 | 0 | 0 |

## Root measurements

The before and after columns count name-only listings of `/private/tmp`, using the longest listed prefix. The left column counts existing paths recorded under that run's own label. M0's four worker logs had 10, 95, 70 and 13 paths. M1's four worker logs had 72, 25, 102 and 27 paths. The other listed increases were `x5d` during M0 and `us10764rules.oLb9oVrY` during M1. The recorder did not attribute either to these runs.

| Prefix | M0 before | M0 after | M0 Δ | M0 left | M1 before | M1 after | M1 Δ | M1 left |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `sf-app-h-` | 4 | 8 | 4 | 4 | 10 | 10 | 0 | 0 |
| `sf-app-d-` | 0 | 5 | 5 | 5 | 7 | 7 | 0 | 0 |
| `sf-app-sg-r1b-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-app-sg-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-app-x-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-app-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-tl-` | 0 | 17 | 17 | 17 | 19 | 19 | 0 | 0 |
| `sf-st-` | 0 | 11 | 11 | 11 | 13 | 13 | 0 | 0 |
| `sf-ob-` | 25 | 37 | 12 | 12 | 39 | 39 | 0 | 0 |
| `sf-wl-` | 2 | 3 | 1 | 1 | 5 | 5 | 0 | 0 |
| `sf-h-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-cl-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-steer-ob-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-steer-o-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-steer-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-reach-` | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 0 |
| `sf-nt-` | 0 | 1 | 1 | 1 | 3 | 3 | 0 | 0 |
| `sf-left-` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `sf-failed` | 1 | 1 | 0 | 0 | 1 | 1 | 0 | 0 |
| `other` | 368 | 369 | 1 | 0 | 368 | 369 | 1 | 0 |

M0 left 52 of 188 recorded paths. M1 left zero of 226. All recorded names matched a listed prefix.

The scratch plugin routed frontend kept copies and session-start pruning to `$R/kept`. Scratch regression tests set their own keep folder under `tmp_path`. State roots themselves stayed under `/tmp` for AF_UNIX. The two pytester subprocesses collected generated scratch files and used their own conftest to direct the copies. The recorder ran in the outer frontend session and each xdist worker. Unit runs used the base keep path and did not load the plugin.

```python
import os
from pathlib import Path
import tempfile

import pytest

from tests import failed_keep


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    label = os.environ.get("SF_FIXTURES1_RUN")
    if not label:
        raise pytest.UsageError("SF_FIXTURES1_RUN is required")
    scratch = Path(os.environ["R"])
    failed_keep.ROOT = scratch / "kept"
    logs = scratch / "made" / label
    logs.mkdir(parents=True, exist_ok=True)
    mkdtemp = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = mkdtemp(*args, **kwargs)
        if Path(os.fsdecode(path)).parent in (Path("/tmp"), Path("/private/tmp")):
            with (logs / f"{os.getpid()}.txt").open("a", encoding="utf-8") as log:
                print(os.fsdecode(path), file=log)
        return path

    tempfile.mkdtemp = recording
```

## Commands

The first command recorded `2026-11-28T14:55:42Z`, checked the full base ID, found no tracked changes and found that `build/frontend-probes` was absent. The lane venv imported `subfleet` from the job worktree, with pytest 9.1.1, Hypothesis 6.168.1, xdist 3.8.0 and pytest-timeout 2.4.0. `xcrun swiftc --version` succeeded with Apple Swift 6.4.2. The warm cache binaries were 5,611,624 and 5,706,040 bytes, so four copies of both fit the 150 MB check.

The following shell function expands the commands used, including the exact basetemps, arguments and module sets. The run wrapper recorded UTC time, uptime and disk availability before invoking pytest, then recorded sizes and guarded basetemp removal after pytest.

```zsh
export R="$PWD/build/sf-fixtures1"
export TMPDIR="$R/runtime" HYPOTHESIS_STORAGE_DIRECTORY="$R/hypothesis" PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT=/Users/maxghenis/subfleet-v2-lanes/port-71/.venv PYTHONPATH="$PWD"
export CLANG_MODULE_CACHE_PATH="$PWD/build/sf-fixtures1-modules" SWIFT_MODULECACHE_PATH="$PWD/build/sf-fixtures1-modules"
export SUBFLEET_SWIFT_NESTED_SANDBOX=off

pytest_job() (
    local suite=$1 id=$2 label=$3 workers=$4 timeout=$5
    shift 5
    local -a options
    if [[ $suite == frontend ]]; then
        export SF_FIXTURES1_RUN=$label PYTHONPATH="$R/plugin:$PWD"
        options+=(-p sf_fixtures1_plugin)
    fi
    if [[ $workers != 0 ]]; then
        options+=(-n "$workers")
    fi
    uv --no-cache run --no-sync python -m pytest -p no:cacheprovider "${options[@]}" \
        --timeout="$timeout" --basetemp "$TMPDIR/pytest-$id-$label" -q -ra \
        -m 'not real_lsof and not real_cwd' --junitxml "$R/runs/$label.xml" "$@"
)
base=(tests/frontend/test_core_{protocol,diff,timeline,store,outbox,client,steer,steer_properties,suggestions,suggestions_r1b,approval_reach,notifications}.py)
roots=tests/frontend/test_state_roots.py
steer=(
    tests/frontend/test_core_steer.py::test_the_steer_fixtures_leave_no_root_after_teardown
    tests/frontend/test_core_steer_properties.py::test_the_property_harness_leaves_no_root_after_each_example
)
layout=tests/unit/test_failed_keep_layout.py
unit=(tests/unit/test_failed_keep{,_layout,_prune,_teardown}.py tests/unit/test_test_module_names.py)

pytest_job frontend 01 warm 0 900 \
    tests/frontend/test_core_outbox.py::test_c28_3_sends_are_journaled_and_chained_in_order \
    tests/frontend/test_core_notifications.py::test_c29_9_a_click_for_the_focused_conversation_reasserts_it
pytest_job frontend 02 m0 4 540 "${base[@]}"
pytest_job unit 03 red-layout 4 540 "$layout"
pytest_job frontend 04 red1 4 540 "$roots" "${steer[@]}"
pytest_job frontend 05 red2 4 540 "$roots"
pytest_job frontend 06 m1 4 540 "$roots" "${base[@]}"
pytest_job unit 07 unit-end 4 540 "${unit[@]}"
pytest_job frontend 08 mut1 4 540 "$roots" "${steer[@]}"
pytest_job unit 09 mut1-layout 4 540 "$layout"
pytest_job frontend 10 mut2 4 540 "$roots" "${steer[@]}"
pytest_job unit 11 mut2-layout 4 540 "$layout"
pytest_job frontend 12 mut3 4 540 "$roots" "${steer[@]}"
pytest_job unit 13 mut3-layout 4 540 "$layout"
pytest_job frontend 14 mut4 4 540 "$roots" "${steer[@]}"
pytest_job unit 15 mut4-layout 4 540 "$layout"
pytest_job frontend 16 mut5 4 540 "$roots" "${steer[@]}"
pytest_job unit 17 mut5-layout 4 540 "$layout"
pytest_job frontend 18 mut6 4 540 "$roots" "${steer[@]}"
pytest_job unit 19 mut6-layout 4 540 "$layout"
```

## Footprint and cleanup

UTC times are for 2026-11-28. The three footprint columns and the basetemp size come from `du -sk` after pytest and before cleanup. The peak measured combined scratch and cache size was 122,660 KiB. Available disk stayed above the stop threshold, and the write-stop marker was absent. Unit sessions emitted no `pruned` lines. The green runs had no timeout or unexpected failure to retry.

| Run | Start UTC | Load averages | Available KiB | R KiB | Module cache KiB | Probe cache KiB | Basetemp KiB |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| warm | 14:56:49Z | 8.75 6.90 5.12 | 72000000 | 11132 | 67588 | 11060 | 11076 |
| m0 | 14:57:32Z | 9.14 7.27 5.34 | 71600000 | 39680 | 67588 | 11060 | 39124 |
| red-layout | 14:58:31Z | 11.68 8.31 5.86 | 71200000 | 584 | 67588 | 11060 | 0 |
| red1 | 14:59:06Z | 12.10 8.82 6.15 | 71200000 | 864 | 67588 | 11060 | 144 |
| red2 | 15:00:03Z | 13.72 10.01 6.77 | 71200000 | 1184 | 67588 | 11060 | 352 |
| m1 | 15:02:29Z | 16.91 14.11 9.04 | 71200000 | 44012 | 67588 | 11060 | 43088 |
| unit-end | 15:04:57Z | 41.65 27.48 15.35 | 70800000 | 2624 | 67588 | 11060 | 1688 |
| mut1 | 15:05:30Z | 31.72 26.68 15.56 | 70800000 | 5204 | 67588 | 11060 | 3964 |
| mut1-layout | 15:05:46Z | 27.94 26.07 15.53 | 70800000 | 1252 | 67588 | 11060 | 0 |
| mut2 | 15:06:51Z | 22.37 24.99 15.91 | 70800000 | 5284 | 67588 | 11060 | 3964 |
| mut2-layout | 15:07:14Z | 24.54 25.32 16.24 | 70800000 | 1332 | 67588 | 11060 | 0 |
| mut3 | 15:08:22Z | 29.50 26.51 17.39 | 70800000 | 5368 | 67588 | 11060 | 3968 |
| mut3-layout | 15:09:01Z | 33.15 27.80 18.28 | 70800000 | 1416 | 67588 | 11060 | 0 |
| mut4 | 15:10:04Z | 23.93 26.54 18.48 | 70800000 | 5440 | 67588 | 11060 | 3968 |
| mut4-layout | 15:10:49Z | 17.31 24.40 18.11 | 70400000 | 1484 | 67588 | 11060 | 0 |
| mut5 | 15:11:53Z | 13.66 21.76 17.56 | 70400000 | 2180 | 67588 | 11060 | 336 |
| mut5-layout | 15:12:23Z | 20.16 22.51 17.98 | 70400000 | 1856 | 67588 | 11060 | 0 |
| mut6 | 15:13:25Z | 28.26 24.41 19.02 | 70400000 | 5300 | 67588 | 11060 | 3476 |
| mut6-layout | 15:14:08Z | 36.08 26.83 20.15 | 70400000 | 1840 | 67588 | 11060 | 0 |

The guarded cleanup removed each of the 19 `$TMPDIR/pytest-<id>-<label>` basetemps after its run. End cleanup covered `build/sf-fixtures1` with its `runtime`, `plugin`, `runs`, `original`, `hypothesis` and `made/<frontend label>` directories, `build/sf-fixtures1-modules` (67,588 KiB), and the job-created `build/frontend-probes` (11,060 KiB). The measured cases and commands above identify each basetemp. Scratch pytest homes and uv temporary directories were under `$R/runtime`.

The union of all frontend recorder labels had 67 surviving roots, totaling 18,116 KiB. M0 left 52, the conftest-only red run left 14, and warm left one. Every listed path was left in place for the hub. The name-only scanner also observed the two unattributed names listed above and `zeb_def_ipc_24404` during mutant 4. None was in the run recorder. No additional outside directory was attributable to the job.

| Root left in place | Run | KiB |
| --- | --- | ---: |
| `/tmp/sf-app-d-3ha4z27a` | m0 | 496 |
| `/tmp/sf-app-d-iappodir` | m0 | 428 |
| `/tmp/sf-app-d-lcvizw4x` | m0 | 400 |
| `/tmp/sf-app-d-tpm71_jy` | m0 | 420 |
| `/tmp/sf-app-d-ytavfj2q` | m0 | 428 |
| `/tmp/sf-app-h-bqf5xsno` | m0 | 248 |
| `/tmp/sf-app-h-es2cpw99` | m0 | 244 |
| `/tmp/sf-app-h-p7493l76` | m0 | 248 |
| `/tmp/sf-app-h-zy9u0oe5` | m0 | 336 |
| `/tmp/sf-nt-nodh6f33` | m0 | 252 |
| `/tmp/sf-ob-4mko_ib2` | m0 | 256 |
| `/tmp/sf-ob-6tzhl0xx` | m0 | 256 |
| `/tmp/sf-ob-7kp_dbgj` | m0 | 260 |
| `/tmp/sf-ob-88x9ra1e` | m0 | 252 |
| `/tmp/sf-ob-g1av6kbb` | m0 | 252 |
| `/tmp/sf-ob-hk1k93tl` | m0 | 244 |
| `/tmp/sf-ob-kduii6bd` | m0 | 252 |
| `/tmp/sf-ob-p1qw6aij` | m0 | 256 |
| `/tmp/sf-ob-qj8h4sgl` | m0 | 252 |
| `/tmp/sf-ob-x4q6px_k` | m0 | 248 |
| `/tmp/sf-ob-yf8w8v2_` | m0 | 252 |
| `/tmp/sf-ob-yzyhn5vs` | m0 | 256 |
| `/tmp/sf-reach-u9oxwzd_` | m0 | 260 |
| `/tmp/sf-st-1d4a2hw_` | m0 | 256 |
| `/tmp/sf-st-2kxvjree` | m0 | 256 |
| `/tmp/sf-st-5grfe8g8` | m0 | 248 |
| `/tmp/sf-st-5v0u9mjn` | m0 | 252 |
| `/tmp/sf-st-chc0_6n0` | m0 | 244 |
| `/tmp/sf-st-dn1u603z` | m0 | 248 |
| `/tmp/sf-st-eqticiuq` | m0 | 252 |
| `/tmp/sf-st-fl8jh361` | m0 | 248 |
| `/tmp/sf-st-fqy_z2lq` | m0 | 248 |
| `/tmp/sf-st-nocea497` | m0 | 248 |
| `/tmp/sf-st-zs8cvkpc` | m0 | 252 |
| `/tmp/sf-tl-32d1safk` | m0 | 248 |
| `/tmp/sf-tl-4y9jh8g5` | m0 | 248 |
| `/tmp/sf-tl-55w9f3ka` | m0 | 252 |
| `/tmp/sf-tl-7ksan3t7` | m0 | 252 |
| `/tmp/sf-tl-c952_pgc` | m0 | 248 |
| `/tmp/sf-tl-dg7p574p` | m0 | 252 |
| `/tmp/sf-tl-e4hi9ds4` | m0 | 252 |
| `/tmp/sf-tl-fmzxs04r` | m0 | 248 |
| `/tmp/sf-tl-jofmwvs3` | m0 | 252 |
| `/tmp/sf-tl-kttrjike` | m0 | 252 |
| `/tmp/sf-tl-l34j08ie` | m0 | 252 |
| `/tmp/sf-tl-lmiw1p_d` | m0 | 248 |
| `/tmp/sf-tl-lmy8njkm` | m0 | 252 |
| `/tmp/sf-tl-r2qv7to_` | m0 | 248 |
| `/tmp/sf-tl-va3bvj35` | m0 | 244 |
| `/tmp/sf-tl-wh2e0iws` | m0 | 252 |
| `/tmp/sf-tl-zmw3ee5c` | m0 | 256 |
| `/tmp/sf-wl-87nuq_a1` | m0 | 244 |
| `/tmp/sf-app-d-hwpfn062` | red2 | 452 |
| `/tmp/sf-app-d-yez2bye3` | red2 | 408 |
| `/tmp/sf-app-h-67kxqvx7` | red2 | 248 |
| `/tmp/sf-app-h-w5354ok_` | red2 | 244 |
| `/tmp/sf-nt-24xo12m5` | red2 | 244 |
| `/tmp/sf-nt-ch6my468` | red2 | 248 |
| `/tmp/sf-ob-ff03drmk` | red2 | 248 |
| `/tmp/sf-ob-glio345b` | red2 | 244 |
| `/tmp/sf-st-8zyj0apx` | red2 | 248 |
| `/tmp/sf-st-xbvm4jfw` | red2 | 244 |
| `/tmp/sf-tl-2n63cpyk` | red2 | 248 |
| `/tmp/sf-tl-ip5zkq86` | red2 | 244 |
| `/tmp/sf-wl-tv5xkwja` | red2 | 244 |
| `/tmp/sf-wl-vyzwr087` | red2 | 248 |
| `/tmp/sf-ob-vzlug5f0` | warm | 256 |

67 recorded paths remain.

## Deviations and open items

- The linked worktree index lock prevented all five named staging steps. The report and source changes are available through salvage.
- Through `red2`, the wrapper checked the write-stop path using `Path.exists()`. Later runs used `test -e` on the same path.
- The existing `ServiceServer` constructor's bind/listen failure path is outside the passed-call leak check. The census identifies the edge and the brief preserves that code.

`git status --porcelain` after restoring all mutants and writing this report

```text
 M docs/lanes/README.md
 M tests/frontend/conftest.py
 M tests/frontend/test_core_approval_reach.py
 M tests/frontend/test_core_client.py
 M tests/frontend/test_core_diff.py
 M tests/frontend/test_core_notifications.py
 M tests/frontend/test_core_outbox.py
 M tests/frontend/test_core_protocol.py
 M tests/frontend/test_core_steer.py
 M tests/frontend/test_core_steer_properties.py
 M tests/frontend/test_core_store.py
 M tests/frontend/test_core_suggestions.py
 M tests/frontend/test_core_suggestions_r1b.py
 M tests/frontend/test_core_timeline.py
 M tests/unit/test_failed_keep_layout.py
?? docs/reports/2026-11-28-frontend-state-roots-rebase.md
?? tests/frontend/test_state_roots.py
```
