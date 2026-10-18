"""Replay past merges of `docs/acceptance-contract.md` under each layout (defect D-MRG3).

For every two-parent merge commit taken, read the contract at the merge base and at both
parents, and when both sides changed it, merge the three with `git merge-file` as they
were, with the changes list moved out (C-20.7), and with the list out and the current
layout (`tools/contract_layout.py`). It counts the conflicting merges and hunks for
each. For the current layout it also sorts the hunks that remain by what settles them:

- `both inserted`: the base side of the hunk is empty, both sides added text there;
- `word-mergeable`: `tools/contract_merge.py --words` merges it;
- `still conflicts by word`: a word merge conflicts too. This says what the tool does,
  not why: an insertion beside a deletion of the same words lands here as well.

Merges come from one of two places:

    python3 tools/contract_replay.py --since 2026-10-01 --until 2026-10-14T10:05:00Z
    python3 tools/contract_replay.py --merges-from docs/reports/2026-10-14-d-mrg3-contract-replay.tsv

A range reads every merge on the remote branches committed in it. A bare date means
midnight UTC (git alone would take the current time of day on that date), and the range
used is printed. Branches deleted after merging take their merge commits out of
`--remotes`, so a range can see fewer merges later; `--merges-from` replays exactly the
merges listed (the first column of a `--tsv` file this tool wrote), as long as those
commits exist.

Merges with more than one merge base are skipped (their base is not one commit), and so
are merges where either side left the contract as the base had it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import contract_history as history  # noqa: E402
from tools import contract_merge as merge_tool  # noqa: E402

PATH = "docs/acceptance-contract.md"
LAYOUTS = {
    "as it was": lambda text: text,
    "history out": history.without_list,
    "history out, current layout": merge_tool.normalize,
}
KINDS = ("both inserted", "word-mergeable", "still conflicts by word")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _contract(rev: str) -> str | None:
    result = subprocess.run(["git", "show", f"{rev}:{PATH}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def cutoff(value: str) -> str:
    """A date as git should read it: a bare `YYYY-MM-DD` is midnight UTC that day."""
    return f"{value}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value


def kinds(merged: str, size: int = 7) -> dict[str, int]:
    """The conflict hunks of one diff3 merge, sorted by what would settle them."""
    found = dict.fromkeys(KINDS, 0)
    for _, _, ours, base, theirs in merge_tool._hunks(merged, size):
        if not any(line.strip() for line in base):
            found["both inserted"] += 1
        elif merge_tool.merge_words("\n".join(ours), "\n".join(base), "\n".join(theirs)) is None:
            found["still conflicts by word"] += 1
        else:
            found["word-mergeable"] += 1
    return found


def candidates(since: str | None, until: str | None, listed: Path | None) -> list[str]:
    if listed:
        rows = listed.read_text(encoding="utf-8").split("\n")[1:]
        return [row.split("\t", 1)[0] for row in rows if row.strip()]
    args = ["rev-list", "--merges", f"--since={cutoff(since)}", "--remotes=origin"]
    if until:
        args.insert(3, f"--until={cutoff(until)}")
    return _git(*args).split()


def replay(merges: list[str]):
    for merge in merges:
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
            texts = (shape(ours), shape(base), shape(theirs))
            size = merge_tool.marker_size(*texts)
            merged, conflicts = merge_tool._merge_file(*texts, "--diff3", size=size)
            row[name] = conflicts
        row.update(kinds(merged, size))   # the last layout's merge: the current one
        yield row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--since", help="first commit date (a bare date is midnight UTC)")
    source.add_argument("--merges-from", type=Path, help="replay the merges a --tsv listed")
    parser.add_argument("--until", help="last commit date (a bare date is midnight UTC)")
    parser.add_argument("--tsv", type=Path, help="also write one row per merge here")
    args = parser.parse_args(argv)
    if args.merges_from:
        print(f"merges listed in {args.merges_from}")
    else:
        print(f"merges on the remote branches from {cutoff(args.since)}"
              f" to {cutoff(args.until) if args.until else 'now'}")
    rows = list(replay(candidates(args.since, args.until, args.merges_from)))
    if args.tsv:
        columns = ["merge", *LAYOUTS, *KINDS, "subject"]
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
          + ", ".join(f"{k} {sum(r[k] for r in left)}" for k in KINDS))
    print(f"  merges that still conflict after --words: "
          f"{sum(1 for r in left if r['both inserted'] or r['still conflicts by word'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
