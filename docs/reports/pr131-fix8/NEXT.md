Remaining verification after item commits 0ac643306, 712b833d8, 6d6c0546b,
and e4d51cdfd, on .git-local feat/quarantine-self-resolve:

1. Re-run tools/quarantine_world_history.py with
   SF_WORLD_EVIDENCE=docs/reports/pr131-fix8/final-history. Require 8/8 minimized
   S1/S2 counterexamples, including the newly covered missing-start branch.
2. Run tools/quarantine_mutations.py. Require passing controls and 33/33 kills.
   Final oracle source already has 4/4 kills in oracle-mutations.txt.
3. Run historical state probes and relevant unit files serially, foreground,
   with --assert=plain; exclude real-process round-three nodes per the user's
   no-background-process constraint.
4. Primary model: 2,000 worlds at fixed seed 131 and 500 at a recorded fresh
   random seed. Keep production/model bytes unchanged; verify exact counts.
5. Commit evidence, write final report with fix lines and history/mutation
   tables, remove this NEXT file, and deliver a verified bundle at
   docs/reports/2026-10-09-pr131-fix8.bundle, prerequisite 169f6b6c17ae.
6. Remove only the task-owned directory named by .fix8-tempdir, then remove
   that ignored pointer. Never write shared metadata or the caller checkout.

Every test command must export TMPDIR from .fix8-tempdir. It is a fresh Darwin
user temp folder with no component named tmp, outside HOME. No sub-agents,
parallel pytest, -n, background services, or pattern kills. Each remaining
coherent evidence step must be committed with the required final co-author
trailer. Both shared production files still match the starting commit.
