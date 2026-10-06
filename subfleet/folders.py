"""C-6.5, C-24.5, C-26.3: who holds a folder, as lease rows.

A detached writer holds its write target alone: `worktree:<folder>`, one row,
one holder (C-6.5). Conversation turns share theirs (the owner's ruling of
2026-09-28, "nothing should be queued"): each turn job has its own row, so any
number of conversations in one folder run at once, and every holder-keyed
release site frees a turn's row with the job's other leases.

- `worktree-turn:<folder>:<job id>`: a writable turn. A detached writer waits
  while one exists, and a turn waits while `worktree:<folder>` is held.
- `worktree-read:<folder>:<job id>`: a read-only turn. It excludes no writer;
  it only keeps retention from removing the folder under it (C-8.4, C-13.4).
- `worktree:<folder>` held by `retention:<job id>`: a retirement's fence on a
  job's tree. No turn, writable or read-only, starts on that folder or on one
  inside it while it is held (`retiring`, C-8.4).

`<folder>` is a real path and may itself contain `:`; a job id never does
(C-1.1), so a key names a folder exactly when what follows `<prefix><folder>:`
has no colon. Rows are found by a range on the primary key and that check.
Admission reserves a row only on a folder it has just spelled in full
(`present`), so a row's folder is the one spelling of a folder that was there.

Keys compare folders as strings, so one folder must have one spelling:
`canonical` gives it, symlinks resolved and each name in the case the file
system stores it (review of 5e9f2fbd, P3-4). A git checkout's folder is its
top level, which git already spells so; a folder outside git (a scratch folder
a conversation works in) was kept as typed, and APFS is case-insensitive, so
`~/Scratch` and `~/scratch` were two keys for one folder and two conversations
there were never marked as sharing it.

The spelling is the kernel's (`getattrlist`'s ATTR_CMN_FULLPATH), which needs
only search permission on the directories above a folder. It was read from
directory listings, so under a directory that can be searched but not listed
(mode 0111) a name kept the case it was typed in: two spellings of one folder
were two keys again, and a `HOME` typed in another case than the volume stores
passed the C-26.10 refusal of a workspace that contains `~/.claude` (review of
b0033e5d, P2). The kernel's path also names a firmlinked folder one way
(`/Users/…`, never `/System/Volumes/Data/Users/…`), which the listings did not,
and a mount point by its own name, where ATTR_CMN_NAME gives the volume's
(`/` is "Macintosh HD"). What a path cannot be spelled by, `spelling` says.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys
import unicodedata
from typing import Any, Callable, Iterable

EXCLUSIVE = "worktree:"
TURN = "worktree-turn:"
READER = "worktree-read:"
SHARED = (TURN, READER)
RETENTION = "retention:"                    # the holder of a retirement's fence: `retention:<job id>`


def canonical(path: str | os.PathLike[str]) -> str:
    """`path`'s one spelling (`spelling`), whether or not it could be established:
    the key for a folder the daemon can reach, and for one it cannot (no job can
    work in that either) the path as far as it could be spelled."""
    return spelling(path)[0]


def spelling(path: str | os.PathLike[str]) -> tuple[str, str | None]:
    """`path`'s one spelling, and why it may not be (None when it is).

    `~` expanded, symlinks, `.` and `..` resolved, then the longest leading part
    the kernel can look up spelled as it holds it (`_kernel_path`), so every case
    and Unicode normalization a case-insensitive volume accepts for a folder gives
    one string, whatever the modes of the directories above it as long as they can
    be searched. The names after that part stay as given. That is still the one
    spelling when the first of them does not exist (its directory said so: ENOENT,
    or ENOTDIR for a name under a file), since no other spelling names anything.
    Otherwise (a directory above it that cannot be searched, a path too long, no
    getattrlist on this system) the second value says why, and a caller that must
    not be wrong about a folder (C-26.10) refuses instead of comparing it."""
    out, doubt, _ = _spelled(path)
    return out, doubt


def present(path: str | os.PathLike[str]) -> tuple[str, OSError | None]:
    """`path`'s one spelling (`spelling`), and, when some name in it could not be
    looked up now, the error that said so (None when every name was).

    A name `spelling` cannot look up because it does not exist stays as given, and
    that is the one spelling only while it does not exist. A folder that is not
    there now can come back under the name its volume stores, in another case than
    the one given: retention moves a finished job's tree into quarantine and,
    rolling the retirement back, moves it back, and a native session's recorded
    cwd may say `…/worktrees/jOB/vendor/lib` for the tree `Job`. A lease row names
    its folder for as long as it lives, and a row on `jOB` is one that no check on
    the tree's spelling matches (review of 8a112986, finding 1). So admission
    reserves a row only on a folder this finds there, spelled in full (C-8.4).

    Where the kernel spells nothing (no getattrlist) a folder is there when its
    real path is: such a system compares names as given."""
    out, _, missing = _spelled(path)
    return out, missing


def _spelled(path: str | os.PathLike[str]) -> tuple[str, str | None, OSError | None]:
    """`spelling`'s two values, and the error for the first name of the path that
    was not looked up (None when the kernel looked up all of it), for `present`."""
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    head, tail, first = real, [], None
    while True:
        try:
            spelled = _kernel_path(head)
            break
        except OSError as exc:
            parent, name = os.path.split(head)
            if parent == head:                  # not even `/`: nothing of it can be spelled
                there = exc.errno == errno.ENOSYS and os.path.lexists(real)
                gone = None if there else (exc if exc.errno != errno.ENOSYS
                                           else FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), real))
                return real, f"{real}: {exc.strerror or exc}", gone
            head, tail, first = parent, [name, *tail], exc     # `first`: the first name not looked up
    out = os.path.join(spelled, *tail)
    if first is None:
        return out, None, None
    name = os.path.join(spelled, tail[0])
    missing = OSError(first.errno, first.strerror, name)      # the name as far as spelled, its subclass by errno
    if first.errno in (errno.ENOENT, errno.ENOTDIR):
        return out, None, missing
    return out, f"{name}: {first.strerror or first}", missing


# getattrlist(2), <sys/attr.h> and <unistd.h> (LP64).
_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_FULLPATH = 0x08000000
_FSOPT_NOFOLLOW = 0x00000001


class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16), ("commonattr", ctypes.c_uint32),
                ("volattr", ctypes.c_uint32), ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


_getattrlist: Any = None


def _kernel_path(path: str) -> str:
    """The path the kernel holds for the file `path` names (not following a final
    symlink): getattrlist(2)'s ATTR_CMN_FULLPATH. Like `lstat`, it needs search
    permission on the directories above `path` and nothing else; nothing is listed
    or opened, so a directory above it at 0111, or the folder itself at 0111, does
    not stop it, where `fcntl(F_GETPATH)` needs the folder opened. Each name is as
    its directory stores it (case and Unicode form), also when the first lookup of
    it since the volume was mounted typed another; this and the modes were checked
    on APFS on 2026-10-03, on a disk image attached afresh. The answer is checked
    to name the same file as `path`. Raises OSError otherwise, and where there is
    no getattrlist (ENOSYS)."""
    global _getattrlist
    if _getattrlist is None:
        try:
            function = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True).getattrlist
            function.argtypes = [ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_uint]
            function.restype = ctypes.c_int
            _getattrlist = function
        except (OSError, AttributeError, TypeError):
            _getattrlist = False
    if not _getattrlist:
        raise OSError(errno.ENOSYS, "this system has no getattrlist", path)
    request = _AttrList(bitmapcount=_ATTR_BIT_MAP_COUNT, commonattr=_ATTR_CMN_FULLPATH)
    buffer = ctypes.create_string_buffer(8192)      # its length, an attrreference_t, then the path
    if _getattrlist(os.fsencode(path), ctypes.byref(request), buffer, len(buffer), _FSOPT_NOFOLLOW):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    raw = buffer.raw
    returned = int.from_bytes(raw[0:4], sys.byteorder)
    start = 4 + int.from_bytes(raw[4:8], sys.byteorder, signed=True)    # from the attrreference_t itself
    end = start + int.from_bytes(raw[8:12], sys.byteorder)
    spelled = os.fsdecode(raw[start:end].split(b"\0", 1)[0]) if 12 <= start < end <= returned <= len(raw) else ""
    try:
        same = spelled.startswith(os.sep) and os.path.samestat(os.lstat(spelled), os.lstat(path))
    except OSError:
        same = False
    if not same:
        raise OSError(errno.EIO, f"the kernel's path for it ({spelled or 'none'}) does not name it", path)
    return spelled


def exclusive_key(folder: str) -> str:
    return f"{EXCLUSIVE}{folder}"


def turn_key(folder: str, job_id: str, *, writable: bool) -> str:
    if ":" in job_id:
        raise ValueError(f"a job id never contains ':' (C-1.1): {job_id!r}")
    return f"{TURN if writable else READER}{folder}:{job_id}"


def parse(key: str) -> tuple[str, str, str] | None:
    """`(prefix, folder, job id)` of a turn's row, or None for any other key."""
    for prefix in SHARED:
        if key.startswith(prefix):
            folder, sep, job_id = key[len(prefix):].rpartition(":")
            if sep and folder and job_id:
                return prefix, folder, job_id
    return None


def _row(row: Any) -> tuple[str, str]:
    if isinstance(row, dict):
        return row["lease_key"], row["holder"]
    return row[0], row[1]


def within(folder: str, top: str) -> bool:
    """Whether `folder` is `top` or a folder inside it, as spelled: `/a/b/c` is in
    `/a/b`, and `/a/bc` and `/a/b:c` are not."""
    return folder == top or folder.startswith(top.rstrip("/") + "/")


def above(folder: str) -> list[str]:
    """The folders `folder` is inside, nearest first, `/` last: `/a/b/c` is in
    `/a/b`, `/a` and `/`. String operations only (`os.path.dirname`), so a store
    transaction may call it: the folder is spelled once, before the transaction
    (`canonical`). For a folder so spelled (absolute, no `.`, `..`, `//` or
    trailing `/`) these are exactly the other folders it is `within`."""
    found = []
    while (parent := os.path.dirname(folder)) not in (folder, ""):
        found.append(parent)
        folder = parent
    return found


def fold(path: str) -> str:
    """`path` as a case-insensitive volume compares names: Unicode's canonical
    caseless form, NFD(casefold(NFD(path))) (The Unicode Standard §3.13, D145), so
    `Job`, `jOB` and `JOB`, or a name in NFC and in NFD, fold alike. On APFS
    (checked 2026-10-05, nine pairs) a lookup found a folder by another name
    exactly when the two fold alike: `jOB` for `Job`, `STRASSE` for `straße`, `FI`
    for `ﬁ`, `ς` for `Σ`, NFD for NFC, and not `i` for `İ`, which do not. `/`
    folds to itself, so `within` holds between folded spellings as between
    spellings."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def retiring(read: Callable[[str, tuple], Iterable[Any]], folder: str, *, folded: bool = False) -> list[str]:
    """The `worktree:` keys retention holds (`RETENTION`) on `folder` or on a
    folder it is inside (`above`), nearest first. While one is held a retirement
    is archiving and moving that tree, so no turn starts there: also not one in
    a repository nested in the tree, whose row is keyed on that repository, not
    on the tree (C-8.4). A detached writer's `worktree:` on a folder above is
    not among them: a checkout's top level is the hold, and a repository nested
    in it is a hold of its own (C-6.5). `read(sql, params)` as for `turn_holds`;
    no filesystem work.

    `folded` is for a folder `present` could not spell in full, whose names it
    could not look up stay as given: its tree may be in quarantine under that
    very fence, spelled `Job` where the folder says `jOB`. Every fence of
    retention's is read then, and each compared `fold`ed. On a volume that tells
    case apart this also names a fence on another folder whose name differs from
    one of these only in case; the turn then only waits for that retirement."""
    if folded:
        rows = read("SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                    (EXCLUSIVE, EXCLUSIVE[:-1] + ";"))      # ':' + 1: every `worktree:` key
        mine = fold(folder)
        found = [key for key, holder in map(_row, rows)
                 if str(holder or "").startswith(RETENTION) and within(mine, fold(key[len(EXCLUSIVE):]))]
        return sorted(found, key=lambda key: (-key.count("/"), key))
    keys = [exclusive_key(each) for each in (folder, *above(folder))]
    held = dict(_row(row) for row in read(
        f"SELECT lease_key, holder FROM leases WHERE lease_key IN ({','.join('?' * len(keys))})", tuple(keys)))
    return [key for key in keys if str(held.get(key) or "").startswith(RETENTION)]


def turn_holds(read: Callable[[str, tuple], Iterable[Any]], folder: str,
               kinds: tuple[str, ...] = SHARED, *, inside: bool = False) -> list[tuple[str, str]]:
    """The turn rows `(lease key, holder)` on exactly `folder`, of the prefixes in
    `kinds`, and with `inside` also those on a folder inside it (`within`): a
    conversation in a repository nested in a job's worktree keys its row on that
    repository, and retention removes it with the worktree (review of 599af189,
    P3-1). `read(sql, params)` runs one statement: a store's `query`, or a
    transaction's `execute(...).fetchall()`, so the answer is the transaction's."""
    found: dict[str, str] = {}
    for prefix in kinds:
        low = f"{prefix}{folder}:"
        high = low[:-1] + ";"                 # ':' + 1: every key that starts with `low`
        for row in read("SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                        (low, high)):
            key, holder = _row(row)
            if ":" not in key[len(low):]:     # not a longer folder that starts `<folder>:`
                found[key] = holder
        if inside:
            low = f"{prefix}{folder.rstrip('/')}/"
            high = low[:-1] + "0"             # '/' + 1: every key that starts with `low`
            for row in read("SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                            (low, high)):
                key, holder = _row(row)
                parsed = parse(key)
                if parsed and within(parsed[1], folder):
                    found[key] = holder
    return list(found.items())


def turn_folders(read: Callable[[str, tuple], Iterable[Any]]) -> set[str]:
    """Every folder a live turn holds, writable or read-only (retention's pins)."""
    folders = set()
    for prefix in SHARED:
        for row in read("SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                        (prefix, prefix[:-1] + ";")):
            parsed = parse(_row(row)[0])
            if parsed:
                folders.add(parsed[1])
    return folders
