"""Retention's narrow holder reader (design 8): libproc, asked only about what can hold a tree.

Retention runs this file as a child process (`retention_holders.vnode_holders`):
`python -I -S retention_vnodes.py --path <P> [--path ...] [--dev <st_dev> ...]`, where each
path is an existing directory or file on a volume that holds a watched tree. It never runs inside the
daemon, so a structure layout that does not match the kernel's can kill only this
process, and the daemon fails closed on its exit status. It imports only the standard
library, so it runs isolated (`-I -S`: no site directory, no script directory on the path).

For every process (`proc_listallpids`), one `PROC_PIDT_SHORTBSDINFO` call gives its user
and command; a process of another user is skipped (a non-root `lsof` cannot read it
either), and for each process of this user the reader asks:

- its current and root directory: one `PROC_PIDVNODEPATHINFO` call;
- its descriptors: `PROC_PIDLISTFDS` (one call to size the table, one to read it), then one
  `proc_pidfdinfo(PROC_PIDFDVNODEPATHINFO)` call per vnode descriptor, which gives its path,
  device, inode, vnode type and open flags. Sockets, pipes, kqueues and every other kind
  of descriptor are never asked about;
- its mapped files: one `PROC_PIDREGIONPATHINFO3` call per watched volume, which walks the
  process's map in the kernel and returns the first region backed by a file on that
  volume (its argument is the volume's 64-bit fsid, `val[1] << 32 | val[0]`, which the
  reader takes from a descriptor it opens on each `--path`). Only a process that maps
  such a file is walked region by region, with `PROC_PIDREGIONPATHINFO2` (one call per
  file-backed region, from address 0: the kernel skips regions no file backs). A `--dev`
  with no `--path` on it cannot be probed, so with one every process is walked.

A process that exits while it is read is skipped (`ESRCH`; what was read of it stays). A
process of this user that cannot be read (`EPERM`, `EACCES`) is skipped and counted, as
`lsof -w` skips it. Any other error, or a reply whose size is not the structure's, ends
the reader with a non-zero status.

It prints, in the format of `lsof -F pcftaDin` (so `retention_holders.parse` applies the
same hold rules to both readers), only what can be a hold on a watched device: the
current and root directory, directory descriptors, descriptors open for writing, and
mapped files (`txt`). A read-only descriptor on anything but a directory is never a hold,
so it is not printed. A newline in a path or command is printed as the two characters
`\\n`. The last line is `#end` with the counts and the seconds each part took; output
without it is malformed, and the parent treats it as a failure.
"""
from __future__ import annotations

import ctypes
import errno
import os
import sys
import time

# <sys/proc_info.h>
PROC_PIDLISTFDS = 1
PROC_PIDREGIONPATHINFO2 = 22
PROC_PIDREGIONPATHINFO3 = 23
PROC_PIDVNODEPATHINFO = 9
PROC_PIDT_SHORTBSDINFO = 13
PROC_PIDFDVNODEPATHINFO = 2
PROX_FDTYPE_VNODE = 1
FREAD = 0x1
FWRITE = 0x2
MAXPATHLEN = 1024
MAXCOMLEN = 16
VTYPES = {1: "REG", 2: "DIR", 3: "BLK", 4: "CHR", 5: "LINK", 6: "SOCK", 7: "FIFO"}
VREG, VDIR = 1, 2
#: A region walk longer than this is a reader fault, not a process.
MAX_REGIONS = 1_000_000


class VinfoStat(ctypes.Structure):
    _fields_ = [("vst_dev", ctypes.c_uint32), ("vst_mode", ctypes.c_uint16), ("vst_nlink", ctypes.c_uint16),
                ("vst_ino", ctypes.c_uint64), ("vst_uid", ctypes.c_uint32), ("vst_gid", ctypes.c_uint32),
                ("vst_atime", ctypes.c_int64), ("vst_atimensec", ctypes.c_int64),
                ("vst_mtime", ctypes.c_int64), ("vst_mtimensec", ctypes.c_int64),
                ("vst_ctime", ctypes.c_int64), ("vst_ctimensec", ctypes.c_int64),
                ("vst_birthtime", ctypes.c_int64), ("vst_birthtimensec", ctypes.c_int64),
                ("vst_size", ctypes.c_int64), ("vst_blocks", ctypes.c_int64), ("vst_blksize", ctypes.c_int32),
                ("vst_flags", ctypes.c_uint32), ("vst_gen", ctypes.c_uint32), ("vst_rdev", ctypes.c_uint32),
                ("vst_qspare", ctypes.c_int64 * 2)]


class VnodeInfo(ctypes.Structure):
    _fields_ = [("vi_stat", VinfoStat), ("vi_type", ctypes.c_int), ("vi_pad", ctypes.c_int),
                ("vi_fsid", ctypes.c_int32 * 2)]


class VnodeInfoPath(ctypes.Structure):
    _fields_ = [("vip_vi", VnodeInfo), ("vip_path", ctypes.c_char * MAXPATHLEN)]


class ProcVnodePathInfo(ctypes.Structure):
    _fields_ = [("pvi_cdir", VnodeInfoPath), ("pvi_rdir", VnodeInfoPath)]


class ProcFileInfo(ctypes.Structure):
    _fields_ = [("fi_openflags", ctypes.c_uint32), ("fi_status", ctypes.c_uint32), ("fi_offset", ctypes.c_int64),
                ("fi_type", ctypes.c_int32), ("fi_guardflags", ctypes.c_uint32)]


class VnodeFdInfoWithPath(ctypes.Structure):
    _fields_ = [("pfi", ProcFileInfo), ("pvip", VnodeInfoPath)]


class ProcFdInfo(ctypes.Structure):
    _fields_ = [("proc_fd", ctypes.c_int32), ("proc_fdtype", ctypes.c_uint32)]


class ProcRegionInfo(ctypes.Structure):
    _fields_ = [("pri_protection", ctypes.c_uint32), ("pri_max_protection", ctypes.c_uint32),
                ("pri_inheritance", ctypes.c_uint32), ("pri_flags", ctypes.c_uint32),
                ("pri_offset", ctypes.c_uint64), ("pri_behavior", ctypes.c_uint32),
                ("pri_user_wired_count", ctypes.c_uint32), ("pri_user_tag", ctypes.c_uint32),
                ("pri_pages_resident", ctypes.c_uint32), ("pri_pages_shared_now_private", ctypes.c_uint32),
                ("pri_pages_swapped_out", ctypes.c_uint32), ("pri_pages_dirtied", ctypes.c_uint32),
                ("pri_ref_count", ctypes.c_uint32), ("pri_shadow_depth", ctypes.c_uint32),
                ("pri_share_mode", ctypes.c_uint32), ("pri_private_pages_resident", ctypes.c_uint32),
                ("pri_shared_pages_resident", ctypes.c_uint32), ("pri_obj_id", ctypes.c_uint32),
                ("pri_depth", ctypes.c_uint32), ("pri_address", ctypes.c_uint64), ("pri_size", ctypes.c_uint64)]


class ProcRegionWithPathInfo(ctypes.Structure):
    _fields_ = [("prp_prinfo", ProcRegionInfo), ("prp_vip", VnodeInfoPath)]


class ProcBsdShortInfo(ctypes.Structure):
    _fields_ = [("pbsi_pid", ctypes.c_uint32), ("pbsi_ppid", ctypes.c_uint32), ("pbsi_pgid", ctypes.c_uint32),
                ("pbsi_status", ctypes.c_uint32), ("pbsi_comm", ctypes.c_char * MAXCOMLEN),
                ("pbsi_flags", ctypes.c_uint32), ("pbsi_uid", ctypes.c_uint32), ("pbsi_gid", ctypes.c_uint32),
                ("pbsi_ruid", ctypes.c_uint32), ("pbsi_rgid", ctypes.c_uint32), ("pbsi_svuid", ctypes.c_uint32),
                ("pbsi_svgid", ctypes.c_uint32), ("pbsi_rfu", ctypes.c_uint32)]


#: The sizes the kernel's structures have (xnu `bsd/sys/proc_info.h`); a reply of
#: another size means this file's layout is wrong, and the reader stops.
SIZES = {VinfoStat: 136, VnodeInfo: 152, VnodeInfoPath: 1176, ProcVnodePathInfo: 2352, ProcFileInfo: 24,
         VnodeFdInfoWithPath: 1200, ProcFdInfo: 8, ProcRegionInfo: 96, ProcRegionWithPathInfo: 1272,
         ProcBsdShortInfo: 64}


class Fault(Exception):
    """The reader cannot vouch for its answer: exit non-zero."""


class Gone(Exception):
    """The process exited while it was read."""


class Unreadable(Exception):
    """A process of this user that libproc will not read."""


def _lib():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.proc_listallpids.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidfdinfo.restype = ctypes.c_int
    return lib


def _escape(raw: bytes) -> bytes:
    return raw.replace(b"\n", b"\\n")


class Reader:
    def __init__(self, paths: list[str], devices: set[int] = frozenset()):
        for struct, size in SIZES.items():
            if ctypes.sizeof(struct) != size:
                raise Fault(f"{struct.__name__} is {ctypes.sizeof(struct)} bytes here, {size} in the kernel")
        self.lib = _lib()
        self.uid = os.geteuid()
        #: The volumes to probe (64-bit fsid) and the devices whose entries are printed.
        self.probes: set[int] = set()
        self.devices: set[int] = set()
        for path in paths:
            dev, _, fsid, _ = self.identity(path)
            self.devices.add(dev)
            self.probes.add(fsid)
        #: A device no path names cannot be probed: every process is walked then.
        self.walk_all = bool(set(devices) - self.devices)
        self.devices |= set(devices)
        if not self.devices:
            raise Fault("no volume to watch")
        self.out: list[bytes] = []
        self.counts = dict.fromkeys(("processes", "read", "exited", "unreadable", "other_users", "descriptors",
                                     "vnode_descriptors", "descriptors_gone", "descriptor_errors", "region_probes",
                                     "regions", "mapped", "records"), 0)
        #: pid -> seconds, for `--timings` (measurement only; no path or command).
        self.per_pid: dict[int, float] = {}
        self.seconds = {"processes": 0.0, "descriptors": 0.0, "regions": 0.0}

    # one call ------------------------------------------------------------------------

    def _info(self, pid: int, flavor: int, arg: int, buf, *, missing: tuple[int, ...] = ()) -> bool:
        """`proc_pidinfo` into `buf`; False when the reply is an errno in `missing`."""
        size = ctypes.sizeof(buf)
        got = self.lib.proc_pidinfo(pid, flavor, arg, ctypes.byref(buf), size)
        if got == size:
            return True
        if got > 0:
            raise Fault(f"flavor {flavor} answered {got} bytes for a {size}-byte structure")
        code = ctypes.get_errno()
        if code in missing:
            return False
        if code == errno.ESRCH:
            raise Gone()
        if code in (errno.EPERM, errno.EACCES):
            raise Unreadable()
        raise Fault(f"pid {pid}: flavor {flavor}: {errno.errorcode.get(code, code)}")

    def _fds(self, pid: int) -> list[ProcFdInfo]:
        size = self.lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
        if size <= 0:
            self._raise_for(pid, PROC_PIDLISTFDS)
        count = size // ctypes.sizeof(ProcFdInfo) + 32
        table = (ProcFdInfo * count)()
        got = self.lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, ctypes.byref(table), ctypes.sizeof(table))
        if got <= 0:
            self._raise_for(pid, PROC_PIDLISTFDS)
        if got % ctypes.sizeof(ProcFdInfo):
            raise Fault(f"PROC_PIDLISTFDS answered {got} bytes, not a whole number of entries")
        return list(table[:got // ctypes.sizeof(ProcFdInfo)])

    def _raise_for(self, pid: int, flavor: int) -> None:
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            raise Gone()
        if code in (errno.EPERM, errno.EACCES):
            raise Unreadable()
        raise Fault(f"pid {pid}: flavor {flavor}: {errno.errorcode.get(code, code)}")

    # records -------------------------------------------------------------------------

    def _record(self, fd: bytes, info: VnodeInfoPath, access: bytes | None = None) -> None:
        st = info.vip_vi.vi_stat
        if st.vst_dev not in self.devices:
            return
        lines = [b"f" + fd]
        if access is not None:
            lines.append(b"a" + access)
        lines += [b"t" + VTYPES.get(info.vip_vi.vi_type, "VNON").encode(), b"D0x%x" % st.vst_dev,
                  b"i%d" % st.vst_ino, b"n" + _escape(info.vip_path)]
        self.out.extend(lines)
        self.counts["records"] += 1

    def process(self, pid: int) -> None:
        started = time.perf_counter()
        bsd = ProcBsdShortInfo()
        try:
            self._info(pid, PROC_PIDT_SHORTBSDINFO, 0, bsd)
        except Gone:
            self.counts["exited"] += 1
            return
        except Unreadable:
            self.counts["unreadable"] += 1
            return
        finally:
            self.seconds["processes"] += time.perf_counter() - started
        if bsd.pbsi_uid != self.uid:
            self.counts["other_users"] += 1
            return
        header = len(self.out)
        self.out += [b"p%d" % pid, b"c" + _escape(bsd.pbsi_comm)]
        records = self.counts["records"]
        phase = "descriptors"
        started = time.perf_counter()
        try:
            dirs = ProcVnodePathInfo()
            self._info(pid, PROC_PIDVNODEPATHINFO, 0, dirs)
            self._record(b"cwd", dirs.pvi_cdir)
            self._record(b"rtd", dirs.pvi_rdir)
            fds = self._fds(pid)
            self.counts["descriptors"] += len(fds)
            info = VnodeFdInfoWithPath()
            for entry in fds:
                if entry.proc_fdtype != PROX_FDTYPE_VNODE:
                    continue
                self.counts["vnode_descriptors"] += 1
                size = ctypes.sizeof(info)
                got = self.lib.proc_pidfdinfo(pid, entry.proc_fd, PROC_PIDFDVNODEPATHINFO, ctypes.byref(info), size)
                if got != size:
                    if got > 0:
                        raise Fault(f"PROC_PIDFDVNODEPATHINFO answered {got} bytes for {size}")
                    code = ctypes.get_errno()
                    if code in (errno.EBADF, errno.ENOENT):
                        # Closed since the table was read, or its vnode reclaimed
                        # (`vnode_getwithvid` fails): it holds no file now.
                        self.counts["descriptors_gone"] += 1
                        continue
                    if code not in (errno.ESRCH, errno.EPERM, errno.EACCES):
                        # What `lsof` prints as an error in the name field: no
                        # path to match, so no hold. Counted and logged.
                        self.counts["descriptor_errors"] += 1
                        continue
                    self._raise_for(pid, PROC_PIDFDVNODEPATHINFO)
                flags = info.pfi.fi_openflags
                access = b"u" if flags & FREAD and flags & FWRITE else b"w" if flags & FWRITE else b"r"
                if access != b"r" or info.pvip.vip_vi.vi_type == VDIR:
                    self._record(b"%d" % entry.proc_fd, info.pvip, access)
            self.seconds["descriptors"] += time.perf_counter() - started
            phase, started = "regions", time.perf_counter()
            self._regions(pid)
            self.counts["read"] += 1
        except Gone:
            self.counts["exited"] += 1
        except Unreadable:
            self.counts["unreadable"] += 1
        finally:
            self.seconds[phase] += time.perf_counter() - started
        if self.counts["records"] == records:
            del self.out[header:]                       # nothing of this process to print

    def identity(self, path: str) -> tuple[int, int, int, int]:
        """(device, inode, 64-bit fsid, vnode type) of `path`, read the way every
        descriptor is read: through a descriptor this process opens on it."""
        fd = os.open(path, os.O_RDONLY)
        try:
            info = VnodeFdInfoWithPath()
            got = self.lib.proc_pidfdinfo(os.getpid(), fd, PROC_PIDFDVNODEPATHINFO, ctypes.byref(info),
                                          ctypes.sizeof(info))
            if got != ctypes.sizeof(info):
                raise Fault(f"PROC_PIDFDVNODEPATHINFO on this process's own descriptor answered {got}")
            st = os.fstat(fd)
        finally:
            os.close(fd)
        vi = info.pvip.vip_vi
        if (vi.vi_stat.vst_dev, vi.vi_stat.vst_ino) != (st.st_dev, st.st_ino):
            raise Fault(f"self-test: PROC_PIDFDVNODEPATHINFO does not describe {path}")
        fsid = (ctypes.c_uint32(vi.vi_fsid[1]).value << 32) | ctypes.c_uint32(vi.vi_fsid[0]).value
        return vi.vi_stat.vst_dev, vi.vi_stat.vst_ino, fsid, vi.vi_type

    def _regions(self, pid: int) -> None:
        region = ProcRegionWithPathInfo()
        if not self.walk_all and not any(self._probe(pid, fsid, region) for fsid in sorted(self.probes)):
            return
        seen: set[tuple[int, int, bytes]] = set()
        address = 0
        for _ in range(MAX_REGIONS):
            if not self._info(pid, PROC_PIDREGIONPATHINFO2, address, region, missing=(errno.EINVAL,)):
                return
            self.counts["regions"] += 1
            info = region.prp_vip
            key = (info.vip_vi.vi_stat.vst_dev, info.vip_vi.vi_stat.vst_ino, info.vip_path)
            if key not in seen:
                seen.add(key)
                if info.vip_vi.vi_stat.vst_dev in self.devices:
                    self.counts["mapped"] += 1
                    self._record(b"txt", info)
            following = region.prp_prinfo.pri_address + region.prp_prinfo.pri_size
            if following <= address:
                raise Fault(f"pid {pid}: the region walk did not move forward at {address:#x}")
            address = following
        raise Fault(f"pid {pid}: more than {MAX_REGIONS} regions")

    def _probe(self, pid: int, fsid: int, region: ProcRegionWithPathInfo) -> bool:
        self.counts["region_probes"] += 1
        return self._info(pid, PROC_PIDREGIONPATHINFO3, fsid, region, missing=(errno.EINVAL,))

    # the whole machine ---------------------------------------------------------------

    def pids(self) -> list[int]:
        estimate = self.lib.proc_listallpids(None, 0)
        if estimate <= 0:
            raise Fault(f"proc_listallpids: {errno.errorcode.get(ctypes.get_errno(), '?')}")
        buffer = (ctypes.c_int * (estimate + 256))()
        count = self.lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
        if count <= 0:
            raise Fault(f"proc_listallpids: {errno.errorcode.get(ctypes.get_errno(), '?')}")
        return sorted({pid for pid in buffer[:count] if pid >= 0})

    def self_test(self) -> None:
        """Read this process the way every other is read and compare with what the
        operating system says directly: a layout or flag fault shows here first."""
        me = os.getpid()
        bsd = ProcBsdShortInfo()
        self._info(me, PROC_PIDT_SHORTBSDINFO, 0, bsd)
        if bsd.pbsi_pid != me or bsd.pbsi_uid != self.uid:
            raise Fault("self-test: PROC_PIDT_SHORTBSDINFO does not describe this process")
        dirs = ProcVnodePathInfo()
        self._info(me, PROC_PIDVNODEPATHINFO, 0, dirs)
        here = os.stat(".")
        cdir = dirs.pvi_cdir
        if (cdir.vip_vi.vi_stat.vst_dev, cdir.vip_vi.vi_stat.vst_ino) != (here.st_dev, here.st_ino) \
                or cdir.vip_vi.vi_type != VDIR or os.fsencode(os.path.realpath(".")) != cdir.vip_path:
            raise Fault("self-test: PROC_PIDVNODEPATHINFO does not describe this process's directory")
        script = os.path.realpath(__file__)
        fd = os.open(script, os.O_RDONLY)
        try:
            st = os.fstat(fd)
            info = VnodeFdInfoWithPath()
            if self.lib.proc_pidfdinfo(me, fd, PROC_PIDFDVNODEPATHINFO, ctypes.byref(info),
                                       ctypes.sizeof(info)) != ctypes.sizeof(info):
                raise Fault("self-test: PROC_PIDFDVNODEPATHINFO failed on this process")
            vi = info.pvip.vip_vi
            if (vi.vi_stat.vst_dev, vi.vi_stat.vst_ino) != (st.st_dev, st.st_ino) or vi.vi_type != VREG \
                    or info.pfi.fi_openflags & (FREAD | FWRITE) != FREAD or info.pvip.vip_path != os.fsencode(script):
                raise Fault("self-test: PROC_PIDFDVNODEPATHINFO does not describe this process's descriptor")
            if not any(e.proc_fd == fd and e.proc_fdtype == PROX_FDTYPE_VNODE for e in self._fds(me)):
                raise Fault("self-test: PROC_PIDLISTFDS does not list this process's descriptor")
        finally:
            os.close(fd)
        mapped = _a_mapped_file()
        if mapped is not None:
            st = os.stat(mapped)
            fsid = self.identity(mapped)[2]
            region = ProcRegionWithPathInfo()
            if not self._info(me, PROC_PIDREGIONPATHINFO3, fsid, region, missing=(errno.EINVAL,)):
                raise Fault("self-test: PROC_PIDREGIONPATHINFO3 finds no mapping of this process")
            address, found = 0, False
            for _ in range(MAX_REGIONS):
                if not self._info(me, PROC_PIDREGIONPATHINFO2, address, region, missing=(errno.EINVAL,)):
                    break
                vst = region.prp_vip.vip_vi.vi_stat
                if (vst.vst_dev, vst.vst_ino) == (st.st_dev, st.st_ino):
                    found = True
                    break
                address = region.prp_prinfo.pri_address + region.prp_prinfo.pri_size
            if not found:
                raise Fault(f"self-test: PROC_PIDREGIONPATHINFO2 does not list {mapped}, which this process maps")

    def run(self) -> bytes:
        self.self_test()
        started = time.perf_counter()
        pids = self.pids()
        self.counts["processes"] = len(pids)
        self.seconds["processes"] += time.perf_counter() - started
        for pid in pids:
            began = time.perf_counter()
            self.process(pid)
            self.per_pid[pid] = time.perf_counter() - began
        trailer = " ".join([f"{k}={v}" for k, v in self.counts.items()]
                           + [f"seconds_{k}={v:.3f}" for k, v in self.seconds.items()])
        return b"\n".join(self.out + [b"#end " + trailer.encode()]) + b"\n"


def _a_mapped_file() -> str | None:
    """A file this process certainly maps outside the shared cache: the `_ctypes`
    extension module, or the interpreter itself."""
    import _ctypes
    for candidate in (getattr(_ctypes, "__file__", None), sys.executable):
        if candidate and os.path.isfile(candidate):
            return os.path.realpath(candidate)
    return None


USAGE = "usage: retention_vnodes.py --path <existing path> [--path ...] [--dev <st_dev> ...]"


def main(argv: list[str]) -> int:
    paths: list[str] = []
    devices: set[int] = set()
    timings = False
    args = list(argv)
    try:
        while args:
            flag = args.pop(0)
            if flag == "--path" and args:
                paths.append(args.pop(0))
            elif flag == "--dev" and args:
                devices.add(int(args.pop(0)))
            elif flag == "--timings":
                timings = True
            else:
                raise ValueError(flag)
    except ValueError as exc:
        print(f"{USAGE} (not {exc})", file=sys.stderr)
        return 2
    if not paths and not devices:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        reader = Reader(paths, devices)
        output = reader.run()
    except (Fault, OSError) as exc:
        print(f"retention_vnodes.py: {exc}", file=sys.stderr)
        return 3
    if timings:
        slowest = sorted(reader.per_pid.items(), key=lambda item: -item[1])[:10]
        print("slowest " + " ".join(f"{pid}:{seconds:.3f}" for pid, seconds in slowest), file=sys.stderr)
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
