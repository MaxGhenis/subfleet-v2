# D-MRG3: the contract's history out, one sentence per line

Measured on 2026-10-14, and revised on 2026-10-18 after review r1b, for C-20.7 (PR #218).

## The defect

`docs/acceptance-contract.md` kept its changes as a list near the top, and every pull request prepended to it.
Each clause was one line; the longest, C-8.4, ran to 18,180 characters.
Git merges line by line and treats changes to adjacent lines as a conflict.
So each merge into release/217 made every other open pull request conflict, GitHub ran no CI on them (D-CI1), and each needed a release merge and a fresh CI run.

## What changed

- The 157 list entries are `docs/contract-history/version-3/001.md` to `157.md`, in order.
  New entries are `docs/contract-history/<YYYY-MM-DD>-<branch>.md`, one file per entry.
  The entries release/217 added to its list before the move (12, one of them listed twice) are 11 files named after the pull request that brought each in.
- The contract starts a line at each sentence and after each semicolon (`tools/contract_layout.py`).
- `tools/contract_merge.py` is for the one release merge each open pull request needs after the move; `tools/contract_dry_run.py` tries it on many heads at once.
- The history clause is C-20.7: #198 took C-20.6 for D-ST1 while this change was open.

## Checks of the move

- **Migration.** The 157 files, concatenated in order, equal the `- ` lines of the old section with their markers removed:
  `git show 3f50142ac:docs/acceptance-contract.md | sed -n '8,177p' | grep '^- ' | sed 's/^- //'` diffs empty against `cat docs/contract-history/version-3/*.md`.
  The SHA-256 of the texts joined by newlines is `5a72c934…bea22d` both ways (`MIGRATED_SHA256`).
  The old section held nothing but list items and 13 blank lines.
- **Clauses.** All 252 clauses of 3f50142ac read the same in the contract at 1e65b60d9 when line breaks and runs of spaces are read as one space (`contract_layout.clauses`), C-8.5 among them; C-20.7 is the only clause this change adds.
  The merge of release/217 at 3fbc4b7c5 changed none of its 255 clauses.
- **Rendering.** The laid-out contract and the same contract with each paragraph on one line render alike in markdown-it-py 3.0.0 (CommonMark with tables) and cmarkgfm 2024.11.20 (cmark-gfm, GitHub's renderer).
  "Alike" is `tests/markdown_signature.py`: every tag, every attribute and the text of code exactly, other text with each run of whitespace as one space.
  `tests/unit/test_contract_layout.py` checks this for the contract and, with Hypothesis, for generated documents; markdown-it-py is a dev dependency for it.
- **What relayout leaves alone.** Review r1b found inputs whose rendering relayout changed: thematic breaks after a semicolon, link reference definitions, mixed fences, indented code in list items, hard breaks, tables without a leading pipe, `<pre>` blocks, code spans and link titles across lines, escaped backticks.
  The block reader now follows CommonMark's fence, HTML block (kinds 1-7) and GFM table rules, and leaves a paragraph as written wherever a line break may carry meaning.
  It leaves everything from a fence or HTML block opened inside a list item to the end of the document as written, because it does not track where the item ends.
  The reviewer's 47 cases render alike in both renderers.
  While this was being fixed, generated documents found twelve more such inputs (eleven kinds); each kind is fixed and is among the test's examples.
  On 2026-10-18, two runs outside the suite of 20,000 random generated documents each found no difference in either renderer; their seeds were not recorded.

## Replaying past merges

The sample is the 185 merges in `docs/reports/2026-10-14-d-mrg3-contract-replay.tsv`, one row per merge with its commit.
They are every two-parent merge on the remote branches, committed before 2026-10-14T10:05:00Z and selected with `--since 2026-10-01`, in which both sides changed the contract.
Git read that bare date at the time of day of the run, so the sample begins on 2026-10-01 but not at midnight (review r1b, finding 15).
`tools/contract_replay.py` now reads a bare date as midnight UTC, and replays a fixed sample exactly:

    python3 tools/contract_replay.py --merges-from docs/reports/2026-10-14-d-mrg3-contract-replay.tsv

| Layout | Merges that conflict on the contract | Conflict hunks |
|---|---|---|
| As it was | 128 of 185 | 182 |
| History moved out | 71 | 95 |
| History out, one sentence per line, a line after each semicolon (this change) | 37 | 48 |

The reviewer reproduced every cell of the TSV.
On 2026-10-18 the command above, run with the revised layout code, gave the same rows and totals.

Of the 48 hunks left in the new layout:
- 2 are both sides inserting at one place;
- 22 merge word by word (`contract_merge.py --words`);
- 24 do not merge word by word either.
  This is what the tool does, not why: an insertion beside a deletion of the same words lands here too.

So 23 of the 185 merges would still need a person.
13 of those 23 merge subjects name a fence branch (`awk -F'\t' 'NR>1 && ($5>0 || $7>0)' docs/reports/2026-10-14-d-mrg3-contract-replay.tsv | grep -ci fence`): merges between branches of the stacked admission-fence work, which edit the same clauses.

Other layouts were tried with an exploratory script kept outside the repository: one sentence per line without the semicolon break, and a blank line between clauses.
Its numbers are not reported here.
The blank line was not adopted in any case, because it changes how the list renders.

## The one release merge each open pull request needs

`docs/reports/2026-10-18-d-mrg3-open-prs.tsv` is `tools/contract_dry_run.py` on the 47 open release/217 pull requests other than #218, with their heads as of 2026-10-18 16:10Z.
It ran with `--target HEAD` (this branch at 1e65b60d9, after the merge of release/217 at 3fbc4b7c5) and `--baseline origin/release/217`.

- 36 of them conflict on the contract with release/217 as it is (`baseline conflicts`), and 44 would with the move as plain Git merges it (`git conflicts`).
- After `tools/contract_merge.py`, 25 have no contract conflict left; with `--words`, 33.
  Among the 32 that are not drafts, `--words` leaves none for 23, including every one opened since #204 (#204, #212, #219, #222, #223, #226).
- None edited or removed an old-list entry, and none has text in the old section that is not a list item; together they would write 73 entries.
- The tool leaves more hunks than the head has with release/217 today for 5 of the 47: #78, #82, #84, #92 and #131.
  All five also conflict on code files; the finer layout splits one long-line conflict into several sentence-level hunks.
- For every head, the files besides the contract that would conflict with this branch are the ones that conflict with release/217 (`git merge-tree`): the move adds no other conflict.
