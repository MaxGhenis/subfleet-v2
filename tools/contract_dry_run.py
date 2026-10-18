"""Dry-run the move's merge into open pull requests (defect D-MRG3, C-20.7).

For each pull request head, this asks what `git merge <target>` followed by
`tools/contract_merge.py` would leave, without touching any branch or worktree: Git's
own contract conflicts merging the head with the target, the conflicts left after the
tool's merge and after `--words`, the entries the tool would write, and the other files
that would conflict (`git merge-tree`). With `--baseline` it also counts the contract
conflicts the head has with that ref, as the head stands today. A head with more than
one merge base (`merge bases`) is read from the first, so its row is approximate.

    gh pr list --base release/217 --state open --json number,headRefOid \\
        --jq '.[] | [.number, .headRefOid] | @tsv' > heads.tsv
    python3 tools/contract_dry_run.py heads.tsv --target HEAD --baseline origin/release/217

The heads file has one `<number>\\t<commit>` line per pull request; the commits must be
in this repository (fetch them first). The result is one TSV row per pull request on
standard output.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import contract_merge as merge_tool  # noqa: E402

PATH = "docs/acceptance-contract.md"
COLUMNS = ("pr", "head", "merge bases", "baseline conflicts", "git conflicts",
           "tool conflicts", "--words conflicts", "entries added", "entries edited",
           "entries removed", "unrecognised", "other conflicting files")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def _contract(rev: str) -> str:
    result = _git("show", f"{rev}:{PATH}", check=False)
    return result.stdout if result.returncode == 0 else ""


def _git_conflicts(ours: str, base: str, theirs: str) -> int:
    size = merge_tool.marker_size(ours, base, theirs)
    return merge_tool._merge_file(ours, base, theirs, size=size)[1]


def dry_run(head: str, target: str, baseline: str | None) -> dict[str, object]:
    row: dict[str, object] = {"head": head}
    if baseline:
        base = _git("merge-base", head, baseline).stdout.split()[0]
        row["baseline conflicts"] = _git_conflicts(_contract(head), _contract(base),
                                                   _contract(baseline))
    bases = _git("merge-base", "--all", head, target).stdout.split()
    # With more than one merge base Git merges from a base it builds; this reads the
    # first, so such a row is approximate.
    row["merge bases"] = len(bases)
    ours, older, theirs = _contract(head), _contract(bases[0]), _contract(target)
    row["git conflicts"] = _git_conflicts(ours, older, theirs)
    plain = merge_tool.merge(older, ours, theirs)
    row["tool conflicts"] = plain.conflicts
    row["--words conflicts"] = merge_tool.merge(older, ours, theirs, words=True).conflicts
    row["entries added"] = len(plain.added)
    row["entries edited"] = len(plain.replaced)
    row["entries removed"] = len(plain.dropped)
    row["unrecognised"] = len(plain.unrecognised)
    tree = _git("merge-tree", "--write-tree", "--name-only", "--no-messages", head, target,
                check=False).stdout.split("\n")[1:]
    row["other conflicting files"] = " ".join(f for f in tree if f.strip() and f != PATH)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("heads", type=Path, help="<number>\\t<commit> per line")
    parser.add_argument("--target", default="HEAD", help="what the heads would merge")
    parser.add_argument("--baseline", help="also count contract conflicts with this ref")
    args = parser.parse_args(argv)
    print("\t".join(COLUMNS))
    for line in args.heads.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        number, head = line.split("\t")[:2]
        row = {"pr": number, **dry_run(head, args.target, args.baseline)}
        print("\t".join(str(row.get(column, "")) for column in COLUMNS), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
