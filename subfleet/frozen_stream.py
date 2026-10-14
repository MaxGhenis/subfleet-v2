"""C-4.3/C-9.2: durable stream copies, independent of provider writer identities."""

from __future__ import annotations

from collections.abc import Callable

import errno
import io
import json
import os
import stat
from pathlib import Path
from uuid import uuid4
from typing import BinaryIO

from .adapters.base import open_stream
from .contracts import Launch
from .guardian import atomic_copy, stream_digest
from .sessions.transcripts import NotRegularFile


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _stamp(info: os.stat_result) -> tuple:
    return (_identity(info), info.st_mode, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _writable_aliases(stream: BinaryIO, source: Path, launch: Launch,
                      held_source: BinaryIO | None = None) -> list[str]:
    """Compare descriptors, resolving destinations through directory aliases.

    An inode already held by the candidate/source descriptor is fstat-ed, never
    reopened, even when multiple destination names reach it. Link counts cannot
    account for an unlinked file with an open writer.
    """
    candidate = _identity(os.fstat(stream.fileno()))
    descriptors = {candidate: stream}
    if held_source is not None:
        descriptors[_identity(os.fstat(held_source.fileno()))] = held_source
    matches = []
    for role, path in (("stdout", launch.stdout_path), ("stderr", launch.stderr_path),
                       ("raw", launch.raw_stream_path), ("last", source.parent / "last.md")):
        if not path:
            continue
        resolved = Path(os.path.realpath(path))
        try:
            entry = resolved.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(entry.st_mode):
            continue
        held = descriptors.get(_identity(entry))
        if held is not None:
            observed = _identity(os.fstat(held.fileno()))
        else:
            try:
                with open_stream(resolved) as live:
                    observed = _identity(os.fstat(live.fileno()))
            except NotRegularFile:
                continue
            except FileNotFoundError:
                if _entry_exists(resolved):
                    raise
                continue
        if observed == candidate:
            matches.append(role)
    return matches


def read_freeze_receipt(adir: Path) -> dict | None:
    path = adir / "stream.freeze.json"
    try:
        with open_stream(path) as stream:
            data = stream.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            return None
        receipt = json.loads(data)
        return receipt if isinstance(receipt, dict) else None
    except (NotRegularFile, ValueError):
        return None
    except FileNotFoundError:
        if _entry_exists(path):
            raise
        return None


def _restored_copy(adir: Path, receipt: dict | None, launch: Launch,
                   held_source: BinaryIO | None = None) -> Path | None:
    """Only the published copy and its recorded identity/content prove freezing."""
    if not isinstance(receipt, dict) or receipt.get("version") != 1:
        return None
    name = receipt.get("file")
    if (not isinstance(name, str) or Path(name).name != name
            or not name.startswith("stream.frozen-") or not name.endswith(".jsonl")):
        return None
    path = adir / name
    try:
        with open_stream(path) as stream:
            before = os.fstat(stream.fileno())
            if _identity(before) != (receipt.get("device"), receipt.get("inode")):
                return None
            if _writable_aliases(stream, path, launch, held_source):
                return None
            digest = stream_digest(stream)
            after = os.fstat(stream.fileno())
    except NotRegularFile:
        return None
    except FileNotFoundError:
        if _entry_exists(path):
            raise
        return None
    if (_stamp(before) != _stamp(after) or digest["bytes"] != receipt.get("bytes")
            or digest["sha256"] != receipt.get("sha256")):
        return None
    return path


def freeze_stream(adir: Path, launch: Launch, publish: Callable[[str, Path, bytes], None],
                  publish_hook: Callable[[str, Path], None] | None = None,
                  opener: Callable[..., BinaryIO] = open_stream) -> Path:
    receipt = read_freeze_receipt(adir)
    if (restored := _restored_copy(adir, receipt, launch)) is not None:
        return restored
    reset = _entry_exists(adir / "stream.freeze.json")
    raw = Path(launch.raw_stream_path) if launch.raw_stream_path else None
    source = next((path for path in (raw, adir / "stream.jsonl")
                   if path is not None and _entry_exists(path)), None)
    if source is None:
        source = next((Path(path) for path in (launch.stdout_path, adir / "stdout")
                       if path and _entry_exists(Path(path))), None)
    if source is None:
        # Freeze absence too, so a later live entry cannot supply new events.
        source = adir / "stream.jsonl"
    events, aliases = True, []
    try:
        stream = opener(source)
    except NotRegularFile:
        stream, events = io.BytesIO(), False
    except FileNotFoundError:
        if _entry_exists(source):
            raise  # ENOENT on a still-present regular file is a real I/O error.
        stream, events = io.BytesIO(), False
    with stream:
        if events:
            before = os.fstat(stream.fileno())
            aliases = _writable_aliases(stream, source, launch)
        for _ in range(3):
            path = adir / f"stream.frozen-{uuid4().hex}.jsonl"
            stream.seek(0)
            if publish_hook:
                publish_hook("raw-stream", path)
            # Always copying covers unknown/unlinked writers and changes after
            # the identity decision. Retries retain this one source descriptor.
            capture = atomic_copy(path, stream)
            changed = False
            if events:
                after = os.fstat(stream.fileno())
                try:
                    changed = _stamp(before) != _stamp(after) or _stamp(before) != _stamp(source.lstat())
                except FileNotFoundError:
                    changed = True
            receipt = {"version": 1, "file": path.name, **capture, "source": str(source),
                       "events": events, "writable_aliases": aliases,
                       "changed_during_capture": changed, "reset_finalization": reset}
            # A new output alias or replacement at publication cannot grant
            # frozen authority. Try another fresh inode from the same descriptor.
            if _restored_copy(adir, receipt, launch, stream if events else None) is None:
                continue
            publish("stream-freeze", adir / "stream.freeze.json",
                    (json.dumps(receipt, sort_keys=True) + "\n").encode())
            if _restored_copy(adir, receipt, launch, stream if events else None) is not None:
                return path
    raise OSError(errno.ESTALE, "stream changed throughout frozen publication", str(path))
