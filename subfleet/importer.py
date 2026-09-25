"""Import v1's state into the v2 store, per the manifest in `docs/migration.md`.

The manifest is the specification: every v1 store found on 2026-09-05 is
classified there, and this module has one function per row whose disposition is
`import`. A row not in the manifest is reported and left alone (migration.md,
"Anything found at import time that is not in this table").

Four properties hold for every row (migration.md principle 4):

* **Read-only towards v1.** Nothing here opens a v1 path for writing. The one
  write to a v1 file in this repository is `subfleet lanes transfer`, which
  lives in `lanes_transfer.py` behind `--i-understand-v1-edit`.
* **Idempotent.** A second run changes nothing: every row function checks the
  store for what it is about to write before writing it.
* **Incremental.** Each store keeps a cursor in `events` rows of kind
  `import.cursor` (last id, last mtime, last line), so a later pass reads only
  what is new — and revisits the v1 runs that were still live last time
  (migration.md principle 3).
* **Facts, not estimates.** Learned percentages, token-sum ratios and derived
  window boundaries never become readings (migration.md principle 2).

## The dry run

`import_v1(..., dry_run=True)` is a real import against a throwaway copy of the
store: it snapshots `state.sqlite3` into a scratch database (C-3.4 keeps the real
one read-only), imports into that, and deletes it. Nothing under the state root
changes except the report, no `jobs/<id>/manifest.json` is written, no salt is
copied, and no v1 artifact is hashed. The counts are therefore what a real pass
would write, not an estimate of it. The report JSON is still saved to
`<state root>/import-report-<utc>.json`, because the report is the deliverable of
a dry run; pass `write_report=False` to suppress even that.

The opt-in dry run against the real v1 state is `tests/live/test_import_dry_run.py`,
gated by `SUBFLEET_LIVE=1` (C-20.1). From a shell:

    SUBFLEET_HOME=~/.subfleet uv run python -m subfleet.importer --dry-run

A real import refuses to start while a daemon holds `daemon.lock` (plan
amendment 3): the daemon is the store's writer, and two writers is the one thing
the migration must never do.

## The legacy cockpit

`outbox.sqlite3` and `cockpit-client/` are the legacy desktop cockpit's message
outbox and client journal (C-30.4, design §13; the manifest rows `outbox` and
`cockpit`, milestone 9). Terminal outbox messages become read-only history in
`conversations.sqlite3`; everything else keeps its legacy owner and is reported
item by item. They run in a milestone-9 pass, or alone:

    python -m subfleet.importer --legacy-cockpit --v1-state <dir> [--dry-run] [--json]

which opens no `state.sqlite3` and holds `daemon.lock` exactly as above.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import procs
from .actions import reset_credit_op_keys
from .conversations import legacy
from .conversations.store import ConversationError, ConversationStore
from .contracts import READING_TTL_S, WINDOW_KEYS
from .policy import DEFAULT_POLICY_PATH
from .store import Store, utc_now

# --- where v1 lives (migration.md, "Import manifest": S and D) ----------------

V1_STATE = Path("~/chief-of-staff/state/subfleet").expanduser()
DELEGATE_STATE = Path("~/.local/state/delegate").expanduser()
V1_ROSTER_DIR = Path("~/chief-of-staff/subfleet").expanduser()

CLAUDE_ROSTER = "claude-accounts.json"
CODEX_ROSTER = "codex-accounts.json"

#: Milestone this lane serves. Rows the manifest stages later than this are
#: reported as `staged` and left alone until the integrator raises it.
DEFAULT_MILESTONE = 4

#: v1 run ids sort by their `YYYYMMDD-HHMMSS-` prefix, so the newest imported id
#: is a high-water mark (C-1.1).
RUN_ID_PREFIX_LEN = len("YYYYMMDD-HHMMSS")

#: rc values v1 records for a killed run (migration.md `S/runs/`: "-9/143/killed").
#: -15/137 are the same two signals in the other two encodings v1's runner uses.
KILLED_RCS = frozenset({-9, -15, 137, 143})

#: C-30.4: the milestone of the legacy cockpit rows (`outbox`, `cockpit`). The
#: outbox is the cockpit's message outbox, not a notice outbox; its terminal
#: messages become conversation history (`subfleet/conversations/legacy.py`).
LEGACY_MILESTONE = 9

#: v1 capacity window names to their duration in minutes, keyed by C-9.7.
V1_WINDOW_MINUTES = {"five_hour": 300, "weekly": 10080, "seven_day": 10080}

#: C-9.8: the only `window` value that is not a duration key.
ADMISSION_WINDOW = "admission"


# --- the manifest as data -----------------------------------------------------

@dataclass(frozen=True)
class ManifestRow:
    """One row of the import manifest in `docs/migration.md`."""

    key: str
    disposition: str            # import | retain | drop
    milestone: int              # the milestone the row is imported at
    destination: str            # the manifest's "v2 destination and rules", abridged
    names: tuple[str, ...] = ()  # entries of S (or D, prefixed "D/") the row claims
    root: str = "S"


MANIFEST: tuple[ManifestRow, ...] = (
    ManifestRow("roster", "import", 4, "lanes rows with owner: v1", (), "roster"),
    ManifestRow("runs", "import", 4, "jobs, attempts, artifacts at the v1 paths", ("runs",)),
    ManifestRow("runs-out", "retain", 0, "read-only side files of named runs", ("runs-out",)),
    ManifestRow("notices", "import", 4, "notices rows, pending or surfaced", ("notices",)),
    ManifestRow("outbox", "import", LEGACY_MILESTONE,
                "the cockpit's message outbox: terminal messages become legacy history in "
                "conversations.sqlite3, the rest keep their legacy owner (C-30.4)",
                ("outbox.sqlite3", "outbox.sqlite3-wal", "outbox.sqlite3-shm", "outbox-attachments")),
    ManifestRow("gates", "retain", 7, "gates finish in v1; import verifies at milestone 7", ("gates",)),
    ManifestRow("sessions-kit", "retain", 6, "tickle dedup and native workers as events",
                ("tickles", "revive", "revive-lane.json", "session-locks",
                 "session-continuations.lock", "native-workers.json")),
    ManifestRow("history", "drop", 0, "never readings or percentages; the file stays",
                ("history.jsonl", "lane-usage.jsonl")),
    ManifestRow("capacity-live-cache", "import", 4, "readings per account and window",
                ("capacity-live-cache.json",)),
    ManifestRow("claude-oauth-raw", "import", 4, "readings for the desktop account",
                ("claude-oauth-raw.json",)),
    ManifestRow("statusline", "drop", 0, "the tap is dead for the desktop app",
                ("claude-statusline.json", "claude-statusline-history.jsonl",
                 "claude-statusline-invoked.json")),
    ManifestRow("derived-caches", "drop", 0, "regenerated by the daemon's probe cycle",
                ("snapshot.json", "rollout-scan-cache.json", "rollout-scan-memo.json",
                 "refresh-probes.json")),
    ManifestRow("keepalive", "import", 4, "admission-observed readings on the Haiku model",
                ("keepalive.json",)),
    ManifestRow("reset-policy", "import", 4, "confirmed reset-credit actions", ("reset-policy.json",)),
    ManifestRow("alerts", "import", 5, "events of kind alert-latch", ("alerts.json",)),
    ManifestRow("cooldowns", "import", 4, "closures with scope, clock source, source_event",
                ("D/cooldowns.json", "D/cooldowns.json.lock"), "D"),
    ManifestRow("decisions", "retain", 0, "inputs to the shadow-week compare script",
                ("D/decisions.jsonl", "D/rotation.json"), "D"),
    ManifestRow("prompts", "retain", 0, "referenced by imported v1 artifacts",
                ("prompts", "briefs", "dispatch")),
    ManifestRow("integration-events", "retain", 0,
                "kept and written to; the daemon emits to the same spool from milestone 5",
                ("integration-events",)),
    ManifestRow("cockpit", "retain", LEGACY_MILESTONE,
                "the cockpit client's journal: every entry keeps its legacy owner and is reported, "
                "never sent (C-30.4)", ("cockpit-client",)),
    ManifestRow("job-specific", "drop", 0, "regenerated or belonging to one finished campaign",
                ("composer-attachments", "iariw-drain.json", "iariw-drain.log",
                 "autopick.log", "brief.md")),
    ManifestRow("locks", "drop", 0, "SQLite and daemon.lock replace them; never copied",
                ("broker.lock", "broker.sock", ".integration-events.salt.lock",
                 "keepalive.json.lock", "reset-policy.json.lock", "revive.lock",
                 "rollout-scan.lock")),
    ManifestRow("salt", "import", 4, "copied so event ids stay stable across the cutover",
                ("integration-events.salt",)),
)

MANIFEST_BY_KEY = {row.key: row for row in MANIFEST}


# --- the report ---------------------------------------------------------------

@dataclass
class StoreReport:
    """Per-store outcome of one import pass (migration.md, principle 4)."""

    store: str
    disposition: str
    destination: str
    seen: int = 0
    imported: int = 0
    skipped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    #: Per-item dispositions, for the rows that owe one per item (C-30.4: every
    #: legacy message and journal entry). Ids and statuses only, never content.
    items: list[dict[str, Any]] = field(default_factory=list)

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped += count
        self.reasons[reason] = self.reasons.get(reason, 0) + count

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def count(self, reason: str, count: int = 1) -> None:
        """Record a reason without calling it a skip (facts about what was written)."""
        self.reasons[reason] = self.reasons.get(reason, 0) + count


@dataclass
class ImportReport:
    """What one pass saw, imported and skipped, and why (brief: written report)."""

    state_root: str
    v1_state: str
    delegate_state: str
    roster_dir: str
    dry_run: bool
    milestone: int
    started_at: str
    finished_at: str | None = None
    stores: dict[str, StoreReport] = field(default_factory=dict)
    unmanifested: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    path: str | None = None

    def store_report(self, key: str) -> StoreReport:
        row = MANIFEST_BY_KEY[key]
        if key not in self.stores:
            disposition = row.disposition
            if row.milestone > self.milestone:
                # The manifest's own words: an `import` row later than this pass
                # is staged, a `retain` row is retained until its milestone.
                disposition = (f"staged-milestone-{row.milestone}" if row.disposition == "import"
                               else f"{row.disposition}-until-milestone-{row.milestone}")
            self.stores[key] = StoreReport(key, disposition, row.destination)
        return self.stores[key]

    @property
    def imported(self) -> int:
        return sum(report.imported for report in self.stores.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "state_root": self.state_root,
            "v1_state": self.v1_state,
            "delegate_state": self.delegate_state,
            "roster_dir": self.roster_dir,
            "dry_run": self.dry_run,
            "milestone": self.milestone,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "imported_total": self.imported,
            "stores": {
                key: {
                    "store": report.store,
                    "disposition": report.disposition,
                    "destination": report.destination,
                    "seen": report.seen,
                    "imported": report.imported,
                    "skipped": report.skipped,
                    "reasons": dict(sorted(report.reasons.items())),
                    "notes": report.notes,
                    "cursor": report.cursor,
                    "items": report.items,
                }
                for key, report in sorted(self.stores.items())
            },
            "unmanifested": sorted(self.unmanifested),
            "errors": self.errors,
        }

    def write(self, state_root: Path) -> Path:
        """Save to `<state root>/import-report-<utc>.json` (brief: a written report)."""
        stamp = (self.finished_at or self.started_at).replace("-", "").replace(":", "")
        path = Path(state_root) / f"import-report-{stamp}.json"
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _publish(path, json.dumps(self.as_dict(), indent=1, sort_keys=True).encode() + b"\n")
        self.path = str(path)
        return path


def _publish(path: Path, payload: bytes) -> None:
    """C-8.1: temp file in the destination directory, fsync, rename, fsync the directory."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _copy_sqlite(source: Path, destination: Path) -> None:
    """Copy a SQLite database and its WAL sidecars, touching neither.

    Opening a WAL database read-only still creates and writes its `-shm` file, so
    the importer never opens a v1 database, or the live store during a dry run,
    in place: it copies `db`, `db-wal` and `db-shm` and opens the copy, where
    recovery may write freely. A copy taken while another process is committing
    can be torn; a real import refuses to run while a daemon holds the lock, and
    a dry run says in its report that its numbers are a snapshot.
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = source.with_name(source.name + suffix)
        if candidate.is_file():
            shutil.copy2(candidate, destination.with_name(destination.name + suffix))


class ImportRefused(RuntimeError):
    """A precondition of a real import is not met (a live daemon, a bad root)."""

    code = 7


# --- time, digests, small readers --------------------------------------------

def _utc(value: Any) -> str | None:
    """C-1.7: ISO 8601 UTC with a Z suffix and second precision.

    v1 writes local time with an offset (`iso()`, second precision) and epoch
    seconds in a few places; both convert here on read.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(float(value), timezone.utc)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    elif isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_s(observed: str | None, now: str) -> float | None:
    if not observed:
        return None
    start, end = _parse(observed), _parse(now)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _reading_label(observed_at: str | None, now: str) -> str:
    """C-9.1: a provider reading beyond `reading_ttl_s` is `stale-provider`."""
    age = _age_s(observed_at, now)
    return "provider" if age is not None and 0 <= age <= READING_TTL_S else "stale-provider"


def _read_json(path: Path) -> Any:
    """Read one v1 JSON file. A missing or malformed file is not an import failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sha256(path: Path) -> tuple[str, int] | None:
    """Stream a v1 file's digest and size for an `artifacts` row (C-8.2)."""
    digest, size = hashlib.sha256(), 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return digest.hexdigest(), size


def _window_key(name: str) -> str:
    """C-9.7: classify a window by duration, never by slot position."""
    minutes = V1_WINDOW_MINUTES.get(name)
    if minutes is None:
        return name
    return WINDOW_KEYS.get(minutes, str(minutes))


def _fraction(percent: Any) -> float | None:
    """v1 records 0..100; `readings.utilization` is a fraction in [0, 1] (C-9.8)."""
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    return max(0.0, min(1.0, float(percent) / 100.0))


def _model_ids(policy: Mapping[str, Any]) -> dict[str, str]:
    """Short name, declared scope and id, each to the provider model id (C-11.1)."""
    index: dict[str, str] = {}
    for short, model in (policy.get("models") or {}).items():
        identifier = model.get("id")
        if not identifier:
            continue
        index[short.lower()] = identifier
        index[identifier.lower()] = identifier
        if model.get("scope"):
            index[str(model["scope"]).lower()] = identifier
    for alias, short in (policy.get("retired") or {}).items():
        if short.lower() in index:
            index[alias.lower()] = index[short.lower()]
    return index


def _load_policy_models(state_root: Path) -> dict[str, str]:
    """The state root's policy if the daemon has written one, else the default."""
    for candidate in (state_root / "policy.json", DEFAULT_POLICY_PATH):
        value = _read_json(candidate)
        if isinstance(value, dict) and value.get("models"):
            return _model_ids(value)
    return {}


# --- the writer ---------------------------------------------------------------

class _Writer:
    """The store this pass writes to, and whether it may touch the filesystem.

    A dry run is not a set of suppressed writes: it is a real import against a
    snapshot. `import_v1` copies the store to a scratch database (or starts an
    empty one when no store exists yet) and hands it here, so every row function
    reads back exactly what it just wrote and the report counts what a real pass
    would do. Only effects outside that database - the `jobs/<id>/manifest.json`
    files, the salt copy, and hashing 600 MB of v1 artifacts - are skipped, and
    `dry_run` is the flag that skips them.
    """

    def __init__(self, store: Store | None, dry_run: bool):
        self.store = store
        self.dry_run = dry_run
        self.depth = 0

    @property
    def writable(self) -> bool:
        return self.store is not None

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.store.query(sql, params) if self.store is not None else []

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return self.store.one(sql, params) if self.store is not None else None

    def exists(self, sql: str, params: tuple[Any, ...] = ()) -> bool:
        return self.one(sql, params) is not None

    @contextmanager
    def transaction(self, kind: str, **keys: Any) -> Iterator[None]:
        """Record one item's rows together, with its audit event (C-3.2).

        Nested `_insert` calls become savepoints inside it. The transaction opens
        after every v1 read, `ps` call and digest the item needs, so it never
        spans a subprocess, a filesystem sync or a sleep (C-3.3).
        """
        if not self.writable:
            yield
            return
        self.depth += 1
        try:
            with self.store.transaction(kind, **keys):
                yield
        finally:
            self.depth -= 1

    def insert(self, table: str, values: Mapping[str, Any], *, kind: str | None = None) -> None:
        if self.writable:
            self.store._insert(table, dict(values), kind=kind)

    def update(self, table: str, key: str, identity: Any, values: Mapping[str, Any]) -> None:
        if self.writable:
            self.store._update(table, key, identity, dict(values))

    def event(self, kind: str, *, data: Mapping[str, Any] | None = None, **keys: Any) -> None:
        """Append one `events` row inside the caller's transaction (C-3.2).

        `Store.add_event` opens its own transaction, which then logs a second row
        of the same kind; an import that writes thousands of `tickle` rows needs
        exactly one row per fact. Outside a transaction the connection is in
        autocommit (`isolation_level=None`, `synchronous=FULL`), so the row is
        durable on return either way.
        """
        if not self.writable:
            return
        self.store.conn.execute(
            "INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) VALUES (?,?,?,?,?,?)",
            (utc_now(), kind, keys.get("job_id"), keys.get("attempt_id"), keys.get("lane_id"),
             json.dumps(dict(data or {}), sort_keys=True, separators=(",", ":"))))


# --- cursors (migration.md principle 4) ---------------------------------------

def read_cursors(writer: _Writer) -> dict[str, dict[str, Any]]:
    """The newest `import.cursor` event per store (last id, last mtime, last line)."""
    cursors: dict[str, dict[str, Any]] = {}
    for row in writer.query("SELECT data_json FROM events WHERE kind='import.cursor' ORDER BY event_id"):
        try:
            payload = json.loads(row["data_json"])
        except (ValueError, TypeError):
            continue
        name = payload.get("store")
        if isinstance(name, str) and isinstance(payload.get("cursor"), dict):
            cursors[name] = payload["cursor"]
    return cursors


def write_cursor(writer: _Writer, store: str, cursor: Mapping[str, Any], report: StoreReport) -> None:
    report.cursor = dict(cursor)
    writer.event("import.cursor", data={"store": store, "cursor": dict(cursor), "at": utc_now()})


# --- lanes (manifest row: claude-accounts.json, codex-accounts.json) ----------

def _codex_homes(home: Path) -> list[Path]:
    """`~/.codex-<n>` lanes; `~/.codex` is observed and never a lane (C-10.3)."""
    found = []
    for candidate in sorted(home.glob(".codex-*")):
        if candidate.name == ".codex" or not candidate.is_dir():
            continue
        if (candidate / "auth.json").is_file():
            found.append(candidate)
    return found


def _codex_lane_id(path: Path) -> str:
    """C-1.3: `<provider>-<n>`, taken from the home's own suffix so ids are stable."""
    suffix = path.name[len(".codex-"):] or path.name
    slug = "".join(character if character.isalnum() else "-" for character in suffix.lower())
    return f"codex-{slug.strip('-') or 'home'}"


def _tilde(path: str | Path, home: Path) -> str:
    text = str(path)
    root = str(home)
    return "~" + text[len(root):] if text.startswith(root) else text


def import_roster(writer: _Writer, report: StoreReport, *, roster_dir: Path, home: Path,
                  now: str) -> None:
    """Manifest row `claude-accounts.json, codex-accounts.json`: the roster.

    "lanes rows with `owner: v1` initially; credential refs copied, never values;
    `desktop` set from `~/.claude.json` `oauthAccount`; each Codex home's account
    key read from its `auth.json` at import" (C-1.3, C-1.4, C-10.1, C-10.3).

    Ownership is set on insert only. A lane already in the store keeps the owner
    it has: re-importing must never take an account back from v2 (C-10.4,
    migration.md principle 1).
    """
    existing = {row["lane_id"]: row for row in writer.query("SELECT * FROM lanes")}
    by_binding = {(row["provider"], row["account_key"], row["credential_ref"]): row
                  for row in existing.values()}

    def enrol(lane_id: str, provider: str, account_key: str, credential_ref: str,
              credential_kind: str, home_path: str | None, desktop: bool, enabled: bool,
              reason: str | None = None, label: str | None = None) -> None:
        report.seen += 1
        binding = (provider, account_key, credential_ref)
        if binding in by_binding:
            row = by_binding[binding]
            if label and not row.get("label"):
                # Older imports omitted the operator's email claim, leaving the
                # adapter nothing to compare its profile response against. Learn
                # that label once, never an identity or a replacement binding.
                values = {"label": label, "updated_at": now}
                if not row.get("identity") and row.get("identity_status") is None:
                    values["identity_status"] = "unverified"
                with writer.transaction("lane.import-label", lane_id=row["lane_id"],
                                        data={"label": label, "source": CLAUDE_ROSTER}):
                    writer.update("lanes", "lane_id", row["lane_id"], values)
                row.update(values)
                report.imported += 1
                report.count("missing-operator-label-repaired")
            else:
                report.skip("already-imported")
            if bool(row["desktop"]) != desktop:
                report.note(f"{row['lane_id']}: desktop flag differs from v1's current login; "
                            "the daemon re-reads it each probe cycle (C-10.3)")
            return
        if lane_id in existing:
            report.skip("lane-id-taken-by-a-different-binding")
            report.note(f"{lane_id} already binds a different account; v1 account "
                        f"{account_key} was not enrolled")
            return
        writer.insert("lanes", {
            "lane_id": lane_id, "provider": provider, "account_key": account_key,
            "credential_ref": credential_ref, "credential_kind": credential_kind,
            "credential_epoch": 1, "home": home_path, "owner": "v1",
            "desktop": int(desktop), "enabled": int(enabled), "plan": None,
            "label": label, "identity": None,
            "identity_status": "unverified" if label else None,
            "created_at": now, "updated_at": now,
        }, kind="lane.imported")
        existing[lane_id] = {"lane_id": lane_id, "desktop": int(desktop), "label": label}
        by_binding[binding] = existing[lane_id]
        report.imported += 1
        if reason:
            report.count(reason)

    desktop_account = ""
    claude_config = _read_json(home / ".claude.json")
    if isinstance(claude_config, dict):
        account = claude_config.get("oauthAccount")
        if isinstance(account, dict):
            desktop_account = str(account.get("emailAddress") or "").strip().lower()
    if not desktop_account:
        report.note("no ~/.claude.json oauthAccount: no lane is marked desktop (C-10.3)")

    roster = _read_json(roster_dir / CLAUDE_ROSTER)
    if not isinstance(roster, dict):
        report.note(f"{CLAUDE_ROSTER} is absent or malformed; no Claude lane imported")
        roster = {}
    enrolled = roster.get("enrolled") if isinstance(roster.get("enrolled"), dict) else {}
    transferred = (roster.get("transferred_to_v2")
                   if isinstance(roster.get("transferred_to_v2"), dict) else {})
    listed = [str(email) for email in (roster.get("accounts") or []) if isinstance(email, str)]
    order = list(dict.fromkeys(listed + list(enrolled) + list(transferred)))
    def next_claude_id() -> str:
        """C-1.3: `claude-<n>`, the first free n.

        Not the account's position in the roster: v1 inserts and removes entries,
        and an index that shifted would either rename a lane that already exists
        or collide with one, and a lane id is stable for the life of its binding.
        """
        index = 1
        while f"claude-{index}" in existing:
            index += 1
        return f"claude-{index}"

    for email in order:
        key = email.strip().lower()
        # C-10.1: the credential reference is a keychain item name, `claude-quota-<email>`
        # as v1. An account with no setup token has no item yet; the lane records
        # the name v1 would use and stays disabled until enrolment (C-10.2).
        reference = enrolled.get(email) or transferred.get(email) or f"claude-quota-{key}"
        enrol(next_claude_id(), "claude", f"claude:{key}", str(reference), "keychain-token",
              None, key == desktop_account, email in enrolled,
              None if email in enrolled else "not-enrolled-in-v1", label=key)

    for path in _codex_homes(home):
        auth = _read_json(path / "auth.json")
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        account_id = (tokens or {}).get("account_id") if isinstance(tokens, dict) else None
        if isinstance(auth, dict) and (auth.get("OPENAI_API_KEY") or auth.get("auth_mode") == "apikey"):
            report.seen += 1
            report.skip("api-key-login-refused")            # C-10.2
            continue
        if not account_id:
            report.seen += 1
            report.skip("no-account-id-in-auth-json")       # C-1.4
            report.note(f"{_tilde(path, home)}: auth.json has no tokens.account_id; not enrolled")
            continue
        enrol(_codex_lane_id(path), "codex", f"codex:{account_id}", str(path), "home",
              str(path), False, True)

    codex_roster = _read_json(roster_dir / CODEX_ROSTER)
    if isinstance(codex_roster, dict) and codex_roster.get("protected_account"):
        protected = codex_roster["protected_account"]
        report.note("v1 protected_account (fallback app account) is "
                    f"{json.dumps(protected, sort_keys=True)}; v2 marks only the Claude "
                    "desktop login (C-10.3), so no Codex lane is flagged from it")


# --- lane lookup shared by the readings, closures and runs rows ---------------

def _lane_index(writer: _Writer, home: Path) -> dict[str, str]:
    """Every name a v1 file uses for a lane, to the v2 lane id."""
    index: dict[str, str] = {}
    lanes = {row["lane_id"]: row for row in writer.query("SELECT * FROM lanes")}
    for row in lanes.values():
        lane_id = row["lane_id"]
        for name in (lane_id, row["account_key"], row["credential_ref"], row["home"]):
            if name:
                index.setdefault(str(name).lower(), lane_id)
        account = str(row["account_key"])
        if ":" in account:
            index.setdefault(account.split(":", 1)[1].lower(), lane_id)
        for path in (row["home"], row["credential_ref"]):
            if path and str(path).startswith(str(home)):
                index.setdefault(_tilde(path, home).lower(), lane_id)
    # Moving a Codex home must not disconnect v1 history from its account. The
    # journal records both paths and the account at the ownership flip. Accept
    # only a move into this lane's still-current binding, never an inferred
    # ~/.codex-N name or an alias that another current lane now owns.
    old_names: dict[str, set[str]] = {}
    for event in writer.query("SELECT lane_id,data_json FROM events WHERE kind='lane.transferred' ORDER BY event_id"):
        lane = lanes.get(event["lane_id"])
        if lane is None or lane["provider"] != "codex":
            continue
        try:
            data = json.loads(event["data_json"])
        except (TypeError, ValueError):
            continue
        if (not isinstance(data, dict) or data.get("from") != "v1" or data.get("to") != "v2"
                or data.get("account_key") != lane["account_key"]):
            continue
        move = data.get("home_move")
        if (not isinstance(move, list) or len(move) != 2
                or not all(isinstance(path, str) and Path(path).is_absolute() for path in move)
                or move[1] != lane["home"] or move[1] != lane["credential_ref"]):
            continue
        for name in (move[0], _tilde(move[0], home)):
            old_names.setdefault(name.lower(), set()).add(lane["lane_id"])
    for name, candidates in old_names.items():
        # A numbered home can later host another account and be transferred
        # again. Without a source timestamp/account binding, neither the first
        # nor the latest historical owner can safely claim all records there.
        if len(candidates) == 1:
            index.setdefault(name, next(iter(candidates)))
    return index


def _lane_of(index: Mapping[str, str], name: Any, home: Path) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    for candidate in (name, name.lower(), _tilde(name, home).lower(),
                      str(Path(name).expanduser()).lower()):
        if candidate.lower() in index:
            return index[candidate.lower()]
    return None


def _add_reading(writer: _Writer, report: StoreReport, *, lane_id: str, scope: str,
                 window: str, utilization: float | None, resets_at: str | None,
                 label: str, source: str, observed_at: str) -> None:
    """Insert one reading unless the same observation is already recorded.

    The caller counts the row in `seen`; this counts only what it wrote.
    """
    if writer.exists(
            "SELECT 1 FROM readings WHERE lane_id=? AND scope=? AND window=? AND source=? "
            "AND observed_at=? AND label=? AND (utilization IS ?)",
            (lane_id, scope, window, source, observed_at, label, utilization)):
        report.skip("already-imported")
        return
    writer.insert("readings", {
        "lane_id": lane_id, "scope": scope, "window": window, "utilization": utilization,
        "resets_at": resets_at, "label": label, "source": source,
        "observed_at": observed_at, "attempt_id": None,
    }, kind="reading.imported")
    report.imported += 1


# --- capacity-live-cache.json -------------------------------------------------

def import_capacity_cache(writer: _Writer, report: StoreReport, *, v1_state: Path,
                          home: Path, cursor: dict[str, Any], now: str) -> dict[str, Any]:
    """Manifest row `S/capacity-live-cache.json`: the last wham probe results.

    "`readings` rows per account and window classified by duration (C-9.7), label
    `provider` if `probed_at` within `READING_TTL_S`, else `stale-provider`."

    A window v1 marked with any confidence other than `live` is a learned or
    derived value, and `learned_capacity` is never read at all: principle 2,
    "Learned percentages, token-sum ratios, and derived window boundaries are
    never imported as quota."
    """
    path = v1_state / "capacity-live-cache.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        report.skip("absent-or-malformed")
        return cursor
    probed_at = _utc(payload.get("probed_at"))
    if not probed_at:
        report.skip("no-probed-at")
        return cursor
    if cursor.get("probed_at") == probed_at:
        report.skip("cursor-unchanged")
        return cursor
    label = _reading_label(probed_at, now)
    index = _lane_index(writer, home)
    accounts = payload.get("accounts")
    for account in accounts if isinstance(accounts, list) else []:
        if not isinstance(account, dict):
            continue
        family = account.get("family")
        lane_id = (_lane_of(index, account.get("id"), home)
                   or _lane_of(index, account.get("account_id"), home)
                   or _lane_of(index, account.get("email"), home))
        if lane_id is None:
            report.seen += 1
            report.skip("no-lane-for-account")
            continue

        source = "wham" if family == "codex" else "oauth-usage"
        for name in ("five_hour", "weekly"):
            window = account.get(name)
            if not isinstance(window, dict):
                continue
            report.seen += 1
            if window.get("confidence") != "live":
                report.skip("not-a-live-reading")
                continue
            utilization = _fraction(window.get("used_percent"))
            if utilization is None:
                report.skip("no-used-percent")
                continue
            _add_reading(writer, report, lane_id=lane_id, scope="account",
                         window=_window_key(name), utilization=utilization,
                         resets_at=_utc(window.get("reset_at")), label=label,
                         source=source, observed_at=probed_at)
        if account.get("learned_capacity") is not None:
            report.count("learned-capacity-not-imported")
        if account.get("scoped_limits"):
            report.count("scoped-limits-left-to-the-claude-oauth-raw-row")
    report.note("the file's one claude-family account is imported with source "
                "oauth-usage; the manifest's rule column is per account, its "
                "description names the Codex wham probe")
    return {"probed_at": probed_at, "mtime": path.stat().st_mtime if path.exists() else None}


# --- claude-oauth-raw.json ----------------------------------------------------

def import_desktop_oauth(writer: _Writer, report: StoreReport, *, v1_state: Path,
                         home: Path, models: Mapping[str, str], cursor: dict[str, Any],
                         now: str) -> dict[str, Any]:
    """Manifest row `S/claude-oauth-raw.json`: the last desktop OAuth payload.

    "`readings` for the desktop account, label by age as above; per-model scoped
    limits in the payload become `admission-observed` rows for those models."

    The payload's `utilization` is a percent, not the [0, 1] fraction of a
    `rate_limit_event` (C-9.8), so it is divided here. A scoped limit becomes an
    `admission-observed` row with no utilization: that label means "remaining
    quota unknown" (C-9.1) and plan amendment 15 forbids rendering a percentage
    from anything but a provider reading.
    """
    path = v1_state / "claude-oauth-raw.json"
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("raw"), dict):
        report.skip("absent-or-malformed")
        return cursor
    checked_at = _utc(payload.get("checked_at"))
    if not checked_at:
        report.skip("no-checked-at")
        return cursor
    if cursor.get("checked_at") == checked_at:
        report.skip("cursor-unchanged")
        return cursor
    lane = writer.one("SELECT * FROM lanes WHERE desktop=1 AND provider='claude' ORDER BY lane_id")
    if lane is None:
        report.skip("no-desktop-lane")
        report.note("no lane carries the desktop flag; import the roster first (C-10.3)")
        return cursor
    lane_id, raw = lane["lane_id"], payload["raw"]
    label = _reading_label(checked_at, now)
    for name in ("five_hour", "seven_day"):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        report.seen += 1
        utilization = _fraction(window.get("utilization"))
        if utilization is None:
            report.skip("no-utilization")
            continue
        _add_reading(writer, report, lane_id=lane_id, scope="account", window=name,
                     utilization=utilization, resets_at=_utc(window.get("resets_at")),
                     label=label, source="oauth-usage", observed_at=checked_at)

    scoped: dict[str, str | None] = {}
    for limit in raw.get("limits") if isinstance(raw.get("limits"), list) else []:
        if not isinstance(limit, dict):
            continue
        model = (limit.get("scope") or {}).get("model") if isinstance(limit.get("scope"), dict) else None
        if not isinstance(model, dict):
            continue
        name = str(model.get("id") or model.get("display_name") or "").strip()
        if name:
            scoped[name] = _utc(limit.get("resets_at"))
    for key, window in raw.items():
        if not key.startswith("seven_day_") or not isinstance(window, dict):
            continue
        scoped.setdefault(key[len("seven_day_"):], _utc(window.get("resets_at")))
    for name, resets_at in sorted(scoped.items()):
        report.seen += 1
        model_id = models.get(name.lower())
        if model_id is None:
            report.skip("unmapped-scoped-model")
            report.note(f"payload scopes a limit to {name!r}, which no policy model "
                        "names; no reading written")
            continue
        _add_reading(writer, report, lane_id=lane_id, scope=model_id, window=ADMISSION_WINDOW,
                     utilization=None, resets_at=resets_at, label="admission-observed",
                     source="oauth-usage", observed_at=checked_at)
    unmapped = sorted(key for key, value in raw.items()
                      if isinstance(value, dict) and "utilization" in value
                      and key not in ("five_hour", "seven_day") and not key.startswith("seven_day_"))
    if unmapped:
        report.note("payload windows with no manifest row, left alone: " + ", ".join(unmapped))
    return {"checked_at": checked_at, "mtime": path.stat().st_mtime if path.exists() else None}


# --- keepalive.json -----------------------------------------------------------

def import_keepalive(writer: _Writer, report: StoreReport, *, v1_state: Path, home: Path,
                     models: Mapping[str, str], cursor: dict[str, Any]) -> dict[str, Any]:
    """Manifest row `S/keepalive.json`: the keepalive pings per lane.

    "`readings` with label `admission-observed`, scope the Haiku model id, source
    `keepalive`, observed at the ping time; never a window reset."

    The ping time is `last_opened_at`: the moment a keepalive turn actually
    opened the window on that account. A lane v1 only checked, or skipped for
    auth, never ran the model and is not admission evidence (C-9.1).
    """
    path = v1_state / "keepalive.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        report.skip("absent-or-malformed")
        return cursor
    updated_at = _utc(payload.get("updated_at"))
    if cursor.get("updated_at") and cursor["updated_at"] == updated_at:
        report.skip("cursor-unchanged")
        return cursor
    haiku = models.get("haiku")
    if not haiku:
        report.skip("no-haiku-model-in-policy")
        return cursor
    index = _lane_index(writer, home)
    lanes = payload.get("lanes")
    for account, entry in sorted((lanes or {}).items()) if isinstance(lanes, dict) else ():
        if not isinstance(entry, dict):
            continue
        report.seen += 1
        lane_id = _lane_of(index, account, home)
        if lane_id is None:
            report.skip("no-lane-for-account")
            continue
        opened_at = _utc(entry.get("last_opened_at"))
        if not opened_at:
            report.skip("never-opened")
            continue
        _add_reading(writer, report, lane_id=lane_id, scope=haiku, window=ADMISSION_WINDOW,
                     utilization=None, resets_at=None, label="admission-observed",
                     source="keepalive", observed_at=opened_at)
    return {"updated_at": updated_at, "mtime": path.stat().st_mtime if path.exists() else None}


# --- reset-policy.json --------------------------------------------------------

def import_reset_policy(writer: _Writer, report: StoreReport, *, v1_state: Path, home: Path,
                        cursor: dict[str, Any]) -> dict[str, Any]:
    """Manifest row `S/reset-policy.json`: reset-credit redemption history.

    "`actions` rows of kind `reset-credit`, state `confirmed`, `op_key` = account
    key plus credit id, so the one-at-a-time rule and the minimum interval
    respect history" (C-19.1, C-18.1).

    v1 records the credit id of the most recent redemption only; the per-home
    entries in `last_redemptions` carry a timestamp and no id. Those keep the
    same op_key shape with `at-<utc>` in the credit id's place, which is what the
    minimum-interval rule actually reads, and `request_json` says the id was
    absent rather than inventing one.
    """
    path = v1_state / "reset-policy.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        report.skip("absent-or-malformed")
        return cursor
    index = _lane_index(writer, home)
    redemptions: dict[str, tuple[str, str | None]] = {}
    for lane, moment in (payload.get("last_redemptions") or {}).items():
        stamp = _utc(moment)
        if stamp:
            redemptions[str(lane)] = (stamp, None)
    latest = _utc(payload.get("last_redeemed_at"))
    if latest and payload.get("lane"):
        redemptions[str(payload["lane"])] = (latest, payload.get("credit_id"))

    for lane, (redeemed_at, credit_id) in sorted(redemptions.items()):
        report.seen += 1
        lane_id = _lane_of(index, lane, home)
        row = writer.one("SELECT * FROM lanes WHERE lane_id=?", (lane_id,)) if lane_id else None
        if row is None and ("/" in lane or lane.startswith("~")):
            # A moved, reused or unknown home is not an account identity. Keep
            # unresolved evidence in v1 and report it instead of fabricating an
            # account-key action that cannot deduplicate the actual redemption.
            report.skip("reset-home-without-account-binding")
            report.note(f"{_tilde(lane, home)}: no unambiguous account binding; "
                        "reset-credit history left in v1")
            continue
        account_key = row["account_key"] if row else lane
        op_keys = reset_credit_op_keys(account_key, credit_id or 'at-' + redeemed_at)
        op_key = op_keys[0]
        # v1 remembers the credit id of the most recent redemption only, so the
        # same redemption is named `:<credit id>` on one pass and `:at-<utc>` on
        # the next, once v1 redeems somewhere else. One redemption is one action,
        # so the subject and the instant decide, not the op_key alone (C-19.1).
        if writer.exists("SELECT 1 FROM actions WHERE op_key IN (?,?)", op_keys) or writer.exists(
                "SELECT 1 FROM actions WHERE kind='reset-credit' AND subject=? AND created_at=?",
                (account_key, redeemed_at)):
            report.skip("already-imported")
            continue
        writer.insert("actions", {
            "action_id": op_key, "kind": "reset-credit", "op_key": op_key,
            "subject": account_key, "state": "confirmed",
            "request_json": json.dumps({"lane": lane, "lane_id": lane_id,
                                        "credit_id": credit_id,
                                        "email": payload.get("email") if lane == payload.get("lane") else None,
                                        "source": "v1-reset-policy",
                                        "credit_id_absent_in_v1": credit_id is None},
                                       sort_keys=True),
            "result_json": json.dumps({"redeemed_at": redeemed_at}, sort_keys=True),
            "created_at": redeemed_at, "updated_at": redeemed_at,
        }, kind="action.imported")
        report.imported += 1
        if lane_id is None:
            report.count("no-lane-for-account-key-kept-v1-name")
    return {"last_redeemed_at": latest, "mtime": path.stat().st_mtime if path.exists() else None}


# --- D/cooldowns.json ---------------------------------------------------------

def import_cooldowns(writer: _Writer, report: StoreReport, *, delegate_state: Path,
                     home: Path, models: Mapping[str, str], cursor: dict[str, Any],
                     now: str) -> dict[str, Any]:
    """Manifest row `D/cooldowns.json`: active cooldowns from the delegate.

    "`closures` with scope `account` for legacy unscoped holds, the model scope
    when recorded, `until_at` from the entry, `clock_source: reported` when the
    entry came from a provider reset and `guessed` otherwise, `source_event:
    v1-cooldown`; expired entries skipped" (C-9.4, C-9.6).

    v1's file records a timestamp and nothing about where it came from. Its
    writers are `capacity.store_lane_cooldown` fed by either a provider reset
    (`delegate._explicit_limited_until`, `probe["reset_at"]`) or a fallback of
    now + 60 minutes, and both shapes reach the file as second-precision local
    time, so the timestamp alone cannot tell them apart. A clock is called
    `reported` here only when a `provider` or `stale-provider` reading already in
    the store puts the same lane's window reset at exactly that instant; every
    other entry is `guessed`, which is the conservative direction (a guessed
    closure still expires by its clock, C-9.6).
    """
    path = delegate_state / "cooldowns.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        report.skip("absent-or-malformed")
        return cursor
    mtime = path.stat().st_mtime if path.exists() else None
    if cursor.get("mtime") == mtime and mtime is not None:
        report.skip("cursor-unchanged")
        return cursor
    index = _lane_index(writer, home)
    for account, scopes in sorted(payload.items()):
        if not isinstance(scopes, dict):
            continue
        lane_id = _lane_of(index, account, home)
        for scope, until in sorted(scopes.items()):
            report.seen += 1
            if lane_id is None:
                report.skip("no-lane-for-account")
                continue
            until_at = _utc(until)
            if not until_at:
                report.skip("unparsable-until")
                continue
            if (_parse(until_at) or datetime.min.replace(tzinfo=timezone.utc)) <= (_parse(now) or datetime.now(timezone.utc)):
                report.skip("expired")
                continue
            model_scope = "account" if scope == "*" else models.get(str(scope).lower(), str(scope))
            reported = writer.exists(
                "SELECT 1 FROM readings WHERE lane_id=? AND resets_at=? AND label IN "
                "('provider','stale-provider')", (lane_id, until_at))
            if writer.exists(
                    "SELECT 1 FROM closures WHERE lane_id=? AND scope=? AND until_at=? "
                    "AND source_event='v1-cooldown'", (lane_id, model_scope, until_at)):
                report.skip("already-imported")
                continue
            clock_source = "reported" if reported else "guessed"
            existing = writer.one(
                "SELECT * FROM closures WHERE lane_id=? AND scope=? AND released_at IS NULL "
                "ORDER BY until_at DESC LIMIT 1", (lane_id, model_scope))
            if existing is not None and existing["until_at"] >= until_at:
                # C-9.6: a new closure on the same lane and scope extends `until`
                # but never shortens it.
                report.skip("a-later-closure-already-holds-this-scope")
                continue
            if existing is not None:
                writer.update("closures", "closure_id", existing["closure_id"], {
                    "until_at": until_at, "reason": "cooldown", "clock_source": clock_source,
                    "source_event": "v1-cooldown"})
                report.count("extended-an-open-closure")
            else:
                writer.insert("closures", {
                    "lane_id": lane_id, "scope": model_scope, "until_at": until_at,
                    "reason": "cooldown", "clock_source": clock_source,
                    "source_event": "v1-cooldown", "created_at": now,
                }, kind="closure.imported")
            report.imported += 1
            report.count(f"clock-source-{clock_source}")
    return {"mtime": mtime}


# --- S/runs/ ------------------------------------------------------------------

def _run_is_live(meta: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Principle 3: a v1 run still running at import time is external.

    An unfinalized run whose recorded pid cannot be inspected is treated as live,
    never as lost: v2 must not claim a run it cannot prove is over (C-5.3).
    """
    if meta.get("finished_at") or meta.get("rc") is not None:
        return False, None
    pid = meta.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False, "no-pid"
    try:
        return (procs.identity(pid) is not None), None
    except procs.InspectionError:
        return True, "pid-inspection-failed"


def _run_state(meta: Mapping[str, Any], live: bool) -> str:
    """Manifest row `S/runs/`: "state from rc (0 succeeded, 4 or 5 failed,
    -9/143/killed interrupted, never finalized lost)". Any other non-zero rc is
    a failure by the same rule.

    This is the attempt's state (C-4.2). `interrupted` is not a job state, so the
    job takes `_job_state` of it.
    """
    if live:
        return "running"
    rc = meta.get("rc")
    if rc is None:
        return "lost"
    if rc == 0:
        return "succeeded"
    if rc in KILLED_RCS:
        return "interrupted"
    return "failed"


def _job_state(attempt_state: str) -> str:
    """C-4.1 has no `interrupted`: a job whose only attempt was killed is `failed`.

    `cancelled` is not used, because v1's rc says the run was killed and not who
    killed it, and a v2 `cancelled` job means a cancel request v2 recorded (C-7).
    """
    return "failed" if attempt_state == "interrupted" else attempt_state


def _signal_of(rc: Any) -> int | None:
    if not isinstance(rc, int) or isinstance(rc, bool):
        return None
    if rc < 0:
        return -rc
    if 128 < rc < 160:
        return rc - 128
    return None


def _run_task(meta: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """v1's task, tier and class. `class` is only read where v1 and v2 spell a
    task the same way (`build`, `review`, `sweep`; cli.LEGACY_TASK_CLASSES)."""
    decision = meta.get("routing_decision") if isinstance(meta.get("routing_decision"), dict) else {}
    overrides = decision.get("overrides") if isinstance(decision.get("overrides"), dict) else {}
    v1_class = decision.get("class") or meta.get("class")
    task = decision.get("task") or overrides.get("task")
    if not task and v1_class in ("build", "review", "sweep"):
        task = v1_class
    return (task or None, decision.get("tier") or overrides.get("tier") or None,
            v1_class or None)


def _run_digest(meta: Mapping[str, Any], prompt_sha: str | None) -> str:
    """A payload digest over the v1 facts amendment 7 names and v1 recorded.

    Marked `v1-import` so it can never collide with a digest computed from a v2
    submission, which also covers the policy hash and the caller's exclusions.
    """
    task, tier, _ = _run_task(meta)
    canonical = json.dumps({
        "v": "v1-import", "job_id": meta.get("id"), "prompt_sha256": prompt_sha,
        "workdir": meta.get("workdir"), "workdir_head": meta.get("git_head_before"),
        "task": task, "tier": tier, "model": meta.get("model"),
        "out_path": meta.get("out_path") or meta.get("original_out_path"),
        "lane": meta.get("lane"),
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _run_sandbox(meta: Mapping[str, Any]) -> tuple[str, bool]:
    decision = meta.get("routing_decision") if isinstance(meta.get("routing_decision"), dict) else {}
    overrides = decision.get("overrides") if isinstance(decision.get("overrides"), dict) else {}
    value = meta.get("sandbox") or overrides.get("sandbox")
    if value in ("read-only", "workspace-write"):
        return value, False
    return "read-only", True


def _prepare_artifacts(report: StoreReport, run_dir: Path, meta: Mapping[str, Any],
                       dry_run: bool) -> list[tuple[str, str, str, int]]:
    """Hash the v1 files an imported run points at, outside any transaction (C-3.3).

    A dry run hashes nothing: 500 v1 runs are 653 MB of deliverables and logs.
    """
    prepared: list[tuple[str, str, str, int]] = []
    for role, path in _artifact_paths(run_dir, meta):
        if dry_run:
            report.count("artifact-would-be-recorded")
            continue
        digest = _sha256(path)
        if digest is None:
            report.count("artifact-unreadable")
            continue
        prepared.append((role, str(path), digest[0], digest[1]))
    return prepared


def _artifact_paths(run_dir: Path, meta: Mapping[str, Any]) -> list[tuple[str, Path]]:
    """The v1 files an imported run points at; nothing is copied (manifest row)."""
    found: list[tuple[str, Path]] = []
    for role, name in (("deliverable", "out.md"), ("stderr", "err.log"), ("lane-log", "lane.log")):
        candidate = run_dir / name
        if candidate.is_file():
            found.append((role, candidate))
    export = meta.get("out_path") or meta.get("original_out_path")
    if isinstance(export, str) and export:
        path = Path(export)
        if path.is_file() and path != run_dir / "out.md":
            found.append(("export", path))
    stream = meta.get("rollout_path") or meta.get("transcript_path")
    if isinstance(stream, str) and stream and Path(stream).is_file():
        found.append(("raw-stream", Path(stream)))
    return found


def import_runs(writer: _Writer, report: StoreReport, *, v1_state: Path, state_root: Path,
                home: Path, models: Mapping[str, str], cursor: dict[str, Any],
                now: str, limit: int | None = None) -> dict[str, Any]:
    """Manifest row `S/runs/`: the v1 ledger.

    "one `jobs` row and one `attempts` row per directory; `job_id` keeps the v1
    id; `request_id` = `v1:<id>`; state from rc; `artifacts` rows point at the v1
    paths (not copied); `imported: true`; live entries per principle 3."

    `imported` and `imported_external` are written to
    `<state root>/jobs/<job id>/manifest.json` (C-2.3), which is where principle
    3 puts the external flag, and to the attempt's `evidence_json` so a reader of
    the row alone can see it.

    The cursor is the newest run id seen plus an `open` list of everything this
    pass did not finish: runs v1 has not given an rc, and runs it refused for a
    reason a later pass can fix (no `meta.json` yet, no lane in the roster). A
    later pass re-reads exactly those, so nothing below the high-water mark is
    lost and the rc v1 finally writes still lands (principles 3 and 4).
    """
    runs = v1_state / "runs"
    if not runs.is_dir():
        report.skip("absent")
        return cursor
    last_id = str(cursor.get("last_id") or "")
    open_runs = {str(name) for name in (cursor.get("open") or [])}
    names = sorted(entry.name for entry in runs.iterdir() if entry.is_dir())
    todo = [name for name in names if name > last_id or name in open_runs]
    if limit is not None:
        # The OLDEST `limit`, so the high-water mark advances contiguously and a
        # later pass picks up the rest; taking the newest would strand every
        # directory below the cursor for good.
        todo = todo[:limit]
    index = _lane_index(writer, home)
    still_open: list[str] = []
    high_water = last_id
    for name in todo:
        report.seen += 1
        high_water = max(high_water, name)
        run_dir = runs / name
        meta = _read_json(run_dir / "meta.json")
        if not isinstance(meta, dict) or not meta.get("id"):
            report.skip("no-meta-json")
            still_open.append(name)         # v1 may not have written it yet
            continue
        job_id = str(meta["id"])
        request_id = f"v1:{job_id}"
        live, live_reason = _run_is_live(meta)
        if live and live_reason:
            report.count(live_reason)
        state = _run_state(meta, live)
        job_state = _job_state(state)
        attempt_id = f"{job_id}/a1"                                   # C-1.2
        existing = writer.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if existing is not None:
            if not str(existing["request_id"]).startswith("v1:"):
                report.skip("job-id-taken-by-a-v2-job")
                continue
            if existing["state"] == job_state:
                report.skip("already-imported")
                if meta.get("rc") is None:
                    # v1 has not finalized it: read it again next pass so the rc
                    # it eventually writes settles the row (principle 3).
                    still_open.append(name)
                continue
            # A run that was live last pass and that v1 has since finalized, or
            # one whose liveness reading changed while it is still unfinalized.
            _finalize_imported_run(writer, report, meta=meta, job_id=job_id,
                                   attempt_id=attempt_id, state=state, run_dir=run_dir,
                                   state_root=state_root, now=now)
            if meta.get("rc") is None:
                still_open.append(name)
            continue
        if writer.exists("SELECT 1 FROM jobs WHERE request_id=?", (request_id,)):
            report.skip("request-id-taken")
            continue
        lane_id = _lane_of(index, meta.get("lane"), home) or _lane_of(index, meta.get("codex_home"), home)
        if lane_id is None:
            report.skip("no-lane-for-run")
            still_open.append(name)         # enrolling the lane later imports it
            report.note(f"{job_id}: v1 lane {meta.get('lane')!r} is not in the roster; "
                        "the ledger row was not imported")
            continue
        prompt = run_dir / "prompt.md"
        prompt_digest = _sha256(prompt) if prompt.is_file() else None
        task, tier, v1_class = _run_task(meta)
        sandbox, defaulted = _run_sandbox(meta)
        caller = meta.get("caller") if isinstance(meta.get("caller"), dict) else {}
        started_at = _utc(meta.get("started_at")) or now
        served = meta.get("model") if isinstance(meta.get("model"), str) else None
        decision = meta.get("routing_decision") if isinstance(meta.get("routing_decision"), dict) else {}
        requested_short = decision.get("requested_model") or decision.get("model")
        requested = models.get(str(requested_short).lower(), served) if requested_short else served
        # C-3.3: the digests and the manifest file are written before the
        # transaction opens, never inside it.
        artifacts = [] if live else _prepare_artifacts(report, run_dir, meta, writer.dry_run)
        _write_job_manifest(writer, state_root, job_id, meta, live, run_dir)
        with writer.transaction("import.run", job_id=job_id):
            writer.insert("jobs", {
                "job_id": job_id, "request_id": request_id,
                "payload_digest": _run_digest(meta, prompt_digest[0] if prompt_digest else None),
                "kind": "dispatch", "state": job_state, "task": task, "tier": tier,
                "workdir": str(meta.get("workdir") or ""),
                "workdir_head": meta.get("git_head_before"),
                "prompt_path": str(prompt), "out_path": meta.get("out_path") or meta.get("original_out_path"),
                "sandbox": sandbox, "exclusions": "[]", "allow_desktop": 0,
                "caller_session": caller.get("session_id"), "caller_pid": caller.get("pid"),
                "policy_hash": None, "rc": meta.get("rc"),
                "accepted_attempt_id": attempt_id if job_state == "succeeded" else None,
                "created_at": started_at, "started_at": started_at,
                "finished_at": _utc(meta.get("finished_at")),
            }, kind="job.imported")
            writer.insert("attempts", {
                "attempt_id": attempt_id, "job_id": job_id, "seq": 1, "lane_id": lane_id,
                "model_requested": requested or served or "unknown", "model_served": served,
                # `model_requested` is NOT NULL; a v1 run that recorded neither a
                # requested nor a served model is reported below, never invented.
                "attestation": "unattested", "state": state,
                "child_pid": meta.get("pid") if live else None,
                "native_session_id": meta.get("session_id") or meta.get("codex_thread_id"),
                "transcript_path": meta.get("transcript_path") or meta.get("rollout_path"),
                "rc": meta.get("rc"), "signal": _signal_of(meta.get("rc")),
                "evidence_json": json.dumps({
                    "imported": True, "imported_external": live,
                    "model_short": requested_short, "v1_class": v1_class,
                    "v1_lane": meta.get("lane"), "v1_meta": str(run_dir / "meta.json"),
                    "salvage_refs": meta.get("salvage_refs") or [],
                }, sort_keys=True),
                "reserved_at": started_at, "started_at": started_at,
                "finished_at": _utc(meta.get("finished_at")),
            }, kind="attempt.imported")
            _record_artifacts(writer, report, attempt_id, artifacts)
        report.imported += 1
        if defaulted:
            report.count("sandbox-defaulted-to-read-only")
        if not (requested or served):
            report.count("no-model-recorded-by-v1")
        if meta.get("rc") is None:
            still_open.append(name)
        if live:
            report.count("imported-external-never-adopted")
    return {"last_id": high_water, "open": sorted(set(still_open))}


def _finalize_imported_run(writer: _Writer, report: StoreReport, *, meta: Mapping[str, Any],
                           job_id: str, attempt_id: str, state: str, run_dir: Path,
                           state_root: Path, now: str) -> None:
    """Principle 3: an external run becomes terminal when a later pass reads its rc.

    v2 records the outcome v1 reached. It never adopts, kills or finalizes the
    run itself.
    """
    finished_at = _utc(meta.get("finished_at")) or now
    job_state = _job_state(state)
    artifacts = _prepare_artifacts(report, run_dir, meta, writer.dry_run)   # C-3.3
    _write_job_manifest(writer, state_root, job_id, meta, False, run_dir)
    with writer.transaction("import.run-finalized", job_id=job_id):
        if writer.writable:
            writer.store.update_job(job_id, state=job_state, rc=meta.get("rc"),
                                    finished_at=finished_at,
                                    accepted_attempt_id=attempt_id if job_state == "succeeded" else None)
            row = writer.one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
            if row is not None:
                # The attempt stops being external the moment v1 is done with it:
                # `imported_external` is what stops v2 acting on a live attempt
                # (daemon.imported_external), and a settled one is nobody's to act on.
                try:
                    evidence = json.loads(row["evidence_json"] or "{}")
                except (TypeError, ValueError):
                    evidence = {}
                evidence.update(imported_external=False, settled_from_v1=True)
                writer.store.update_attempt(attempt_id, state=state, rc=meta.get("rc"),
                                            signal=_signal_of(meta.get("rc")),
                                            child_pid=None, finished_at=finished_at,
                                            evidence_json=json.dumps(evidence, sort_keys=True))
        _record_artifacts(writer, report, attempt_id, artifacts)
    report.imported += 1
    report.count("external-run-settled-from-v1")


def _record_artifacts(writer: _Writer, report: StoreReport, attempt_id: str,
                      prepared: list[tuple[str, str, str, int]]) -> None:
    """`artifacts` rows point at the v1 paths (not copied); the digests are already read."""
    for role, path, digest, size in prepared:
        if writer.exists("SELECT 1 FROM artifacts WHERE attempt_id=? AND role=? AND path=?",
                         (attempt_id, role, path)):
            continue
        if writer.writable:
            writer.store.add_artifact(attempt_id, role, path, digest, size)
        report.count("artifact-recorded")


def _write_job_manifest(writer: _Writer, state_root: Path, job_id: str,
                        meta: Mapping[str, Any], live: bool, run_dir: Path) -> None:
    """C-2.3 `jobs/<job id>/manifest.json`, carrying principle 3's external flag."""
    if writer.dry_run:
        return
    directory = state_root / "jobs" / job_id
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = {
        "job_id": job_id, "imported": True, "imported_external": live,
        "source": "v1", "v1_run_dir": str(run_dir),
        "v1": {key: meta.get(key) for key in
               ("family", "model", "lane", "workdir", "git_head_before", "git_head_after",
                "rc", "started_at", "finished_at", "duration_s", "out_path",
                "original_out_path", "session_id", "transcript_path", "codex_thread_id",
                "codex_home", "rollout_path", "resumed_from", "salvage_refs", "launcher")},
    }
    _publish(directory / "manifest.json",
             json.dumps(payload, indent=1, sort_keys=True, default=str).encode() + b"\n")


# --- S/notices/ ---------------------------------------------------------------

def import_notices(writer: _Writer, report: StoreReport, *, v1_state: Path,
                   cursor: dict[str, Any]) -> dict[str, Any]:
    """Manifest row `S/notices/`: parked completion notices per caller session.

    "`notices` rows with `state: pending` for entries v1 marked unsurfaced,
    `surfaced` otherwise; `session_id` from the file name" (C-15.3).

    A notice references a job (`notices.job_id`), so an entry whose run is older
    than the retained ledger is reported and left. v1's socket-push bookkeeping
    is not carried.
    """
    directory = v1_state / "notices"
    if not directory.is_dir():
        report.skip("absent")
        return cursor
    # `{name: {"lines": n, "retry": [offset]}}`; an older cursor held a bare count.
    files: dict[str, dict[str, Any]] = {}
    for key, value in (cursor.get("files") or {}).items():
        files[str(key)] = ({"lines": int(value), "retry": []} if isinstance(value, int)
                           else {"lines": int(value.get("lines") or 0),
                                 "retry": [int(item) for item in (value.get("retry") or [])]})
    for path in sorted(directory.glob("*.jsonl")):
        session_id = path.stem
        state = files.get(path.name, {"lines": 0, "retry": []})
        start = state["lines"]
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            report.skip("unreadable-file")
            continue
        if len(lines) < start:
            start = 0                       # the file was rewritten; dedupe protects us
        # An entry whose run was not in the ledger yet is retried next pass, so a
        # run imported later still gets its notice (migration.md principle 4).
        todo = sorted({*range(start, len(lines)),
                       *(offset for offset in state["retry"] if offset < len(lines))})
        retry: list[int] = []
        for offset in todo:
            line = lines[offset]
            report.seen += 1
            if not line.strip():
                report.skip("blank-line")
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                report.skip("unparsable-line")
                continue
            job_id = entry.get("run_id")
            text = entry.get("text")
            if not isinstance(job_id, str) or not isinstance(text, str):
                report.skip("no-run-id-or-text")
                continue
            if not writer.exists("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)):
                report.skip("unknown-job")
                retry.append(offset)
                continue
            created_at = _utc(entry.get("ts")) or _utc(entry.get("surfaced_at")) or utc_now()
            notice_state = "surfaced" if entry.get("surfaced") else "pending"
            if writer.exists("SELECT 1 FROM notices WHERE job_id=? AND session_id=? AND created_at=?",
                             (job_id, session_id, created_at)):
                report.skip("already-imported")
                continue
            writer.insert("notices", {
                "job_id": job_id, "session_id": session_id, "text": text,
                "state": notice_state, "transport": None, "created_at": created_at,
            }, kind="notice.imported")
            report.imported += 1
            report.count(f"state-{notice_state}")
        files[path.name] = {"lines": len(lines), "retry": retry}
    unresolved = sum(len(entry["retry"]) for entry in files.values())
    if unresolved:
        report.note(f"{unresolved} notice entries name a run the ledger does not hold; "
                    "v1 keeps 500 run directories and far more notices, so most of these "
                    "are for evicted runs. They stay on the cursor and land if their run "
                    "is ever imported")
    return {"files": files}


# --- S/outbox.sqlite3 and S/cockpit-client/ (C-30.4) --------------------------

class _Conversations:
    """`conversations.sqlite3` for this pass, opened on first use (C-30.4).

    A real pass opens the store under the state root, which only this process
    can be writing: it holds `daemon.lock`, and the daemon is that store's only
    other writer (design D-4). A dry run opens a copy in its scratch directory
    (the live store and its WAL sidecars are copied, never opened in place), so
    the message text it publishes lands there and is deleted with it. A pass
    that never reaches a legacy row creates no conversation store at all.
    """

    def __init__(self, state_root: Path, scratch_dir: str | None):
        self.state_root = state_root
        self.scratch_dir = scratch_dir
        self.store = None

    def get(self) -> ConversationStore:
        if self.store is None:
            if self.scratch_dir is not None:
                root = Path(self.scratch_dir)
                source = self.state_root / "conversations.sqlite3"
                if source.is_file():
                    _copy_sqlite(source, root / "conversations.sqlite3")
                self.store = ConversationStore(root)
            else:
                self.store = ConversationStore(self.state_root)
        return self.store

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None


def _read_legacy_outbox(report: StoreReport | None, v1_state: Path) -> list | None:
    """The cockpit outbox's rows, read from a copy (never v1's WAL in place)."""
    def skip(reason: str) -> None:
        if report is not None:
            report.skip(reason)

    path = v1_state / "outbox.sqlite3"
    if not path.is_file():
        skip("absent")
        return None
    scratch = tempfile.mkdtemp(prefix="subfleet-import-outbox-")
    try:
        copy = Path(scratch) / "outbox.sqlite3"
        try:
            _copy_sqlite(path, copy)                 # never open v1's WAL in place
            connection = sqlite3.connect(str(copy))
        except (OSError, sqlite3.Error):
            skip("unreadable-database")
            return None
        try:
            return legacy.read_outbox(connection)
        except sqlite3.Error:
            skip("no-messages-table")
            return None
        finally:
            connection.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def import_outbox(conversations: _Conversations, report: StoreReport, *, v1_state: Path,
                  projects: Path | None = None, writer: _Writer | None = None) -> dict[str, Any] | None:
    """Manifest row `S/outbox.sqlite3`: the legacy cockpit's message outbox (C-30.4).

    It is not a notice outbox. Each message is classified by itself
    (`subfleet/conversations/legacy.py`): a terminal message of a Claude session
    whose transcript is found becomes read-only history of that session's
    `legacy` conversation; every other message keeps its legacy owner, and is
    reported with its disposition in `items`, as is every terminal message this
    pass could not place. Nothing is sent, queued or dispatched.

    Every pass reads every row: the outbox is small, and idempotence comes from
    the legacy message id in `conversations.sqlite3`, not from a cursor, so a
    message held back last time lands once it can. The cursor this row writes
    is a record of what was read. Notices an earlier version of this row made
    from the same outbox (transport `v1-socket`) are left as they are.
    """
    messages = _read_legacy_outbox(report, v1_state)
    if messages is None:
        return None
    # C-30.4: a journal that cannot be read holds every session; it is never read as empty.
    entries, problem = legacy.read_journal(v1_state / legacy.JOURNAL)
    result = legacy.import_outbox(conversations.get(), messages, projects=projects, journal=entries,
                                  journal_problem=problem)
    report.seen += len(messages)
    report.imported += result.imported
    for item in result.items:
        if item["source"] == "conversation":
            report.count(item["disposition"])       # a conversation, not one of the `seen` messages
        elif item["disposition"] == "history":
            report.count(f"history-{item['state']}")
        else:
            report.skip(item["disposition"])
    report.items.extend(result.items)
    if result.conversations_created:
        report.count("legacy-conversations-created", result.conversations_created)
    owned = sum(1 for item in result.items if item["disposition"] == "legacy-owned")
    if owned:
        report.note(f"{owned} messages are not terminal: they keep their legacy owner, and "
                    "their sessions with them; this import never sends one (C-30.4)")
    if result.journal_problem:
        report.note(f"cockpit-client/pending-messages.json is {result.journal_problem}: every session is "
                    "held, as any of them may have an unacknowledged send, until a pass can read it (C-30.4)")
    held = sum(1 for item in result.items if item["disposition"] == "bound-session-held")
    if held:
        report.note(f"{held} conversations an earlier pass bound are blocked while the legacy writer may be "
                    f"using their sessions (blocked_by {legacy.LEGACY_HOLD!r}); the first pass that finds a "
                    "session settled lifts its block (C-30.4)")
    if any(item["disposition"] == "transcript-not-found" for item in result.items):
        report.note("terminal messages whose Claude transcript was not found are listed in items; "
                    "a later pass imports them if the transcript appears")
    if writer is not None:
        earlier = writer.one("SELECT COUNT(*) AS n FROM notices WHERE transport='v1-socket'")
        if earlier and earlier["n"]:
            report.note(f"{earlier['n']} notices an earlier import made from this outbox are left as they are")
    return {"mapping": "legacy-history", "rows": len(messages),
            "max_sequence": max((message.sequence for message in messages), default=0)}


def import_cockpit_client(report: StoreReport, *, v1_state: Path) -> None:
    """Manifest row `S/cockpit-client/`: the cockpit app's pending-send journal.

    Every entry is an unacknowledged send the cockpit app would retry itself, so
    it keeps its legacy owner and is reported with the outbox's status for its
    id, if the broker took it; nothing is sent (C-30.4). The image snapshot
    folders beside the journal stay where they are.
    """
    directory = v1_state / "cockpit-client"
    if not directory.is_dir():
        report.skip("absent")
        return None
    entries, problem = legacy.read_journal(v1_state / legacy.JOURNAL)
    if problem:
        report.skip("unreadable-journal")
        report.note(f"pending-messages.json is {problem}; it is left as it is")
    statuses = {message.message_id: message.status for message in _read_legacy_outbox(None, v1_state) or ()}
    items = legacy.journal_items(entries, statuses)
    report.seen += len(items)
    for item in items:
        report.skip(item["disposition"])
    report.items.extend(items)
    snapshots = [path for path in directory.glob("images-*") if path.is_dir()]
    if snapshots:
        report.note(f"{len(snapshots)} image snapshot folders are left in place")
    return None


# --- S/integration-events.salt ------------------------------------------------

def import_salt(writer: _Writer, report: StoreReport, *, v1_state: Path,
                state_root: Path) -> None:
    """Manifest row `S/integration-events.salt`.

    "copied to `$SUBFLEET_HOME/integration-events.salt` so event ids stay stable
    across the cutover". A destination that already holds different bytes is
    reported, never overwritten: overwriting would renumber every event id the
    spool has already published.
    """
    source = v1_state / "integration-events.salt"
    if not source.is_file():
        report.skip("absent")
        return
    report.seen += 1
    try:
        salt = source.read_bytes()
    except OSError:
        report.skip("unreadable")
        return
    destination = state_root / "integration-events.salt"
    if destination.is_file():
        try:
            if destination.read_bytes() == salt:
                report.skip("already-imported")
                return
        except OSError:
            pass
        report.skip("destination-differs")
        report.note(f"{destination} holds different bytes; v1's salt was not copied over it")
        return
    if writer.dry_run:
        report.imported += 1
        return
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _publish(destination, salt)
    report.imported += 1


# --- S/alerts.json (milestone 5) ----------------------------------------------

def import_alerts(writer: _Writer, report: StoreReport, *, v1_state: Path,
                  cursor: dict[str, Any]) -> dict[str, Any]:
    """Manifest row `S/alerts.json`: alert latches, imported at milestone 5.

    "`events` of kind `alert-latch`; the timer reads them before its first cycle
    so it does not re-alert" (C-18.1).
    """
    path = v1_state / "alerts.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        report.skip("absent-or-malformed")
        return cursor
    mtime = path.stat().st_mtime if path.exists() else None
    imported = {json.loads(row["data_json"]).get("key")
                for row in writer.query("SELECT data_json FROM events WHERE kind='alert-latch'")}
    for key, latch in sorted(payload.items()):
        report.seen += 1
        if key in imported:
            report.skip("already-imported")
            continue
        writer.event("alert-latch", data={
            "key": key, "active": bool((latch or {}).get("active")) if isinstance(latch, dict) else None,
            "last_sent": _utc((latch or {}).get("last_sent")) if isinstance(latch, dict) else None,
            "source": "v1-alerts",
        })
        report.imported += 1
    return {"mtime": mtime}


# --- sessions kit (milestone 6) -----------------------------------------------

def import_sessions_kit(writer: _Writer, report: StoreReport, *, v1_state: Path,
                        cursor: dict[str, Any]) -> dict[str, Any]:
    """Manifest row `S/tickles/`, `S/native-workers.json`: the sessions kit.

    "tickle dedup records import as `events` of kind `tickle` with their
    timestamps so the sessions kit does not re-nudge; revive logs are not
    imported; `native-workers.json` (two claude worker ids) imports as `events`
    of kind `native-worker` for the twin check."
    """
    last_mtime = float(cursor.get("tickle_mtime") or 0.0)
    high_water = last_mtime
    directory = v1_state / "tickles"
    if directory.is_dir():
        # The newest record per session is what keeps the sessions kit from
        # re-nudging, so a session already imported is re-imported when v1 has
        # tickled it again since (manifest row: "with their timestamps").
        seen: dict[Any, str] = {}
        for row in writer.query("SELECT data_json FROM events WHERE kind='tickle'"):
            try:
                entry = json.loads(row["data_json"])
            except (ValueError, TypeError):
                continue
            key = entry.get("session_id")
            at = str(entry.get("at") or "")
            if key not in seen or at > seen[key]:
                seen[key] = at
        for path in sorted(directory.glob("*.json")):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime <= last_mtime:
                continue
            report.seen += 1
            high_water = max(high_water, mtime)
            entry = _read_json(path)
            if not isinstance(entry, dict):
                report.skip("unparsable-file")
                continue
            session_id = entry.get("session_id") or path.stem
            at = _utc(entry.get("at")) or ""
            if session_id in seen and at <= seen[session_id]:
                report.skip("already-imported")
                continue
            writer.event("tickle", data={
                "session_id": session_id, "at": at or None,
                "last_uuid": entry.get("last_uuid"), "turn_uuid": entry.get("turn_uuid"),
                "delivered": bool(entry.get("delivered")), "source": "v1-tickles",
            })
            seen[session_id] = at
            report.imported += 1
    else:
        report.skip("tickles-absent")

    workers = _read_json(v1_state / "native-workers.json")
    if isinstance(workers, dict):
        known = {json.loads(row["data_json"]).get("worker_id")
                 for row in writer.query("SELECT data_json FROM events WHERE kind='native-worker'")}
        for worker_id, entry in sorted(workers.items()):
            report.seen += 1
            if worker_id in known:
                report.skip("already-imported")
                continue
            writer.event("native-worker", data={
                "worker_id": worker_id, "pid": (entry or {}).get("pid"),
                "broker_pid": (entry or {}).get("broker_pid"),
                "account": (entry or {}).get("account"), "source": "v1-native-workers",
            })
            report.imported += 1
    return {"tickle_mtime": high_water}


# --- what the manifest does not name ------------------------------------------

def scan_unmanifested(v1_state: Path, delegate_state: Path) -> list[str]:
    """migration.md: "Anything found at import time that is not in this table is
    reported by the importer and left alone; the manifest is extended before it
    is imported"."""
    claimed = {name for row in MANIFEST for name in row.names}
    found: list[str] = []
    for root, prefix in ((v1_state, ""), (delegate_state, "D/")):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            name = prefix + entry.name
            if name in claimed or entry.name.endswith(".lock"):
                continue
            if entry.name.startswith("outbox.sqlite3") or entry.name.endswith(".sock"):
                continue
            found.append(name)
    return found


# --- preconditions ------------------------------------------------------------

def _hold_daemon_lock(state_root: Path) -> int | None:
    """Plan amendment 3: the daemon holds `daemon.lock` for its lifetime.

    The importer writes rows the daemon owns (C-3.4), so a real import takes that
    same lock and keeps it for the whole pass: checking once and letting go would
    leave a window in which a daemon starts and two writers run. A lock file that
    exists and cannot be opened is a refusal, not a pass: the importer must not
    proceed on the strength of a check it could not make. A dry run never takes
    this path.
    """
    lock = state_root / "daemon.lock"
    try:
        handle = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise ImportRefused(f"cannot open {lock} to check for a daemon: {error}") from None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(handle)
        raise ImportRefused(
            f"a daemon holds {lock}; stop it before importing (plan amendment 3)") from None
    except OSError as error:
        os.close(handle)
        raise ImportRefused(f"cannot lock {lock}: {error}") from None
    return handle


def _release_daemon_lock(handle: int | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


# --- the pass -----------------------------------------------------------------

def import_v1(state_root: str | Path, *, v1_state: str | Path = V1_STATE,
              delegate_state: str | Path = DELEGATE_STATE,
              roster_dir: str | Path = V1_ROSTER_DIR, home: str | Path | None = None,
              dry_run: bool = False, milestone: int = DEFAULT_MILESTONE,
              write_report: bool = True, runs_limit: int | None = None,
              now: str | None = None, claude_projects: str | Path | None = None) -> ImportReport:
    """Import v1's state per `docs/migration.md`, idempotently and incrementally.

    Returns the `ImportReport` and, unless `write_report=False`, saves it to
    `<state root>/import-report-<utc>.json`. `dry_run=True` writes no row and no
    file under the state root except that report (see the module docstring).
    From `LEGACY_MILESTONE` the pass also imports the legacy cockpit rows
    (C-30.4), which read Claude transcripts under `claude_projects` (default
    `~/.claude/projects`, or `$SUBFLEET_CLAUDE_DIR/projects`).
    """
    state_root = Path(state_root).expanduser()
    v1_state = Path(v1_state).expanduser()
    delegate_state = Path(delegate_state).expanduser()
    roster_dir = Path(roster_dir).expanduser()
    home = Path(home).expanduser() if home is not None else Path.home()
    now = now or utc_now()
    report = ImportReport(str(state_root), str(v1_state), str(delegate_state), str(roster_dir),
                          dry_run, milestone, now)

    database = state_root / "state.sqlite3"
    scratch_dir: str | None = None
    lock: int | None = None
    if dry_run:
        # A dry run imports for real, into a throwaway copy of the store, so the
        # report counts what a real pass would write and every row function reads
        # back what it wrote. Nothing under the state root changes but the report,
        # which is why the store is copied rather than opened: opening a WAL
        # database, even read-only, writes its `-shm` file.
        scratch_dir = tempfile.mkdtemp(prefix="subfleet-import-dry-")
        try:
            scratch = Path(scratch_dir) / "state.sqlite3"
            if database.is_file():
                _copy_sqlite(database, scratch)
            store = Store(scratch)
        except BaseException:
            shutil.rmtree(scratch_dir, ignore_errors=True)
            raise
    else:
        state_root.mkdir(parents=True, mode=0o700, exist_ok=True)   # C-2.2
        lock = _hold_daemon_lock(state_root)
        try:
            store = Store(database)
        except BaseException:
            _release_daemon_lock(lock)
            raise
    writer = _Writer(store, dry_run)
    models = _load_policy_models(state_root)
    conversations = _Conversations(state_root, scratch_dir)
    projects = Path(claude_projects).expanduser() if claude_projects is not None else None
    try:
        cursors = read_cursors(writer)

        def staged(key: str) -> bool:
            row = MANIFEST_BY_KEY[key]
            report_for = report.store_report(key)
            if row.milestone > milestone:
                report_for.note(f"the manifest imports this row at milestone {row.milestone}; "
                                f"this pass is milestone {milestone}")
                return True
            return False

        # C-3.3: no transaction spans the v1 reads, the `ps` calls or the digests.
        # Each row function reads first and records in short transactions, and the
        # cursor lands after the rows it describes, so a pass that dies mid-store
        # re-reads that store next time and the existence checks skip what landed.
        def row(key: str, work) -> None:
            if staged(key):
                return
            entry = report.store_report(key)
            cursor = work(entry)
            if cursor is not None:
                write_cursor(writer, key, cursor, entry)

        row("roster", lambda entry: import_roster(
            writer, entry, roster_dir=roster_dir, home=home, now=now))
        row("capacity-live-cache", lambda entry: import_capacity_cache(
            writer, entry, v1_state=v1_state, home=home,
            cursor=cursors.get("capacity-live-cache", {}), now=now))
        row("claude-oauth-raw", lambda entry: import_desktop_oauth(
            writer, entry, v1_state=v1_state, home=home, models=models,
            cursor=cursors.get("claude-oauth-raw", {}), now=now))
        row("keepalive", lambda entry: import_keepalive(
            writer, entry, v1_state=v1_state, home=home, models=models,
            cursor=cursors.get("keepalive", {})))
        row("cooldowns", lambda entry: import_cooldowns(
            writer, entry, delegate_state=delegate_state, home=home, models=models,
            cursor=cursors.get("cooldowns", {}), now=now))
        row("reset-policy", lambda entry: import_reset_policy(
            writer, entry, v1_state=v1_state, home=home,
            cursor=cursors.get("reset-policy", {})))
        row("runs", lambda entry: import_runs(
            writer, entry, v1_state=v1_state, state_root=state_root, home=home, models=models,
            cursor=cursors.get("runs", {}), now=now, limit=runs_limit))
        row("notices", lambda entry: import_notices(
            writer, entry, v1_state=v1_state, cursor=cursors.get("notices", {})))
        row("outbox", lambda entry: import_outbox(
            conversations, entry, v1_state=v1_state, projects=projects, writer=writer))
        row("cockpit", lambda entry: import_cockpit_client(entry, v1_state=v1_state))
        row("salt", lambda entry: import_salt(
            writer, entry, v1_state=v1_state, state_root=state_root))
        row("alerts", lambda entry: import_alerts(
            writer, entry, v1_state=v1_state, cursor=cursors.get("alerts", {})))
        row("sessions-kit", lambda entry: import_sessions_kit(
            writer, entry, v1_state=v1_state, cursor=cursors.get("sessions-kit", {})))

        for row in MANIFEST:
            report.store_report(row.key)            # every row appears, imported or not
        report.unmanifested = scan_unmanifested(v1_state, delegate_state)
    finally:
        store.close()
        conversations.close()
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        _release_daemon_lock(lock)
    report.finished_at = utc_now()
    if write_report:
        report.write(state_root)
    return report


def import_legacy_cockpit(state_root: str | Path, *, v1_state: str | Path = V1_STATE,
                          claude_projects: str | Path | None = None, dry_run: bool = False,
                          write_report: bool = True, now: str | None = None) -> ImportReport:
    """The legacy cockpit rows alone (C-30.4): `outbox` and `cockpit`, whatever the
    milestone, and nothing else. `state.sqlite3` is not opened.

    A real pass takes `daemon.lock` for its whole length, as `import_v1` does, and
    is refused while a daemon holds it: the daemon is `conversations.sqlite3`'s
    writer (design D-4). A dry run never takes the lock; it imports into a copy of
    the conversation store in a scratch directory and deletes it, so nothing under
    the state root changes but the report. Nothing under the v1 state changes
    either way.
    """
    state_root = Path(state_root).expanduser()
    v1_state = Path(v1_state).expanduser()
    projects = Path(claude_projects).expanduser() if claude_projects is not None else None
    now = now or utc_now()
    report = ImportReport(str(state_root), str(v1_state), "", "", dry_run, LEGACY_MILESTONE, now)
    scratch_dir: str | None = None
    lock: int | None = None
    if dry_run:
        scratch_dir = tempfile.mkdtemp(prefix="subfleet-import-dry-")
    else:
        state_root.mkdir(parents=True, mode=0o700, exist_ok=True)   # C-2.2
        lock = _hold_daemon_lock(state_root)
    conversations = _Conversations(state_root, scratch_dir)
    try:
        import_outbox(conversations, report.store_report("outbox"), v1_state=v1_state, projects=projects)
        import_cockpit_client(report.store_report("cockpit"), v1_state=v1_state)
    finally:
        conversations.close()
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        _release_daemon_lock(lock)
    report.finished_at = utc_now()
    if write_report:
        report.write(state_root)
    return report


# --- module entry point -------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """`python -m subfleet.importer [--dry-run] [--legacy-cockpit]`; the CLI keeps its own verbs."""
    parser = argparse.ArgumentParser(prog="subfleet.importer",
                                     description="import v1 state per docs/migration.md")
    parser.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME") or "~/.subfleet")
    parser.add_argument("--v1-state", default=str(V1_STATE))
    parser.add_argument("--delegate-state", default=str(DELEGATE_STATE))
    parser.add_argument("--roster-dir", default=str(V1_ROSTER_DIR))
    parser.add_argument("--home", default=None,
                        help="where ~/.codex-<n> and ~/.claude.json live (default: $HOME)")
    parser.add_argument("--milestone", type=int, default=DEFAULT_MILESTONE)
    parser.add_argument("--runs-limit", type=int, default=None,
                        help="import at most this many run directories this pass")
    parser.add_argument("--dry-run", action="store_true",
                        help="write no row and no state-root file but the report")
    parser.add_argument("--legacy-cockpit", action="store_true",
                        help="import only the legacy cockpit outbox and client journal into "
                             "conversations.sqlite3 (C-30.4), whatever the milestone")
    parser.add_argument("--claude-dir", default=None,
                        help="the ~/.claude whose projects/ holds the Claude transcripts "
                             "(default: $SUBFLEET_CLAUDE_DIR or ~/.claude)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    projects = Path(args.claude_dir).expanduser() / "projects" if args.claude_dir else None
    try:
        if args.legacy_cockpit:
            report = import_legacy_cockpit(args.state_root, v1_state=args.v1_state,
                                           claude_projects=projects, dry_run=args.dry_run)
        else:
            report = import_v1(args.state_root, v1_state=args.v1_state,
                               delegate_state=args.delegate_state, roster_dir=args.roster_dir,
                               home=args.home, dry_run=args.dry_run, milestone=args.milestone,
                               runs_limit=args.runs_limit, claude_projects=projects)
    except ImportRefused as refusal:
        print(f"subfleet import: {refusal}", file=sys.stderr)
        return int(refusal.code)
    except ConversationError as refusal:           # e.g. a conversation store newer than this build
        print(f"subfleet import: {refusal.reason}: {refusal}", file=sys.stderr)
        return int(refusal.code)
    if args.json:
        print(json.dumps(report.as_dict(), sort_keys=True))
    else:
        print(f"{'store':<22} {'disposition':<22} {'seen':>7} {'imported':>9} {'skipped':>8}")
        for key, entry in sorted(report.stores.items()):
            print(f"{key:<22} {entry.disposition:<22} {entry.seen:>7} "
                  f"{entry.imported:>9} {entry.skipped:>8}")
        for key, entry in sorted(report.stores.items()):
            for item in entry.items:
                # Ids, statuses and dispositions only: never a prompt (C-25.5).
                if item.get("source") == "conversation":
                    print(f"  {key}: conversation {item['conversation_id']} {item['session_id']} "
                          f"-> {item['disposition']}"
                          + (f" (blocked_by {item['blocked_by']})" if item.get("blocked_by") else "")
                          + (f" {item['detail']}" if item.get("detail") else ""))
                    continue
                where = item.get("conversation_id") or item.get("detail") or ""
                print(f"  {key}: {item.get('message_id')} {item.get('session_id')} "
                      f"{item.get('status') or '-'} -> {item['disposition']}"
                      + (f" ({item['state']})" if item.get("state") else "") + (f" {where}" if where else ""))
        if report.unmanifested:
            print("not in the manifest, left alone: " + ", ".join(report.unmanifested))
        print(f"report: {report.path}")
    return 0


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
