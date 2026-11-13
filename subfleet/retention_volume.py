"""Volume identity for offloaded retention archives (C-8.8, D-ARCHIVE-OFFLOAD1).

An offloaded archive records the volume that holds its files by UUID, so it can
be found again under whatever mount point the volume gets next time. Everything
this module knows about volumes comes from one function, `volumes`, which tests
replace with stand-in volumes in a temporary folder:

- `volumes(path)` is a one-element list: the volume that holds `path`, with its
  encryption read (`diskutil info -plist`, a read-only verb);
- `volumes()` is every mounted volume, encryption not read (None).

The real reads are `statfs`/`getfsstat` (mount point, file system type, flags,
free space) and `getattrlist` with `ATTR_VOL_UUID` and `ATTR_VOL_NAME` on the
mount point. Nothing here writes to any volume.
"""
from __future__ import annotations

import ctypes
import os
import plistlib
import subprocess
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path

#: <sys/mount.h>
MNT_RDONLY = 0x00000001
MNT_LOCAL = 0x00001000
MNT_IGNORE_OWNERSHIP = 0x00200000       # mounted `noowners`
MNT_NOWAIT = 2
#: File systems that keep no owners or permissions: an archive holds every
#: file of a job's tree, credentials included, so they never take one.
NO_OWNERS = frozenset({"exfat", "msdos", "fat", "fat32", "vfat", "ntfs", "cd9660", "udf"})
NETWORK = frozenset({"smbfs", "nfs", "afpfs", "webdav", "ftp", "cifs", "fuse-t", "macfuse", "osxfuse"})


@dataclass(frozen=True)
class Volume:
    """One mounted volume as offload sees it."""
    uuid: str | None
    name: str
    mount: Path
    fstype: str
    local: bool
    owners: bool          # keeps owners and permissions
    readonly: bool
    free: int             # bytes available to this user
    encrypted: bool | None = None     # None: not read, or unknown

    def describe(self) -> dict[str, object]:
        return {"uuid": self.uuid, "name": self.name, "mount": str(self.mount), "fstype": self.fstype,
                "local": self.local, "owners": self.owners, "readonly": self.readonly, "free": self.free,
                "encrypted": self.encrypted}


# --- the real reads (macOS) ---------------------------------------------------

_MAXPATHLEN = 1024


class _Statfs(ctypes.Structure):
    # `struct statfs` with 64-bit inodes (__DARWIN_STRUCT_STATFS64, the only
    # layout on arm64; `statfs$INODE64` on x86_64).
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
                ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
                ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
                ("f_mntfromname", ctypes.c_char * _MAXPATHLEN), ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7)]


class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32), ("dirattr", ctypes.c_uint32),
                ("fileattr", ctypes.c_uint32), ("forkattr", ctypes.c_uint32)]


_ATTR_VOL_INFO = 0x80000000
_ATTR_VOL_NAME = 0x00002000
_ATTR_VOL_UUID = 0x00040000

_libc = ctypes.CDLL(None, use_errno=True)


def _symbol(*names: str):
    for name in names:
        try:
            return getattr(_libc, name)
        except AttributeError:
            continue
    return None


_statfs = _symbol("statfs$INODE64", "statfs")
_getfsstat = _symbol("getfsstat$INODE64", "getfsstat")
_getattrlist = _symbol("getattrlist")
if _statfs is not None:
    _statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Statfs)]
    _statfs.restype = ctypes.c_int
if _getfsstat is not None:
    _getfsstat.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _getfsstat.restype = ctypes.c_int
if _getattrlist is not None:
    _getattrlist.argtypes = [ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.c_ulong]
    _getattrlist.restype = ctypes.c_int


def _statfs_of(path: str) -> _Statfs:
    if _statfs is None:
        raise OSError("statfs is not available on this platform")
    buf = _Statfs()
    if _statfs(os.fsencode(path), ctypes.byref(buf)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    return buf


def _uuid_and_name(mount: str) -> tuple[str | None, str | None]:
    """`ATTR_VOL_UUID` and `ATTR_VOL_NAME` of the volume mounted at `mount`."""
    if _getattrlist is None:
        return None, None
    request = _AttrList(bitmapcount=5, volattr=_ATTR_VOL_INFO | _ATTR_VOL_NAME | _ATTR_VOL_UUID)
    size = 4 + 8 + 16 + 1024
    buf = ctypes.create_string_buffer(size)
    if _getattrlist(os.fsencode(mount), ctypes.byref(request), buf, size, 0) != 0:
        return None, None
    raw = buf.raw
    # Packed in bit order after the length word: the name's attrreference_t
    # (offset from itself, length with the NUL), then the 16-byte uuid_t.
    offset = int.from_bytes(raw[4:8], "little", signed=True)
    length = int.from_bytes(raw[8:12], "little")
    start = 4 + offset
    name = raw[start:start + length].split(b"\0", 1)[0].decode("utf-8", "replace") if length else None
    value = raw[12:28]
    identity = None if value == bytes(16) else str(_uuid.UUID(bytes=value)).upper()
    return identity, name


def _encrypted(mount: str) -> bool | None:
    """Whether the volume is encrypted, from `diskutil info -plist` (read-only)."""
    try:
        done = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", mount], capture_output=True, timeout=60)
        info = plistlib.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    keys = ("FileVault", "EncryptionThisVolumeProper", "Encryption", "Encrypted")
    if not any(k in info for k in keys):
        return None
    return any(bool(info.get(k)) for k in keys)


def _volume(fs: _Statfs, *, encryption: bool) -> Volume:
    mount = os.fsdecode(fs.f_mntonname)
    fstype = os.fsdecode(fs.f_fstypename).lower()
    flags = fs.f_flags
    local = bool(flags & MNT_LOCAL) and fstype not in NETWORK
    # A network or automounted volume is never a target: asking it for its
    # identity could wait on the network or trigger a mount, so it is not asked.
    identity, name = _uuid_and_name(mount) if local else (None, None)
    return Volume(uuid=identity, name=name or Path(mount).name or mount, mount=Path(mount), fstype=fstype,
                  local=local, owners=not (flags & MNT_IGNORE_OWNERSHIP) and fstype not in NO_OWNERS,
                  readonly=bool(flags & MNT_RDONLY), free=int(fs.f_bavail) * int(fs.f_bsize),
                  encrypted=_encrypted(mount) if encryption and local else None)


def _mounted() -> list[Volume]:
    if _getfsstat is None:
        return []
    count = _getfsstat(None, 0, MNT_NOWAIT)
    if count <= 0:
        return []
    array = (_Statfs * (count + 8))()
    count = _getfsstat(array, ctypes.sizeof(array), MNT_NOWAIT)
    return [_volume(array[i], encryption=False) for i in range(max(count, 0))]


def volumes(path: Path | None = None) -> list[Volume]:
    """The volume holding `path` (its encryption read), or every mounted volume.

    The one seam for volume identity: tests replace this function."""
    if path is not None:
        return [_volume(_statfs_of(str(path)), encryption=True)]
    return _mounted()


# --- what offload asks -----------------------------------------------------------

def volume_of(path: Path) -> Volume:
    (found,) = volumes(path)
    return found


def mounted_with(uuid: str) -> Volume | None:
    """The mounted volume whose UUID is `uuid`, or None."""
    wanted = uuid.upper()
    for volume in volumes():
        if volume.uuid and volume.uuid.upper() == wanted:
            return volume
    return None


def refusals(volume: Volume, *, allow_unencrypted: bool) -> list[str]:
    """Why `volume` cannot take an offloaded archive (empty: it can)."""
    why: list[str] = []
    if not volume.uuid:
        why.append(f"volume {volume.name!r} has no UUID, so the archive could not find it again")
    if not volume.local:
        why.append(f"volume {volume.name!r} is not a local volume ({volume.fstype}); "
                   "an archive holds a job's credentials and must not go over a network")
    if not volume.owners or volume.fstype in NO_OWNERS:
        why.append(f"volume {volume.name!r} ({volume.fstype}) does not keep owners and permissions; "
                   "an archive holds a job's credentials")
    if volume.readonly:
        why.append(f"volume {volume.name!r} is read-only")
    if volume.encrypted is not True and not allow_unencrypted:
        state = "not encrypted" if volume.encrypted is False else "of unknown encryption"
        why.append(f"volume {volume.name!r} is {state}; pass --allow-unencrypted to use it anyway")
    return why
