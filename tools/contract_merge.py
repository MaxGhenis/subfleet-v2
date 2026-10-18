"""Merge `docs/acceptance-contract.md` three ways in its current layout (defect D-MRG3).

Run it after `git merge` stops with the contract conflicted, typically while merging the
release line into a branch from before the history moved out of the contract. It reads
the three versions Git staged for the contract (base, ours, theirs), not the conflicted
working copy, and brings each to the current layout:

- the changes section is the pointer to `docs/contract-history/`, and each entry a side
  added to the old list, whole, becomes a file there (C-20.7);
- every paragraph is one sentence per line (`tools/contract_layout.py`).

Then `git merge-file` merges the three and the result is written to the contract.

    python3 tools/contract_merge.py                 # mid-merge, in the repository
    python3 tools/contract_merge.py --words         # also merge leftover hunks word by word
    python3 tools/contract_merge.py --dry-run       # say what it would do; write nothing

What it writes, and what it will not overwrite:

- It stages every entry file of the merge (new, updated, or already there), so none can
  drop out of the merge commit, and prints the `git add` for the contract: at once when
  it merged cleanly, after you resolve what is left otherwise. It never stages the
  contract.
- It refuses (exit 2) unless the contract is unmerged with all three stages: once it is
  resolved and added, or when the merge left it alone, there is nothing to rebuild.
- It remembers what it wrote (in `<git dir>/contract-merge.json`, for this merge only),
  and refuses to write over a contract or an entry file changed since, or over a contract
  that holds neither Git's conflict markers nor its own last result: that is a person's
  resolution. `--force` overrides that; either way the contract is first copied to
  `<git dir>/contract-merge-backup.md`.
- An old-list entry a side edited is a new round of that entry: when the entry already
  landed as a file (another pull request on the same work merged first), that file gets
  the new text; when it is one of the migrated entries, which are not edited, it is
  named for a person and nothing is written; when it never landed, it is a new entry.
  An entry removed, or text in the old section that is not a list item, is named for a
  person too.

`--words` merges a hunk that still conflicts one word at a time, only when no word was
changed by both sides. Read what it merged: two edits to one sentence can each be right
and still disagree.

Exit status: 0 merged cleanly; 1 a conflict or an entry needs a person (conflicts are
written with diff3 markers); 2 usage, nothing to do, or a refusal to overwrite.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
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
GIT_MARKER = re.compile(r"^<{7,} ", re.M)
#: Two rounds of one entry begin alike: an entry is never written beside a file whose
#: text begins with the same this many characters; both are named for a person instead.
SAME_ENTRY = 60


@dataclass
class Merged:
    text: str
    conflicts: int
    #: (side, entry text) for each entry a side added to the old list, in its order.
    added: list[tuple[str, str]] = field(default_factory=list)
    #: (side, base entry text) for each base entry a side removed from the old list.
    dropped: list[tuple[str, str]] = field(default_factory=list)
    #: (side, base entry text, new text) for each base entry a side edited in place.
    replaced: list[tuple[str, str, str]] = field(default_factory=list)
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


def _changed(base: list[str], side: list[str]):
    """How a side's old list differs from the base's, entry by entry and in order:
    entries added, base entries removed, and base entries edited (a replaced run pairs
    its entries one for one; any extra are added or removed)."""
    added, dropped, replaced = [], [], []
    matcher = difflib.SequenceMatcher(a=base, b=side, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "insert":
            added += side[j1:j2]
        elif op == "delete":
            dropped += base[i1:i2]
        elif op == "replace":
            pairs = min(i2 - i1, j2 - j1)
            replaced += list(zip(base[i1:i1 + pairs], side[j1:j1 + pairs]))
            added += side[j1 + pairs:j2]
            dropped += base[i1 + pairs:i2]
    return added, dropped, replaced


def merge(base: str, ours: str, theirs: str, *, words: bool = False) -> Merged:
    """The contract merged in the current layout, and how each side's old list changed."""
    base_list = history.listed_entries(base)
    base_other = Counter(history.unrecognised(base))
    result = Merged("", 0)
    for side, text in (("ours", ours), ("theirs", theirs)):
        if history.moved(text):
            continue                     # this side already keeps no list
        new, gone, edited = _changed(base_list, history.listed_entries(text))
        result.added += [(side, entry) for entry in new]
        result.dropped += [(side, entry) for entry in gone]
        result.replaced += [(side, old, entry) for old, entry in edited]
        result.unrecognised += [(side, line) for line in
                                (Counter(history.unrecognised(text)) - base_other).elements()]
    texts = [normalize(ours), normalize(base), normalize(theirs)]
    size = marker_size(*texts)
    merged, conflicts = _merge_file(*texts, "--diff3", size=size)
    result.text, result.conflicts, result.marker_size = merged, conflicts, size
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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _files(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Dated and migrated entry files by their text."""
    dated, migrated = {}, {}
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            if path.name == "README.md":
                continue
            target = migrated if path.parent.name == history.MIGRATED_DIR else dated
            target.setdefault(path.read_text(encoding="utf-8").strip("\n"), path)
    return dated, migrated


def against_files(result: Merged, ours: str, theirs: str, root: Path) -> None:
    """For a merge base Git built from several (a criss-cross merge, whose text holds
    conflict markers of its own): the base's list says nothing reliable, so a side's new
    entries are those no file under `root` holds, and edits and removals cannot be told
    (the same-entry guard in `plan_entries` still catches a second round of one)."""
    dated, migrated = _files(root)
    known = set(dated) | set(migrated)
    result.added = [(side, entry) for side, text in (("ours", ours), ("theirs", theirs))
                    if not history.moved(text)
                    for entry in history.listed_entries(text) if entry not in known]
    result.replaced, result.dropped, result.unrecognised = [], [], []


@dataclass
class Plan:
    """What a run does to the history folder, and what it leaves for a person."""
    #: (path, text, note) for each file to write; note says why ("" for a new entry).
    writes: list[tuple[Path, str, str]] = field(default_factory=list)
    #: paths already holding an entry of this merge, written by Git or an earlier run.
    kept: list[tuple[Path, str]] = field(default_factory=list)
    people: list[str] = field(default_factory=list)


def plan_entries(root: Path, result: Merged, slugs: dict[str, str], today: str,
                 state: dict) -> Plan:
    """Decide each entry's file. `state["files"]` maps the paths an earlier run of this
    merge wrote to the SHA-256 of what it wrote, and `state["entries"]` each entry's text
    to its path, so a rerun reuses its names and never writes over a person's edit."""
    dated, migrated = _files(root)
    plan, taken = Plan(), set()
    remembered = {text: Path(path) for text, path in state.get("entries", {}).items()}
    written = state.get("files", {})

    def place(text: str, path: Path, note: str) -> None:
        if path.exists() and str(path) in written \
                and _sha(path.read_text(encoding="utf-8")) != written[str(path)]:
            plan.kept.append((path, "kept: changed since this tool wrote it"))
        elif path.exists() and path.read_text(encoding="utf-8").strip("\n") == text:
            plan.kept.append((path, "already there"))
        else:
            plan.writes.append((path, text, note))
        taken.add(path)

    def new_entry(side: str, text: str) -> None:
        if text in remembered:
            place(text, remembered[text], "")
            return
        if text in dated:
            plan.kept.append((dated[text], "already there"))
            return
        if text in migrated:
            return                        # a migrated entry, there already
        alike = [path for other, path in {**dated, **migrated}.items()
                 if other[:SAME_ENTRY] == text[:SAME_ENTRY] and path not in taken]
        if alike:
            plan.people.append(f"{side} has an entry that begins like {alike[0].name}, which"
                               " holds another round of it (a pull request on the same work"
                               " landed first?); nothing was written. Keep one round by hand."
                               f" This branch's: {text[:200]}")
            return
        stated = history.DATE.search(text)
        path = history.free_path(root, stated.group(1) if stated else today, slugs[side], taken)
        place(text, path, "" if stated else "gives no date; add one, first")

    for side, text in result.added:
        new_entry(side, text)
    for side, old, new in result.replaced:
        if old in dated:
            place(new, dated[old], f"a new round of the entry {dated[old].name} already holds")
        elif old in migrated:
            plan.people.append(f"{side} edited migrated entry {migrated[old].name}, which is not"
                               f" edited; nothing was written. Its new text: {new[:200]}")
        elif new in dated or new in remembered:
            new_entry(side, new)
        else:
            new_entry(side, new)          # the old round never landed: a new entry
    for side, old in result.dropped:
        if old in dated or old in migrated:
            where = (dated.get(old) or migrated.get(old)).name
            plan.people.append(f"{side} removed the entry in {where}; remove or keep it by hand")
    for side, line in result.unrecognised:
        plan.people.append(f"{side} has this in the old changes section, which is not a list"
                           f" item; place it by hand: {line[:200]}")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--words", action="store_true",
                        help="merge each remaining hunk a word at a time")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--force", action="store_true",
                        help="rebuild the contract even over a change made since")
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
    merge_head_path = Path(_git("rev-parse", "--git-path", "MERGE_HEAD").stdout.strip())
    if not merge_head_path.exists():
        print("not mid-merge (no MERGE_HEAD)", file=sys.stderr)
        return 2
    merge_head = merge_head_path.read_text(encoding="utf-8").split()[0]
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

    state_path = Path(_git("rev-parse", "--git-path", "contract-merge.json").stdout.strip())
    backup_path = Path(_git("rev-parse", "--git-path", "contract-merge-backup.md").stdout.strip())
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("merge_head") != merge_head:
            state = {}                     # left by another merge
    current = Path(PATH).read_text(encoding="utf-8") if Path(PATH).exists() else ""
    by_person = (_sha(current) != state["contract"]) if "contract" in state \
        else not GIT_MARKER.search(current)
    if by_person and not args.force:
        print(f"{PATH} has changed since " + ("this tool wrote it" if "contract" in state
                                              else "Git left it conflicted")
              + " (a resolution by hand?); nothing written. --force rebuilds it from the"
              " staged versions, after copying it to " + str(backup_path), file=sys.stderr)
        return 2

    result = merge(base, ours, theirs, words=args.words)
    if GIT_MARKER.search(base):
        print("note: the merge base is one Git built from several (a criss-cross merge);"
              " entries are matched against the files here, not the base; read the result"
              " closely", file=sys.stderr)
        against_files(result, ours, theirs, Path(HISTORY))
    msg_path = Path(_git("rev-parse", "--git-path", "MERGE_MSG").stdout.strip())
    msg = msg_path.read_text(encoding="utf-8") if msg_path.exists() else ""
    names = {"ours": args.ours_branch or _git("branch", "--show-current").stdout.strip(),
             "theirs": args.theirs_branch or theirs_branch(msg)}
    sides = {side for side, _ in result.added} | {side for side, _, _ in result.replaced}
    for side in sides:
        if not names[side]:
            print(f"name the branch whose list entries these are: --{side}-branch <name>"
                  + (" (HEAD is detached)" if side == "ours" else ""), file=sys.stderr)
            return 2
    slugs = {side: history.branch_slug(name) if name else "" for side, name in names.items()}
    plan = plan_entries(Path(HISTORY), result, slugs, dt.date.today().isoformat(), state)

    verb = "would write" if args.dry_run else "wrote"
    if not args.dry_run:
        backup_path.write_text(current, encoding="utf-8")
        Path(PATH).write_text(result.text, encoding="utf-8")
        for path, text, _ in plan.writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        files = dict(state.get("files", {}))
        entries = dict(state.get("entries", {}))
        for path, text, _ in plan.writes:
            files[str(path)] = _sha(text + "\n")
            entries[text] = str(path)
        state_path.write_text(json.dumps({"merge_head": merge_head, "contract": _sha(result.text),
                                          "files": files, "entries": entries}, indent=1),
                              encoding="utf-8")
        staged = [str(p) for p, _, _ in plan.writes] + [str(p) for p, _ in plan.kept]
        if staged:
            _git("add", "--", *dict.fromkeys(staged))

    print(f"{verb} {PATH}: {result.conflicts} conflict(s) left"
          + (f", {result.word_merged} hunk(s) merged word by word" if result.word_merged
             else "") + ("" if args.dry_run else f" (the file before: {backup_path})"))
    for path, _, note in plan.writes:
        print(f"{verb} and {'would stage' if args.dry_run else 'staged'} {path}"
              + (f" ({note})" if note else ""))
    for path, note in plan.kept:
        print(f"{'would stage' if args.dry_run else 'staged'} {path} ({note})")
    for line in plan.people:
        print(line, file=sys.stderr)
    if not args.dry_run:
        print(("then: " if not result.conflicts else "after resolving the conflicts: ")
              + f"git add {PATH}")
    return 1 if result.conflicts or plan.people else 0


if __name__ == "__main__":
    sys.exit(main())
