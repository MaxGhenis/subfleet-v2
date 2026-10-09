# PR #131 fix round seven

Starting point: `684dc4d130c5`, based on `release/217`. The salvaged process world, historical runner and baseline log were recovered verbatim before changing production; `baseline.txt` is evidence inherited from the interrupted attempt, not a new run. `model-head-before.txt` was empty in the salvage.

## Model domain and oracle

The kernel owns process lifetime, PID/start identity, parent, group, session, markers and cwd; production census results never determine whether a writer is alive. Zombies cannot write and are excluded from S1. XNU reserves live group/session numbers, including leaderless groups, so PID reuse cannot consume a populated group number. Reads take independent snapshots, with transitions replayed between table, marker, cwd, identity and group reads. Two cloned worlds replay identical reads through the automatic and operator resolvers. Both kill consumers use production signal helpers with kernel signal calls intercepted.

S1 is claimed only for C-5.7 census-covered writers: a visible marker/cwd, a retained identity/group, or parent lineage keeps a writer in the safety domain. The explicit invisible-writer test demonstrates why unconditional S1 cannot hold; its expected assertion is not a passing safety claim. L1 requires writers gone, successful inspections, empty conservative group evidence, no unresolved legacy child publication and no overflow hold; clearing the world models these premises. Ordinary process transitions cannot spawn a new writer after release.

## Verification

Historical counterexamples, final runs, mutation results and delivery head will be recorded here after execution. The first cold-cache run with automatically selected free-threaded CPython was interrupted without a counterexample; its task-owned pytest PID was verified exited, and production bytes were restored from the starting commit before retrying under standard CPython 3.14.7. The runner now preserves installed-library caches, removes only changing production caches and waits for its child on interruption. Tests run sequentially in a fresh Darwin user temporary directory with no path component named `tmp`; no live Subfleet state, caller checkout, provider or guardian is used.
