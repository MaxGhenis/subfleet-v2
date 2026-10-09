All four fixes are committed in .git-local on feat/quarantine-self-resolve:
0ac643306 (S2), 712b833d8 (primary identity/pace coverage),
6d6c0546b (every protected lease and actual worktree), e4d51cdfd (contract).
No production code changed from 169f6b6c17ae.

Completed verification before the final audit:
- Eight historical minimized S1/S2 counterexamples, including missing-start
  977999487 plus all seven cases on 887552207, 951d9623f, 684dc4d130c5.
- 33/33 production and 5/5 oracle mutations with passing controls.
- 370 historical state and targeted checks, including rebuilt schema-6 migration.
- 2,000 fixed-seed and 500 fresh-seed quiet worlds archived in first-proof/.

The archived first-proof/ run passed 2,000 fixed and 500 fresh worlds.
The final audit reproduced a further S2 overrestriction: a complete child table
identity can become owned after fresh scalar leader confirmation even if the
leader's table start is unavailable. It may then escape after handling SIGTERM
before its first SIGKILL. Two table-complete cases reproduced the oracle failure;
four incomplete-child cases correctly held. No production bug or code change.
The primary model now covers both cases; the oracle acquires only full original-
group member identities, never identities known only through later sampling.

Remaining work:
1. Controls (29), five oracle mutations, eight historical replays and all 370
   targeted checks now pass with the final stronger model. Run tools/quarantine_world_proof.py foreground and serially: exactly
   2,000 passing, zero failing worlds at seed 131 and 500 at freshly drawn seed 3027837772367224465.
   Keep first-proof/ as predecessor evidence; final root logs must correspond
   to final model bytes. Progress counts include invalid draws; use final
   Hypothesis statistics as the proof. Commit code and each verification step.
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
