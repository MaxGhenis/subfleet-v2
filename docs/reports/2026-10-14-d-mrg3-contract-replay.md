# D-MRG3: the contract's history out, one sentence per line

Measured on 2026-10-14 for C-20.6 (PR #218, base release/217 at 3f50142ac).

## The defect

`docs/acceptance-contract.md` kept its changes as a list near the top, and every pull request prepended to it.
Each clause was one line, up to 18,000 characters long.
Git merges line by line and treats changes to adjacent lines as a conflict.
So each merge into release/217 made every other open pull request conflict, GitHub ran no CI on them (D-CI1), and each needed a release merge and a fresh CI run.

## What changed

- The 157 list entries are `docs/contract-history/version-3/001.md` to `157.md`, in order.
  New entries are `docs/contract-history/<YYYY-MM-DD>-<branch>.md`, one file per entry.
- The contract starts a line at each sentence and after each semicolon (`tools/contract_layout.py`).
- `tools/contract_merge.py` is for the one release merge each open pull request needs after the move.

## Checks of the move

- **Migration.** The 157 files, concatenated in order, equal the `- ` lines of the old section with their markers removed:
  `git show 3f50142ac:docs/acceptance-contract.md | sed -n '8,177p' | grep '^- ' | sed 's/^- //'` diffs empty against `cat docs/contract-history/version-3/*.md`.
  The SHA-256 of the texts joined by newlines is `5a72c934…bea22d` both ways (`MIGRATED_SHA256`).
  The old section held nothing but list items and blank lines.
- **Clauses.** All 252 clauses of 3f50142ac read the same in the new contract when line breaks and runs of spaces are read as one space (`contract_layout.clauses`). C-8.5 is among them; its bold marker carries a title. C-20.6 is the only new clause.
- **Rendering.** The laid-out contract and the same contract with each paragraph on one line render to the same HTML, whitespace collapsed, in markdown-it-py 3.0.0 (CommonMark with tables) and in cmarkgfm 2024.11.20 (cmark-gfm, GitHub's renderer).
- **Properties.** `tests/unit/test_contract_layout.py` checks with Hypothesis that relayout is idempotent, keeps every paragraph's words and every other line, never starts a Markdown block on a line it makes, and keeps every clause's text.
  Each property also passed 4,000 random examples once outside the suite.
  Hypothesis found two bugs while these were written, both fixed:
  - a setext underline began a paragraph, so the next line could join it and the heading would be lost;
  - `clauses()` stopped at a nested paragraph's unindented continuation line, which relayout indents.

## Replaying past merges

`python3 tools/contract_replay.py --since 2026-10-01 --until 2026-10-14T10:05:00Z --tsv docs/reports/2026-10-14-d-mrg3-contract-replay.tsv` replayed every two-parent merge on the remote branches in that range in which both sides changed the contract.
It skipped merges with more than one merge base.
The rows are in the `.tsv` beside this report.

| Layout | Merges that conflict on the contract | Conflict hunks |
|---|---|---|
| As it was | 128 of 185 | 182 |
| History moved out | 71 | 95 |
| History out, one sentence per line, a line after each semicolon (this change) | 37 | 48 |

Of the 48 hunks left in the new layout:
- 2 are both sides inserting at one place;
- 22 merge word by word (`contract_merge.py --words`);
- 24 change a word on both sides.

So 23 of the 185 merges would still need a person.
15 of those 23 merged branches of the stacked admission-fence work, which edit the same clauses.

Two other layouts were measured with an earlier version of the replay, over 186 merges: one sentence per line without the semicolon break (46 conflicting merges), and a blank line between clauses on top of this change (35).
The blank line was not adopted, because it changes how the list renders and saves 3 more merges.

## The one release merge each open pull request needs

The heads of the 47 open release/217 pull requests were fetched at 11:30Z on 2026-10-14.
Each was dry-run merged with this branch, using `contract_merge.merge` on the three versions of the contract.
43 of them changed the contract.
None edited an old list entry.

- #198, #202, #204, #205, #206, #207, #210, #211, #212, #213, #214, #216 and #217 merge with no contract conflict, and #200 with `--words`.
- #202 had 2 contract conflicts with release/217 that day, and #204 had 1; after the move both merge cleanly.
- For 40 of the 43, the contract conflicts after the move and the tool were no more than that day's conflicts with release/217, and usually fewer.
  The 3 exceptions are #82, #84 and #92, stale since September.
  They conflict on many code files as well, and the finer layout splits one long-line conflict into several sentence-level hunks.
- Outside the contract, each pull request conflicts on the same files with this branch as with release/217 (`git merge-tree`), so the move adds no other conflict.
