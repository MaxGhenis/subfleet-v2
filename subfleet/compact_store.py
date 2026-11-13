"""Offline, row-preserving store compaction (C-3.8, D-STORE-FREE1).

Run ``python -m subfleet.compact_store --state-root PATH`` to measure, then
add ``--apply`` while the daemon is stopped. No Store writer is constructed:
this pass never migrates, deletes a row, or appends an event (C-3.2).

Before touching SQLite it takes daemon.lock, rejects open database/sidecar
descriptors, and reads schema/space through a sparse, private schema snapshot.
Even a read-only WAL connection can write SHM; the snapshot avoids that on a
refusal. A successful pass holds SQLite's exclusive locking mode, checkpoints
and truncates the old WAL, VACUUM INTOs a same-directory temporary file, checks
integrity, every table's count and ordered content digest, and auto_vacuum=2.

The extra-space admission is live_bytes + max(512 MiB, ceil(live_bytes/50))
+ existing_WAL_bytes. The margin covers pointer maps, the small sparse schema
snapshot, and filesystem rounding. VACUUM INTO needs one output, not an output
plus an output-sized WAL. No full-size backup copy is made: --keep-original
PATH keeps the checkpointed original by hard link on the same filesystem.

The output is fsynced before atomic replacement, and the parent directory is
fsynced afterwards. Before replacement the old, checkpointed DB is authoritative;
afterwards the verified new DB is authoritative. Sidecars are removed only
after the old connection leaves WAL mode and before replacement. Thus an old WAL
can never be replayed over the new file. Python exceptions clean the temporary
file; abrupt process/power loss can leave a hidden .state-compact-* file, which
an operator may remove while stopped. The next pass removes such leftovers
only after all refusal checks. It never resumes an unverified output.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import SCHEMA_VERSION

STORE_NAME = "state.sqlite3"
MARGIN_BYTES = 512 * 1024**2
SWAP_STAGES = ("checkpointed", "built", "verified", "file_synced",
               "sidecars_removed", "original_kept", "replaced", "directory_synced")


class CompactRefused(RuntimeError):
    """A precondition failed; no database or sidecar has been changed."""


class CompactVerificationError(RuntimeError):
    """The output failed verification; the original remains authoritative."""


@dataclass
class CompactReport:
    # Keep the requested dry-run measurements in this order.
    page_count: int
    freelist_count: int
    live_bytes: int
    reclaimable_bytes: int
    needed_bytes: int
    free_bytes: int
    page_size: int
    wal_bytes: int
    margin_bytes: int
    dry_run: bool
    formula: str = "live_bytes + max(512 MiB, ceil(live_bytes / 50)) + wal_bytes"
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    output_bytes: int | None = None
    auto_vacuum: int | None = None
    kept_original: str | None = None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _open_holders(database: Path) -> None:
    """lsof also finds idle connections which hold no SQLite advisory lock."""
    paths = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
    paths = [str(path) for path in paths if path.exists()]
    try:
        result = subprocess.run(["lsof", "-nPl", "-F", "p", "--", *paths],
                                capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CompactRefused(f"cannot establish exclusive store ownership: {error}") from error
    # lsof returns 1 with no output when no supplied file is open.
    if result.stdout.strip():
        raise CompactRefused("a process holds the store or its WAL/SHM open; close it first")
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise CompactRefused(f"cannot inspect store holders: {result.stderr.strip()}")


@contextlib.contextmanager
def _daemon_lock(root: Path) -> Iterator[None]:
    """Never write lock contents; remove our own newly created lock on exit."""
    path = root / "daemon.lock"
    created = False
    try:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CompactRefused("the daemon is running (daemon.lock is held)") from None
        yield
    finally:
        if 'fd' in locals():
            if created and path.exists() and path.stat().st_ino == os.fstat(fd).st_ino:
                path.unlink()
            os.close(fd)


def _wal_checksum(data: bytes, order: str, previous: tuple[int, int]) -> tuple[int, int]:
    first, second = previous
    for left, right in struct.iter_unpack(order + "II", data):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


class _Pages:
    """Read only committed SQLite pages, including a crash-left WAL.

    WAL checksums/salts bound the valid prefix; an incomplete transaction or
    torn tail is ignored just as by SQLite recovery. No SQLite file is opened
    until the private sparse schema snapshot has been assembled.
    """

    def __init__(self, database: Path):
        self.main = None
        self.wal = None
        try:
            self._load(database)
        except BaseException:
            self.close()
            raise

    def _load(self, database: Path) -> None:
        self.main = database.open("rb")
        self.wal = None
        self.frames: dict[int, int] = {}
        header = self.main.read(100)
        if header[:16] != b"SQLite format 3\x00":
            raise CompactRefused("store is not a SQLite database")
        self.page_size = int.from_bytes(header[16:18], "big") or 65536
        if self.page_size == 1:
            self.page_size = 65536
        wal_path = Path(str(database) + "-wal")
        if wal_path.exists() and wal_path.stat().st_size >= 32:
            self.wal = wal_path.open("rb")
            wal_header = self.wal.read(32)
            magic, version, size = struct.unpack(">III", wal_header[:12])
            if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != self.page_size:
                raise CompactRefused("unrecognized WAL header")
            order = "<" if magic == 0x377f0682 else ">"
            checks = _wal_checksum(wal_header[:24], order, (0, 0))
            if checks != struct.unpack(">II", wal_header[24:32]):
                raise CompactRefused("WAL header checksum failed")
            pending: dict[int, int] = {}
            committed_size = None
            while True:
                offset = self.wal.tell()
                frame = self.wal.read(24)
                page = self.wal.read(self.page_size) if len(frame) == 24 else b""
                if len(page) != self.page_size or frame[8:16] != wal_header[16:24]:
                    break
                checks = _wal_checksum(frame[:8] + page, order, checks)
                if checks != struct.unpack(">II", frame[16:24]):
                    break
                number, commit = struct.unpack(">II", frame[:8])
                if not number:
                    break
                pending[number] = offset + 24
                if commit:
                    self.frames.update(pending)
                    pending.clear()
                    committed_size = commit
        self.header = self.read(1)
        self.page_count = int.from_bytes(self.header[28:32], "big")
        if self.wal and committed_size is not None:
            self.page_count = committed_size
        self.freelist_count = int.from_bytes(self.header[36:40], "big")
        if not 0 <= self.freelist_count < self.page_count:
            raise CompactRefused("invalid page/freelist counts")

    def read(self, number: int) -> bytes:
        stream = self.wal if number in self.frames else self.main
        offset = self.frames.get(number, (number - 1) * self.page_size)
        stream.seek(offset)
        data = stream.read(self.page_size)
        if len(data) != self.page_size:
            raise CompactRefused("truncated store page")
        return data

    def close(self) -> None:
        if self.main:
            self.main.close()
        if self.wal:
            self.wal.close()


def _varint(page: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(9):
        byte = page[offset]
        offset += 1
        value = (value << (8 if index == 8 else 7)) | (byte if index == 8 else byte & 127)
        if byte < 128 or index == 8:
            return value, offset
    raise CompactRefused("invalid SQLite varint")


def _copy_tree(pages: _Pages, output, root_page: int, seen: set[int]) -> None:
    """Copy table btree and overflow pages; only schema and version are read."""
    pending = [root_page]
    usable = pages.page_size - pages.header[20]
    while pending:
        number = pending.pop()
        if number in seen:
            continue
        if number < 1 or number > pages.page_count:
            raise CompactRefused("invalid schema page reference")
        seen.add(number)
        page = pages.read(number)
        output.seek((number - 1) * pages.page_size)
        output.write(page)
        start = 100 if number == 1 else 0
        kind = page[start]
        cells = int.from_bytes(page[start + 3:start + 5], "big")
        if kind == 5:
            pending.append(int.from_bytes(page[start + 8:start + 12], "big"))
            for i in range(cells):
                cell = int.from_bytes(page[start + 12 + i * 2:start + 14 + i * 2], "big")
                pending.append(int.from_bytes(page[cell:cell + 4], "big"))
        elif kind == 13:
            for i in range(cells):
                cell = int.from_bytes(page[start + 8 + i * 2:start + 10 + i * 2], "big")
                length, offset = _varint(page, cell)
                _, offset = _varint(page, offset)
                if length <= usable - 35:
                    continue
                minimum = ((usable - 12) * 32 // 255) - 23
                local = minimum + ((length - minimum) % (usable - 4))
                if local > usable - 35:
                    local = minimum
                overflow = int.from_bytes(page[offset + local:offset + local + 4], "big")
                while overflow:
                    if overflow in seen or not 1 <= overflow <= pages.page_count:
                        raise CompactRefused("invalid schema overflow chain")
                    seen.add(overflow)
                    body = pages.read(overflow)
                    output.seek((overflow - 1) * pages.page_size)
                    output.write(body)
                    overflow = int.from_bytes(body[:4], "big")
        else:
            raise CompactRefused("invalid schema table btree")


def _sql_tokens(sql: str) -> list[str]:
    # Strip comments and insignificant whitespace without changing literals.
    pattern = r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\[[^\]]*\]|`[^`]*`|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|[^\s]"
    return [part if part[:1] in "'\"[`" else part.lower()
            for part in re.findall(pattern, sql)
            if not part.startswith(("--", "/*"))]


def _table_definitions(sql: str) -> tuple:
    """Compare DDL semantically enough to permit only column reordering.

    SQLite rewrites ADD COLUMN at the end of a table's original declaration.
    Tokenized top-level definitions retain CHECK/default/collation/constraint
    text while allowing precisely that physical-order difference.
    """
    tokens = _sql_tokens(sql)
    start = tokens.index("(") + 1
    definitions, part, depth = [], [], 0
    for token in tokens[start:]:
        if depth == 0 and token in (",", ")"):
            definitions.append(tuple(part))
            part = []
            if token == ")":
                break
        else:
            part.append(token)
            depth += (token == "(") - (token == ")")
    return tuple(sorted(definitions))


def _layout(connection: sqlite3.Connection, table: str) -> tuple:
    quoted = _quote(table)
    # ALTER TABLE migrations append columns; the fresh DDL groups them by
    # purpose. Their physical ordinal is not a schema mismatch.
    columns = tuple(sorted(row[1:] for row in connection.execute(f"PRAGMA table_xinfo({quoted})")))
    foreign = tuple(connection.execute(f"PRAGMA foreign_key_list({quoted})"))
    indexes = []
    for row in connection.execute(f"PRAGMA index_list({quoted})"):
        # Index column IDs also reflect a migration's column ordering.
        info = tuple((item[0], *item[2:]) for item in
                     connection.execute(f"PRAGMA index_xinfo({_quote(row[1])})"))
        sql_row = connection.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (row[1],)).fetchone()
        sql = _sql_tokens(sql_row[0]) if sql_row and sql_row[0] else []
        indexes.append((row[1:], info, tuple(sql)))
    sql_row = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
    definitions = _table_definitions(sql_row[0]) if sql_row else ()
    table_flags = next((tuple(row[4:]) for row in connection.execute("PRAGMA table_list")
                        if row[1] == table), None)
    return columns, foreign, tuple(sorted(indexes)), definitions, table_flags


def _require_schema(connection: sqlite3.Connection) -> None:
    try:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise CompactRefused(f"store schema {version} differs from this build's {SCHEMA_VERSION}")
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(Path(__file__).with_name("store_schema.sql").read_text())
            objects = "SELECT type,name,tbl_name FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            if list(connection.execute(objects)) != list(expected.execute(objects)):
                raise CompactRefused("store schema objects differ from this build")
            for (table,) in expected.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
                if _layout(connection, table) != _layout(expected, table):
                    raise CompactRefused(f"store schema differs from this build: {table}")
        finally:
            expected.close()
    except sqlite3.Error as error:
        raise CompactRefused(f"cannot verify store schema: {error}") from error


def _preflight(database: Path, apply: bool) -> CompactReport:
    pages = _Pages(database)
    try:
        # This sparse file has the logical DB size but only schema/version pages
        # allocated. Never attach the original WAL to a SQLite connection.
        with tempfile.TemporaryDirectory(prefix="store-compact-schema-") as directory:
            snapshot = Path(directory) / "schema.sqlite3"
            with snapshot.open("w+b") as output:
                output.truncate(pages.page_count * pages.page_size)
                seen: set[int] = set()
                _copy_tree(pages, output, 1, seen)
                output.flush()
                conn = sqlite3.connect(snapshot.as_uri() + "?immutable=1", uri=True)
                try:
                    row = conn.execute("SELECT rootpage FROM sqlite_schema WHERE name='schema_version' AND type='table'").fetchone()
                finally:
                    conn.close()
                if not row:
                    raise CompactRefused("store has no schema_version table")
                _copy_tree(pages, output, row[0], seen)
                output.flush()
            conn = sqlite3.connect(snapshot.as_uri() + "?immutable=1", uri=True)
            try:
                _require_schema(conn)
            finally:
                conn.close()
        wal = Path(str(database) + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        live = (pages.page_count - pages.freelist_count) * pages.page_size
        margin = max(MARGIN_BYTES, (live + 49) // 50)
        need = live + margin + wal_bytes
        free = shutil.disk_usage(database.parent).free
        report = CompactReport(pages.page_count, pages.freelist_count, live,
                               pages.freelist_count * pages.page_size, need, free,
                               pages.page_size, wal_bytes, margin, not apply)
        if free < need:
            raise CompactRefused(f"insufficient space: need {need} bytes; free {free} bytes "
                                 f"({report.formula})")
        return report
    finally:
        pages.close()


def _fingerprints(connection: sqlite3.Connection) -> dict[str, dict]:
    """Stream all values in PK order; include internal sqlite_sequence too.

    Each value carries its SQLite storage class and exact integer/IEEE/string/
    blob bytes, with length framing. Tables without a declared PK use all
    columns as a deterministic multiset order (VACUUM may change their rowid).
    """
    result = {}
    for (table,) in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
        columns = list(connection.execute(f"PRAGMA table_xinfo({_quote(table)})"))
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = keys or [row[1] for row in columns if row[6] != 1]
        # A PK containing NULL can have ties on rowid tables. Remaining columns
        # make their order deterministic without relying on physical rowids.
        order += [row[1] for row in columns if row[1] not in order and row[6] != 1]
        sql = f"SELECT * FROM {_quote(table)} ORDER BY " + ",".join(_quote(key) for key in order)
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(sql):
            count += 1
            digest.update(b"R")
            for value in row:
                if value is None:
                    tag, body = b"N", b""
                elif type(value) is int:
                    tag, body = b"I", str(value).encode("ascii")
                elif type(value) is float:
                    tag, body = b"F", struct.pack(">d", value)
                elif type(value) is str:
                    tag, body = b"T", value.encode("utf-8", "surrogatepass")
                else:
                    tag, body = b"B", bytes(value)
                digest.update(tag + struct.pack(">Q", len(body)))
                digest.update(body)
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def _verify(connection: sqlite3.Connection, before: dict) -> dict:
    check = list(connection.execute("PRAGMA integrity_check"))
    if check != [("ok",)]:
        raise CompactVerificationError(f"compacted integrity_check failed: {check}")
    try:
        _require_schema(connection)
    except CompactRefused as error:
        raise CompactVerificationError(f"compacted schema differs: {error}") from error
    after = _fingerprints(connection)
    if after != before:
        raise CompactVerificationError("compacted table row count or digest differs; original kept")
    if connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        raise CompactVerificationError("compacted auto_vacuum is not INCREMENTAL")
    return after


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(root: Path) -> None:
    _fsync_file(root)


def compact(state_root: str | Path, *, apply: bool = False,
            keep_original: str | Path | None = None,
            fault: Callable[[str], None] | None = None) -> CompactReport:
    """Measure or compact a stopped synthetic/operator-selected store.

    ``fault`` is a test seam after each exported SWAP_STAGES boundary. No
    recovery path ever deletes or overwrites the authoritative database.
    """
    root = Path(state_root).resolve()
    database = root / STORE_NAME
    if not database.is_file() or database.is_symlink():
        raise CompactRefused("store must be an existing regular database file")
    keep = Path(keep_original).absolute() if keep_original is not None else None
    if keep is not None and keep.parent.resolve() == root and keep.name.startswith(".state-compact-"):
        raise CompactRefused("keep-original cannot use the reserved .state-compact- temporary prefix")
    if keep is not None and (keep.exists() or keep.is_symlink() or not keep.parent.is_dir() or
                             keep.parent.stat().st_dev != root.stat().st_dev):
        raise CompactRefused("keep-original must be an unused path on the state filesystem")
    stage = fault or (lambda name: None)
    with _daemon_lock(root):
        _open_holders(database)
        report = _preflight(database, apply)
        if not apply:
            return report
        connection = None
        temporary = None
        try:
            # Every refused precondition above is read-only. From here the old
            # WAL may be folded, but its logical contents remain unchanged.
            connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=0)
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("ROLLBACK")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0] or checkpoint[1] not in (0, -1):
                raise CompactRefused(f"store cannot be exclusively checkpointed: {checkpoint}")
            # Leave WAL mode while still owning SQLite's exclusive lock. Its
            # sidecars disappear before publication, and the main-file lock
            # can now stay held through replacement without retaining SHM.
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise CompactRefused("store could not leave WAL mode exclusively")
            _fsync_file(database)
            stage("checkpointed")
            for leftover in root.glob(".state-compact-*"):
                # Only after exclusive ownership: never remove a crash-left
                # output on an ownership refusal. Never resume such an output.
                if leftover.is_file() or leftover.is_symlink():
                    leftover.unlink()
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA temp_store=MEMORY")
            # Integrity checks revisit table/index pages. A bounded offline
            # cache avoids thousands of cold random reads on a busy host.
            connection.execute("PRAGMA cache_size=-262144")
            # Refuse a corrupt source index rather than silently changing its
            # answers by rebuilding it. The target receives its own full check.
            source_check = list(connection.execute("PRAGMA integrity_check"))
            if source_check != [("ok",)]:
                raise CompactVerificationError(f"source integrity_check failed: {source_check}")
            before = _fingerprints(connection)
            report.before = before
            # SQLite carries this pending setting into VACUUM INTO even when
            # the source header remains NONE until a full rebuild.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            fd, name = tempfile.mkstemp(prefix=".state-compact-", suffix=".sqlite3", dir=root)
            os.close(fd)
            temporary = Path(name)
            connection.execute("VACUUM INTO ?", (str(temporary),))
            stage("built")
            target = sqlite3.connect(temporary.as_uri() + "?immutable=1", uri=True)
            try:
                target.execute("PRAGMA cache_size=-262144")
                report.after = _verify(target, before)
                report.auto_vacuum = target.execute("PRAGMA auto_vacuum").fetchone()[0]
            finally:
                target.close()
            report.output_bytes = temporary.stat().st_size
            stage("verified")
            original_stat = database.stat()
            target_stat = temporary.stat()
            if (target_stat.st_uid, target_stat.st_gid) != (original_stat.st_uid, original_stat.st_gid):
                os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
            os.chmod(temporary, original_stat.st_mode & 0o777)
            _fsync_file(temporary)
            stage("file_synced")
            # The old DB is fully checkpointed, durable, and in DELETE mode.
            # Keep its exclusive lock through replacement. Remove sidecars
            # before replacement, never after it, then sync their removal.
            for suffix in ("-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)
            _fsync_directory(root)
            stage("sidecars_removed")
            if keep is not None:
                os.link(database, keep)
                _fsync_directory(keep.parent)
                report.kept_original = str(keep)
                stage("original_kept")
            os.replace(temporary, database)
            temporary = None
            stage("replaced")
            _fsync_directory(root)
            stage("directory_synced")
            return report
        except sqlite3.OperationalError as error:
            raise CompactRefused(f"store could not be exclusively compacted: {error}") from error
        finally:
            if connection is not None:
                connection.close()
            if temporary is not None:
                for suffix in ("", "-journal", "-wal", "-shm"):
                    Path(str(temporary) + suffix).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Require an explicit path: importing/running this pass never finds live
    # state as a side effect, and installation chooses its state root.
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--keep-original", type=Path)
    args = parser.parse_args(argv)
    try:
        report = compact(args.state_root, apply=args.apply, keep_original=args.keep_original)
    except (CompactRefused, CompactVerificationError) as error:
        print(f"compact store: {error}")
        return 7
    for label, value in (("page count", report.page_count),
                         ("freelist count", report.freelist_count),
                         ("live bytes", report.live_bytes),
                         ("return bytes (freelist estimate)", report.reclaimable_bytes),
                         ("needed bytes", report.needed_bytes),
                         ("free bytes", report.free_bytes)):
        print(f"{label}: {value}")
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
