# D-WALK-FD1 review r1: named mutants

Historical fix r1 production/test head: `63e3158d7`. Every mutation ran in memory through
`tools/retention_descriptor_mutants.py`; production files were unchanged.
All nine mutants were killed by assertion failures in their named tests.
The seven inherited mutants reproduce the build/review results.

Times are 2026-10-29 UTC; loads are 1/5/15-minute averages. Each row is
one serial pytest under the shared suite lock, with isolated scratch.

| Mutant | Start | Load | Passed / failed | Named killing test |
| --- | --- | --- | ---: | --- |
| M1: Drop walk child close, retain reordering | 14:49:39 | 18.92/20.96/27.42 | 16 / 4 | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| M2: Restore the whole release source | 14:49:46 | 17.31/20.54/27.19 | 9 / 11 | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| MO: Restore release open_dir | 14:49:54 | 17.61/20.55/27.16 | 0 / 2 | `tests/unit/test_retention_walk_descriptors.py::test_open_dir_failed_validation_closes_descriptor[error0]` |
| ML: Restore release _layout | 14:50:00 | 18.84/20.76/27.19 | 0 / 1 | `tests/unit/test_retention_descriptor_review.py::test_layout_allocation_before_open` |
| MI: Drop repository descriptor registration protection | 14:50:04 | 18.37/20.63/27.11 | 0 / 1 | `tests/unit/test_retention_descriptor_review.py::test_in_repository_registration_failure_closes_descriptor` |
| MP: Restore release _open_path | 14:50:07 | 17.94/20.50/27.03 | 0 / 2 | `tests/unit/test_retention_walk_descriptors.py::test_open_path_failed_descent_closes_intermediate_descriptors[a/missing]` |
| MC: Restore release _conflict_dir | 14:50:12 | 17.70/20.41/26.95 | 1 / 3 | `tests/unit/test_retention_walk_descriptors.py::test_conflict_dir_failed_descent_closes_descriptor[error0-mkdir]` |
| MH: Drop both descent handoff protections (P2-1) | 14:50:17 | 19.25/20.69/27.01 | 0 / 6 | `tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child[True-set-aside]` |
| MD: Stop the cleanup drain at its first failing close (P2-2) | 14:50:21 | 20.43/20.91/27.05 | 0 / 3 | `tests/unit/test_retention_descriptor_review.py::test_walk_cleanup_interruption_closes_remaining_children[close]` |

M1 and M2 ran all 20 walk-descriptor tests. MO ran both validation cases;
MP both failed-descent cases; MC all four conflict-descent cases.
MH ran all six path/conflict/set-aside cases, before and after the
interrupted close completed. MD ran both walk close/throw cases and
repository cleanup. ML and MI each ran their named review test.

The plugin is opt-in: select a mutant with
`-p tools.retention_descriptor_mutants --retention-descriptor-mutant=<name>`
and pass only the named test file or node IDs under the suite hold.

## Current-head rerun

Fix r2 adapts M1 and MI to the ownership guards and shared cleanup calls while
preserving their removed behavior. The other inherited mutations retain their
meaning. All nine were rerun against their named tests; the [r2 report](2026-10-29-walk-descriptors-r2.md)
records current-head counts, starts and loads, plus four additional mutants:
MR (retry a released parent close), MA (ignore registration ownership), ME
(replace an active exception), and MX (drop cancellation from cleanup).
