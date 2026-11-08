# Synthetic admission measurement

All fixture paths are synthetic. Four Codex lanes have account-scoped operator holds until 24 hours after setup. There are 100 waiting jobs, alternating fresh writable jobs (the unprepared path) and read-only jobs (the real workspace / route / `attempt.reserved` path). There are at least 1,000,000 padding events, using the real schema and indexes. The process hosts a real `Daemon`, real control loop and real worker scheduler. Timer probes, conversation ticks and retention are suppressed to prevent unrelated work or artificial admission wakes. No provider process runs.

Warm-up uses real admission passes and then sets the standing verdict count to 20. The next real pass computes the actual steady ceiling, and the jobs are staggered uniformly over that ceiling. Setup, warm-up and profiling are excluded from the timed interval. CPU comes from `time.process_time()` and `resource.getrusage(RUSAGE_SELF)` inside the daemon process; child Git CPU is not included. `os.lstat` is wrapped and counted only while `_admit_pass(kind="detached")` runs. Profiles additionally record Python caller chains.

## Before and final after

The final comparison uses a frozen source snapshot containing every final production fix, including closure epochs, within-pass expiry handling, malformed-expiry guarding and retained news after failed passes. SHA-256 hashes of all four changed production files matched the worktree both before and after the run; see `source-fingerprints.json`. The required final pytest run was concurrent on the shared development host. These are synthetic measurements, not live-host sizing.

| Metric | Before a8624a489 | Final after |
| --- | ---: | ---: |
| Wall seconds | 120.069263 | 120.088752 |
| Process CPU seconds | 10.138547 | 1.275699 |
| CPU seconds / minute | 5.066349 | 0.637378 |
| Detached passes | 949.000000 | 56.000000 |
| Detached passes / minute | 474.226279 | 27.979306 |
| Events committed | 401.000000 | 20.000000 |
| Events / hour | 12023.060352 | 599.556568 |
| os.lstat calls / pass | 3.683878 | 14.500000 |
| Total os.lstat calls | 3496.000000 | 812.000000 |

The before interval committed 200 `attempt.reserved` plus 201 `job.unprepared_wait` events. The final after interval used a 600-second steady ceiling and committed 10 events of each kind. All 100 jobs remained capacity-waiting and no attempt existed at the end. On this fixture, CPU seconds per minute fell 87.4%, detached passes per minute 94.1%, and event traffic 95.0%. These percentages describe this shared host and synthetic workload; they do not predict the live daemon’s CPU.

`os.lstat` calls per pass increased because `REGISTRY_READ_TTL_S = 2`: frequent passes reused the session-registry cache, while resting passes usually read it each time. Total calls nevertheless fell from 3,496 to 812 over the same two minutes. Counts match 14 filesystem-spelling calls per due read-only job plus 12 path-validation calls per registry refresh: before `200 × 14 + 58 × 12 = 3496`; after `10 × 14 + 56 × 12 = 812`.

## One due pass, top 15 functions by cumulative time

| Function | Calls | Cumulative seconds |
| --- | ---: | ---: |
| `subfleet/daemon.py:7268(_admit)` | 1 | 6.121699 |
| `subfleet/daemon.py:7287(_admit_kind)` | 2 | 6.121693 |
| `tools/benchmark_idle_admission.py:145(counted_pass)` | 1 | 6.121277 |
| `subfleet/daemon.py:7561(_admit_pass)` | 1 | 6.121011 |
| `subfleet/daemon.py:6306(_workspace)` | 50 | 4.256525 |
| `subfleet/salvage.py:200(_git)` | 100 | 4.255065 |
| `stdlib/subprocess.py:627(run)` | 100 | 4.137713 |
| `stdlib/subprocess.py:991(__init__)` | 100 | 2.276223 |
| `stdlib/subprocess.py:1992(_execute_child)` | 100 | 2.232101 |
| `subfleet/salvage.py:256(git_head)` | 50 | 2.198696 |
| `subfleet/salvage.py:286(git_tree)` | 50 | 2.056791 |
| `stdlib/subprocess.py:1351(communicate)` | 100 | 1.845544 |
| `stdlib/subprocess.py:2367(_communicate)` | 100 | 1.830426 |
| `stdlib/subprocess.py:253(_communicate_io_posix)` | 100 | 1.696073 |
| `stdlib/selectors.py:385(select)` | 200 | 1.678262 |

The detached pass itself took 6.121267 seconds under cProfile and the caller-counting wrapper. It issued 736 `os.lstat` calls: 500 from `posixpath.realpath` via `folders._spelled` (250 via `_detached_folders`/`folders.present`, 250 via admission’s `folders.canonical`), 200 from `folders._kernel_path` checking that the kernel spelling names the same inode (100 on each path), and 36 from `home_paths` validating the synthetic Claude session-registry path (18 component checks and 18 account-home comparisons). These are the callers in this fixture; this does not attribute every native `lstat` seen in the live daemon sample.

The all-not-due 100-job pass took 0.008428 seconds and issued zero `os.lstat` calls. It still called `_retry_pin` and `list_attempts` once per waiting job. Its top cumulative functions are preserved in `before-100.json` and `before-100.not_due.prof`.

## Waiting-job scaling

| Waiting jobs | Due pass seconds | Due lstat | Not-due pass seconds | Not-due lstat | Not-due list_attempts calls |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 25 | 0.592083 | 168 | 0.007452 | 12 | 25 |
| 100 | 6.121267 | 736 | 0.008428 | 0 | 100 |
| 200 | 3.241415 | 1424 | 0.032810 | 0 | 200 |

Per-pass work grows with the number of waiting jobs, including between looks: the queue walk and attempt-history read run once per job. Due read-only jobs additionally run `_workspace`, two Git commands, route evaluation and the wait-clock transaction. Times include profiling overhead and a shared development host, so they are illustrative rather than an isolated scaling benchmark. Counts show the dependency directly. Inexpensive session-registry caching can make not-due lstat counts differ between adjacent profiles.

## Commands and results

- `git archive a8624a4899a2 -o /private/tmp/idle-admission-build/baseline-source.tar` and `tar -xf ... -C /private/tmp/idle-admission-build/baseline-source` created a committed-source baseline without altering any checkout.
- `.venv/bin/python tools/benchmark_idle_admission.py --source /private/tmp/idle-admission-build/baseline-source --state-root /private/tmp/idle-admission-build/bench-before-100 --output docs/reports/2026-11-07-idle-admission-data/before-100.json --seconds 120 --jobs 100 --events 1000000 --profile --label before-a8624a489` exited 0.
- The same command with `--seconds 0 --jobs 25` and `--seconds 0 --jobs 200`, unique state/output paths and scaling labels produced two profile-only comparisons, both exit 0.
- One smoke invocation failed during fixture setup because the default policy has no explicit ramp key; the harness was corrected. The subsequent 10-job / 1,000-event / one-second smoke run exited 0.

Raw JSON includes exact counters and per-profile cumulative records; `.prof` files can be opened with `pstats`. All measured intervals are synthetic and are not sized to the live host.

## Final after profile

The final after all-due profile recorded 7.741647 seconds and 748 `os.lstat` calls. The all-not-due profile recorded 0.064972 seconds and 0 `os.lstat` calls. These single-pass wall times include profiling and shared-host scheduling noise; cumulative profiles and complete caller counts remain in `after-frozen-100.json` and its `.prof` files.

| Function | Calls | Cumulative seconds |
| --- | ---: | ---: |
| `subfleet/daemon.py:7295(_admit)` | 1 | 7.742264 |
| `subfleet/daemon.py:7339(_admit_kind)` | 2 | 7.742258 |
| `subfleet/daemon.py:7377(_admit_kind_once)` | 2 | 7.742127 |
| `tools/benchmark_idle_admission.py:146(counted_pass)` | 1 | 7.741658 |
| `subfleet/daemon.py:7686(_admit_pass)` | 1 | 7.741401 |
| `subfleet/daemon.py:6333(_workspace)` | 50 | 5.591270 |
| `subfleet/salvage.py:200(_git)` | 100 | 5.589658 |
| `stdlib/subprocess.py:627(run)` | 100 | 5.463157 |
| `subfleet/salvage.py:286(git_tree)` | 50 | 2.906131 |
| `stdlib/subprocess.py:991(__init__)` | 100 | 2.759111 |
| `stdlib/subprocess.py:1351(communicate)` | 100 | 2.684377 |
| `subfleet/salvage.py:256(git_head)` | 50 | 2.684054 |
| `stdlib/subprocess.py:2367(_communicate)` | 100 | 2.676547 |
| `stdlib/subprocess.py:1992(_execute_child)` | 100 | 2.664727 |
| `stdlib/subprocess.py:253(_communicate_io_posix)` | 100 | 2.455542 |

Final after command (exit 0): `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=/private/tmp/idle-admission-build/uv-cache .venv/bin/python tools/benchmark_idle_admission.py --source /private/tmp/idle-admission-build/final-source --state-root /private/tmp/idle-admission-build/bench-after-frozen-100 --output docs/reports/2026-11-07-idle-admission-data/after-frozen-100.json --seconds 120 --jobs 100 --events 1000000 --profile --label after-frozen-final-both-changes`.

The frozen source was extracted from the same baseline archive and overlaid with the four changed production files. Earlier successful 120-second runs are retained as `after-100.json` and `after-final-100.json`, with their profiles. A final failed-pass news-retention fix changed the source during the latter run, so both were superseded by the frozen-source result above. No mutant or live state was used. Harness compilation passed.
