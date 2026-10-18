"""The acceptance contract's change history, one file per entry (defect D-MRG3).

Until 2026-10-14 `docs/acceptance-contract.md` kept its changes as one list near the top,
and every pull request prepended to it. Each merge into the release line therefore made
every other open pull request conflict on that list; GitHub runs no CI on a conflicting
pull request (D-CI1), so each one needed a merge and a fresh CI run before it could land.

Now each change is a file of its own under `docs/contract-history/`, named after the
branch that adds it, so two pull requests write the same path only when their branch
names differ in nothing but case or punctuation (see that folder's README):

    docs/contract-history/<YYYY-MM-DD>-<branch>[-<n>].md   one entry, newest first in the index
    docs/contract-history/version-3/<NNN>.md               the list as the contract kept it, in order

The index is printed, never checked in, so adding an entry touches no shared file:

    python3 tools/contract_history.py              # print every entry, newest first
    python3 tools/contract_history.py check        # exit 1 and name what is wrong
    python3 tools/contract_history.py new          # start this branch's entry file

Standard library only, like the rest of the repository.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import contract_layout as layout  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "docs" / "acceptance-contract.md"
HISTORY = REPO / "docs" / "contract-history"
MIGRATED_DIR = "version-3"
#: The list the contract kept until 2026-10-14 (release/217 at 3f50142ac), one file per
#: entry in its order: how many entries, and the SHA-256 of their text joined by newlines.
MIGRATED_COUNT = 157
MIGRATED_SHA256 = "5a72c93441d958fd7eb6c0ffb1a3e5af5245eec9d1d71f6b6024aedf54bea22d"

HEADING = "## Changes in version 3"
#: What the contract's changes section says now, in place of the list. The contract lays
#: it out one sentence per line (`tools/contract_layout.py`); checks compare its words.
POINTER = (
    "The changes are kept one file per entry in `docs/contract-history/`, not here, so that"
    " two pull requests never edit the same lines for them (defect D-MRG3)."
    " `python3 tools/contract_history.py` prints them all, newest first, ending with the"
    " 157 entries this section listed before the move, in their order."
    " A pull request that changes a clause adds its own file there, as"
    " `docs/contract-history/README.md` says, and never lists a change in this section."
)

ENTRY_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
MIGRATED_NAME = re.compile(r"^(\d{3})\.md$")
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
CLAUSE_REF = re.compile(r"\bC-(\d+)\.(\d+[a-z]?)\b")
#: A clause's first line, titled ones (C-8.5) included: `contract_layout.CLAUSE`.
CLAUSE_DEF = re.compile(layout.CLAUSE.pattern, re.M)


@dataclass(frozen=True)
class Entry:
    path: Path
    text: str              # the entry as one Markdown paragraph, with no list marker
    date: str | None       # from the file name; None for a migrated entry
    order: int | None      # position in the migrated list (1 = its first line)

    @property
    def migrated(self) -> bool:
        return self.order is not None


def clause_ids(contract: str) -> set[str]:
    """The clauses the contract defines (`- **C-x.y**` list items)."""
    return set(CLAUSE_DEF.findall(contract))


def section(lines: list[str]) -> tuple[int, int] | None:
    """Line range (heading, next heading) of the contract's changes section."""
    start = next((i for i, line in enumerate(lines) if line.startswith("## Changes in version")),
                 None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return start, end


def _section_items(contract: str) -> tuple[list[str], list[str]]:
    """The changes section's list items, each whole (its continuation lines too, one per
    line, indentation removed), and every other line that is neither blank nor the
    pointer: text this reader does not recognise."""
    lines = contract.split("\n")
    span = section(lines)
    if span is None:
        return [], []
    body = lines[span[0] + 1:span[1]]
    if " ".join(" ".join(body).split()) == POINTER:
        return [], []
    items, other, current = [], [], None
    for line in body:
        if line.startswith("- "):
            current = [line[2:].strip()]
            items.append(current)
        elif line.strip() and current is not None and not layout.starts_block(line):
            current.append(line.strip())  # a continuation line, indented or lazy
        elif line.strip():
            other.append(line)
            current = None
        else:
            current = None
    return ["\n".join(item) for item in items], other


def listed_entries(contract: str) -> list[str]:
    """The entries a contract still lists in its changes section (the old layout), each
    whole: a wrapped item keeps its later lines."""
    return _section_items(contract)[0]


def unrecognised(contract: str) -> list[str]:
    """Lines in the changes section that are neither the pointer, blank, nor a list item:
    the move would drop them, so a person must place them."""
    return _section_items(contract)[1]


def moved(contract: str) -> bool:
    """Whether the contract's changes section is the pointer (the new layout)."""
    lines = contract.split("\n")
    span = section(lines)
    return span is not None and " ".join(" ".join(lines[span[0] + 1:span[1]]).split()) == POINTER


def without_list(contract: str) -> str:
    """The contract with its changes section reduced to the pointer (the new layout)."""
    lines = contract.split("\n")
    span = section(lines)
    if span is None:
        return contract
    return "\n".join(lines[:span[0]] + [HEADING, "", POINTER, ""] + lines[span[1]:])


def read_entries(root: Path = HISTORY) -> list[Entry]:
    """Every entry file, in index order: dated entries newest first, then the migrated list."""
    dated, migrated = [], []
    for path in sorted(root.glob("*.md")):
        match = ENTRY_NAME.match(path.name)
        if match:
            dated.append(Entry(path, path.read_text(encoding="utf-8").strip("\n"),
                               match.group(1), None))
    for path in sorted((root / MIGRATED_DIR).glob("*.md")):
        match = MIGRATED_NAME.match(path.name)
        if match:
            migrated.append(Entry(path, path.read_text(encoding="utf-8").strip("\n"),
                                  None, int(match.group(1))))
    dated.sort(key=lambda e: e.path.stem)                      # same date: by name
    dated.sort(key=lambda e: e.date, reverse=True)             # newest date first (stable)
    migrated.sort(key=lambda e: e.order)
    return dated + migrated


def migrated_digest(entries: list[Entry]) -> str:
    return hashlib.sha256("\n".join(e.text for e in entries if e.migrated).encode()).hexdigest()


def as_item(text: str) -> str:
    """One entry as a list item: its first line after `- `, later lines indented."""
    lines = text.split("\n")
    return "\n".join(["- " + lines[0], *("  " + line.strip() for line in lines[1:])])


def index(root: Path = HISTORY) -> str:
    """The whole history as Markdown, generated: newest first, then the migrated list."""
    items = [as_item(e.text) for e in read_entries(root)]
    return "\n".join([
        "# Acceptance contract changes",
        "",
        "Generated by `python3 tools/contract_history.py` from `docs/contract-history/`;"
        " do not edit or check in.",
        "",
        HEADING,
        "",
        *items,
        "",
    ])


def problems(root: Path = HISTORY, contract_path: Path = CONTRACT,
             today: dt.date | None = None) -> list[str]:
    """Everything wrong with the history folder and the contract's changes section."""
    found: list[str] = []
    contract = contract_path.read_text(encoding="utf-8")
    defined = clause_ids(contract)

    # The contract lists no change of its own any more.
    lines = contract.split("\n")
    span = section(lines)
    if span is None:
        found.append(f"{contract_path.name}: no `{HEADING}` section")
    else:
        body = " ".join(" ".join(lines[span[0] + 1:span[1]]).split())
        if body != POINTER:
            found.append(f"{contract_path.name}: the `{HEADING}` section must hold only the"
                         " pointer to docs/contract-history/; add a file there instead of a"
                         " line here (docs/contract-history/README.md)")

    # Every file is an entry, the README, or a migrated entry.
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue                      # .DS_Store and the like are not tracked
        if path.is_dir():
            if path != root / MIGRATED_DIR:
                found.append(f"{path.relative_to(root)}: no folders but {MIGRATED_DIR}/")
            continue
        rel = path.relative_to(root)
        if rel.parent == Path(MIGRATED_DIR):
            if not MIGRATED_NAME.match(path.name):
                found.append(f"{rel}: a migrated entry is named NNN.md")
        elif rel.parent == Path("."):
            if path.name != "README.md" and not ENTRY_NAME.match(path.name):
                found.append(f"{rel}: an entry is named YYYY-MM-DD-<branch>.md, lowercase"
                             " letters, digits and single hyphens")
        else:
            found.append(f"{rel}: not an entry")

    entries = read_entries(root)
    seen_text: dict[str, Path] = {}
    for entry in entries:
        rel = entry.path.relative_to(root)
        raw = entry.path.read_text(encoding="utf-8")
        if not entry.text.strip():
            found.append(f"{rel}: empty")
            continue
        if not raw.endswith("\n") or raw.endswith("\n\n") or raw.startswith("\n"):
            found.append(f"{rel}: the text, then one newline")
        lines = entry.text.split("\n")
        if any(not line.strip() for line in lines):
            found.append(f"{rel}: one paragraph, no blank lines")
        if lines[0][:1].isspace() or layout.starts_block(lines[0]) \
                or layout.HEADING.match(lines[0]) \
                or layout.TABLE.match(lines[0]) or layout.LINK_DEFINITION_LINE.match(lines[0]):
            found.append(f"{rel}: the entry's text only, as a paragraph; the index adds the"
                         " list marker")
        if any(line.strip() and (layout.starts_block(line) or layout.HEADING.match(line)
                                 or layout.TABLE.match(line)) for line in lines[1:]):
            found.append(f"{rel}: one paragraph: a later line may not start a list, heading,"
                         " quote, table, fence, rule or HTML")
        if entry.text in seen_text:
            found.append(f"{rel}: the same text as {seen_text[entry.text].relative_to(root)}")
        seen_text.setdefault(entry.text, entry.path)
        if entry.migrated:
            if "\n" in entry.text:
                found.append(f"{rel}: a migrated entry is one line, as the contract had it")
            continue
        try:
            when = dt.date.fromisoformat(entry.date)
        except ValueError:
            found.append(f"{rel}: {entry.date} is not a date")
            continue
        if today is not None and when > today + dt.timedelta(days=1):
            found.append(f"{rel}: dated {entry.date}, after today")
        stated = DATE.search(entry.text)
        if not stated or stated.group(1) != entry.date:
            found.append(f"{rel}: the first date the entry gives must be its file's date,"
                         f" {entry.date} (as in `C-x.y ({entry.date}, why): what changed`)")
        cited = {f"C-{a}.{b}" for a, b in CLAUSE_REF.findall(entry.text)}
        if not cited & defined:
            found.append(f"{rel}: cites no clause the contract defines (`C-x.y`)")

    # The migrated list is whole and unchanged.
    migrated = [e for e in entries if e.migrated]
    orders = [e.order for e in migrated]
    if orders != list(range(1, MIGRATED_COUNT + 1)):
        missing = sorted(set(range(1, MIGRATED_COUNT + 1)) - set(orders))
        extra = sorted(set(orders) - set(range(1, MIGRATED_COUNT + 1)))
        found.append(f"{MIGRATED_DIR}/: entries 001 to {MIGRATED_COUNT:03d} each once"
                     f" (missing {missing}, unexpected {extra})")
    elif migrated_digest(entries) != MIGRATED_SHA256:
        found.append(f"{MIGRATED_DIR}/: the migrated entries differ from the list the contract"
                     " kept; they are history and are not edited")
    return found


def branch_slug(branch: str) -> str:
    """A branch name as an entry name's tail: lowercase, `[a-z0-9]` runs joined by `-`."""
    return "-".join(re.findall(r"[a-z0-9]+", branch.lower())) or "change"


def current_branch() -> str:
    result = subprocess.run(["git", "branch", "--show-current"], cwd=REPO,
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def free_path(root: Path, date: str, slug: str,
              taken: set[Path] | frozenset = frozenset()) -> Path:
    """`<date>-<slug>.md`, or the first `<date>-<slug>-<n>.md` from n = 2 that neither
    exists nor is in `taken` (names chosen already, written or not)."""
    path = root / f"{date}-{slug}.md"
    n = 2
    while path.exists() or path in taken:
        path = root / f"{date}-{slug}-{n}.md"
        n += 1
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("index", help="print every entry, newest first (the default)")
    sub.add_parser("check", help="exit 1 and name what is wrong")
    new = sub.add_parser("new", help="start an entry file for this branch")
    new.add_argument("--date", default=dt.date.today().isoformat())
    new.add_argument("--branch", default=None, help="default: the current branch")
    args = parser.parse_args(argv)
    if args.command == "check":
        found = problems(today=dt.date.today())
        for problem in found:
            print(f"docs/contract-history: {problem}", file=sys.stderr)
        return 1 if found else 0
    if args.command == "new":
        dt.date.fromisoformat(args.date)
        branch = args.branch or current_branch()
        if not branch:
            print("HEAD is detached: name the branch with --branch <name>", file=sys.stderr)
            return 2
        HISTORY.mkdir(parents=True, exist_ok=True)
        path = free_path(HISTORY, args.date, branch_slug(branch))
        path.write_text(f"C-x.y ({args.date}, why): what changed.\n", encoding="utf-8")
        print(path.relative_to(REPO))
        return 0
    sys.stdout.write(index())
    return 0


if __name__ == "__main__":
    sys.exit(main())
