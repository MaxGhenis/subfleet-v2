# Round-seven checkpoint

Base/prerequisite: a309de525882. Work and commits use this workspace's `.git-local`, branch `feat/quarantine-self-resolve`; shared metadata is outside the writable roots. No push.

Completed:

- `f2d15d66f`: remove missing-shape ownership defaults in both kill consumers; 50 retained-authority model worlds pass (S2), eight authority probes and six mutation-runner controls pass.
- `31dc1dcc8`: remove both leader-identity/reuse discharge shortcuts for retained groups; 50 reused-before-group model worlds pass (S1), 128 round-six/process checks pass. The older saved-group unit expectation now holds on leader reuse.
- `9681c7247`: C-5.7 states each rule in one sentence.

Completed: the full process world is quiet at 2,000 examples, seed 13107, 25 steps, all scenarios and consumers, with zero failing examples; the explicit residual test also passed. Output: `model-fixed-2000.txt` (2 passed in 1794.78 seconds). Completed: 500 passing worlds, zero failures, fresh seed 1564217222, output `model-fresh-500.txt` (2 passed in 534.60 seconds). The model is quiet on both required budgets. Completed: all 205 checks in the nine-file targeted slice, including every one of the 74 round-4/5/6 probes, pass; outputs `targeted.txt` and `targeted-counts.tsv`. In progress: all 33 mutations run serially, output `mutations.txt`; a partial log is not a passing verdict. Wait for exit before further pytest or production edits. If interrupted with a mutant remaining, restore only `subfleet/daemon.py` and `subfleet/procs.py` from this local HEAD before resuming. An early baseline/model overlap was caught; the first model was interrupted and awaited, and the authority model was rerun serially. Subsequent runs are serial.

Remaining:

1. Completed: model quiet at 2,000 fixed-seed and 500 fresh-seed examples without any additional production fix.
2. Both model budgets pass; no new model findings remain.
3. Completed: all round-4/5/6 probes, the older 21 probes, 96 process units, eight census interleavings and six mutation-runner controls (205 total).
4. `tools/quarantine_mutations.py` adds the two authority consumers and each sampled-group discharge shortcut (33 cases total), removes only production bytecode caches and uses `--assert=plain` to avoid repeated assertion rewriting; Python assertions remain enabled. All 33 old snippets match exactly once. Run all mutations serially and require passing controls/assertion kills and exact source restoration; the cases and runner preparation are committed, their execution is pending.
5. Replace this checkpoint with final report/results, commit, and regenerate/verify `docs/reports/2026-10-09-pr131-fix7c.bundle` from `refs/heads/feat/quarantine-self-resolve` with prerequisite a309de525882; name its exact head.

Use a fresh `TMPDIR` created by `mktemp -d "$(getconf DARWIN_USER_TEMP_DIR)sf-pr131-fix7c.XXXXXX"` (no path component named `tmp`). Remove the task-owned directory after all processes exit. Dependencies are installed in workspace `.venv` (CPython 3.14.7, pytest 9.1.1, Hypothesis 6.168.1); installed and stable repo library caches were compiled to avoid cold imports. Never access live Subfleet state or the caller checkout. Every commit ends with the requested Claude Opus 5.5 co-author trailer.

Model command (fresh temporary directory assigned to `TMPDIR`):

```sh
SF_WORLD_EXAMPLES=2000 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest --assert=plain -q tests/fake/test_quarantine_process_world.py --hypothesis-seed=13107 --hypothesis-show-statistics
```
