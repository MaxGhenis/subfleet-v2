"""Host memory pressure for admission (C-6.15): what macOS's compressor occupies.

`vm_stat` prints the page size and `Pages occupied by compressor`; their product
is the memory the compressor holds. The reading is taken outside any store
transaction, at most once per `host_pressure.sample_s`, and only while
`host_pressure.enabled` is true; `scheduler.evaluate` decides what it holds.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import procs
from .contracts import HOST_PRESSURE_DEFAULTS

GIB = 1024 ** 3
#: A reading this many sample intervals old is no longer evidence, so it holds nothing.
STALE_AFTER = 4

_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_OCCUPIED = re.compile(r"^Pages occupied by compressor:\s+(\d+)\.?\s*$", re.MULTILINE)


def settings(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {**HOST_PRESSURE_DEFAULTS, **(policy.get("host_pressure") or {})}


def parse_vm_stat(text: str) -> int | None:
    """Bytes the compressor occupies, or None when `vm_stat` did not say."""
    size, pages = _PAGE_SIZE.search(text), _OCCUPIED.search(text)
    if size is None or pages is None:
        return None
    return int(size.group(1)) * int(pages.group(1))


def read_compressor_bytes() -> int | None:
    """One `vm_stat`, started without a fork as `ps` is (C-5.12); None when it cannot be read."""
    try:
        return parse_vm_stat(procs._read(["/usr/bin/vm_stat"]))
    except procs.InspectionError:
        return None


class Sampler:
    """The last reading, read again at most once per interval.

    A read that fails is rationed like one that works, and a caller that finds a
    read running returns without waiting for it. The daemon reads on a worker of
    its own (`Daemon._control`), never on admission's, so a slow `vm_stat` (it is
    given 10 s, and the host is under pressure when it matters) delays no pass:
    admission uses the last reading, and a reading too old holds nothing.

    Ages are on the monotonic clock, which does not run while a Mac sleeps: after
    a wake a reading can be hours older than its age says, for at most one
    `sample_s`, until the next read replaces it.
    """

    def __init__(self, read: Callable[[], int | None] = read_compressor_bytes,
                 clock: Callable[[], float] = time.monotonic):
        self._read, self._clock = read, clock
        self._lock = threading.Lock()
        self._reading_now = False
        self._began: float | None = None            # when the last read began
        self._last: tuple[float, int] | None = None  # (when it was read, bytes)

    def due(self, sample_s: float) -> bool:
        """Whether `refresh` would read now: no read is running and the last began an interval ago."""
        with self._lock:
            return not self._reading_now and (self._began is None or self._clock() - self._began >= sample_s)

    def refresh(self, sample_s: float) -> None:
        with self._lock:
            now = self._clock()
            if self._reading_now or (self._began is not None and now - self._began < sample_s):
                return
            self._reading_now, self._began = True, now
        value = None
        try:
            value = self._read()
        except Exception:                           # noqa: BLE001 - an unreadable host holds nothing
            pass
        finally:
            # One section: a second read cannot begin between the flag falling
            # and this read's value being kept, and overwrite a newer one.
            with self._lock:
                self._last = (self._clock(), value) if value is not None else None
                self._reading_now = False

    def reading(self, sample_s: float) -> dict[str, Any] | None:
        """The last reading while it is evidence: read, and less than `STALE_AFTER` intervals old."""
        with self._lock:
            last, now = self._last, self._clock()
        if last is None or now - last[0] > STALE_AFTER * sample_s:
            return None
        return {"compressor_bytes": last[1], "age_s": round(now - last[0], 1)}
