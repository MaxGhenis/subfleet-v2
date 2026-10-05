# Quarantine self-resolution — 2026-10-05

Base: `08d6a09a0d566d2b94e14a109259f23001d95576` (2.1.11 integration). Work and verification are confined to the assigned worktree and task-owned temporary directories. No live Subfleet store, daemon, installed app, caller checkout, or Application Support files were changed. No subagents, history rewrites, or pushes were used. Verification commands run sequentially in the foreground; test-owned child processes are cleaned up by their fixtures and every slice is awaited.

The sandbox denied the shared worktree's Git index lock. Commits therefore use workspace-local `.git-local`, on `feat/quarantine-self-resolve`. The final delivery bundle will contain only commits after the base and name that branch's head. Neither `.git-local` nor verification artifacts are committed.

| Change | Location |
|---|---|
| Offer one worker pass per second; claim durable per-attempt due times before census; limit each pass to eight censuses and eight paced notification retries | `subfleet/daemon.py:5915` |
| Reuse the operator resolver, serialize stale/concurrent resolutions, save writable dispatch salvage or a turn's end snapshot before deleting every attempt/job lease, record `quarantine.self_resolved` without operator note or override | `subfleet/daemon.py:5950` |
| Announce saved salvage refs, salvage failure, or no new changes; deliver turn release events through an idempotent outbox | `subfleet/daemon.py:5995`, `subfleet/conversations/service.py:1145` |
| Render the labelled Subfleet release line as a timeline notice | `app/Sources/Timeline.swift:479` |
| Match recorded writers in the census snapshot; exclude absent/reused identities from group and descendant roots while retaining live recorded escapes and matching markers; any error prevents verified empty | `subfleet/procs.py:339`, `subfleet/procs.py:419` |
| Display holds older than one pace with their age and latest reason in text and structured daemon status | `subfleet/render.py:111`, `subfleet/daemon.py:1121` |
| Default `quarantine_recheck_s` to 600 in code; validate positive finite values including subsecond settings | `subfleet/policy.py:25`, `subfleet/policy.py:271` |
| Add schema 7's due time, notification outbox and partial indexes, preserving older stores and leases | `subfleet/store.py:77`, `subfleet/store_schema.sql:139` |
| Pin the released turn's job and manifest while its conversation notification is pending; zero-budget retention resumes after delivery | `subfleet/retention.py:189`, `tests/unit/test_retention_archive.py:959`, `tests/unit/test_retention_turns.py:41` |
| Rewrite C-5.7 and the state table; document the 211-attempt incident, 32-hour turn wait, enrollment fence and retention impact | `docs/acceptance-contract.md:9`, `docs/acceptance-contract.md:164`, `docs/acceptance-contract.md:179` |
| Add fake-provider integration, property, migration, policy, identity and timeline coverage; provide a foreground mutation runner that restores source after every check | `tests/fake/test_quarantine_self_resolve.py:1`, `tests/unit/test_quarantine_migration.py:1`, `tests/unit/test_policy.py:25`, `tests/unit/test_procs.py:137`, `tests/unit/test_store_migration_2.py:101`, `tests/frontend/test_core_timeline.py:51`, `tools/quarantine_mutations.py:1` |

The contract now says: “A `quarantined` attempt keeps its leases (lane slot excluded), its workspace, and its worktree until a C-5.5 census verifies its writers gone.” Automatic checks never signal processes or force release. Live or unverifiable writers retain the quarantine and are checked again on the next pace. Operator `kill`, immediate `--confirm-dead`, and explicit audited `--force-release` remain available. The complete replacement clause and recovery row are in the acceptance contract.

The fake-provider tests exercise real stores, admission, Git salvage and turn snapshots, with scripted process-table/marker inputs. They prove release within one pace, deletion of all lease types, admission of a waiting turn, retention across twenty live/unverifiable paces, PID reuse with a changed start time, restart recovery before and after the release transaction, exactly one conversation line after a crash between stores, notification failure isolation, retention of a pending notification even with zero age/size/count budgets, pruning after delivery, old-hold status, and the 300-attempt bound.

The Hypothesis property runs 100 valid generated sequences plus an explicit adversarial sequence (seed 57; 17 invalid generated cases rejected), over two recorded writers, exits, PID reuse, unavailable/restored marker census, real daemon reconstruction, ticks and operator `--confirm-dead`. On the first release, the most recent census must have verified both recorded writers gone; every released attempt has exactly one resolution event and no remaining leases. Subsequent ticks, restarts and stale operator requests cannot release it twice. Explicit force overrides are outside this invariant and retain their existing operator tests.

For 300 quarantines, a measured pass performed exactly eight censuses and used the partial index's range search. Store-side pass time was 190.441 ms with census results stubbed; 1,000 timer checks took 0.076 ms and offered exactly one worker pass. This is a bound measurement, not host `ps` latency. The added control-tick cost is O(1), with no SQL or census. A pass costs two bounded indexed queue reads, at most eight censuses (normally sixteen `ps` reads, plus cached boot checks and identity fallbacks), at most sixteen short census transactions, and at most eight paced outbox retries. Each newly resolved turn may also deliver its prepared notice; notice claims/acknowledgements are short transactions. Verified-empty attempts additionally run the existing receipt-backed salvage/end-snapshot path. At one pass per second, a 300-attempt due backlog needs 38 passes when each pass completes within a second; actual Git/process IO can extend that wall time without increasing per-pass work.

Verification used Python 3.12.14, pytest 9.1.1 and Hypothesis 6.168.1. Across 61 selected test files, 1,603 distinct test nodes were evaluated: **1,512 passed, 26 skipped, 65 baseline-matched failures** (17 assertion failures and 48 setup errors). Repeated property batches and focused reruns are excluded from these distinct-node counts.

| Suite grouping | Passed | Skipped | Baseline-matched failures/errors |
|---|---:|---:|---:|
| Daemon and tick prefilter | 139 | 22 | 62 |
| Quarantine and schema-7 migration | 12 | 0 | 0 |
| Containment and guardian process | 55 | 4 | 0 |
| Salvage | 187 | 0 | 0 |
| Retention | 230 | 0 | 0 |
| Conversations, including 19 native Swift timeline tests | 607 | 0 | 2 |
| Policy, rendering, older migration, fake state/workspace/resume and other support | 282 | 0 | 1 |

The broad run used 111 foreground slices; the longest was **440.961 s (7 min 21 s)**. Ordinary tests were batched at 40 nodes, retention at 20, and properties separately. The existing 200-example hard-link property used four 50-example batches (seeds 57–60); the 40-example sweep property used two 20-example batches (seeds 57–58), preserving explicit examples. No slice reached its nine-minute deadline. All runner-owned processes were awaited/reaped; no background service was left running.

My initial runner placed pytest workspaces under `/private/tmp`, which C-2.4 correctly refuses and retention correctly treats as scratch. All 15 resulting catalog/retention failures passed when rerun under the system temporary directory. The other 65 nodes were rerun on both the changed source and an exact export of base `08d6a09a`; their outcomes and normalized error messages match. These comprise unavailable macOS boot identity (57), direct denied `/bin/ps` calls (2), daemon commands refusing an unverifiable identity or waiting for a lock its stub could not write (5), and the preexisting missing-exit-receipt fixture expecting `lost` while it remains `running` (1). The process-dependent skip guards account for 26 skips. Thus the complete suites cannot be called green in this sandbox; the checks that can run are green. No production identity, containment or signaling requirement was relaxed.

A final focused slice passed **13/13** (76.14 s pytest time, 104.678 s including runner overhead): all automatic-release fake tests, the 100-sequence property, and both new retention-pin tests. The property generation phase took 18.35 s with 100 passing and zero failing examples. It asserts that release follows the latest verified census, every recorded writer is absent/reused, leases are gone, there is no override, and exactly one resolution event remains after subsequent actions.

Reproduce focused verification in a system-temp pytest workspace:

```sh
.venv312/bin/python -m pytest -q --hypothesis-seed=57 --hypothesis-show-statistics tests/fake/test_quarantine_self_resolve.py
.venv312/bin/python -m pytest -q tests/unit/test_retention_turns.py::test_a_released_turns_notification_survives_retention_until_delivered 'tests/unit/test_retention_archive.py::test_every_pin_keeps_its_job[quarantine-notice]'
.venv312/bin/python tools/quarantine_mutations.py
```

Five foreground mutations were killed by the targeted assertion tests (pytest exit 1 plus `AssertionError`, not collection/infrastructure failures):

| Mutation | Result | Pytest time |
|---|---|---:|
| Accept an unverifiable/error census as empty | Killed | 22.86 s |
| Ignore a changed recorded process start time | Killed | 65.96 s |
| Keep job/attempt leases after release | Killed | 88.33 s |
| Disable the durable per-attempt pace | Killed | 48.91 s |
| Ignore the salvage receipt on restart | Killed | 88.78 s |

The mutation runner restored source after every run; an independent SHA-256 check confirmed that `subfleet/daemon.py` and `subfleet/procs.py` match their pre-mutation bytes. `git diff --check` is clean. No logs, JUnit output, dependency directories, Hypothesis databases or local Git directory are committed.

Coherent implementation commits on the workspace-local `feat/quarantine-self-resolve` branch:

- `b30f7af835ac4c4625a2517b36085a0a0fef0d71`: paced automatic resolution, identity census, notices/timeline/status, schema/policy/contract, integration/property/mutation coverage.
- `03b59fec0c0512aba9bab217c89983017c1b1498`: retain the notification outbox's job/manifest until delivery; verify zero-budget pruning resumes; assert the literal eight-census bound.
- This report is a separate final commit. Every commit includes `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

Delivery is `docs/reports/2026-10-05-quarantine-self-resolve.bundle`, with prerequisite `08d6a09a0d566d2b94e14a109259f23001d95576` and named head `refs/heads/feat/quarantine-self-resolve`. The bundle includes the report commit. Verify it with `git bundle verify` and inspect its exact head with `git bundle list-heads`; the final response records that head. No push or shared Git metadata write was made.
