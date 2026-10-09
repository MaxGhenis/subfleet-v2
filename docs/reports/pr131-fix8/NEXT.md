All four fixes are committed in .git-local on feat/quarantine-self-resolve:
0ac643306 (S2), 712b833d8 (primary identity/pace coverage),
6d6c0546b (every protected lease and actual worktree), e4d51cdfd (contract).
No production code changed from 169f6b6c17ae.

Completed verification:
- Final strengthened historical replay: 8/8 minimized S1/S2 counterexamples,
  including the previously omitted missing-start branch at 977999487, plus
  all seven cases on 887552207, 951d9623f and 684dc4d130c5.
- 33/33 production and 4/4 oracle mutations, all with passing controls.
- All 364 historical state and targeted checks passed. The initial migration
  skip was resolved by rebuilding the synthetic schema-6 fixture from 08d6a09a.
  Background-process host probes were excluded under the user's constraint.

Remaining work:
1. Finish the foreground tools/quarantine_world_proof.py run. It requires
   exactly 2,000 passing, zero failing worlds at fixed seed 131, then 500 at
   freshly drawn seed 11783757351826154477. Progress is fixture completions,
   including invalid draws; only final Hypothesis statistics prove counts.
   Logs: model-fixed-2000.txt, model-fresh-500.txt, world-proof.txt; configuration
   and byte-restoration evidence: proof-config.json, proof-restoration.json.
   Commit completed proof evidence. If interrupted, re-run with those explicit
   counts/seeds in a new fresh Darwin user temp directory, serially, -B,
   --assert=plain, --hypothesis-show-statistics, no -n. Production/model must
   remain byte-identical throughout. Never leave background processes running.
2. Write the final concise report with fix file:line references, complete
   historical verdict table, mutation table link, test counts and scope.
   Remove NEXT.md only when the required work is complete.
3. Save and verify docs/reports/2026-10-09-pr131-fix8.bundle using prerequisite
   169f6b6c17ae and ref feat/quarantine-self-resolve, naming its final head.
   Verify the bundle can fetch that head into isolated metadata containing the
   prerequisite. Do not write shared Git metadata, rewrite history or push.
4. Remove only the task-owned directory named by .fix8-tempdir, then remove
   that ignored pointer. This run's directory is
   /private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-pr131-fix8-ptktfiul.

Every commit must end with:
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

No sub-agents, parallel pytest, background services, pattern kills, live
~/.subfleet state, or caller-checkout writes. All test runs export the fresh
Darwin TMPDIR above, with no path component named tmp and outside HOME.
