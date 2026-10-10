# PR 131 finished run loss investigation

A deterministic regression in #131 can report a completed, contained run as
`lost` with rc 125. A background waiter born between process reads is wrongly
retained as the run's lineage. The two scripted cases fail at `7368159467ff`
and pass on `origin/release/217` and with the proposed fix.

This establishes a #131 regression, but attribution of CI job `114097907972`
to this particular race remains unconfirmed. The sandbox refuses `/bin/ps`, so
every attempted real reproduction skipped. The CI run has no downloadable
artifacts containing its quarantine reason or daemon database. An unrestricted
macOS rerun is still needed before treating the CI incident as fully explained.

## Lost and quarantine paths

All line numbers in this section refer to the investigated head, `7368159467ff`.
Changes are compared using `git diff origin/release/217...7368159467ff -- subfleet/`.
The merge base is `694723d9c0cf`; the available release ref is `cf3a22e2e73c`.

| Location | Transition or caller | Change in PR 131 |
| --- | --- | --- |
| `subfleet/daemon.py:6632` | `_quarantine` writes attempt `quarantined`; `:6635` selects job `lost`, rc 125 unless cancelled, and `:6636` writes it. This is independent of an existing successful exit receipt. | Existing terminal behavior; adds retained lineage and a durable recheck time. |
| `subfleet/daemon.py:6224` | `_process_attempt` calls `_quarantine` when start grace expires, neither receipt exists, and containment is not verified empty. | Caller unchanged; containment implementation changed. |
| `subfleet/daemon.py:6612` | `_kill_attempt` calls `_quarantine` after termination and its bounded settle window cannot verify containment. Used by cancellation, wall limits, and dead-guardian recovery. | Caller unchanged; ownership retention and signal targets changed. |
| `subfleet/daemon.py:7056` | `_finalize` calls `_quarantine` when containment remains nonempty or unverifiable past the three-second exit settle window (`:7052`). | Finalization and deadline unchanged; its census now includes cwd and retained lineage. |
| `subfleet/daemon.py:7078` | `_finalize` declares receipt loss only when `exit.json` is absent or has no rc. `:7181` selects attempt `lost`, written at `:7194`. `:7182` selects job `lost` unless cancellation or an eligible read-only retry wins; rc 125 at `:7208`, written at `:7214`. | Unchanged. The receipt on disk overrides the caller's earlier `lost=True` verdict. |
| `subfleet/daemon.py:6317` | `_inspect_running` calls `_lost` for a dead guardian, no receipt, and an empty census. A nonempty census instead enters `_kill_attempt(lost=True)` at `:6315`. `_lost` at `:6781` delegates to `_finalize`. | Unchanged callers; census implementation changed. |
| `subfleet/daemon.py:6617` | Recovery's `_kill_attempt(lost=True)` calls `_lost` after verifying emptiness when no exit receipt exists. | Caller unchanged; containment and ownership logic changed. |
| `subfleet/daemon.py:6746` | `_resolve_quarantine_once` writes attempt `lost` at `:6747` after automatic, confirmed-dead, or forced release; cancellation yields `interrupted`. The job was already terminalized by `_quarantine` and is not reclassified here. | Existing release verdict; refactored into the shared resolver, adds automatic resolution and turn notification outbox. |
| `subfleet/daemon.py:4138` | `_contain_probe` sets its durable probe record `quarantined` unless its last census is verified empty. `:4144` holds the associated job in `waiting/uncertain`. This is a probe record, not an attempt `quarantined` or job `lost`. | Existing transition; census lineage and identity-based signal handling changed. |

`subfleet/procs.py` never writes job or attempt state. It supplies
`Containment.verified_empty` (`:453`), requiring no live source, errors, or
unverifiable reads. Its `containment` function (`:498`) can therefore cause the
quarantine callers above to hold on live group members, descendants, markers,
cwd processes, retained identities/groups, incomplete identity/publication
proof, lineage overflow, or failed inspection. #131 adds cwd and retained
lineage/provider proof and changes identity capture; its state decisions
remain in the daemon.

## Reproduction and timing window

The CI log downloaded with the requested `gh run view` command reports
`waiter_rc=125`, waiter return and run finish at `2026-10-10T01:37:55Z`, and a
wake at `01:37:56.691Z`. Its suite result is one failure, 10,649 passes,
21 skips, and two xfails. The successful wake does not establish run success.

The real reproduction configuration uses Python 3.12.14, four Python busy loops,
`PYTHONHASHSEED=131`, and `--hypothesis-seed=131`. Each pytest process runs under
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`. Load starts only
inside the lock and is terminated and reaped in `finally`. Individual test
processes have a 150-second limit. The slowed configuration adds 150 ms after
each successful daemon `ps`/`lsof` read; this hook cannot run past the blocked
process-inspection prerequisite.

| Real automatic strand attempts | Head | Release |
| --- | --- | --- |
| Four busy loops, ordinary reads | 8 skipped, 0 executed | 8 skipped, 0 executed |
| Four busy loops, slowed read configuration | 0 executed; lock wait timed out | 0 executed; lock wait timed out |

No real failure rate can be inferred from these skips. In particular, these
are not eight passes on either base. The slowed runs were configured for four
attempts per base but exited after a 120-second lock wait, before starting any
pytest process or load generator. Brief retries after final verification also
could not acquire the lock within ten seconds.

The deterministic test uses the actual daemon finalizer and production census,
with scripted reads and a fake monotonic clock. This timing window uses line
numbers from the unpatched head:

1. The run has an rc 0 exit receipt and no remaining process of its own.
2. PID 200 starts as the parent turn's background waiter after the first table,
   or replaces a PID shown in that table. The later environment listing shows
   `SUBFLEET_ATTEMPT=parent/a1`, and the cwd listing shows the shared workspace.
3. `subfleet/procs.py:742` only exempts a foreign incarnation whose identity
   matches the earlier table. The late waiter receives no exemption, so `:759`
   captures its identity and group as a cwd writer.
4. `subfleet/daemon.py:6371` retains that lineage. On the next census, retained
   identity roots (`subfleet/procs.py:558`) and groups (`:642`) hold even though
   the current marker and cwd sources correctly exclude the foreign waiter.
5. Advancing the clock past three seconds reaches `daemon.py:7056`, marking
   the attempt quarantined and the completed run lost with rc 125. The waiter
   can then return. Slower reads widen the interval in step 2.

| Scripted cases | Result |
| --- | --- |
| Unpatched head, waiter absent or replaced in earlier table | 2 failed; both jobs became `lost`, rc 125 |
| Release with the identical test and process world | 2 passed; jobs `succeeded`, rc 0 |
| Proposed fix | 2 passed; no foreign lineage retained |

## Fix and validation

`subfleet/procs.py:743` keeps the existing foreign-identity check for stable
table rows. When the foreign PID is late or replaced, it reads that PID's
environment again between fresh identity checks before allowing a cwd
exemption. A failed check still holds; an own attempt/root marker discovered
during reinspection enters the marker census. No signal authority is added,
and retained lineage or the settle deadline is not weakened.

`tests/fake/test_finished_run_containment.py:16` exercises both finished-run
cases without real load or sleeps. `tests/unit/test_procs.py:908` checks stable
foreign exclusion, reuse, unreadable identity/marker, own attempt/root markers,
and disappearance of the foreign marker.

Final focused verification: **269 passed, 26 skipped, 1 xfailed in 73.63 s**.
This includes `tests/unit/test_procs.py`, the finished-run regression, all five
`test_quarantine_*.py` files named below, the PR 131 probes and round-three
checks, and the daemon state contracts. The process-world S1, S2, K1, L1, and P1
oracle passes with its default 100 examples and 25 steps, seed 131; the prepared
world pool is enabled and its equivalence checks also pass. All 26 skips require
real process inspection. The existing strict xfail is
`test_real_lsof_protects_a_worktree_from_an_intermittent_invisible_writer`:
a never-observed writer outside the workdir closes its file between appends.

An earlier verification had 267 passes and one fixture failure: the scripted
`ps` helper rejected the newly added per-PID environment read. Updating that
helper to return the selected PID's scripted command resolved the failure;
the final run above has no unexpected failures.

The final pytest selection is:

```text
tests/unit/test_procs.py
tests/fake/test_finished_run_containment.py
tests/fake/test_quarantine_process_world.py
tests/fake/test_quarantine_self_resolve.py
tests/fake/test_quarantine_detached_writers.py
tests/fake/test_quarantine_review_fixes.py
tests/fake/test_quarantine_identity_invariants.py
tests/fake/test_review_pr131_probes.py
tests/fake/test_review_pr131_round3.py
tests/fake/test_state_contract.py
```

It runs with `SF_WORLD_POOL=1`, `--hypothesis-seed=131`, and the required lock,
with a 1,500-second outer bound including lock acquisition. Test temporary
directories and caches are under `getconf DARWIN_USER_TEMP_DIR` and removed
after `chmod -R u+rwX`. No logs or JSON evidence are committed.

Shared git metadata is unwritable. Commits use `.git-local`, branch
`subfleet/pr131-lost-run`, rooted at the assigned commit. No history was
rewritten and nothing was pushed. The failing regression is committed as
`f77e4b70cf4e`; the product fix and safety cases are `99bc97cc5`.
