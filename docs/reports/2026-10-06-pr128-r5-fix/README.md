# PR #128 round-five fixes

Checkpoint: `5a11c73edc30470be18a9be57d6cc23294082709`. Changes are confined to the assigned worktree.

| Finding | Change | Regression proof |
| --- | --- | --- |
| P2: absent fields appear as null grants in history | [ApprovalPresentation.swift:56](../../../app/Sources/ApprovalPresentation.swift#L56) removes top-level display nulls only when the request is unloaded. Line 84 prevents an empty root row when nothing remains. Loaded values and nested nulls retain the round-four behavior. | The unchanged reviewer's [real-adapter regression](../../../tests/frontend/test_visual_round_five.py#L136) fails on checkpoint production sources and passes with the fix. Generated tests cover 1,000 loaded requests and 1,000 real-adapter summaries. [Round-four null tests](../../../tests/frontend/test_visual_round_four.py#L23) still require nested nulls and every loaded null. |
| P2: question history regression assertion required the bug | [test_visual_round_two.py:141](../../../tests/frontend/test_visual_round_two.py#L141) requires no placeholder text and a 184 pt `question-two` history card. | Offscreen OCR and fitting-height assertions pass. The [Codex history assertion](../../../tests/frontend/test_visual_round_five.py#L360) pins 168 pt and compares against the same card without placeholders. Notion Salary and Owner removals remain visible on card, sheet and unloaded history in both themes. |
| P3: 29 missing evidence links | The [visual-review report](../2026-10-04-visual-review-fixes/README.md#run-evidence) and [gallery](../2026-10-04-visual-review-fixes/GALLERY.md) name historical local commit `5e73e4ea402895f43ca51157fdacce4052917695` as the evidence location, replacing links to absent artifacts. Every named missing target was verified in that commit. | [Report hygiene](../../../tests/frontend/test_visual_report_hygiene.py#L28) checks local links in every `docs/reports/*/` directory. Restoring the checkpoint report fails this test. The visual reports retain their separate artifact policy. |

Three mutations were killed by assertion failures, with no compiler/setup errors or skips. Original file bytes were restored after each run.

| Mutation | Failed assertions | Passing controls | Foreground seconds |
| --- | --- | --- | --- |
| Restore checkpoint approval presentation | 2 | 0 | 84.73 |
| Drop nulls recursively during flattening | 10 | 5 | 73.19 |
| Restore checkpoint README and GALLERY links | 1 | 11 | 17.65 |

All **205 targeted cases passed**, with zero final failures, errors or skips. Counts exclude repeated cases, mutation runs and the superseded artifact-policy checks.

| Targeted file | Passed |
| --- | --- |
| `test_visual_round_two.py` | 85 |
| `test_visual_round_three.py` | 28 |
| `test_visual_round_four.py` | 27 |
| `test_visual_round_five.py` | 9 |
| `test_visual_review_fixes.py` | 43 |
| `test_visual_report_hygiene.py` | 13 |

Validation completed in targeted, serial foreground slices. Every test invocation sets `TMPDIR=$PWD/.fix-tmp/`, uses one pytest process without `-n`, and has a 560-second deadline. A private source-hash cache compiles all app Swift sources with the same model/view test flags into a testing module, then links the probe bodies with only `@testable import` prepended. The reviewer's committed Swift probe matches the supplied patch byte for byte, and every supplied Python function and assertion matches its AST. Render batches select only their asserted fixture IDs, without changing any selected request, view or assertion. Every backing window stays unshown.

Two earlier combined slices reached their 560-second deadlines; their exact owned process groups were terminated and confirmed gone. Every required case was subsequently completed in smaller slices; a collection-to-results audit confirms none is missing. An initial attempt to apply the visual-only artifact policy to older reports failed seven cases; that unrelated scope expansion was removed, while link checking remains universal. Generated run files stay in `.fix-tmp/` and are not committed. The full frontend sweep remains for CI and the next reviewer.

Delivery uses `.git-local` on `refs/heads/fix/pr128-r5` because shared worktree git metadata is read-only. Fix commits are `87a5e66801ee407125712d37d3d8902003f12613` (P2) and `4a49cb36bc5935a2847e2cc787434b1c422a248b` (P3), each with the required co-author trailer. The final report commit and verified bundle head will be named in the delivery response. The bundle path is `docs/reports/2026-10-06-pr128-r5-fix.bundle`, with prerequisite checkpoint `5a11c73edc30470be18a9be57d6cc23294082709`.

No live `~/.subfleet`, daemon, installed app, Application Support, caller checkout or default policy was written. Nothing was pushed, no desktop window was opened, and all owned test/compiler/OCR groups exited. No sub-agents were used.
