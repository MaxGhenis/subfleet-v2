# D-MRG3: the contract's history out, one sentence per line

Measured on 2026-10-14, and revised on 2026-10-18 after reviews r1b and r2, for C-20.7 (PR #218).

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
- **Clauses.** All 252 clauses of 3f50142ac read the same at 8c0414587, before release/217 was merged in, when line breaks and runs of spaces are read as one space (`contract_layout.clauses`); C-8.5, whose bold carries a title, is among them.
  C-20.7 is the only clause this change adds.
  The merge of release/217 at 3fbc4b7c5 changed none of its 255 clauses.
- **Rendering.** The laid-out contract and the same contract with each paragraph on one line render alike in markdown-it-py (3.0.0 by hand; 4.2.0, the locked dev dependency, in the test) and in cmarkgfm 2024.11.20 (cmark-gfm, GitHub's renderer).
  "Alike" is `tests/markdown_signature.py`: every tag, every attribute and the text of code exactly, other text with each run of whitespace as one space.
  `tests/unit/test_contract_layout.py` checks this for the contract and, with Hypothesis, for generated documents.

## What relayout leaves alone

Reviews r1b and r2 found inputs whose rendering relayout changed, none of them in the contract.
The block reader now follows CommonMark's fence, HTML block (kinds 1-7) and GFM table rules, reads footnote definitions as blocks as cmark-gfm does, and counts indentation in spaces.
Inline, it reads code spans, autolinks and raw HTML left to right with their CommonMark precedence and honours escapes, and it never breaks a line inside any of them or inside a link destination or title.
It leaves as written:

- a paragraph whose line breaks may carry meaning (a hard break, a tab, a raw `<pre>`-like element, a span that crosses a line);
- a paragraph inside an open raw `<pre>`, or one that may continue a list item, definition or quote, or a block that left a span open (until the next blank line);
- everything from a fence or HTML block opened inside a list item, or a line indented with a tab, to the end of the document;
- a document with carriage returns.

Every input the reviews and the fuzzing found is an example in the test, with what relayout now does with it.
`tools/contract_layout_fuzz.py`, adapted from the fuzzer review r2 wrote, renders each generated document in markdown-it-py and in cmark-gfm with footnotes on:

    uv run --no-project --with markdown-it-py==4.2.0 --with cmarkgfm==2024.11.20 \
        python tools/contract_layout_fuzz.py exotic 1 3000

At f85ea2a5b, seeds 1 to 12 of `exotic` and 1 to 6 of `real`, 3,000 documents each, found no difference.

## Replaying past merges

The sample is the 185 merges in `docs/reports/2026-10-14-d-mrg3-contract-replay.tsv`, one row per merge with its commit: two-parent merges on the remote branches, committed before 2026-10-14T10:05:00Z, in which both sides changed the contract.
They were selected with `--since 2026-10-01`, which Git read at the time of day of the run, so the sample begins at about 12:06Z that day; at least two such merges from earlier on 2026-10-01 (a914f38d8, 16f6bd196) are not in it (review r2).
`tools/contract_replay.py` now reads a bare date as midnight UTC, and replays a fixed sample exactly:

    python3 tools/contract_replay.py --merges-from docs/reports/2026-10-14-d-mrg3-contract-replay.tsv

| Layout | Merges that conflict on the contract | Conflict hunks |
|---|---|---|
| As it was | 128 of 185 | 182 |
| History moved out | 71 | 95 |
| History out, one sentence per line, a line after each semicolon (this change) | 37 | 48 |

Both reviewers reproduced every cell of the TSV, and so did the command above at b624670a4 and at f85ea2a5b.

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

`tools/contract_merge.py` reads the three versions Git staged, normalizes them, and merges; what it does with entries and with a person's work is in `docs/contract-history/README.md`.
In short, it stages every entry file of the merge, never writes over a contract or entry a person changed (`--force` rebuilds the contract after a backup), and puts a new round of an entry another pull request on the same work landed first into that entry's file.
When both had their own round, it writes neither beside the other and names them for a person.
Review r2 replayed the mirror pull requests in a scratch clone, #168 landing first and #167 after it: #167's release merge has two merge bases and both carry their own round of one incident entry.
The tool, as of f85ea2a5b, reads that base as Git built it, writes no second copy, and names #168's file for a person.

`docs/reports/2026-10-18-d-mrg3-open-prs.tsv` is `tools/contract_dry_run.py` on the 48 open release/217 pull requests other than #218, with their heads as of 2026-10-18 19:05Z.
It ran with `--target HEAD` (this branch at f85ea2a5b, after the merge of release/217 at 3fbc4b7c5) and `--baseline origin/release/217`.

- 30 of them conflict on the contract with release/217 as it is (`baseline conflicts`), and 45 would with the move as plain Git merges it (`git conflicts`).
- After `tools/contract_merge.py`, 32 have no contract conflict left; with `--words`, 36.
  Draft status, from `gh pr list` at the same time and not in the TSV: among the 32 that are not drafts, `--words` leaves none for 24, including every one from #204 on (#204, #212, #219, #222, #223, #226).
- None edited or removed an old-list entry, and none has text in the old section that is not a list item; together they would write 74 entries.
  None has more than one merge base with this branch (that comes when a sibling lands first, as above).
- The tool leaves more hunks than the head has with release/217 today for 5 of the 48: #78, #82, #84, #92 and #131.
  All five also conflict on code files; the finer layout splits one long-line conflict into several sentence-level hunks.
- For every head, the files besides the contract that would conflict with this branch are the ones that conflict with release/217 (`git merge-tree`): the move adds no other conflict.
