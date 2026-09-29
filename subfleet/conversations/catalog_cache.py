"""The catalog worker's resumable scan (C-30.1), never the daemon's store.

The worker holds catalog.lock. A small transaction advances a directory cursor
and its records together; interruption replays at most CHECKPOINT_EVERY entries.
The legacy JSON cache is imported once. It is never rewritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

CHECKPOINT_EVERY = 64


class OwnerGone(Exception):
    """The worker's fence no longer permits publication."""


class Scan:
    def __init__(self, root, sources, may_write):
        from .catalog import _read_json
        self.may_write = may_write
        self.db = None
        self.pending = 0
        self.check_owner()
        path = root / "catalog-cache.sqlite3"
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS records (
                    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, version INTEGER,
                    record TEXT, item TEXT, provider TEXT, sid TEXT, seen INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY, source TEXT, after TEXT);
                CREATE TABLE IF NOT EXISTS names (sid TEXT PRIMARY KEY, title TEXT, seen INTEGER);
            """)
            if self.get("migrated") is None:
                # Mark first: a kill during migration may lose cache hits, never
                # catalog entries. The scan reads any not imported transcripts.
                self.put("migrated", True)
                self.checkpoint()
                legacy = _read_json(root / "catalog-cache.json")
                for key, hit in legacy.items():
                    if isinstance(hit, dict) and isinstance(hit.get("record"), dict):
                        self.db.execute("INSERT OR IGNORE INTO records(path,size,mtime,version,record) VALUES(?,?,?,?,?)",
                                        (key, hit.get("size"), hit.get("mtime"), hit.get("version"),
                                         json.dumps(hit["record"], separators=(",", ":"))))
                        self.step()
                self.checkpoint()
            previous = self.get("sources")
            unfinished = self.db.execute("SELECT 1 FROM pending LIMIT 1").fetchone()
            if previous != sources or not unfinished:
                self.epoch = (self.get("epoch") or 0) + 1
                self.put("epoch", self.epoch)
                self.put("sources", sources)
                self.db.execute("DELETE FROM pending")
                self.db.executemany("INSERT INTO pending(source,after) VALUES(?,?)",
                                    [(json.dumps(s), "") for s in sources])
                self.checkpoint()
            else:
                self.epoch = self.get("epoch")
        except BaseException:
            self.close()
            raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def check_owner(self):
        if self.may_write is not None and not self.may_write():
            raise OwnerGone()

    def checkpoint(self):
        self.check_owner()
        self.db.commit()
        self.pending = 0

    def step(self):
        self.pending += 1
        if self.pending >= CHECKPOINT_EVERY:
            self.checkpoint()

    def close(self):
        if self.db is not None:
            self.db.close()                  # rolls back an uncommitted cursor and its records
            self.db = None

    def run(self, in_budget, reader):
        while in_budget():
            task = self.db.execute("SELECT * FROM pending ORDER BY id LIMIT 1").fetchone()
            if task is None:
                self.db.execute("DELETE FROM records WHERE seen != ?", (self.epoch,))
                self.checkpoint()
                return True
            source = json.loads(task["source"])
            if source["provider"] == "names":
                done = self.scan_names(task, source, in_budget)
            else:
                done = self.scan_directory(task, source, in_budget, reader)
            if done:
                self.db.execute("DELETE FROM pending WHERE id=?", (task["id"],))
                self.step()
            else:
                break
        self.checkpoint()
        return False

    def scan_directory(self, task, source, in_budget, reader):
        from .catalog import CLAUDE_RECORD_VERSION
        try:
            with os.scandir(source["path"]) as entries:
                entries = sorted(entries, key=lambda e: e.name)
        except OSError:
            return True
        for entry in entries:
            if entry.name <= task["after"]:
                continue
            if not in_budget():
                return False
            provider = source["provider"]
            try:
                is_dir = entry.is_dir(follow_symlinks=provider == "claude")
                if is_dir and (provider == "codex" or source["depth"]):
                    child = {**source, "path": entry.path}
                    if provider == "claude":
                        child["depth"] = 0
                    self.db.execute("INSERT INTO pending(source,after) VALUES(?,?)", (json.dumps(child), ""))
                elif (entry.name.endswith(".jsonl") and
                      ((provider == "claude" and not source["depth"]) or
                       (provider == "codex" and entry.name.startswith("rollout-")))):
                    st = entry.stat()
                    hit = self.db.execute("SELECT * FROM records WHERE path=?", (entry.path,)).fetchone()
                    version = CLAUDE_RECORD_VERSION if provider == "claude" else None
                    cached = None
                    if hit and (hit["size"], hit["mtime"], hit["version"]) == (st.st_size, st.st_mtime, version):
                        cached = json.loads(hit["record"])
                    record, item = reader(Path(entry.path), source, cached)
                    if item is not None:
                        item["mtime"] = st.st_mtime
                    sid = Path(entry.path).stem if provider == "claude" else record.get("id")
                    self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                                    (entry.path, st.st_size, st.st_mtime, version,
                                     json.dumps(record, separators=(",", ":")),
                                     None if item is None else json.dumps(item, separators=(",", ":")),
                                     provider, sid, self.epoch))
            except OSError:
                pass
            self.db.execute("UPDATE pending SET after=? WHERE id=?", (entry.name, task["id"]))
            self.step()
        return True

    def scan_names(self, task, source, in_budget):
        from .catalog import INDEX_MAX, scrub
        offset = int(task["after"] or 0)
        try:
            with open(source["path"], "rb") as stream:
                stream.seek(offset)
                while offset < INDEX_MAX:
                    if not in_budget():
                        return False
                    raw = stream.readline(INDEX_MAX - offset)
                    if not raw:
                        break
                    offset += len(raw)
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        row = None
                    if isinstance(row, dict) and row.get("id") and row.get("thread_name"):
                        title = scrub(row["thread_name"])[:200]
                        old = self.db.execute("SELECT title FROM names WHERE sid=?", (row["id"],)).fetchone()
                        self.db.execute("INSERT OR REPLACE INTO names VALUES(?,?,?)",
                                        (row["id"], old[0] if old and old[0] == title else title, self.epoch))
                    self.db.execute("UPDATE pending SET after=? WHERE id=?", (str(offset), task["id"]))
                    self.step()
        except OSError:
            pass
        self.db.execute("DELETE FROM names WHERE seen != ?", (self.epoch,))
        return True

    def items(self):
        # Deduplicate Claude copies before exclusions: a newer headless copy
        # must not expose its older interactive transcript. Match opening's
        # newest-copy rule, with a path tie-break for reproducible slices.
        seen = set()
        for row in self.db.execute("SELECT provider,sid,item FROM records ORDER BY mtime DESC,path"):
            if row["provider"] == "claude":
                if row["sid"] in seen:
                    continue
                seen.add(row["sid"])
            if row["item"] is not None:
                item = json.loads(row["item"])
                if item["provider"] == "codex":
                    title = self.db.execute("SELECT title FROM names WHERE sid=?", (item["native_session_id"],)).fetchone()
                    item["title"] = title[0] if title else None
                yield item
