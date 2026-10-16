# PR #131 fix round seven

Starting point: `684dc4d130c5`, based on `release/217`. The salvaged process world, historical runner and baseline log were recovered verbatim before changing production; `baseline.txt` is evidence inherited from the interrupted attempt, not a new run. `model-head-before.txt` was empty in the salvage.

## Model domain and oracle

The kernel owns process lifetime, PID/start identity, parent, group, session, markers and cwd; production census results never determine whether a writer is alive. Zombies cannot write and are excluded from S1. XNU reserves live group/session numbers, including leaderless groups, so PID reuse cannot consume a populated group number. Reads take independent snapshots, with transitions replayed between table, marker, cwd, identity and group reads. Two cloned worlds replay identical reads through the automatic and operator resolvers. Both kill consumers use production signal helpers with kernel signal calls intercepted.

S1 is claimed only for C-5.7 census-covered writers: a visible marker/cwd, a retained identity/group, or parent lineage keeps a writer in the safety domain. The explicit invisible-writer test demonstrates why unconditional S1 cannot hold; its expected assertion is not a passing safety claim. L1 requires writers gone, successful inspections, empty conservative group evidence, no unresolved legacy child publication and no overflow hold; clearing the world models these premises. Ordinary process transitions cannot spawn a new writer after release.

## Verification

Historical proof: **7/7 found**, with minimized state-machine counterexamples and exact production restoration; see `history-summary.txt` and the seven `model-*-attempt.txt` / `model-*-probe.txt` logs. All minimize to `state.initial(source="marker", scenario=..., consumer=...)` and teardown: the scenario itself replays two production resolver calls or a resolver call followed by a production kill consumer.

| Revision | Minimized scenario | Kernel truth at violation | Invariant |
|---|---|---|---|
| `887552207` | `failed-bracket-child` | Writer 99 is seen after the table, forks 200 in sampled group 700, and exits during confirmation; 200 remains alive when both resolvers release | S1 |
| `951d9623f`, `684dc4d130c5` | `reused-before-group` | Writer 99 in group 700 exits after its first identity read; replacement 99 and child 300 occupy group 99, whose lookup succeeds but identity confirmation fails; the next pace releases both twins | S1 |
| `951d9623f`, `684dc4d130c5` | `retained-authority`, both consumers | Original guardian 100 stays alive in group 100; member 200 remains in retained group 700; failed confirmation removes its shape and a kill consumer signals it | S2 |

Final verification: **2,000 fixed-seed + 500 fresh-seed worlds quiet; 205 targeted checks pass; 33/33 mutations killed with passing controls**. [Final report, counts and full mutation table](final-report.md) records the fixes and verified bundle delivery. The first cold-cache run with automatically selected free-threaded CPython was interrupted without a counterexample; its task-owned pytest PID was verified exited, and production bytes were restored from the starting commit before retrying under standard CPython 3.14.7. The authority setup was corrected to retain the original guardian rather than manufacture a matching start identity on a respawn; all seven final historical logs use the corrected setup. Shrinking stays enabled; Hypothesis explanation replays are disabled because they add no smaller counterexample. The runner now preserves installed-library caches, removes only changing production caches and waits for its child on interruption. Tests run sequentially in a fresh Darwin user temporary directory with no path component named `tmp`; no live Subfleet state, caller checkout, provider or guardian is used.
