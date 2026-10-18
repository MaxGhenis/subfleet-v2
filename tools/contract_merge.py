"""Merge `docs/acceptance-contract.md` three ways in its current layout (defect D-MRG3).

Run it after `git merge` stops with the contract conflicted, typically while merging the
release line into a branch from before the history moved out of the contract. It reads
the three versions Git staged (base, ours, theirs), not the conflicted working copy, and
brings each to the current layout:

- the changes section is the pointer to `docs/contract-history/`, and each entry a side
  added to the old list is written there as a file of its own (C-20.7);
- every paragraph is one sentence per line (`tools/contract_layout.py`).

Then `git merge-file` merges the three and the result is written to the contract. Nothing
is staged; the command prints what it wrote and what to add.

    python3 tools/contract_merge.py                 # mid-merge, in the repository
    python3 tools/contract_merge.py --words         # also merge leftover hunks word by word
    python3 tools/contract_merge.py --dry-run       # say what it would do; write nothing

`--words` merges a hunk that still conflicts one word at a time, and only when no word
was changed by both sides (the hub's `contract_merge.py` did this per clause). Read what
it merged: two edits to one sentence can each be right and still disagree.

Exit status: 0 merged cleanly; 1 a conflict or an old-list edit needs a person (markers
written in diff3 style); 2 usage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import contract_history as history  # noqa: E402
from tools import contract_layout as layout    # noqa: E402

PATH = "docs/acceptance-contract.md"
HISTORY = "docs/contract-history"
LABELS = ("ours", "base", "theirs")


@dataclass
class Merged:
    text: str
    conflicts: int
    #: (side, entry text) for each entry a side added to the old list, in its order.
    added: list[tuple[str, str]] = field(default_factory=list)
    #: (side, entry text) for each base entry a side edited or removed from the old list.
    dropped: list[tuple[str, str]] = field(default_factory=list)
    word_merged: int = 0


def normalize(text: str) -> str:
    """A contract in the current layout: no changes list, one sentence per line."""
    return layout.relayout(history.without_list(text))


def _merge_file(ours: str, base: str, theirs: str, *extra: str) -> tuple[str, int]:
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for name, text in zip(LABELS, (ours, base, theirs)):
            path = os.path.join(folder, name)
            Path(path).write_text(text, encoding="utf-8")
            paths.append(path)
        result = subprocess.run(
            ["git", "merge-file", "-p", *extra,
             "-L", "ours", "-L", "base", "-L", "theirs", *paths],
            capture_output=True, text=True)
    if result.returncode < 0 or result.returncode > 127:
        raise RuntimeError(f"git merge-file failed: {result.stderr.strip()}")
    return result.stdout, result.returncode


_SPACE = "\x1f"        # a newline inside a whitespace token, while tokens are lines


def _tokens(text: str) -> str:
    return "".join(t.replace("\n", _SPACE) + "\n" for t in re.findall(r"\S+|\s+", text))


def _untokens(text: str) -> str:
    return "".join(text.split("\n")).replace(_SPACE, "\n")


def merge_words(ours: str, base: str, theirs: str) -> str | None:
    """One hunk merged a word at a time, or None when a word was changed by both sides."""
    if ours == theirs or base == theirs:
        return ours
    if base == ours:
        return theirs
    merged, conflicts = _merge_file(_tokens(ours), _tokens(base), _tokens(theirs))
    return None if conflicts else _untokens(merged)


def _hunks(merged: str) -> list[tuple[int, int, list[str], list[str], list[str]]]:
    """diff3 hunks: (first line, line after, ours, base, theirs)."""
    lines, found, i = merged.split("\n"), [], 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            i += 1
            continue
        mid_base = next(k for k in range(i, len(lines)) if lines[k].startswith("||||||| "))
        mid = next(k for k in range(mid_base, len(lines)) if lines[k] == "=======")
        end = next(k for k in range(mid, len(lines)) if lines[k].startswith(">>>>>>> "))
        found.append((i, end + 1, lines[i + 1:mid_base], lines[mid_base + 1:mid],
                      lines[mid + 1:end]))
        i = end + 1
    return found


def merge(base: str, ours: str, theirs: str, *, words: bool = False) -> Merged:
    """The contract merged in the current layout, and what moved to the history folder."""
    base_list = history.listed_entries(base)
    added, dropped = [], []
    for side, text in (("ours", ours), ("theirs", theirs)):
        side_list = history.listed_entries(text)
        if not side_list:
            continue                     # this side already keeps no list
        known = set(base_list)
        added += [(side, e) for e in side_list if e not in known]
        kept = set(side_list)
        dropped += [(side, e) for e in base_list if e not in kept]
    merged, conflicts = _merge_file(normalize(ours), normalize(base), normalize(theirs),
                                    "--diff3")
    result = Merged(merged, conflicts, added, dropped)
    if conflicts and words:
        lines = merged.split("\n")
        out, done = [], 0
        for start, end, o, b, t in _hunks(merged):
            joined = merge_words("\n".join(o), "\n".join(b), "\n".join(t))
            out.extend(lines[done:start])
            if joined is None:
                out.extend(lines[start:end])
            else:
                out.extend(joined.split("\n") if joined else [])
                result.word_merged += 1
                result.conflicts -= 1
            done = end
        out.extend(lines[done:])
        result.text = "\n".join(out)
    if not result.conflicts:
        result.text = layout.relayout(result.text)
    return result


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def _version(stage: int, rev: str) -> str:
    staged = _git("show", f":{stage}:{PATH}", check=False)
    if staged.returncode == 0:
        return staged.stdout
    return _git("show", f"{rev}:{PATH}").stdout


def theirs_branch(merge_msg: str) -> str:
    """The branch a merge message names (`Merge branch 'x'`, `... 'origin/x'`), or ''."""
    match = re.search(r"'([^']+)'", merge_msg.split("\n", 1)[0])
    if not match:
        return ""
    name = match.group(1)
    return name.split("/", 1)[1] if name.startswith("origin/") else name


def write_entries(root: Path, added: list[tuple[str, str]], slugs: dict[str, str],
                  today: str, dry_run: bool) -> list[tuple[Path, str]]:
    """Write each added entry as its own file; skip one whose text is already a file."""
    existing = {p.read_text(encoding="utf-8").strip("\n")
                for p in root.rglob("*.md") if p.name != "README.md"} if root.exists() else set()
    written = []
    for side, text in added:
        if text in existing:
            continue
        stated = history.DATE.search(text)
        date = stated.group(1) if stated else today
        path = history.free_path(root, date, slugs[side])
        if not dry_run:
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        existing.add(text)
        written.append((path, "" if stated else "gives no date; add one, first"))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--words", action="store_true",
                        help="merge each remaining hunk a word at a time")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--ours-branch", help="entries ours added are named after this branch")
    parser.add_argument("--theirs-branch", help="entries theirs added are named after this")
    args = parser.parse_args(argv)

    top = _git("rev-parse", "--show-toplevel", check=False)
    if top.returncode:
        print("not in a Git repository", file=sys.stderr)
        return 2
    os.chdir(top.stdout.strip())
    merge_head = Path(_git("rev-parse", "--git-path", "MERGE_HEAD").stdout.strip())
    if not merge_head.exists():
        print("not mid-merge (no MERGE_HEAD)", file=sys.stderr)
        return 2
    base_rev = _git("merge-base", "HEAD", "MERGE_HEAD").stdout.split()[0]
    base, ours, theirs = (_version(1, base_rev), _version(2, "HEAD"),
                          _version(3, "MERGE_HEAD"))
    if history.listed_entries(ours) and history.listed_entries(theirs):
        print("both sides still list their changes in the contract; this merge is not the"
              " move, so merge it as before", file=sys.stderr)
        return 2
    result = merge(base, ours, theirs, words=args.words)

    msg_path = Path(_git("rev-parse", "--git-path", "MERGE_MSG").stdout.strip())
    msg = msg_path.read_text(encoding="utf-8") if msg_path.exists() else ""
    slugs = {
        "ours": history.branch_slug(args.ours_branch
                                    or _git("branch", "--show-current").stdout.strip() or "ours"),
        "theirs": history.branch_slug(args.theirs_branch or theirs_branch(msg) or "theirs"),
    }
    written = write_entries(Path(HISTORY), result.added, slugs,
                            dt.date.today().isoformat(), args.dry_run)
    if not args.dry_run:
        Path(PATH).write_text(result.text, encoding="utf-8")

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {PATH}: {result.conflicts} conflict(s) left"
          + (f", {result.word_merged} hunk(s) merged word by word" if result.word_merged
             else ""))
    for path, note in written:
        print(f"{verb} {path}" + (f" ({note})" if note else ""))
    for side, text in result.dropped:
        print(f"{side} edited or removed this old-list entry; the migrated list is not"
              f" edited, so carry the change by hand: {text[:160]}", file=sys.stderr)
    if not args.dry_run and not result.conflicts:
        print("then: git add " + " ".join([PATH, *(str(p) for p, _ in written)]))
    return 1 if result.conflicts or result.dropped else 0


if __name__ == "__main__":
    sys.exit(main())
