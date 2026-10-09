# Round-seven checkpoint

Base/prerequisite: a309de525882. Work and commits use this workspace's `.git-local`, branch `feat/quarantine-self-resolve`; shared metadata is outside the writable roots. No push.

Completed:

- `f2d15d66f`: remove missing-shape ownership defaults in both kill consumers; 50 retained-authority model worlds pass (S2), eight authority probes and six mutation-runner controls pass.
- `31dc1dcc8`: remove both leader-identity/reuse discharge shortcuts for retained groups; 50 reused-before-group model worlds pass (S1), 128 round-six/process checks pass. The older saved-group unit expectation now holds on leader reuse.
- `9681c7247`: C-5.7 states each rule in one sentence.

In progress: a foreground full process-world run, 2,000 examples, seed 13107, 25 steps, all scenarios and consumers. Output: `model-fixed-2000.txt`. A partial/empty file is not passing evidence. Wait for its exit before any further pytest or production edit. An early baseline/model overlap was caught; the first model was interrupted and awaited, and the authority model was rerun serially. Subsequent runs are serial.

Remaining:

1. Fix and separately commit any minimized S1/S2/P1/L1 counterexample from the full model; rerun until quiet at 2,000 examples with seed 13107.
2. Run 500 examples with a newly generated seed, recording the seed and Hypothesis statistics.
3. Run every round-4/5/6 review probe (including both round6b files), unit census interleavings and mutation-runner controls; broaden only for relevant concerns.
4. `tools/quarantine_mutations.py` adds the two authority consumers and each sampled-group discharge shortcut (33 cases total), removes only production bytecode caches and uses `--assert=plain` to avoid repeated assertion rewriting; Python assertions remain enabled. All 33 old snippets match exactly once. Run all mutations serially and require passing controls/assertion kills and exact source restoration; the cases and runner preparation are committed, their execution is pending.
5. Replace this checkpoint with final report/results, commit, and regenerate/verify `docs/reports/2026-10-09-pr131-fix7c.bundle` from `refs/heads/feat/quarantine-self-resolve` with prerequisite a309de525882; name its exact head.

Use a fresh `TMPDIR` created by `mktemp -d "$(getconf DARWIN_USER_TEMP_DIR)sf-pr131-fix7c.XXXXXX"` (no path component named `tmp`). Remove the task-owned directory after all processes exit. Dependencies are installed in workspace `.venv` (CPython 3.14.7, pytest 9.1.1, Hypothesis 6.168.1); installed and stable repo library caches were compiled to avoid cold imports. Never access live Subfleet state or the caller checkout. Every commit ends with the requested Claude Opus 5.5 co-author trailer.

Model command (fresh temporary directory assigned to `TMPDIR`):

```sh
SF_WORLD_EXAMPLES=2000 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest --assert=plain -q tests/fake/test_quarantine_process_world.py --hypothesis-seed=13107 --hypothesis-show-statistics
```
