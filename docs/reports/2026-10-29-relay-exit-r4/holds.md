# Review r4 holds (#271 at `c8904e4b2`)

One pytest per hold, under `host-runs/suite_hold.sh`. All 14 holds ran under the lock, none directly. Each ran
in-process with a SIGALRM backstop (400 to 560 s) and none needed it. `TMPDIR` and Hypothesis storage were in scratch,
with `PYTHONDONTWRITEBYTECODE=1`, `env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH`, and the scratch venvs' own
`python` (3.14.7, 3.12.14). Start is admission to the slot (UTC, 2026-10-29); load is the 1-minute average at
admission. Counts are passed/failed/skipped per file, from each hold's JUnit file.

| Hold | Python | Queued, start (slot) | Load | Wall | Per file |
|---|---|---|---|---|---|
| exit | 3.14 | 02:04:35, 02:08:43 (A) | 35.8 | 120.6 s | `test_turn_relay_exit.py` 10/0/0, `_peek` 27/0/0, `_properties` 4/0/0, `_r3` 24/0/0, `_recovery` 13/0/0, `_review` 12/0/0 |
| exit | 3.12 | 02:11:10, 02:11:55 (B) | 26.8 | 34.6 s | `test_turn_relay_exit.py` 10/0/0, `_peek` 18/0/9 (the `os.waitid` cases, with their reason), `_properties` 4/0/0, `_r3` 24/0/0, `_recovery` 13/0/0, `_review` 12/0/0 |
| unit | 3.14 | 02:12:37, 02:15:13 (B) | 37.7 | 102.4 s | `test_relay.py` 30/0/0, `test_guardian.py` 13/0/0, `test_turn_runner.py` 83/0/0 |
| unit | 3.12 | 02:17:09, 02:34:03 (A) | 34.3 | 116.7 s | `test_relay.py` 30/0/0, `test_guardian.py` 13/0/0, `test_turn_runner.py` 83/0/0 |
| dev | 3.14 | 02:38:09, 02:42:03 (A) | 21.2 | 13.4 s | an earlier draft of `test_turn_relay_exit_r4.py` (10 cases, the timing two since moved to the report folder) 10/0/0; not counted below |
| probe, replay | 3.14 | 02:47:23, 02:47:23 (B) | 23.4 | 92.9 s | `tests/fake/test_probe_turn.py` 61/0/0, `tests/unit/test_turn_replay.py` 34/0/0 |
| wall | 3.14 | 02:49:06, 02:49:29 (A) | 26.3 | 89.5 s | `test_wall_stop_deliveries.py` 25/0/0, `test_wall_cancel_races.py` 30/0/0 |
| guards | 3.14 | 02:51:08, 02:51:37 (A) | 20.7 | 53.9 s | `test_state_contract.py` 37/0/0, `test_test_module_names.py` 1/0/0, `test_invariants_index.py` 25/0/0 (with `test_turn_relay_exit_r4.py` in the tree) |
| mutants | 3.14 | 02:52:44, 02:52:45 (B) | 23.2 | 176.3 s | `fix_r3_mutants.py` 24/0/0, `named_mutants.py` 10/0/0 (5 property reports deselected), `review_mutants.py` 14/0/0 |
| mutants | 3.12 | 02:56:09, 02:57:14 (B) | 26.4 | 168.2 s | `fix_r3_mutants.py` 24/0/0, `named_mutants.py` 10/0/0 (5 deselected), `review_mutants.py` 14/0/0 |
| review | 3.14 | 03:00:16, 03:00:39 (A) | 30.9 | 51.3 s | `test_turn_relay_exit_r4.py` 8/0/0, `review_exit_timing.py` 3/0/0 (before the unread variant), the peek property 2/0/0 |
| review | 3.12 | 03:01:58, 03:03:03 (A) | 29.5 | 44.5 s | `test_turn_relay_exit_r4.py` 6/0/2, `review_exit_timing.py` 3/0/0, the peek property 1/0/1 |
| timing | 3.14 | 03:09:08, 03:09:09 (B) | 31.4 | 14.1 s | `review_exit_timing.py` 4/0/0 |
| timing | 3.12 | 03:09:26, 03:09:26 (B) | 35.4 | 16.6 s | `review_exit_timing.py` 4/0/0 |

- **Load-bound tests.** `test_relay.py`'s stop-bound tests passed 30/30 at load 34 to 38 on both Pythons, and the
  probe property passed at load 23. No bound was loosened.
- **`test_probe_turn.py`** has 61 cases at head against round 4's 43: #273 (`542e5f534`, `0230a0163`,
  `7be2a6f17`) added them on release before the merge.
- **The widened property** (`test_loss_interleavings_keep_one_outcome_and_all_stdout`) took 60.4 s on 3.14 at load
  36 and 13.1 s on 3.12 at load 27. The peek property took 13.6 s (libc) and 7.3 s (os) on 3.14, and 8.6 s on 3.12.

## Measured outside the lock

These were measurement scripts in scratch, each with one child at a time and under a minute, while choosing the
harness's inputs. They are not tests and are not counted above.
- `gap.py`: a parent writing one byte at a time in a tight loop until EPIPE. Its gaps were 1.6 to 2.5 ms at the
  median and 44 to 48 ms at most, but the relay never writes that way, and the kqueue measurements below don't
  reproduce them.
- `gap2.py`: `proc_pidinfo` answers ESRCH at the EPIPE moment, for zombies and exiting children alike.
- `gap3.py` and `gap4.py`: stdin close to waitable exit, with and without unread pipe data, 0.05 to 0.14 ms at the
  median. They also ran the relay path with an unread frame (80 of 80 refusals flagged) and timed the journal
  append (0.09 to 2.8 ms at the median, 46 ms at most).
- A C probe of `siginfo_t` and the `waitid` constants, and one `gh pr view 271 --json body`.

## Not run

- e2e and frontend: lanes can't run them, and CI runs them.
- The red run of round 4's regression at `3769a17e5`: not on this brief, and round 4 recorded it.
- Round 4's other holds, outside this brief: round 3's unrun list and `test_daemon_log_sites.py`.

## Processes and cleanup

Every child was started and reaped by its test or script, and no process of mine is left running. Scratch
(under 200 MB) was removed with the rules' guarded cleanup at the end. The worktree holds no bytecode,
`.hypothesis` or `build/` of mine, and `/tmp/sf-failed` holds nothing of mine.
