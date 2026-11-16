Recommended wording for the two importer failures:

`test_close_waits_for_a_read_in_progress_and_refuses_later_ones` failed its elapsed-time assertion in parallel batch 00: the measured wait was 0.148 s against a 0.2 s minimum. The test starts its 0.3 s release timer before recording the measurement start, so scheduling delay between those operations can shorten the measured interval. I infer timing sensitivity from that ordering and the passing isolated recheck. The same test and the daemon-refusal file passed all 9 targeted rechecks with the corrected outside-git basetemp; no store lifecycle change was made for this assertion.

Batch 01 found a real regression in the resume fix. Authoritative source inheritance replaced a forged review request's subject and round with a build source's null fields before the existing effective-task guard could reject them. `_resume_submission` now checks requested review fields against the source task before replacing those fields. Invalid review metadata on a nonreview resume again refuses with `review-fields`, while a valid review resume retains its original subject and round. The existing regression test failed in the broad run; all 31 tests in the validation and resume files passed the focused recheck after the correction.

Aggregate wording is generated in `ledger-summary.md`. Primary importer totals count each disjoint batch once and exclude focused reruns. The separate focused green total uses nonoverlapping complete file selections; it overlaps the primary importer scope and must not be added to it. Historical failed results remain visible in the per-command ledger even after their focused rechecks pass.


V2 full-selection remaining failures and safe targeted recheck:

Batch12 (exact ledger L41): 4,977 passed, 90 skipped, 52 xfailed, 6 failed; launcher elapsed 903.43s. Every failure belongs to two R5 PR-refusal restart functions: four hold/rearm parameters and two atomic acceptance parameters. Each traceback contains captured warnings at wakes.py:448: `sf-a2-fix1 safety guard: external gh command`. The helper at test_review_r2_repro.py:59–70 writes a real shell executable named gh; production query_prs requests its read-only GraphQL result, and the guard refuses that external executable. Four cases therefore never latch their missing-PR observation; two never reach the claim point that should raise SimulatedCrash. These are guard-baseline fixture failures, not evidence of a review-subject regression.

Scratch-only `sf_fix1_graphql.py` replaces only those two functions' imported fake_gh fixture alias. It intercepts only the exact GraphQL argv, query-only synthetic o/r#1 request and fixed missing-PR response. It preserves production query_prs parsing and durable wake/restart logic, starts no gh process and leaves all other commands under the unchanged strict guard. This is a test-lane boundary substitution, not a repository test/product edit. The prepared proof has six predicate checks and no runner; outcomes remain pending parent execution at this point.

Extras (exact ledger L42): 82 passed, 107 skipped, 1 failed; launcher elapsed32.83s (pytest's own summary reports32.62s). The remaining installed-publish-hook case cannot run under the external-command guard; the parent chose no guard exemption or rerun because the self-test includes write-shaped boundaries. Mixed safe nodes (L43):99 passed,2.37s. Do not add these overlapping scopes to the primary case aggregate.


Final targeted outcomes (launcher times from exact ledger):

- Final required check01 found two new CLI ProtocolError constructions missing reason metadata (cli.py207 and1782). The parent added `reason="refused-args"`; changed CLI files then passed330 cases in11.72s.
- The callback proof passed six inert checks with zero external commands; actual artifact `graphql-callback-proof.json`.
- First R5 callback attempt passed11/failed6 in0.58s because module-name qualification did not activate under pytest's import mode. The helper now matches the exact resolved module file path and logs activation; this was a scratch infrastructure correction, not a product change.
- Second full R5 file recheck passed all17 in0.50s. The exact callback replaced six opaque shell-fixture boundaries while preserving production query_prs parsing, wake observation, crash/restart and durable acceptance. Every callback log record reports zero external commands; external gh remains prohibited by the unchanged guard.
- Final required four files then passed447 in19.45s (ledger L48). The one extras installed-hook environment failure remains disclosed; no write-shaped self-test exemption was granted.
