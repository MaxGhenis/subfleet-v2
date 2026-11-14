"""Run once, after validation stops, to preserve evidence and remove job temps.

Execute from the assigned worktree with the system Python. This script is not a
pytest plugin. It refuses to overwrite evidence or move any tracked cache/data
file. The two plugin manifests are preserved byte-for-byte; cleanup's own
deletions and original-path/count audit are in ci-r1-cleanup.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import traceback


MARKER = "sf-sbh-c1"
ROOT = Path("/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1")
SCRATCH = Path("/private/tmp/sf-sbh-c1")
SOURCE = ROOT / "docs/reports/2026-11-12-sandbox-hold-ci-r1-data"
DESTINATION = ROOT / "docs/reports/2026-11-12-sandbox-hold-data"
RESULT = DESTINATION / "ci-r1-cleanup.json"
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".hypothesis"}
MANIFEST_NAMES = ("temp-directories.jsonl", "external-deletions.jsonl")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def absent(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def guarded_rmtree(path: Path) -> None:
    """Only remove a real directory whose supplied and canonical paths carry the marker."""
    assert path.is_absolute(), path
    assert MARKER in str(path), path
    assert not path.is_symlink(), path
    canonical = path.resolve()
    assert MARKER in str(canonical), canonical
    assert canonical.is_dir(), canonical
    shutil.rmtree(canonical)


def manifest(path: Path, *, allow_missing: bool = False) -> list[str]:
    if allow_missing and absent(path):
        return []
    assert path.is_file() and not path.is_symlink(), path
    values = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert all(isinstance(value, str) and Path(value).is_absolute() for value in values), path
    return values


def contains_tracked(path: Path, tracked: set[Path]) -> bool:
    relative = path.relative_to(ROOT)
    return any(item == relative or relative in item.parents for item in tracked)


def evidence_files(directory: Path) -> list[Path]:
    """Inventory only the assigned tree, without following directory symlinks."""
    files = []
    for current, names, leaves in os.walk(directory, followlinks=False):
        here = Path(current)
        assert here.resolve().is_relative_to(ROOT), here
        for name in names:
            assert not (here / name).is_symlink(), here / name
        for name in leaves:
            path = here / name
            assert path.is_file() and not path.is_symlink(), path
            files.append(path)
    return sorted(files)


def cache_inventory(tracked: set[Path]) -> tuple[list[Path], list[str]]:
    candidates, skipped = [], []
    for current, names, _ in os.walk(ROOT, followlinks=False):
        here = Path(current)
        assert here.resolve().is_relative_to(ROOT), here
        for name in list(names):
            path = here / name
            if name in {".git", ".agents", ".codex", ".aws"} or path.is_symlink():
                names.remove(name)
                continue
            if name in CACHE_NAMES:
                names.remove(name)
                if contains_tracked(path, tracked):
                    skipped.append(str(path))
                else:
                    candidates.append(path)
    return sorted(candidates), sorted(skipped)


def cache_counts(directory: Path) -> dict[str, int]:
    """Count only an owned worktree cache; do not traverse its symlink targets."""
    assert directory.resolve().is_relative_to(ROOT), directory
    directories, files, symlinks = 0, 0, 0
    for current, names, leaves in os.walk(directory, followlinks=False):
        directories += 1
        here = Path(current)
        for name in list(names):
            if (here / name).is_symlink():
                names.remove(name)
                symlinks += 1
        files += len(leaves)
        symlinks += sum((here / name).is_symlink() for name in leaves)
    return {"directories": directories, "files": files, "symlinks": symlinks}


def copy_exclusively(source: Path, target: Path) -> dict[str, object]:
    assert absent(target), target
    digest = hashlib.sha256()
    size = 0
    original_present = source.is_file()
    # The deletion log need not exist when every fixture stayed under scratch.
    # Preserve that as an explicitly audited empty log without making a temp.
    assert original_present or source == SCRATCH / "external-deletions.jsonl", source
    with target.open("xb") as writer:
        if original_present:
            with source.open("rb") as reader:
                while block := reader.read(1024 * 1024):
                    writer.write(block)
                    digest.update(block)
                    size += len(block)
        writer.flush()
        os.fsync(writer.fileno())
    copied_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert copied_digest == digest.hexdigest(), target
    return {"original_path": str(source), "preserved_path": str(target),
            "original_present": original_present, "bytes": size, "sha256": copied_digest}


def clean(audit: dict[str, object]) -> None:
    assert Path(__file__).resolve().parents[3] == ROOT, __file__
    assert Path.cwd().resolve() == ROOT, Path.cwd()
    assert ROOT.is_dir() and ROOT.resolve() == ROOT, ROOT
    assert SCRATCH.is_dir() and not SCRATCH.is_symlink() and SCRATCH.resolve() == SCRATCH, SCRATCH
    assert SOURCE.is_dir() and not SOURCE.is_symlink() and SOURCE.resolve() == SOURCE, SOURCE
    assert DESTINATION.is_dir() and not DESTINATION.is_symlink(), DESTINATION
    assert DESTINATION.resolve() == DESTINATION, DESTINATION
    assert absent(RESULT), RESULT

    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "ls-files", "-z"]
    completed = subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE)
    tracked = {Path(os.fsdecode(value)) for value in completed.stdout.split(b"\0") if value}
    assert not contains_tracked(SOURCE, tracked), "Temporary evidence has tracked/staged files"

    allocated = manifest(SCRATCH / MANIFEST_NAMES[0])
    previous_deletions = manifest(SCRATCH / MANIFEST_NAMES[1], allow_missing=True)
    aliases: dict[Path, list[str]] = {}
    for original in allocated:
        canonical = Path(original).resolve()
        if not canonical.is_relative_to(SCRATCH):
            assert MARKER in str(canonical), canonical
            aliases.setdefault(canonical, []).append(original)

    external = []
    for path in sorted(aliases, key=lambda value: (len(value.parts), str(value))):
        assert MARKER in str(path), path
        assert not path.is_symlink(), path
        assert not ROOT.is_relative_to(path), path
        assert not path.is_relative_to(SOURCE) and not SOURCE.is_relative_to(path), path
        assert not path.is_relative_to(DESTINATION) and not DESTINATION.is_relative_to(path), path
        if path.is_relative_to(ROOT):
            assert not contains_tracked(path, tracked), path
        if absent(path):
            continue
        assert path.is_dir(), path
        if not any(path.is_relative_to(parent) for parent in external):
            external.append(path)

    caches, skipped_caches = cache_inventory(tracked)
    caches = [path for path in caches if not any(path.is_relative_to(parent) for parent in external)]
    cache_quarantine = SCRATCH / "worktree-cache-cleanup"
    evidence_quarantine = SCRATCH / "removed-ci-r1-evidence"
    assert absent(cache_quarantine), cache_quarantine
    assert absent(evidence_quarantine), evidence_quarantine

    # Preflight all output names before any mutation. Cache files are removed
    # first, so their bytecode/database artifacts are not evidence exports.
    files = [path for path in evidence_files(SOURCE)
             if not any(path.is_relative_to(cache) for cache in caches)]
    files.extend(SCRATCH / name for name in MANIFEST_NAMES)
    targets = [DESTINATION / ("ci-r1-" + path.name) for path in files]
    assert len(set(targets)) == len(targets), "Evidence basenames collide"
    assert RESULT not in targets, "Evidence collides with cleanup's final result"
    for target in targets:
        assert absent(target), target

    audit.update({
        "git_inventory_command": command,
        "allocation_manifest_lines": len(allocated),
        "allocated_unique_paths": len(set(allocated)),
        "external_allocated_unique_roots": len(aliases),
        "plugin_external_deletion_lines": len(previous_deletions),
        "plugin_external_deletion_unique_paths": len(set(previous_deletions)),
        "external_allocations": [{"canonical_path": str(path), "original_paths": sorted(set(values))}
                                 for path, values in sorted(aliases.items(), key=lambda pair: str(pair[0]))],
        "external_roots_deleted": [],
        "worktree_caches": [],
        "tracked_cache_directories_skipped": skipped_caches,
        "preserved_files": [],
        "temporary_evidence_original_path": str(SOURCE),
        "temporary_evidence_quarantine_path": str(evidence_quarantine),
        "scratch_original_path": str(SCRATCH),
    })

    for path in external:
        guarded_rmtree(path)
        audit["external_roots_deleted"].append(str(path))

    # Rename, rather than copy/delete, keeps every recursive cache deletion
    # behind the required marker guard. A cross-filesystem rename fails safely.
    if caches:
        cache_quarantine.mkdir()
    for index, path in enumerate(caches):
        assert path.resolve().is_relative_to(ROOT), path
        assert not path.is_symlink() and not contains_tracked(path, tracked), path
        counts = cache_counts(path)
        target = cache_quarantine / f"{index:04d}-{path.name}"
        assert absent(target), target
        path.rename(target)
        guarded_rmtree(target)
        audit["worktree_caches"].append({"original_path": str(path),
                                         "quarantine_path": str(target), **counts})

    # Ensure no evidence appeared/disappeared while validation was supposed
    # to be stopped. This is intentionally a single-use, post-tests command.
    assert evidence_files(SOURCE) == [path for path in files if path.is_relative_to(SOURCE)]
    for source, target in zip(files, targets):
        audit["preserved_files"].append(copy_exclusively(source, target))

    SOURCE.rename(evidence_quarantine)
    guarded_rmtree(evidence_quarantine)
    audit["temporary_evidence_removed"] = True
    guarded_rmtree(SCRATCH)
    audit["scratch_removed"] = True
    audit["external_roots_deleted_count"] = len(audit["external_roots_deleted"])
    audit["worktree_cache_roots_deleted_count"] = len(audit["worktree_caches"])
    audit["worktree_cache_directory_count"] = sum(item["directories"] for item in audit["worktree_caches"])
    audit["worktree_cache_file_count"] = sum(item["files"] for item in audit["worktree_caches"])
    audit["preserved_file_count"] = len(audit["preserved_files"])


if __name__ == "__main__":
    result: dict[str, object] = {"started_at": utc_now(), "marker": MARKER, "success": False}
    try:
        clean(result)
        result["success"] = True
    except BaseException:
        result["error"] = traceback.format_exc()
        raise
    finally:
        result["finished_at"] = utc_now()
        # Never overwrite baseline or a previous cleanup audit, including on
        # preflight failure. The source evidence is copied before its removal.
        assert DESTINATION.is_dir() and DESTINATION.resolve() == DESTINATION
        assert absent(RESULT), RESULT
        with RESULT.open("x") as output:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"success": result["success"], "audit": str(RESULT)}, sort_keys=True))
