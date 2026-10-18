"""Replay past merges of `docs/acceptance-contract.md` under each layout (defect D-MRG3).

For every two-parent merge commit reachable from the remote branches in a date range,
take the contract at the merge base and at both parents, and when both sides changed it,
merge the three with `git merge-file` as they were, with the changes list moved out
(C-20.7), and with the list out and the current layout (`tools/contract_layout.py`). It
counts the conflicting merges and hunks for each. For the current layout it also sorts
the hunks that remain: both sides inserted at one place, mergeable word by word
(`tools/contract_merge.py --words`), or a word changed on both sides.

    python3 tools/contract_replay.py --since 2026-10-01 --until 2026-10-14T11:00:00Z \\
        --tsv docs/reports/2026-10-14-d-mrg3-contract-replay.tsv

Merges with more than one merge base are skipped (their base is not one commit), and so
are merges where either side left the contract as the base had it. Branches deleted
after merging take their merge commits out of `--remotes`, so a later run of the same
range can see fewer merges.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import contract_history as history  # noqa: E402
from tools import contract_layout as layout    # noqa: E402
from tools import contract_merge as merge_tool  # noqa: E402

PATH = "docs/acceptance-contract.md"
LAYOUTS = {
    "as it was": lambda text: text,
    "history out": history.without_list,
    "history out, current layout": merge_tool.normalize,
}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _contract(rev: str) -> str | None:
    result = subprocess.run(["git", "show", f"{rev}:{PATH}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def kinds(merged: str) -> dict[str, int]:
    """The conflict hunks of one diff3 merge, sorted by what would settle them."""
    found = {"both inserted": 0, "word-mergeable": 0, "same word": 0}
    for _, _, ours, base, theirs in merge_tool._hunks(merged):
        if not any(line.strip() for line in base):
            found["both inserted"] += 1
        elif merge_tool.merge_words("\n".join(ours), "\n".join(base), "\n".join(theirs)) is None:
            found["same word"] += 1
        else:
            found["word-mergeable"] += 1
    return found


def replay(since: str, until: str | None):
    args = ["rev-list", "--merges", f"--since={since}", "--remotes=origin"]
    if until:
        args.insert(3, f"--until={until}")
    for merge in _git(*args).split():
        parents = _git("rev-list", "--parents", "-n1", merge).split()[1:]
        if len(parents) != 2:
            continue
        bases = subprocess.run(["git", "merge-base", "--all", *parents], capture_output=True,
                               text=True).stdout.split()
        if len(bases) != 1:
            continue
        base, ours, theirs = _contract(bases[0]), _contract(parents[0]), _contract(parents[1])
        if None in (base, ours, theirs) or base in (ours, theirs) or ours == theirs:
            continue
        row = {"merge": merge, "subject": _git("log", "-1", "--format=%s", merge).strip()}
        for name, shape in LAYOUTS.items():
            merged, conflicts = merge_tool._merge_file(shape(ours), shape(base), shape(theirs),
                                                       "--diff3")
            row[name] = conflicts
        row.update(kinds(merged))         # the last layout's merge: the current one
        yield row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", required=True, help="first commit date, as git takes it")
    parser.add_argument("--until", help="last commit date, as git takes it")
    parser.add_argument("--tsv", type=Path, help="also write one row per merge here")
    args = parser.parse_args(argv)
    rows = list(replay(args.since, args.until))
    if args.tsv:
        columns = ["merge", *LAYOUTS, "both inserted", "word-mergeable", "same word", "subject"]
        lines = ["\t".join(columns)]
        lines += ["\t".join(str(row[c]).replace("\t", " ") for c in columns) for row in rows]
        args.tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)} merges in which both sides changed {PATH}")
    for name in LAYOUTS:
        merges = sum(1 for row in rows if row[name])
        hunks = sum(row[name] for row in rows)
        print(f"  {name:30s} {merges:4d} conflicting merges, {hunks:4d} hunks")
    left = [row for row in rows if row["history out, current layout"]]
    print("  hunks left in the current layout: "
          + ", ".join(f"{k} {sum(r[k] for r in left)}"
                      for k in ("both inserted", "word-mergeable", "same word")))
    print(f"  merges a person settles even with --words: "
          f"{sum(1 for r in left if r['both inserted'] or r['same word'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
