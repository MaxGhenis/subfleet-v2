# Contract history

Each change to `docs/acceptance-contract.md` is recorded here, one file per entry (C-20.7).
Until 2026-10-14 the contract kept these entries as a list under "Changes in version 3", and every pull request prepended to it.
So each merge into the release line made every other open pull request conflict on that list (defect D-MRG3).
GitHub runs no CI on a conflicting pull request, so each one then needed a merge and a fresh CI run before it could land.
A file per entry, named after the branch that adds it, means two pull requests write the same path only when their branch names differ in nothing but case or punctuation (`fix/a_b` and `fix/a-b`).
Git then reports an add/add conflict on that one file, and renaming one of the two settles it.

## Adding an entry

A pull request that changes a clause adds one file:

    docs/contract-history/<YYYY-MM-DD>-<branch>.md

- `<YYYY-MM-DD>` is the entry's date, and the text gives it as its first date.
- `<branch>` is the pull request's branch, lowercased, with each run of other characters written as one hyphen: `fix/c6-10-verdict` gives `fix-c6-10-verdict`.
  A second entry from the same branch on the same date adds `-2`, then `-3`.
- The text is one paragraph, written as the contract's list items were: the clauses it changes, then the date and why in parentheses (the incident, defect, review or decision), then what changed.
  For example: `C-9.6 (2026-10-13, defect D-HOLD1): an operator hold keeps its own closure row.`
- No list marker and no heading: the index adds the marker.
  One sentence per line is welcome, as in the contract.

`python3 tools/contract_history.py new` creates the file for the current branch with today's date, for you to fill in.
A later round on the same pull request may edit its own entry or add another.
Never edit another pull request's entry or a migrated one.

Change the contract itself one sentence per line: `python3 tools/contract_layout.py --write` lays it out, and `tests/unit/test_contract_layout.py` fails until it is.

## Reading the history

`python3 tools/contract_history.py` prints every entry as one Markdown list: the dated entries newest first (entries of the same date by file name), then the 157 entries the contract listed before the move, in their order.
The index is generated and not checked in, because a checked-in index would be one more file every pull request edits.

`version-3/001.md` to `version-3/157.md` are that list, one entry per file, each the text of one list item without its leading `- `, unchanged.
`001` was the list's first line.
The blank lines that split the list into groups marked only the merges that brought each group, and are not kept.
Two pairs of entries are near duplicates that earlier merges kept, `033` and `040`, and `035` and `037`;
they stay as they were.

## Checks

`python3 tools/contract_history.py check`, and `tests/unit/test_contract_history.py` in CI, check that:

- every file here is this README, a dated entry, or a migrated entry, named as above;
- each dated entry is one paragraph, gives its file's date first, and cites a clause the contract defines;
- the migrated entries are `001` to `157`, each once, and their text is the list's, byte for byte (by SHA-256);
- the contract's changes section holds only its pointer here.

## Merging a branch from before the move

A branch that still lists its changes in the contract conflicts with the move once, on the list and on every paragraph it changed.
Merge the release line into it, and when the contract conflicts run:

    python3 tools/contract_merge.py

It reads the three versions Git staged for the contract (base, the branch, the release line), not the conflicted file, brings each to the current layout, and merges the three with `git merge-file`.
Each entry the branch added to the old list becomes a file here, named after the branch; on a detached HEAD, name the branch with `--ours-branch`.

- It stages every entry file of the merge, so none can be left out of the merge commit, and prints the `git add` for the contract: at once when it merged cleanly, after you resolve what is left otherwise.
- A conflict it cannot settle is left with diff3 markers, longer than any marker-shaped line in the contract, and it exits 1.
  `--words` also merges each remaining hunk one word at a time when no word was changed by both sides;
  read what it merged.
- It never writes over a person's work.
  It refuses (exit 2) unless the contract is unmerged with all three stages, so a resolved and added contract is safe.
  It also refuses over a contract changed since it last wrote it, or one with neither Git's markers nor its last result, and it keeps an entry file a person edited.
  `--force` rebuilds the contract anyway; the file as it was is copied to `.git/contract-merge-backup.md` (in the worktree's Git directory) first, every time.
- An old-list entry the branch edited is a new round of that entry.
  If another pull request on the same work landed that entry first, the round goes into its file;
  if it is one of the migrated entries, which are not edited, it is named for a person and nothing is written.
  An entry the branch removed, and text in the old section that is not a list item, are named for a person too, and the tool exits 1.
- `--dry-run` says what it would write and stage.

`python3 tools/contract_dry_run.py` tries this merge on many pull request heads at once, writing nothing (see its help).
