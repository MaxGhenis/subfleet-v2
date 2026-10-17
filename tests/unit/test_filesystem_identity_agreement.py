"""One-way agreement with actual files, including initially absent names.

Disk images are local 20 MB fixtures, automatically detached. Run this file on
the hub when a sandbox denies hdiutil. No download or live state is involved.
"""
from pathlib import Path
import ctypes
import os
import plistlib
import re
import subprocess
import sys
import time

import pytest

from subfleet import folders

PAIRS = [
    ("case", "Result", "result"),
    ("NFC/NFD", "café", "cafe\u0301"),
    ("sharp-s", "ß", "ss"),
    ("ligature", "ﬁ", "fi"),
    ("sigma", "ς", "σ"),
    ("dotted-I", "İ", "i\u0307"),
    ("dotless/lower", "ı", "i"),
    ("dotless/upper", "ı", "I"),
    ("i/I", "i", "I"),
    *[(f"U+{code:04X}", "re" + chr(code) + "sult", "result")
      for code in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x034F, 0x2060, 0xFE0F)],
    ("U+0345-order", "α\u0345\u0301", "α\u0301\u0345"),
    ("U+0345-caseless", "α\u0345\u0301", "άι"),
    ("U+0345-leading", "\u0345\u0301", "\u0301ι"),
    ("U+0345-perispomeni", "ᾼ\u0342", "α\u0345\u0342"),
    ("iota-diaeresis-acute", "ΐ", "Ϊ\u0301"),
]


@pytest.fixture(params=["APFS", "Case-sensitive HFS+", "HFS+", "ExFAT"])
def volume(request, tmp_path):
    if sys.platform != "darwin":
        pytest.skip("Darwin filesystem fixtures")
    if request.param == "APFS":
        # DiskManagement is unavailable in some sandboxes; statfs needs only a
        # lookup on this fixture. Darwin's struct statfs prefix, with ample tail.
        class StatFS(ctypes.Structure):
            _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
                        ("counts", ctypes.c_uint64 * 5), ("fsid", ctypes.c_int32 * 2),
                        ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
                        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
                        ("name", ctypes.c_char * 16), ("tail", ctypes.c_char * 2048)]
        stat = StatFS()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.statfs(os.fsencode(tmp_path), ctypes.byref(stat)) or stat.name != b"apfs":
            pytest.skip("fresh Darwin-user temp folder is not on APFS")
        yield request.param, tmp_path
        return
    image = tmp_path / "volume.dmg"
    mount = tmp_path / "mount"
    mount.mkdir()
    created = subprocess.run(["hdiutil", "create", "-size", "20m", "-fs", request.param,
                              "-volname", "sf-identity", str(image)], capture_output=True, text=True)
    if created.returncode:
        pytest.skip(f"hdiutil create {request.param}: {created.stderr.strip()}")
    attached = subprocess.run(["hdiutil", "attach", "-plist", "-nobrowse", "-mountpoint", str(mount), str(image)],
                              capture_output=True, text=True)
    if attached.returncode:
        pytest.skip(f"hdiutil attach {request.param}: {attached.stderr.strip()}")
    device = _whole_disk(attached.stdout)
    try:
        yield request.param, mount
    finally:
        _detach(device or mount, device)


#: Spotlight or fseventsd can hold a volume for a moment after its files are
#: used; CI saw `hdiutil: couldn't eject "disk5" - Resource busy` on 2026-10-12.
DETACH_TRIES, DETACH_PAUSE_S = 5, 2.0


def _whole_disk(attach_plist: str) -> str | None:
    """The image's whole-disk device (`/dev/diskN`) from `hdiutil attach -plist`;
    None when the output names none."""
    try:
        entities = plistlib.loads(attach_plist.encode()).get("system-entities", [])
        devices = [entity.get("dev-entry") or "" for entity in entities]
    except Exception:                       # noqa: BLE001 - no device: detach by mount point
        return None
    return next((device for device in devices if re.fullmatch(r"/dev/disk[0-9]+", device)), None)


def _still_attached(device: str) -> bool | None:
    """Whether `hdiutil info` still lists `device`; None when it cannot say."""
    info = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, text=True)
    if info.returncode:
        return None
    try:
        images = plistlib.loads(info.stdout.encode()).get("images", [])
        return any(entity.get("dev-entry") == device
                   for image in images for entity in image.get("system-entities", []))
    except Exception:                       # noqa: BLE001 - unreadable: keep trying
        return None


def _detach(target, device=None):
    """Detach a test image, by its whole-disk device when known, waiting out a
    brief hold before forcing it; fail only when even `-force` cannot detach it.

    A busy eject can still unmount the volume, and then its mount point names
    nothing: on 2026-10-17 every try after the first busy one, `-force`
    included, said "No such file or directory". So the image is detached by its
    device, and after a failed try it counts as detached once `hdiutil info` no
    longer lists that device."""
    errors = []
    for attempt in range(DETACH_TRIES + 1):
        force = ["-force"] if attempt == DETACH_TRIES else []
        detached = subprocess.run(["hdiutil", "detach", *force, str(target)], capture_output=True, text=True)
        if detached.returncode == 0:
            return
        errors.append(detached.stderr.strip())
        if device and _still_attached(device) is False:
            return
        if attempt < DETACH_TRIES:
            time.sleep(DETACH_PAUSE_S)
    raise AssertionError(f"hdiutil detach {target}: {errors}")


def test_real_files_agree_with_identity(volume):
    label, root = volume
    for n, (pair, left, right) in enumerate(PAIRS):
        directory = root / f"pair-{n}"
        directory.mkdir()
        x, y = directory / (left + ".md"), directory / (right + ".md")
        absent_equal = folders.identity(x) == folders.identity(y)
        x.write_bytes(b"x")
        if not y.exists():
            y.write_bytes(b"y")
        same = os.path.samefile(x, y)
        equal = folders.identity(x) == folders.identity(y)
        print(f"{pair} | {label} | samefile={same} | identity_equal={equal} | absent_equal={absent_equal}")
        assert not same or equal, (label, pair, "existing")
        assert not same or absent_equal, (label, pair, "initially absent")


@pytest.mark.parametrize("left,right", [("α\u0345\u0301", "άι"), ("ΐ", "Ϊ\u0301"),
                                      ("ᾼ\u0342", "α\u0345\u0342"),
                                      ("ı", "I"), ("re\u200Bsult", "result")])
def test_conservative_rule_also_guards_absent_names(tmp_path, monkeypatch, left, right):
    monkeypatch.setattr(folders, "_case_sensitive", lambda _: False)
    assert folders.identity(tmp_path / left) == folders.identity(tmp_path / right)


@pytest.mark.parametrize("busy, expect_force, expect_error", [
    (0, False, False),                          # detaches at once
    (2, False, False),                          # a brief hold is waited out
    (DETACH_TRIES, True, False),                # only the last try forces it
    (DETACH_TRIES + 1, True, True),             # even -force failed: the test fails
])
def test_detach_waits_out_a_busy_volume_before_forcing(monkeypatch, tmp_path, busy, expect_force, expect_error):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        failed = len(calls) <= busy
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "",
                                           'hdiutil: couldn\'t eject "disk5" - Resource busy' if failed else "")

    slept = []
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", slept.append)
    if expect_error:
        with pytest.raises(AssertionError, match="Resource busy"):
            _detach(tmp_path)
    else:
        _detach(tmp_path)
    assert all(argv[:2] == ["hdiutil", "detach"] and argv[-1] == str(tmp_path) for argv in calls)
    assert ["-force" in argv for argv in calls] == [n == DETACH_TRIES for n in range(len(calls))]
    assert ("-force" in calls[-1]) == expect_force
    assert len(calls) == min(busy, DETACH_TRIES) + 1
    # A pause follows every busy try before the forced one, and none follows the last.
    assert slept == [DETACH_PAUSE_S] * min(busy, DETACH_TRIES)


ATTACH_PLIST = plistlib.dumps({"system-entities": [
    {"dev-entry": "/dev/disk5s1", "mount-point": "/tmp/m", "content-hint": "Apple_HFS"},
    {"dev-entry": "/dev/disk5", "content-hint": "GUID_partition_scheme"}]}).decode()


def _info_plist(*devices):
    return plistlib.dumps({"images": [{"image-path": "/tmp/v.dmg", "system-entities": [
        {"dev-entry": device} for device in devices]}]}).decode()


def test_the_whole_disk_is_read_from_attach_output():
    assert _whole_disk(ATTACH_PLIST) == "/dev/disk5"
    assert _whole_disk("") is None
    assert _whole_disk("not a plist") is None
    assert _whole_disk(plistlib.dumps({"system-entities": [{"dev-entry": "/dev/disk5s1"}]}).decode()) is None


@pytest.mark.parametrize("info_rc, gone_after, expect_error", [
    (0, 1, False),          # CI 10/17: busy once, then the mount point names nothing; the device is gone
    (0, 3, False),          # gone after a few busy tries
    (0, None, True),        # still listed after -force: the test fails
    (1, None, True),        # info cannot say: every try is made, and the test fails
])
def test_a_busy_eject_that_still_detached_counts_as_detached(monkeypatch, info_rc, gone_after, expect_error):
    """The 2026-10-17 shape: `couldn't eject "disk5" - Resource busy`, then "No such
    file or directory" on every later try, `-force` included."""
    detaches, infos, slept = [], [], []

    def run(argv, **kwargs):
        if argv[:2] == ["hdiutil", "info"]:
            infos.append(argv)
            listed = gone_after is None or len(detaches) < gone_after
            return subprocess.CompletedProcess(argv, info_rc, _info_plist("/dev/disk5", "/dev/disk5s1") if listed
                                               else _info_plist("/dev/disk9"), "")
        detaches.append(argv)
        stderr = ('hdiutil: couldn\'t eject "disk5" - Resource busy' if len(detaches) == 1
                  else "hdiutil: detach failed - No such file or directory")
        return subprocess.CompletedProcess(argv, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", slept.append)
    if expect_error:
        with pytest.raises(AssertionError, match="Resource busy"):
            _detach("/dev/disk5", "/dev/disk5")
        assert len(detaches) == DETACH_TRIES + 1 and "-force" in detaches[-1]
    else:
        _detach("/dev/disk5", "/dev/disk5")
        assert len(detaches) == gone_after
        assert not any("-force" in argv for argv in detaches)
    assert all(argv[-1] == "/dev/disk5" for argv in detaches)
    assert len(infos) == len(detaches)
    # A pause follows every failed try but the last.
    assert slept == [DETACH_PAUSE_S] * (len(detaches) - 1)
