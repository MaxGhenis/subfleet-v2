"""D-KILL2: a process whose argument buffer shows no environment, on any macOS.

The incidents' lockf carried its attempt's markers in its environment, but its
`KERN_PROCARGS2` buffer held only its argv. On macOS 26.6.2 (observed 2026-10-18 and
2026-10-19) the kernel returns a waiting `/usr/bin/lockf`'s buffer as argc, the
executable path, its padding and the argv strings, and nothing after them, so neither
`ps -E` nor C-5.5's raw read sees the markers. GitHub's macos-15-arm64 runner (macOS
15.7.9, image 20260907.0337.1, observed 2026-10-19) returns lockf's environment, so
there the incident cannot be reproduced with lockf. Tests that need it on every host
hide the chosen pids' environments here instead: `HidingKernel` for the census's
reader (`procs.kernel`) and `hiding_ps` for the installed census's `ps -axEww` read
(`tests/reference_census_c697.py`). Nothing outside tests imports this.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
import ctypes
from pathlib import Path
import platform
import sys

from subfleet import procs

#: The host's own reader, whatever a test has put in `procs.kernel`.
REAL = procs.Darwin()


def shown_reason() -> str:
    """Why a real-lockf test skips on a host whose kernel shows lockf's environment."""
    version = platform.mac_ver()[0] or "unknown"
    return (f"this host (macOS {version}) shows lockf's environment; the hidden-marker "
            "incident can't be reproduced with lockf here")


def shows_markers(pid: int, attempt_id: str, root: str) -> bool:
    """Does `pid`'s raw buffer, read from the host's kernel, show the attempt's markers?"""
    buffer = REAL.arguments(pid, ctypes.create_string_buffer(procs._ARGS_BUFFER))
    return buffer is not None and procs.procargs_marked(buffer, *procs.marker_strings(attempt_id, root))


def without_environment(buffer: bytes) -> bytes:
    """`buffer` as macOS 26.6.2 returns it for a hardened-runtime Apple platform binary:
    argc, the executable path, its NUL padding and the first argc strings, and nothing
    after them. A buffer that ends inside its argv keeps its whole strings. A first argv
    string that is empty reads as padding, as `procs.procargs_strings` reads it too."""
    if len(buffer) <= 4:
        return buffer
    end = buffer.find(b"\0", 4)
    if end < 0:
        return buffer
    position = end
    while position < len(buffer) and buffer[position] == 0:
        position += 1
    for _ in range(int.from_bytes(buffer[:4], sys.byteorder)):
        stop = buffer.find(b"\0", position)
        if stop < 0:
            return buffer[:position]
        position = stop + 1
    return buffer[:position]


def argv_of(buffer: bytes) -> list[bytes]:
    """The argv strings of `buffer`: the last argc strings once the environment is gone."""
    argc = int.from_bytes(buffer[:4], sys.byteorder) if len(buffer) >= 4 else 0
    strings = procs.procargs_strings(without_environment(buffer))
    return strings[len(strings) - argc:] if argc else []


class HidingKernel:
    """`procs.kernel` with every argument buffer of a pid in `hidden()` cut after its
    argv (`without_environment`); every other read is the real reader's. `hid` names
    each pid it has hidden a buffer of."""

    def __init__(self, real, hidden: Callable[[], Collection[int]]):
        self._real, self._hidden = real, hidden
        self.hid: set[int] = set()

    def arguments(self, pid: int, buffer) -> bytes | None:
        found = self._real.arguments(pid, buffer)
        if found is None or pid not in self._hidden():
            return found
        self.hid.add(pid)
        return without_environment(found)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def published(paths: Iterable[Path | str]) -> Callable[[], frozenset[int]]:
    """The pids written in `paths` (a fixture's pid files), each read until it holds one;
    a pid once read stays hidden."""
    paths = [Path(path) for path in paths]
    found: dict[Path, int] = {}

    def hidden() -> frozenset[int]:
        for path in paths:
            if path not in found:
                try:
                    found[path] = int(path.read_text())
                except (OSError, ValueError):
                    continue
        return frozenset(found.values())
    return hidden


def hiding_ps(real_read: Callable[..., str], hidden: Callable[[], Collection[int]],
              kernel=None) -> Callable[..., str]:
    """`procs._read` for the installed census: in its `ps -axEww -o pid=,command=` read,
    the row of each pid in `hidden()` is its argv alone, as `ps -E` prints a process
    whose buffer shows no environment, and the row of one gone since is dropped. Every
    other read is the real one."""
    kernel = REAL if kernel is None else kernel

    def read(argv: list[str], **options) -> str:
        text = real_read(argv, **options)
        pids = hidden()
        if "-axEww" not in argv or not pids:
            return text
        rows = []
        for row in text.splitlines():
            pid_text = row.strip().partition(" ")[0]
            if not (pid_text.isdigit() and int(pid_text) in pids):
                rows.append(row)
                continue
            buffer = kernel.arguments(int(pid_text), ctypes.create_string_buffer(procs._ARGS_BUFFER))
            if buffer is not None:
                rows.append(f"{pid_text} " + b" ".join(argv_of(buffer)).decode(errors="replace"))
        return "".join(f"{row}\n" for row in rows)
    return read
