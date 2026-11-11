# Fix round 1 tooling evidence

The benchmark runner is unchanged at SHA-256
`e87dfe9d767014e4091b84f72fdc10e104c8f657b53882564ccb443bf103187d`,
the runner used for the previous reported comparison. The rerun uses one sample
per reference in each of the three 300-job scenarios. Python is 3.15.2; the old
record used 3.14.7, so the new paired base/head percentages, rather than absolute
old/new instruction counts or CPU times, are the useful comparison.

The mutation runner adds `forget-sweep-eligibility-recheck`, which removes only
the fresh sweep-time `scheduler.unadmittable` condition. Its selected permanent
regression is `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree`; a
kill requires the failing assertion `assert swept(service) == 0` and the
runner's existing selected-test and infrastructure checks. Each of the three
I3 mutation anchors and the new sweep anchor occurs exactly once.

## Commands so far

All commands use the assigned worktree. Benchmark and archive pytest state use
the permitted child of Darwin's temporary directory:

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)subfleet-attempt-20261107-112016-tt-fix-r1-a1-tw4lucor"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
export PATH="/usr/bin:/bin:/opt/homebrew/bin"
```

- `.venv/bin/python tools/tree_thrash_mutations.py --list`: passed, listing
  nine mutants, including the new sweep recheck mutant.
- `.venv/bin/python -m py_compile tools/tree_thrash_mutations.py`: passed.
- `git diff --check tools/tree_thrash_mutations.py`: passed.
- A Python source-anchor check of the four requested mutants: each occurs once.
- `git add tools/tree_thrash_mutations.py`: refused with exit 128 because the
  sandbox denies the parent repository's worktree `index.lock`; no commit ran.
- `uv run --no-sync python tools/benchmark_tree_thrash.py --samples 1 --jobs 300
  --head-overlay subfleet/daemon.py --temp-root "$TMPDIR" --output
  docs/reports/2026-11-06-tree-thrash-fix-r1-cost.json`: stdout and
  stderr in the matching `.log`; completed with exit 0. The exact archived base is
  `a8624a4899a2ce40c849cc5c6f7d74a7010f11b5`; head is `a22ac8917` plus daemon
  overlay SHA-256
  `7e36c69d8431af1d56bcee8db9f2382c0b6835d82750bb35e88ae7ee07cd9e72`.
  The final sweep-only revision refreshes authoritative desktop identity outside the store transaction. Exact source-slice
  comparison proves `_admit_pass` unchanged between that benchmark overlay and
  final daemon `176f8c848fc0c36629d6c8518c65d4c436b5dc02358fbf7e904bc5eec1074774`;
  the method SHA-256 is
  `7589ddc6d76e7b8fd21e739f31c4924407d50ada3e4b99818679b4924712fe65`.
  The proof is saved in `2026-11-06-tree-thrash-fix-r1-benchmark-source.json`.
- Mutation archive preparation uses `/usr/bin/git archive --format=tar HEAD
  subfleet tests tools pyproject.toml`, extracts beneath `$TMPDIR`, and overlays
  only `subfleet/daemon.py`, `tests/fake/test_admission_tree_thrash.py`,
  `tests/fixtures/tree_thrash_a8624a4899a2.py`, and
  `tools/tree_thrash_mutations.py`. The archive has no `.git`, and its daemon is
  a regular independent file. Its hashes and preparation time are saved in
  `2026-11-06-tree-thrash-fix-r1-mutant-archive.json`. No mutant pytest run has
  started yet. Preparation took 223.546 seconds; the archive overlays were then
  refreshed for the final authoritative sweep-view revision and canonical APFS output-key test fix. Independent inode and
  source-hash checks passed.

## CI budget

Importing `tools.ci_shard_plugin` and applying `validate_map`, `plan`,
`predicted_loads` and `shard_of` to the existing map gives 2,093.8 seconds
(34.897 minutes) for each of the five shards. The unmeasured main tree-thrash
module falls back to shard 1; the independent head module falls back to shard 4.
Each shard has only 306.2 seconds of additional test time before passing the
requested 40-minute warning threshold. A final projection awaits the measured
module testcase durations. The CI map has not been changed.

## Pending

The three-scenario benchmark passed. Its closed-only repeat also passed. Four mutants were selected but did not run because their required healthy control timed out. The final-source direct healthy-control run was interrupted; its reopened-lane case passed and the remaining cases are incomplete. The durable
`2026-11-06-tree-thrash-fix-r1-run-closed-repeat.py` and
`2026-11-06-tree-thrash-fix-r1-run-mutants.py` helpers record their commands,
start/end times, time bounds and gate decisions in the corresponding gate JSON
files. The mutation helper requires the root runner's `checks-ready.json`,
refreshes the four archive overlays, reserves 300 seconds before 19:57 UTC for
cleanup, and verifies the archived daemon was restored byte for byte.

## Cost

The open-pass instruction overhead fell from the previous 27.49% to 22.75%; it
still costs more than base. Thread-CPU overhead increased from 25.63% to 29.24%,
and the noisy wall-time difference changed from +20.12% to -47.86%. The new
open pair adds 3,726,453,150 instructions and 0.729959 CPU seconds, versus the
old pair's 4,582,336,866 instructions and 0.562689 CPU seconds. The interpreter
changed, so these paired percentages do not establish a clean raw timing
comparison between the old head and new head.

| New 300-job pair | Base / head instructions | Base / head wall seconds | Base / head thread-CPU seconds |
| --- | ---: | ---: | ---: |
| Closed, first pair | 927,913,886 / 1,073,641,565 | 36.801422 / 54.704116 | 0.220061 / 0.233947 |
| Controller holds after two | 8,881,967,315 / 9,070,657,161 | 145.442825 / 331.913457 | 1.561347 / 1.787616 |
| Fully open | 16,383,291,429 / 20,109,744,579 | 292.511738 / 152.517614 | 2.496475 / 3.226434 |

Counts match the earlier benchmark: closed makes 607/607 counted reads and
returns 303/303 rows; controller makes 8,710/8,732 reads and returns
7,187/7,198 rows, with 300/2 preparations and 2/2 reservations; open makes
9,007/12,307 reads and returns 228,154/274,504 rows, with 300 preparations and
reservations each. Direct transaction SQL remains outside these partial
counters. Removing the provisional `_slot_lease` search avoids the extra
45,150 slot-number probes in this 300-placement shape; the final reservation
still chooses its slot. The remaining 3,300 counted reads and 46,350 rows of
open-pass overhead remain visible.

The first closed pair was slower on head: base/head were 36.801422/54.704116
wall seconds, 0.220061/0.233947 thread-CPU seconds, and
927,913,886/1,073,641,565 instructions. Both made exactly 607 counted reads,
returned 303 rows, and performed zero evaluations, preparations or
reservations. This single noisy pair is retained, not replaced by a favorable
sample. The closed repeat also completed; its results and combined medians appear below.

`2026-11-06-tree-thrash-fix-r1-closed-prefix.json` proves exact base/head source
equality before the new preparation branch (SHA-256
`2f1725868dd305197fe64cc832e34dcac24c2729d5ad69f7dd33979fb5f389d0`). A
standalone AST compilation under Python 3.15.2 gives identical opcode-name
sequences, each 2,450 instructions with 26 `EXTENDED_ARG`; base/head have
163/166 local slots and 41 cells each. This is a static explanation of the
unchanged closed path, not a timing claim.

The two completed main-module cases in the initial ordered run took 1,008.206
and 233.180 seconds in its JUnit file. Their sum alone projects shard 1 to
55.586 minutes when added to the existing 34.897-minute map, already above the
40-minute warning threshold. The timed-out measured shape (1,222.396 seconds
all phases) is excluded from that lower bound. Final projection should use the
later completed full-module run when available. A split should extract the
long real-Git comparison/property cases into separate modules, then regenerate
and rebalance the shard map, adding a shard if the aggregate load requires it.

## Closed repeat (completed)

The scheduled closed-only repeat completed with exit 0. It started at
2026-11-11T18:40:50.855278+00:00 and used the same frozen runner and
method-identical daemon overlay `02f1ea4c...`. Base/head instructions were
1,034,563,042/1,061,104,894, wall seconds 37.915528/41.491435, and CPU seconds
0.332235/0.350248. Both again made 607 reads returning 303 rows, with no route
evaluation, preparation or reservation.

Across both retained pairs, base/head medians were
981,238,464.0/1,067,373,229.5 instructions,
37.358475/48.097776 wall seconds, and
0.276148/0.292098 CPU seconds. That is
**+8.78% instructions, +28.75% wall, +5.78% CPU**.
The current measurements show a closed-pass increase; they do not establish
the original no-slowdown goal. The identical prefix source/opcode evidence
and unchanged read/evaluation counts do not erase this measured result. No
pacing change was made. The two current pairs and their medians are retained
in `2026-11-06-tree-thrash-fix-r1-cost-comparison.json`.

## Mutation attempt and final-source controls

The authorized mutation campaign began at 19:29:00 UTC using isolated CI
Python 3.14.7, system Git, no xdist workers, and the archive without `.git`.
The four-test healthy baseline timed out after **691.376 seconds**,
pytest exit 124; `timed_out=true` and
`infrastructure_error=true`. The overall
runner exited 2. No completed testcase evidence or JUnit file was produced.
**Zero mutants were attempted and zero kills are claimed.** The archived daemon
was restored byte for byte. The baseline used the superseded sweep/profile and
output-key probe source, so it provides no final-source coverage.

The exact campaign command, from the archive, was:

```sh
"$TMPDIR/tree-thrash-r1/py314-env/bin/python" tools/tree_thrash_mutations.py \
  baseline reserve-without-tree lose-prepared-baseline evaluate-again-after-git \
  forget-sweep-eligibility-recheck --timeout 172 --control-timeout 688 \
  --control-test tests/fake/test_admission_tree_thrash.py::test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree \
  --control-test tests/fake/test_admission_tree_thrash.py::test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation \
  --control-test tests/fake/test_admission_tree_thrash.py::test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass \
  --junit-dir "$WORKTREE/docs/reports" --temp-root "$TMPDIR/fix-r1-mutant-tests" \
  --output "$WORKTREE/docs/reports/2026-11-06-tree-thrash-fix-r1-mutants.json"
```

`WORKTREE` is the assigned workspace. The gate JSON stores the expanded exact
command. The earlier waiting drivers were interrupted before pytest started
when switching to CI Python and adding the extra control nodes; they are not
test results. Source compilation and `git diff --check` passed. A tests-only
search found no exact comparisons for this runner's new optional flags.

All intended killing tests below are in `tests/fake/test_admission_tree_thrash.py`.
These are test selections, **not executed kills**:

| Mutant | Intended killing test | This round |
| --- | --- | --- |
| `reserve-without-tree` | `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | Not run |
| `lose-prepared-baseline` | `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | Not run |
| `evaluate-again-after-git` | `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | Not run |
| `forget-sweep-eligibility-recheck` | `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` | Not run |

The final archive overlays were then refreshed to daemon SHA-256
`176f8c848fc0c36629d6c8518c65d4c436b5dc02358fbf7e904bc5eec1074774` and
test SHA-256 `5503f2cf143601a893e66f5441239a05d21921ab85e968710902d8fca2c1d269`.
The sweep mutant now removes the authoritative `_pin_view(self._desktop_identity())`
recheck; its anchor occurs exactly once. Exact source comparison still gives
`_admit_pass` SHA-256 `7589ddc6d76e7b8fd21e739f31c4924407d50ada3e4b99818679b4924712fe65`,
identical to the benchmark. The output-lease probe now uses the canonical APFS
identity key.

A final direct serial healthy run on that final source was launched with
`-vv -x -p no:cacheprovider`, the fresh basetemp `$TMPDIR/fix-r1-final-controls`,
and JUnit destination `2026-11-06-tree-thrash-fix-r1-final-controls.xml`. Its selected nodes, in
order, are the reopened-lane probe, lease-arrival probe, two-pinned-lanes probe,
and submitted-HEAD/I3 example. It was interrupted at 19:55 UTC, unfinished
and cleaned up by 19:57. Results and named times will be appended when available.

## Final control result and cleanup

The final serial run was interrupted on its own terminal at 19:55 UTC and
exited 1. Its verbose log confirms the reopened-lane probe passed and the
lease-arrival probe then started. The full collection did not pass.

| Final-source case | Seconds, all phases | Outcome |
| --- | ---: | --- |
| `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` | 345.255 | passed |

The lease-arrival, two-pinned-lanes and submitted-HEAD controls did not
complete in this final run. All four mutants remain not run; no kill is
claimed. All own benchmark, mutation and final-control commands have stopped.
The archived daemon still matches final checkout source byte for byte.

The existing CI map remains unchanged. The two earlier completed main-module
cases alone still give the documented 55.586-minute local projection for
shard 1; complete CI fit remains unproven. The other new-case times remain
unavailable when no completed testcase was recorded.
