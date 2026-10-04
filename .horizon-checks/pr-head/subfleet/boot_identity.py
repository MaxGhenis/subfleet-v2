"""Stable macOS boot identity, with conservative legacy timestamp support."""

import re
import uuid
from collections.abc import Callable


def session_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def boot_seconds(read: Callable[[list[str]], str]) -> str | None:
    value = read(["/usr/sbin/sysctl", "-n", "kern.boottime"]).strip()
    match = re.search(r"\bsec\s*=\s*(\d+)", value)
    return match.group(1) if match else value if value.isdecimal() else None


def read_identity(read: Callable[[list[str]], str]) -> str | None:
    value = session_uuid(read(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]))
    return value or boot_seconds(read)


def matches(recorded: str, current: str, legacy_seconds: Callable[[], str | None]) -> bool | None:
    """A shifted legacy wall-clock timestamp is uncertainty, never proof of death.

    An old receipt can still match on its timestamp plus the caller's exact
    process start identity. New receipts use the kernel's boot-session UUID,
    which does not shift when macOS adjusts its wall clock.
    """
    if recorded == current:
        return True
    if recorded.isdecimal():
        seconds = current if current.isdecimal() else legacy_seconds()
        return True if recorded == seconds else None
    if current.isdecimal() and session_uuid(recorded):
        return None
    return False
