"""The `str_replace_based_edit_tool` (text_editor_20250728) (C-31.15).

Commands `view`, `create`, `str_replace` and `insert`, as the claude-api skill's tool
reference lists them; this tool version has no `undo_edit`. Every path is resolved
with realpath and refused outside the worktree and the attempt folder. A write must
also land where the shell's sandbox could write (`Layout.writable`), passes the guard
as an apply_patch payload first, and goes through a descriptor whose real path is
checked again after opening, never through the model's path string.
"""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from subfleet.api_agent import hooks
from subfleet.api_agent.prompt import VIEW_MAX_CHARS
from subfleet.api_agent.sandbox import Layout

MAX_FILE_BYTES = 10 * 1024 * 1024
LIST_DEPTH = 2
LIST_CAP = 1_000
COMMANDS = ("view", "create", "str_replace", "insert")


@dataclass
class EditResult:
    text: str
    is_error: bool = False
    denied: bool = False
    reason: str | None = None


class _Refused(Exception):
    def __init__(self, text: str, *, denied: bool = False) -> None:
        super().__init__(text)
        self.text = text
        self.denied = denied


def _real_of_fd(fd: int) -> Path:
    raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    return Path(os.fsdecode(raw.split(b"\0", 1)[0]))


class Editor:
    def __init__(self, layout: Layout, guard: Callable[[dict], hooks.Verdict] | None,
                 backups: Path, *, readonly: bool) -> None:
        self._layout = layout
        self._guard = guard
        self._backups = backups
        self._readonly = readonly
        self._saved = 0

    # -- paths -------------------------------------------------------------------------
    def _resolve(self, raw: object) -> tuple[str, Path]:
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise _Refused("`path` must be a nonempty string")
        spelled = raw if os.path.isabs(raw) else os.path.join(self._layout.workdir, raw)
        real = Path(os.path.realpath(spelled))
        if not self._layout.readable(real):
            raise _Refused(f"{raw} resolves outside the worktree and the attempt folder, so it was refused",
                           denied=True)
        return raw, real

    def _check_write(self, raw: str, real: Path) -> None:
        if self._readonly:
            raise _Refused("this job is read-only: the editor can only view files", denied=True)
        if not self._layout.writable(real):
            raise _Refused(f"{raw} is not writable in this job (a .git folder or a Subfleet record)",
                           denied=True)

    def _guarded(self, real: Path, added: str, removed: str | None) -> None:
        if self._guard is None:
            return
        verdict = self._guard(hooks.patch_text(str(real), added, removed))
        if not verdict.allowed:
            raise _Refused(f"The guard refused this edit: {verdict.reason}", denied=True)

    def _open_read(self, real: Path) -> int:
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _Refused(f"{real} changed into a link while it was being opened", denied=True) from None
            raise _Refused(f"cannot open {real}: {exc.strerror}") from None
        if not self._layout.readable(_real_of_fd(fd)):
            os.close(fd)
            raise _Refused(f"{real} moved outside the worktree while it was being opened", denied=True)
        return fd

    def _read_text(self, real: Path) -> str:
        fd = self._open_read(real)
        try:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                raise _Refused(f"{real} is a folder")
            if not stat.S_ISREG(info.st_mode):
                raise _Refused(f"{real} is not a regular file")
            if info.st_size > MAX_FILE_BYTES:
                raise _Refused(f"{real} is larger than {MAX_FILE_BYTES} bytes")
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise _Refused(f"{real} is not UTF-8 text") from None

    def _write_text(self, real: Path, text: str, *, create: bool) -> None:
        parent = real.parent
        if create:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise _Refused(f"cannot create {parent}: {exc.strerror}") from None
        try:
            dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise _Refused(f"cannot open {parent}: {exc.strerror}") from None
        try:
            target = _real_of_fd(dfd) / real.name
            if not self._layout.writable(target):
                raise _Refused(f"{real} moved outside the writable folders while it was being opened",
                               denied=True)
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | (os.O_CREAT if create else 0)
            try:
                fd = os.open(real.name, flags, 0o644, dir_fd=dfd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise _Refused(f"{real} is a link; the editor writes only regular files", denied=True) from None
                raise _Refused(f"cannot write {real}: {exc.strerror}") from None
        finally:
            os.close(dfd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _Refused(f"{real} is not a regular file")
            if info.st_nlink > 1:
                raise _Refused(f"{real} has other hard links, which may lie outside the worktree; "
                               "the editor does not write it", denied=True)
            os.ftruncate(fd, 0)
            data = memoryview(text.encode("utf-8"))
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)

    def _backup(self, real: Path, text: str) -> None:
        self._saved += 1
        self._backups.mkdir(mode=0o700, exist_ok=True)
        name = f"{self._saved:05d}-{real.name}"
        fd = os.open(self._backups / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    # -- commands ----------------------------------------------------------------------
    def run(self, tool_input: object) -> EditResult:
        try:
            if not isinstance(tool_input, dict):
                raise _Refused("the editor's input must be an object")
            command = tool_input.get("command")
            if command not in COMMANDS:
                raise _Refused(f"unknown command {command!r}; this editor takes {', '.join(COMMANDS)}")
            raw, real = self._resolve(tool_input.get("path"))
            return getattr(self, "_" + command)(raw, real, tool_input)
        except _Refused as refused:
            return EditResult(refused.text, is_error=True, denied=refused.denied,
                              reason=refused.text if refused.denied else None)

    def _view(self, raw: str, real: Path, tool_input: dict) -> EditResult:
        if real.is_dir():
            return EditResult(self._listing(real))
        text = self._read_text(real)
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        first, last = 1, len(lines)
        window = tool_input.get("view_range")
        if window is not None:
            if (not isinstance(window, list) or len(window) != 2
                    or not all(isinstance(n, int) and not isinstance(n, bool) for n in window)):
                raise _Refused("`view_range` must be two integers, [first, last]")
            first, last = window[0], (len(lines) if window[1] == -1 else window[1])
            if first < 1 or first > max(len(lines), 1) or last < first or last > len(lines):
                raise _Refused(f"`view_range` {window} is outside the file's {len(lines)} lines")
        shown = "\n".join(f"{number:6}\t{lines[number - 1]}" for number in range(first, last + 1))
        if len(shown) > VIEW_MAX_CHARS:
            shown = shown[:VIEW_MAX_CHARS] + (f"\n[output cut at {VIEW_MAX_CHARS} characters; "
                                              "use view_range to see the rest]")
        return EditResult(shown)

    def _listing(self, real: Path) -> str:
        entries: list[str] = []

        def walk(folder: Path, depth: int) -> None:
            try:
                children = sorted(os.scandir(folder), key=lambda entry: entry.name)
            except OSError:
                return
            for child in children:
                if child.name.startswith(".") or len(entries) >= LIST_CAP:
                    continue
                path = folder / child.name
                is_dir = child.is_dir(follow_symlinks=False)
                entries.append(str(path) + ("/" if is_dir else ""))
                if is_dir and depth < LIST_DEPTH:
                    walk(path, depth + 1)

        walk(real, 1)
        note = f"\n[listing cut at {LIST_CAP} entries]" if len(entries) >= LIST_CAP else ""
        return (f"Files and folders up to {LIST_DEPTH} levels deep in {real}, hidden items excluded:\n"
                + "\n".join(entries) + note)

    def _create(self, raw: str, real: Path, tool_input: dict) -> EditResult:
        text = tool_input.get("file_text")
        if not isinstance(text, str):
            raise _Refused("`create` needs `file_text`")
        self._check_write(raw, real)
        old = None
        if real.exists():
            if real.is_dir():
                raise _Refused(f"{raw} is a folder")
            old = self._read_text(real)
        self._guarded(real, text, None)
        if old is not None:
            self._backup(real, old)
        self._write_text(real, text, create=True)
        return EditResult(f"{'Overwrote' if old is not None else 'Created'} {real}"
                          + (" (the old text was backed up)" if old is not None else ""))

    def _str_replace(self, raw: str, real: Path, tool_input: dict) -> EditResult:
        old_str, new_str = tool_input.get("old_str"), tool_input.get("new_str", "")
        if not isinstance(old_str, str) or not old_str:
            raise _Refused("`str_replace` needs a nonempty `old_str`")
        if not isinstance(new_str, str):
            raise _Refused("`new_str` must be a string")
        self._check_write(raw, real)
        text = self._read_text(real)
        count = text.count(old_str)
        if count == 0:
            raise _Refused(f"No replacement was made: `old_str` does not appear verbatim in {raw}.")
        if count > 1:
            starts, index = [], text.find(old_str)
            while index != -1:
                starts.append(text.count("\n", 0, index) + 1)
                index = text.find(old_str, index + 1)
            raise _Refused(f"No replacement was made: `old_str` appears {count} times in {raw}, at lines "
                           f"{', '.join(map(str, starts))}. Include more context so it is unique.")
        self._guarded(real, new_str, old_str)
        self._write_text(real, text.replace(old_str, new_str, 1), create=False)
        return EditResult(f"Edited {real}: replaced one occurrence.")

    def _insert(self, raw: str, real: Path, tool_input: dict) -> EditResult:
        line, new = tool_input.get("insert_line"), tool_input.get("insert_text")
        if not isinstance(line, int) or isinstance(line, bool):
            raise _Refused("`insert` needs an integer `insert_line`")
        if not isinstance(new, str):
            raise _Refused("`insert` needs `insert_text`")
        self._check_write(raw, real)
        text = self._read_text(real)
        lines = text.splitlines(keepends=True)
        if line < 0 or line > len(lines):
            raise _Refused(f"`insert_line` {line} is outside 0..{len(lines)}")
        if line and not lines[line - 1].endswith("\n"):
            lines[line - 1] += "\n"
        block = new if new.endswith("\n") or line == len(lines) else new + "\n"
        self._guarded(real, new, "")
        self._write_text(real, "".join(lines[:line]) + block + "".join(lines[line:]), create=False)
        return EditResult(f"Edited {real}: inserted text after line {line}.")
