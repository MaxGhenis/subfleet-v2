"""Merge `docs/acceptance-contract.md` three ways in its current layout (defect D-MRG3).

Run it after `git merge` stops with the contract conflicted, typically while merging the
release line into a branch from before the history moved out of the contract. It reads
the three versions Git staged for the contract (base, ours, theirs), not the conflicted
working copy, and brings each to the current layout:

- the changes section is the pointer to `docs/contract-history/`, and each entry a side
  added to the old list, whole, is written there as a file of its own (C-20.7);
- every paragraph is one sentence per line (`tools/contract_layout.py`).

Then `git merge-file` merges the three and the result is written to the contract. Nothing
is staged; the command prints what it wrote and every file to add.

    python3 tools/contract_merge.py                 # mid-merge, in the repository
    python3 tools/contract_merge.py --words         # also merge leftover hunks word by word
    python3 tools/contract_merge.py --dry-run       # say what it would do; write nothing

It refuses (exit 2) unless the contract itself is unmerged with all three stages: once
it is resolved and added, or when the merge left it alone, there is nothing to rebuild
and a reviewed resolution is never overwritten. `--words` merges a hunk that still
conflicts one word at a time, only when no word was changed by both sides. Read what it
merged: two edits to one sentence can each be right and still disagree.

Exit status: 0 merged cleanly; 1 a conflict, or an old-list entry edited or removed, or
text in the old changes section it does not recognise, needs a person (conflicts are
written with diff3 markers); 2 usage, or nothing to do.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
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
    #: (side, line) for text in a side's old changes section that is not a list item.
    unrecognised: list[tuple[str, str]] = field(default_factory=list)
    word_merged: int = 0
    marker_size: int = 7


def normalize(text: str) -> str:
    """A contract in the current layout: no changes list, one sentence per line."""
    return layout.relayout(history.without_list(text))


def marker_size(*texts: str) -> int:
    """The shortest conflict marker (7 or more characters) that begins no line of any
    text, so a marker-like line in the contract is never read as a conflict."""
    size = 7
    while any(re.search(rf"^(?:<{{{size}}}|={{{size}}}|>{{{size}}}|\|{{{size}}})", text, re.M)
              for text in texts):
        size += 1
    return size


def _merge_file(ours: str, base: str, theirs: str, *extra: str,
                size: int = 7) -> tuple[str, int]:
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for name, text in zip(LABELS, (ours, base, theirs)):
            path = os.path.join(folder, name)
            Path(path).write_text(text, encoding="utf-8")
            paths.append(path)
        result = subprocess.run(
            ["git", "merge-file", "-p", *extra, f"--marker-size={size}",
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
    size = marker_size(ours, base, theirs)
    merged, conflicts = _merge_file(_tokens(ours), _tokens(base), _tokens(theirs), size=size)
    return None if conflicts else _untokens(merged)


def _hunks(merged: str, size: int = 7) -> list[tuple[int, int, list[str], list[str], list[str]]]:
    """diff3 hunks: (first line, line after, ours, base, theirs), read by their exact
    markers: `size` characters, then a space and the label (none for the separator)."""
    lines, found, i = merged.split("\n"), [], 0
    start, middle, separator, end = ("<" * size + " ours", "|" * size + " base", "=" * size,
                                     ">" * size + " theirs")
    while i < len(lines):
        if lines[i] != start:
            i += 1
            continue
        mid_base = next(k for k in range(i, len(lines)) if lines[k] == middle)
        mid = next(k for k in range(mid_base, len(lines)) if lines[k] == separator)
        stop = next(k for k in range(mid, len(lines)) if lines[k] == end)
        found.append((i, stop + 1, lines[i + 1:mid_base], lines[mid_base + 1:mid],
                      lines[mid + 1:stop]))
        i = stop + 1
    return found


def _changed(base: list[str], side: list[str]) -> tuple[list[str], list[str]]:
    """Entries a side added to the list (in its order) and base entries it took away,
    counting each text as often as it appears."""
    left = Counter(base)
    added = []
    for entry in side:
        if left[entry]:
            left[entry] -= 1
        else:
            added.append(entry)
    return added, list(left.elements())


def merge(base: str, ours: str, theirs: str, *, words: bool = False) -> Merged:
    """The contract merged in the current layout, and what moved to the history folder."""
    base_list = history.listed_entries(base)
    base_other = Counter(history.unrecognised(base))
    added, dropped, unrecognised = [], [], []
    for side, text in (("ours", ours), ("theirs", theirs)):
        if history.moved(text):
            continue                     # this side already keeps no list
        new, gone = _changed(base_list, history.listed_entries(text))
        added += [(side, entry) for entry in new]
        dropped += [(side, entry) for entry in gone]
        unrecognised += [(side, line) for line in
                         (Counter(history.unrecognised(text)) - base_other).elements()]
    texts = [normalize(ours), normalize(base), normalize(theirs)]
    size = marker_size(*texts)
    merged, conflicts = _merge_file(*texts, "--diff3", size=size)
    result = Merged(merged, conflicts, added, dropped, unrecognised, marker_size=size)
    if conflicts and words:
        lines = merged.split("\n")
        out, done = [], 0
        for start, end, o, b, t in _hunks(merged, size):
            joined = merge_words("\n".join(o), "\n".join(b), "\n".join(t))
            out.extend(lines[done:start])
            if joined is None:
                out.extend(lines[start:end])
            else:
                out.extend(joined.split("\n") if joined else [])
                result.word_merged += 1
            done = end
        out.extend(lines[done:])
        result.text = "\n".join(out)
        result.conflicts = len(_hunks(result.text, size))
    if not result.conflicts:
        result.text = layout.relayout(result.text)
    return result


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def unmerged_stages(path: str = PATH) -> set[int]:
    """The index stages Git holds for `path` while it is unmerged (none once resolved)."""
    out = _git("ls-files", "-u", "-z", "--", path).stdout
    return {int(entry.split("\t", 1)[0].split()[2]) for entry in out.split("\0") if entry}


def theirs_branch(merge_msg: str) -> str:
    """The branch a merge message names (`Merge branch 'x'`, `... 'origin/x'`), or ''."""
    match = re.search(r"'([^']+)'", merge_msg.split("\n", 1)[0])
    if not match:
        return ""
    name = match.group(1)
    return name.split("/", 1)[1] if name.startswith("origin/") else name


def write_entries(root: Path, added: list[tuple[str, str]], slugs: dict[str, str],
                  today: str, dry_run: bool) -> list[tuple[Path, str]]:
    """Each added entry's file: an existing file with its text, or a new one, named so
    no two entries of this run share a name whether or not anything is written."""
    existing = {}
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            if path.name != "README.md":
                existing.setdefault(path.read_text(encoding="utf-8").strip("\n"), path)
    taken: set[Path] = set()
    out = []
    for side, text in added:
        if text in existing:
            out.append((existing[text], "already there"))
            continue
        stated = history.DATE.search(text)
        date = stated.group(1) if stated else today
        path = history.free_path(root, date, slugs[side], taken)
        taken.add(path)
        if not dry_run:
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        existing[text] = path
        out.append((path, "" if stated else "gives no date; add one, first"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--words", action="store_true",
                        help="merge each remaining hunk a word at a time")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--ours-branch", help="name ours's entries after this branch "
                        "(default: the current branch; required on a detached HEAD)")
    parser.add_argument("--theirs-branch", help="name theirs's entries after this branch "
                        "(default: the branch the merge message names)")
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
    stages = unmerged_stages()
    if not stages:
        print(f"{PATH} is not unmerged: the merge left it alone, or it is already resolved;"
              " nothing to rebuild (if it needs laying out: python3 tools/contract_layout.py"
              " --write)", file=sys.stderr)
        return 2
    if stages != {1, 2, 3}:
        print(f"{PATH} has stages {sorted(stages)}, not base, ours and theirs; resolve it by"
              " hand", file=sys.stderr)
        return 2
    base, ours, theirs = (_git("show", f":{n}:{PATH}").stdout for n in (1, 2, 3))
    if not history.moved(ours) and not history.moved(theirs):
        print("both sides still list their changes in the contract; this merge is not the"
              " move, so merge it as before", file=sys.stderr)
        return 2
    result = merge(base, ours, theirs, words=args.words)

    msg_path = Path(_git("rev-parse", "--git-path", "MERGE_MSG").stdout.strip())
    msg = msg_path.read_text(encoding="utf-8") if msg_path.exists() else ""
    names = {"ours": args.ours_branch or _git("branch", "--show-current").stdout.strip(),
             "theirs": args.theirs_branch or theirs_branch(msg)}
    for side in {side for side, _ in result.added}:
        if not names[side]:
            print(f"name the branch whose list entries these are: --{side}-branch <name>"
                  + (" (HEAD is detached)" if side == "ours" else ""), file=sys.stderr)
            return 2
    slugs = {side: history.branch_slug(name) for side, name in names.items()}
    written = write_entries(Path(HISTORY), result.added, slugs,
                            dt.date.today().isoformat(), args.dry_run)
    if not args.dry_run:
        Path(PATH).write_text(result.text, encoding="utf-8")

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {PATH}: {result.conflicts} conflict(s) left"
          + (f", {result.word_merged} hunk(s) merged word by word" if result.word_merged
             else ""))
    for path, note in written:
        print(f"{verb if note != 'already there' else 'kept'} {path}"
              + (f" ({note})" if note else ""))
    for side, text in result.dropped:
        print(f"{side} edited or removed this old-list entry; the migrated list is not"
              f" edited, so carry the change by hand: {text[:160]}", file=sys.stderr)
    for side, line in result.unrecognised:
        print(f"{side} has this in the old changes section, which is not a list item; place"
              f" it by hand: {line[:160]}", file=sys.stderr)
    if not args.dry_run and not result.conflicts:
        print("then: git add " + " ".join([PATH, *dict.fromkeys(str(p) for p, _ in written)]))
    return 1 if result.conflicts or result.dropped or result.unrecognised else 0


if __name__ == "__main__":
    sys.exit(main())
