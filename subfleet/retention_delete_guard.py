"""Fail-closed Git read boundary used only by the operator's delete path.

Git reads configuration and follows storage pointers before most commands run.
An exclusion check of the explicit --git-dir alone cannot guard those reads.
This boundary admits plain local configuration, rejects metadata symlinks,
checks storage pointers before following them, and suppresses ambient config
for each command without changing the daemon's environment.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from . import dependencies, folders
from . import retention_fs as rfs
from . import retention_git as rgit

ISOLATED_CONFIG = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "0",
                   "GIT_CONFIG_PARAMETERS": ""}
MAX_ENTRIES = 1_000_000
MAX_DEPTH = 128
_SECTION = re.compile(r'\[([A-Za-z0-9-]+)( "[^"\\\x00-\x1f]*")?\]')
_VALUE = re.compile(r'([A-Za-z][A-Za-z0-9-]*)[ \t]*=[ \t]*([^#;"\\\x00-\x08\x0b-\x1f]*|"[^"\\\x00-\x1f]*")')


def _stamp(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns


class GitReadGuard:
    def __init__(self, excluder: Any):
        self.excluder = excluder
        # A directory-entry change invalidates the inventory before a child is
        # inspected; pointer/config writes also invalidate it. Objects and refs
        # are ordinary files, so their content cannot introduce a filesystem link.
        self.inventory: dict[str, tuple[list[tuple[str, tuple]], list[tuple[str, tuple]]]] = {}

    def refuse(self, message: str):
        raise rgit.GitError("delete Git read refused: " + message)

    def resolve(self, text: str | Path, base: str | Path = "/") -> Path:
        where, why = self.excluder.resolve(os.fspath(text), os.fspath(base))
        if where is None:
            self.refuse(f"{text}: {why}")
        return Path(where)

    def _regular(self, path: Path, limit: int = 1 << 20) -> bytes | None:
        path = self.resolve(path)
        try:
            return rfs.read_regular(path, limit=limit)
        except FileNotFoundError:
            return None

    def _fresh(self, record: tuple[list[tuple[str, tuple]], list[tuple[str, tuple]]]) -> bool:
        try:
            # Inventory order is parent before child. Stop at the first changed
            # parent, before an unchecked new symlink can redirect a child read.
            return all(_stamp(os.lstat(p)) == s for p, s in (*record[0], *record[1]))
        except OSError:
            return False

    def _metadata(self, root: Path) -> None:
        root = self.resolve(root)
        key = str(root)
        old = self.inventory.get(key)
        if old is not None and self._fresh(old):
            return
        directories: list[tuple[str, tuple]] = []
        pointers: list[tuple[str, tuple]] = []
        pending = [(root, 0)]
        entries = 0
        device = os.lstat(root).st_dev
        while pending:
            directory, depth = pending.pop(0)
            if depth > MAX_DEPTH:
                self.refuse(f"metadata deeper than {MAX_DEPTH}: {directory}")
            directory = self.resolve(directory)
            current = os.lstat(directory)
            if not stat.S_ISDIR(current.st_mode) or current.st_dev != device:
                self.refuse(f"metadata directory changed or crosses a volume: {directory}")
            directories.append((str(directory), _stamp(current)))
            with os.scandir(directory) as scan:
                for entry in scan:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        self.refuse(f"metadata exceeds {MAX_ENTRIES} entries: {root}")
                    if self.excluder.covers(entry.path):
                        self.refuse(f"excluded metadata: {entry.path}")
                    st = entry.stat(follow_symlinks=False)
                    if st.st_dev != device or stat.S_ISLNK(st.st_mode):
                        self.refuse(f"symlinked or mounted Git metadata: {entry.path}")
                    if stat.S_ISDIR(st.st_mode):
                        pending.append((Path(entry.path), depth + 1))
                    elif not stat.S_ISREG(st.st_mode):
                        self.refuse(f"nonregular Git metadata: {entry.path}")
                    elif entry.name in {"config", "config.worktree", "commondir", "gitdir", "alternates"}:
                        pointers.append((entry.path, _stamp(st)))
        self.inventory[key] = directories, pointers

    def _config(self, path: Path, git_dir: Path) -> None:
        raw = self._regular(path)
        if raw is None:
            return
        try:
            text = raw.decode("ascii")
        except UnicodeError:
            self.refuse(f"nonplain configuration: {path}")
        section, subsection = "", False
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                match = _SECTION.fullmatch(line)
                if match is None:
                    self.refuse(f"nonplain configuration: {path}")
                section, subsection = match[1].lower(), match[2] is not None
                if section in {"include", "includeif"}:
                    self.refuse(f"configuration includes are not admitted: {path}")
                if subsection and section not in rgit._PLAIN_SUBSECTIONS:
                    self.refuse(f"unknown configuration subsection: {path}")
                if not subsection and section not in {"core", "extensions", *rgit._PLAIN_SECTION_KEYS}:
                    self.refuse(f"unknown configuration section: {path}")
                continue
            match = _VALUE.fullmatch(line)
            if not section or match is None:
                self.refuse(f"nonplain configuration: {path}")
            key, value = match[1].lower(), match[2].strip()
            if section in {"core", "extensions"} and value.startswith('"'):
                self.refuse(f"nonplain startup configuration: {path}")
            if section == "core":
                if key in rgit._PLAIN_CORE_BOOLEANS:
                    if value.lower() not in rgit._BOOLEANS:
                        self.refuse(f"nonplain boolean configuration: {path}")
                elif key == "repositoryformatversion":
                    if value not in {"0", "1"}:
                        self.refuse(f"unknown repository format: {path}")
                elif key == "worktree":
                    self.resolve(value, git_dir)
                elif key not in rgit._PLAIN_CORE_ANY:
                    self.refuse(f"unknown core configuration: {path}")
            elif section == "extensions":
                if key == "worktreeconfig" and value.lower() in rgit._BOOLEANS:
                    pass
                elif key == "objectformat" and value in {"sha1", "sha256"}:
                    pass
                elif key == "partialclone" and value:
                    pass  # GIT_NO_LAZY_FETCH is pinned by rgit.environment.
                else:
                    self.refuse(f"unknown repository extension: {path}")
            elif not subsection and key not in rgit._PLAIN_SECTION_KEYS[section]:
                self.refuse(f"unknown configuration key: {path}")

    def _objects(self, objects: Path, seen: set[str], depth: int = 0) -> None:
        objects = self.resolve(objects)
        if str(objects) in seen:
            return
        seen.add(str(objects))
        if not os.path.lexists(objects):
            return
        self._metadata(objects)
        raw = self._regular(objects / "info" / "alternates")
        if raw is None:
            return
        if depth >= 5:
            self.refuse("alternate nesting exceeds the checked boundary")
        for line in raw.decode("utf-8", "surrogateescape").splitlines():
            if not line:
                continue
            if line != line.strip() or line.startswith(("#", '"')) or any(c in line for c in ('"', "\\", "\0")):
                self.refuse("nonplain alternate pointer")
            self._objects(self.resolve(line, objects), seen, depth + 1)

    def repository(self, git_dir: Path) -> None:
        git_dir = self.resolve(git_dir)
        if not os.path.lexists(git_dir):
            return
        self._metadata(git_dir)
        raw = self._regular(git_dir / "commondir", 65536)
        common = git_dir
        if raw is not None:
            value = raw.decode("utf-8", "surrogateescape").rstrip("\r\n")
            if not value or value != value.strip() or "\0" in value:
                self.refuse("nonplain commondir pointer")
            common = self.resolve(value, git_dir)
            self._metadata(common)
        self._config(common / "config", git_dir)
        self._config(git_dir / "config.worktree", git_dir)
        self._objects(common / "objects", set())

    def legacy(self, read: Any) -> dependencies.Places:
        """Run the existing legacy census only after proving its read paths.

        Legacy discovery inspects every ancestor's checkout and, for an old
        incomplete linked registration, its inferred parent object store.
        Validate those paths as well as ordinary Git storage before it runs.
        An excluded or unprovable pointer refuses this candidate; it never
        silently removes a live job from the dependency census.
        """
        rows = list(read(dependencies._DISCOVERING, ()))
        for row in rows:
            if dependencies.needed(dependencies._loads(row["submitted"])) is not None or not row["workdir"]:
                continue
            for field in ("workdir", "review_root", "out_path"):
                value = row[field]
                if not value:
                    continue
                if not os.path.isabs(value):
                    self.refuse(f"nonabsolute legacy {field}: {value}")
                resolved = self.resolve(value)
                if field == "out_path":
                    continue  # record() canonicalizes it but does not discover Git there
                for folder in (str(resolved), *folders.above(str(resolved))):
                    dotgit = self.resolve(Path(folder) / ".git")
                    if not os.path.lexists(dotgit):
                        continue
                    st = os.lstat(dotgit)
                    if stat.S_ISDIR(st.st_mode):
                        git_dir = dotgit
                    elif stat.S_ISREG(st.st_mode):
                        raw = self._regular(dotgit, 65536)
                        if raw is None or not raw.startswith(b"gitdir: "):
                            self.refuse(f"nonplain legacy gitfile: {dotgit}")
                        value = raw[len(b"gitdir: "):].decode("utf-8", "surrogateescape").rstrip("\r\n")
                        if not value or value != value.strip() or "\0" in value:
                            self.refuse(f"nonplain legacy gitfile: {dotgit}")
                        git_dir = self.resolve(value, folder)
                    else:
                        self.refuse(f"nonregular legacy gitfile: {dotgit}")
                    self.repository(git_dir)
                    if git_dir.parent.name == "worktrees" and self._regular(git_dir / "commondir", 65536) is None:
                        inferred = self.resolve(git_dir.parent.parent)
                        self.repository(inferred)
                        self._objects(inferred / "objects", set())

        def prepared(sql: str, params: tuple):
            return rows if sql == dependencies._DISCOVERING else read(sql, params)

        return dependencies.legacy(prepared)

    def discover(self, cwd: Path) -> Path | None:
        current = self.resolve(cwd)
        while True:
            dotgit = self.resolve(current / ".git")
            try:
                st = os.lstat(dotgit)
            except FileNotFoundError:
                st = None
            if st is not None:
                if stat.S_ISDIR(st.st_mode):
                    return dotgit
                raw = self._regular(dotgit, 65536)
                if raw is None or not raw.startswith(b"gitdir: "):
                    self.refuse(f"nonplain gitfile: {dotgit}")
                value = raw[len(b"gitdir: "):].decode("utf-8", "surrogateescape").rstrip("\r\n")
                if not value or value != value.strip() or "\0" in value:
                    self.refuse(f"nonplain gitfile: {dotgit}")
                return self.resolve(value, current)
            if os.path.lexists(self.resolve(current / "HEAD")):
                return current
            if current == current.parent:
                return None
            current = current.parent

    def __call__(self, args: tuple[str, ...], git_dir: Path | None, cwd: Path | None) -> None:
        for value in args:
            if os.path.isabs(value):
                self.resolve(value)
        if args and args[0] == "init":
            if any(a == "--template" or (a.startswith("--template=") and a != "--template=") for a in args):
                self.refuse("only an empty Git template is admitted")
            candidates = [a for a in args[1:] if not a.startswith("-")]
            destination = self.resolve(candidates[-1] if candidates else (cwd or Path.cwd()))
            if os.path.lexists(destination / "config"):
                self.repository(destination)
            return
        repository = self.resolve(git_dir) if git_dir is not None else self.discover(cwd or Path.cwd())
        if repository is not None:
            self.repository(repository)
