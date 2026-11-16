#!/usr/bin/env python3
"""Inventory or remove this job's recorded artifacts after every test returns.

Do not create the guard until the coordinator has stopped/joined every owned
test command. No process listing or live-state lookup is performed.

Inventory only (no marker needed, no filesystem mutations):
    python final-cleanup.py --dry-run

Execution requires S/NO_ACTIVE_TESTS.json with this shape:
    {
      "no_active_tests": true,
      "workspace": "/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b",
      "scratch_roots": ["<S below>", "<N below>"],
      "reviewed_early_roots": {}
    }
If an early root unexpectedly still exists, review its dry-run inventory and
put its exact original path and observed {"dev": ..., "ino": ...} in
reviewed_early_roots before execution. The script otherwise refuses to delete
that root and makes no changes. It never discovers other global temp roots.

Run with a Python outside the workspace .venv because that generated .venv
will itself be removed. The cleanup journal is flushed to repository evidence
before any deletion and remains there after S and this script are removed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


TASK = "sf-a2-fix1"
W = Path("/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b")
S = Path("/private/tmp/sf-a2-fix1")
N = Path("/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/"
         "subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1")
EVIDENCE = W / "docs/reports/2026-11-13-review-subject-fix1-data"
GUARD = S / "NO_ACTIVE_TESTS.json"
EARLY = (
    "/tmp/sf-lzm3a2wm", "/tmp/sf-_07k5_2c", "/tmp/sf-pyarwa1q",
    "/tmp/sf-ebps5x1o", "/tmp/sf-kkj7cda_", "/tmp/sf-_1pjv228",
    "/tmp/sf-0gxogwz4", "/tmp/sf-e8wrr61a", "/tmp/sf-towj8w04",
)


def normalise(raw: str) -> Path:
    """Fold known macOS aliases lexically; never resolve an input symlink."""
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"refusing nonabsolute/traversing path: {raw}")
    if raw == "/tmp" or raw.startswith("/tmp/"):
        path = Path("/private" + raw)
    elif raw == "/var" or raw.startswith("/var/"):
        path = Path("/private" + raw)
    return path


def below(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def no_link_parents(path: Path, boundary: Path) -> None:
    """Require every existing ancestor under its trusted boundary to be a directory."""
    if not below(path, boundary):
        raise RuntimeError(f"path escapes its boundary: {path}")
    if path == boundary:
        return
    current = boundary
    ancestors = [current]
    for part in path.parent.relative_to(boundary).parts:
        current = current / part
        ancestors.append(current)
    for current in ancestors:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise RuntimeError(f"refusing symlink/non-directory ancestor: {current}")


def state(path: Path) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    kind = "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file"
    return {"exists": True, "kind": kind, "dev": info.st_dev, "ino": info.st_ino}


def boundary(path: Path) -> Path:
    if below(path, W):
        if ".git" in path.relative_to(W).parts:
            raise RuntimeError(f"refusing workspace git metadata: {path}")
        return W
    if below(path, S):
        return S
    if below(path, N):
        return N
    if path in {normalise(raw) for raw in EARLY}:
        return Path("/private/tmp")
    for temporary_parent in (Path("/private/tmp"), N.parent):
        if below(path, temporary_parent) and any(part.startswith(TASK) for part in path.relative_to(temporary_parent).parts):
            return temporary_parent
    raise RuntimeError(f"path is not a recorded job artifact: {path}")


def tracked_files() -> set[Path]:
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "ls-files", "-z"], cwd=W, check=True, capture_output=True)
    return {W / os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw}


def read_inventory() -> tuple[list[dict], set[Path]]:
    tracked = tracked_files()
    records = []
    for manifest in (S / "outside.jsonl", N / "outside.jsonl"):
        no_link_parents(manifest, manifest.parent)
        if manifest.is_symlink():
            raise RuntimeError(f"refusing symlink manifest: {manifest}")
        try:
            lines = manifest.read_text().splitlines()
        except FileNotFoundError:
            continue
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            raw = value["path"]
            if not isinstance(raw, str):
                raise RuntimeError(f"non-string manifest path: {manifest}:{line_number}")
            path = normalise(raw)
            owner = boundary(path)
            if owner == W and not any(part.startswith(TASK) for part in path.relative_to(W).parts):
                raise RuntimeError(f"outside manifest lacks job marker: {path}")
            if owner in (S, N) and path != owner:
                # The root's descriptor-safe rmtree owns these descendants.
                # Never stat a descendant through a synthetic parent symlink.
                info = {"exists": None, "inspection": "covered by scratch root; descendant not followed"}
            else:
                no_link_parents(path, owner)
                info = state(path)
            records.append({"source": str(manifest), "line": line_number,
                            "original": raw, "path": str(path), "category": "outside",
                            **info})
    for raw in EARLY:
        path = normalise(raw)
        no_link_parents(path, Path("/private/tmp"))
        records.append({"source": "known-early-roots", "original": raw,
                        "path": str(path), "category": "early", **state(path)})

    # Discover caches only beside git-known Python sources (plus the root),
    # never by walking the workspace, .git, HOME or the global temp directory.
    source_parents = {W, *(path.parent for path in tracked if path.suffix == ".py")}
    generated = {W / ".venv", W / ".pytest_cache", *(parent / "__pycache__" for parent in source_parents)}
    for path in sorted(generated):
        no_link_parents(path, W)
        info = state(path)
        if info["exists"]:
            if any(below(source, path) for source in tracked):
                raise RuntimeError(f"generated cleanup would remove tracked files: {path}")
            records.append({"source": "generated-workspace-root", "original": str(path),
                            "path": str(path), "category": "generated", **info})
    for path in (N, S):
        no_link_parents(path, path.parent)
        records.append({"source": "declared-scratch", "original": str(path),
                        "path": str(path), "category": "scratch", **state(path)})
    protected = set(tracked)
    for source in tracked:
        protected.update(parent for parent in source.parents if below(parent, W))
    for record in records:
        path = Path(record["path"])
        if path in (W, EVIDENCE) or below(EVIDENCE, path) or path in protected:
            raise RuntimeError(f"cleanup would remove source/evidence: {path}")
        if below(path, S) and path != S:
            record["covered_by"] = str(S)
        elif below(path, N) and path != N:
            record["covered_by"] = str(N)
    return records, tracked


def check_guard(records: list[dict]) -> dict:
    if GUARD.is_symlink():
        raise RuntimeError("refusing symlink no-active-tests guard")
    guard = json.loads(GUARD.read_text())
    if (guard.get("no_active_tests") is not True or guard.get("workspace") != str(W)
            or set(guard.get("scratch_roots", [])) != {str(S), str(N)}):
        raise RuntimeError("NO_ACTIVE_TESTS.json must explicitly attest all owned tests returned for this workspace and both scratch roots")
    reviewed = guard.get("reviewed_early_roots", {})
    for record in records:
        if record["category"] == "early" and record["exists"]:
            expected = {key: record[key] for key in ("dev", "ino")}
            if reviewed.get(record["original"]) != expected:
                raise RuntimeError(f"early root unexpectedly exists; coordinator must review its inventory before deletion: {record['original']}")
    return guard


def writable_directories(path: Path) -> None:
    """Repair synthetic directory modes through no-follow directory descriptors."""
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RuntimeError("this Python lacks descriptor-safe shutil.rmtree")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("this Python lacks O_NOFOLLOW")
    os.chmod(path, stat.S_IRWXU, follow_symlinks=False)

    def visit(fd: int) -> None:
        os.fchmod(fd, stat.S_IRWXU)
        with os.scandir(fd) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    os.chmod(entry.name, stat.S_IRWXU, dir_fd=fd, follow_symlinks=False)
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=fd)
                    try:
                        visit(child)
                    finally:
                        os.close(child)
    opened = os.open(path, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    try:
        visit(opened)
    finally:
        os.close(opened)


def guarded_remove(path: Path) -> None:
    assert TASK in str(path), f"unguarded cleanup path: {path}"
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        path.unlink()
    elif stat.S_ISDIR(info.st_mode):
        writable_directories(path)
        shutil.rmtree(path)
    else:
        raise RuntimeError(f"refusing non-directory artifact: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print inventory only; never mutate or require the guard")
    args = parser.parse_args()
    if Path.cwd() != W:
        raise RuntimeError(f"run only from assigned workspace: {W}")
    records, _tracked = read_inventory()
    if args.dry_run:
        for record in records:
            print(json.dumps({"event": "inventory", **record}, sort_keys=True))
        print(json.dumps({"event": "inventory-summary", "records": len(records),
                          "categories": dict(Counter(record["category"] for record in records))}, sort_keys=True))
        return 0
    if below(Path(sys.executable), W / ".venv"):
        raise RuntimeError("run cleanup with a Python outside the generated workspace .venv")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False) or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("cleanup requires descriptor-safe rmtree and O_NOFOLLOW")
    guard = check_guard(records)
    no_link_parents(EVIDENCE, W)
    evidence_state = state(EVIDENCE)
    if evidence_state["exists"] and evidence_state["kind"] != "directory":
        raise RuntimeError("refusing symlink/non-directory evidence destination")
    EVIDENCE.mkdir(exist_ok=True)
    journal_path = EVIDENCE / "cleanup.jsonl"
    journal_fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(journal_fd, "a") as journal:
        def log(event: str, **values) -> None:
            journal.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **values}, sort_keys=True) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        log("guard", **guard)
        for record in records:
            log("inventory", **record)
        dispose = S / "dispose"
        no_link_parents(dispose, S)
        if dispose.is_symlink():
            raise RuntimeError("refusing symlink disposal directory")
        dispose.mkdir(exist_ok=True)
        operations = {}
        for record in records:
            path = Path(record["path"])
            if record["exists"] and "covered_by" not in record:
                operations.setdefault(path, record)
        selected = []
        for path in sorted(operations, key=lambda value: (len(value.parts), str(value))):
            if path not in (S, N) and any(below(path, parent) for parent in selected):
                log("covered", original=str(path), covered_by=next(str(parent) for parent in selected if below(path, parent)))
            else:
                selected.append(path)
        # Scratch roots go last: S holds disposal, manifests, scripts and guard.
        ordered = [path for path in selected if path not in (S, N)] + [path for path in (N, S) if path in selected]
        for path in ordered:
            record = operations[path]
            current = state(path)
            if current != {key: record[key] for key in current}:
                raise RuntimeError(f"artifact changed after inventory: {path}")
            no_link_parents(path, boundary(path))
            target = path
            if TASK not in str(path):
                suffix = hashlib.sha256(str(path).encode()).hexdigest()[:16]
                target = dispose / f"{suffix}-{path.name}"
                if os.path.lexists(target):
                    raise RuntimeError(f"disposal target already exists: {target}")
                log("move-start", original=str(path), disposal=str(target))
                os.rename(path, target)
                log("move-done", original=str(path), disposal=str(target))
            log("delete-start", original=str(path), disposal=str(target))
            guarded_remove(target)
            log("delete-done", original=str(path), disposal=str(target), exists=os.path.lexists(target))
        log("cleanup-complete", scratch_removed=not os.path.lexists(S), neutral_removed=not os.path.lexists(N),
            inventory_records=len(records))
    print(f"Cleanup journal: {journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
