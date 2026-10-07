"""C-6.5/C-26.3: comparison keys, including leases from older daemons.

Keep old rows rather than migrating a primary key: several old aliases may
already have different holders. Every such holder must remain a guard until
the ordinary holder-keyed release removes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from . import folders
from .conversations.store import canonical_native


def native_key(provider: str, session_id: str) -> str:
    return f"native:{provider}:{canonical_native(session_id)}"


def canonical_native_key(key: str) -> str:
    """Normalize only UUID subjects; opaque ids and other namespaces stay exact."""
    if key.startswith(("native:", "native-session:")):
        prefix, scope, session = key.split(":", 2)
        return f"{prefix}:{scope}:{canonical_native(session)}"
    if key.startswith("session:") and key.endswith(":revive"):
        return f"session:{canonical_native(key[8:-7])}:revive"
    return key


def _pair(row) -> tuple[str, str]:
    return (row["lease_key"], row["holder"]) if isinstance(row, dict) else (row[0], row[1])


def native_holds(read: Callable, key: str) -> list[tuple[str, str]]:
    """SQL-only lookup of canonical and old raw UUID keys, within one namespace."""
    prefix = key.partition(":")[0] + ":"
    if prefix not in ("native:", "native-session:", "session:"):
        return [_pair(row) for row in read("SELECT lease_key,holder FROM leases WHERE lease_key=?", (key,))]
    wanted = canonical_native_key(key)
    return [_pair(row) for row in read(
        "SELECT lease_key,holder FROM leases WHERE lease_key>=? AND lease_key<?",
        (prefix, prefix[:-1] + ";")) if canonical_native_key(_pair(row)[0]) == wanted]


def continuation_holds(read: Callable, session_id: str) -> list[tuple[str, str]]:
    """Old lane-scoped continuation guards also exclude a turn on any lane."""
    wanted = canonical_native(session_id)
    return [_pair(row) for row in read(
        "SELECT lease_key,holder FROM leases WHERE lease_key>=? AND lease_key<?",
        ("native-session:", "native-session;"))
        if canonical_native(_pair(row)[0].split(":", 2)[2]) == wanted]


@dataclass(frozen=True)
class OutputClaim:
    """Filesystem identity established off the store lock; rechecked with SQL.

    Submit holds its submit lock while preparing this snapshot, so no output
    job can appear between the live-job census and its insertion. Admission
    can add leases meanwhile, but this daemon writes only canonical keys,
    which are always included. Old raw rows never change their keys.
    """
    key: str
    keys: tuple[str, ...]
    jobs: tuple[str, ...]

    @classmethod
    def prepare(cls, read: Callable, path: str, *, identities: dict[str, str] | None = None) -> OutputClaim:
        # A submit/export owns one memo; every reservation in an admission
        # pass shares one. No filesystem answers survive the operation.
        if identities is None:
            identities = {}

        def identity(value):
            if value not in identities:
                try:
                    identities[value] = folders.identity(value)
                except OSError as exc:
                    identities[value] = folders.identity_fallback(value, exc)
            return identities[value]

        wanted = identity(path)
        key = f"out:{wanted}"
        keys = {key}
        for row in read("SELECT lease_key FROM leases WHERE lease_key>=? AND lease_key<?", ("out:", "out;")):
            raw = row["lease_key"]
            if identity(raw[4:]) == wanted:
                keys.add(raw)
        jobs = tuple(row["job_id"] for row in read(
            "SELECT job_id,out_path FROM jobs WHERE out_path IS NOT NULL "
            "AND state NOT IN ('succeeded','failed','cancelled','lost')", ())
            if identity(row["out_path"]) == wanted)
        return cls(key, tuple(sorted(keys)), jobs)

    def holds(self, read: Callable) -> list[tuple[str, str]]:
        return [_pair(row) for row in read(
            f"SELECT lease_key,holder FROM leases WHERE lease_key IN ({','.join('?' for _ in self.keys)}) "
            "ORDER BY acquired_at,holder,lease_key",
            self.keys)]

    def live_jobs(self, read: Callable) -> Iterable:
        if not self.jobs:
            return ()
        return read(f"SELECT job_id FROM jobs WHERE job_id IN ({','.join('?' for _ in self.jobs)}) "
                    "AND state NOT IN ('succeeded','failed','cancelled','lost')", self.jobs)
