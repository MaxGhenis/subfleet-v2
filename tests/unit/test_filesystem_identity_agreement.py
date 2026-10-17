"""One-way agreement with actual files, including initially absent names.

Disk images are local 20 MB fixtures, automatically detached. Run this file on
the hub when a sandbox denies hdiutil. No download or live state is involved.
"""
from contextlib import contextmanager
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
    with _attached(request.param, image, mount):
        yield request.param, mount


#: Spotlight or fseventsd can hold a volume for a moment after its files are
#: used; CI saw `hdiutil: couldn't eject "disk5" - Resource busy` on 2026-10-12.
DETACH_TRIES, DETACH_PAUSE_S = 5, 2.0


@contextmanager
def _attached(label, image, mount):
    """Attach `image` at `mount` for the block; detach it afterwards, however the
    block or the reading of attach's output ends."""
    attached = subprocess.run(["hdiutil", "attach", "-plist", "-nobrowse", "-mountpoint", str(mount), str(image)],
                              capture_output=True, text=True)
    if attached.returncode:
        pytest.skip(f"hdiutil attach {label}: {attached.stderr.strip()}")
    device = None
    try:
        device = _whole_disk(attached.stdout)
        yield
    finally:
        _detach(device or mount, device, image)


def _whole_disk(attach_plist: str) -> str | None:
    """The image's whole-disk device (`/dev/diskN`) from `hdiutil attach -plist`;
    None when the output names none or cannot be read."""
    try:
        for entity in plistlib.loads(attach_plist.encode())["system-entities"]:
            device = entity.get("dev-entry")
            if isinstance(device, str) and re.fullmatch(r"/dev/disk[0-9]+", device):
                return device
    except Exception:                       # noqa: BLE001 - no device: detach by mount point
        pass
    return None


def _still_attached(device: str, image=None) -> bool | None:
    """Whether `hdiutil info` still lists `device`, a slice of it (`/dev/diskNs1`)
    or `image`. False only when info's output has the expected shape (each
    image record naming its file and its devices) and lists none of them; None
    when it cannot say (a failed, unreadable or incomplete info)."""
    info = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, text=True)
    if info.returncode:
        return None
    family = re.compile(re.escape(device) + r"(?:s[0-9]+)*")
    want = os.path.realpath(image) if image else None
    try:
        images = plistlib.loads(info.stdout.encode())["images"]
        if not isinstance(images, list):
            return None
        for entry in images:
            # Every record hdiutil writes names its image file and its devices; one
            # that names neither is incomplete output, not evidence of absence.
            path, entities = entry.get("image-path"), entry.get("system-entities")
            if not isinstance(path, str) or not isinstance(entities, list):
                return None
            if want and os.path.realpath(path) == want:
                return True
            for entity in entities:
                listed = entity.get("dev-entry")
                if listed is not None and not isinstance(listed, str):
                    return None
                if listed is not None and family.fullmatch(listed):
                    return True
    except Exception:                       # noqa: BLE001 - unreadable: keep trying
        return None
    return False


def _detach(target, device=None, image=None):
    """Detach a test image, by its whole-disk device when known, waiting out a
    brief hold before forcing it; fail only when even `-force` cannot detach it.

    A busy eject can still unmount the volume, and then its mount point names
    nothing: on 2026-10-17 every try after the first busy one, `-force`
    included, said "No such file or directory". So the image is detached by its
    device, and after a failed try it counts as detached once `hdiutil info`,
    read in its expected shape, lists neither that device, any slice of it, nor
    the image."""
    errors = []
    for attempt in range(DETACH_TRIES + 1):
        force = ["-force"] if attempt == DETACH_TRIES else []
        detached = subprocess.run(["hdiutil", "detach", *force, str(target)], capture_output=True, text=True)
        if detached.returncode == 0:
            return
        errors.append(detached.stderr.strip())
        if device and _still_attached(device, image) is False:
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


@pytest.mark.parametrize("rc, stdout, expect", [
    (0, _info_plist("/dev/disk5", "/dev/disk5s1"), True),
    (0, _info_plist("/dev/disk5s1"), True),                  # review r1: a slice alone is still attached
    (0, _info_plist("/dev/disk50", "/dev/disk50s1"), False),  # another disk, not a slice of this one
    (0, _info_plist(), False),
    (0, plistlib.dumps({"images": []}).decode(), False),
    (0, plistlib.dumps({"images": [{"image-path": "/tmp/mine.dmg", "system-entities": []}]}).decode(), True),  # the image, by path
    (0, plistlib.dumps({"images": [{}]}).decode(), None),                      # review r2: an empty record
    (0, plistlib.dumps({"images": [{"image-path": "/tmp/v.dmg"}]}).decode(), None),  # no devices named
    (0, plistlib.dumps({"images": [{"system-entities": []}]}).decode(), None),  # no file named
    (0, plistlib.dumps({}).decode(), None),                  # review r1: no images key is no answer
    (0, "not a plist", None),                                 # review r1 P3: unreadable, rc 0
    (0, plistlib.dumps({"images": {"x": 1}}).decode(), None),
    (0, plistlib.dumps({"images": [{"image-path": "/tmp/v.dmg", "system-entities": [{"dev-entry": 5}]}]}).decode(), None),
    (0, plistlib.dumps({"images": ["x"]}).decode(), None),
    (1, "", None),
], ids=["whole-and-slice", "slice-only", "other-disk", "no-devices", "no-images", "image-path",
      "empty-record", "record-without-devices", "record-without-file", "no-images-key", "not-a-plist", "images-not-a-list", "device-not-a-string", "image-not-a-dict", "info-failed"])
def test_info_says_detached_only_when_it_lists_nothing_of_the_image(monkeypatch, rc, stdout, expect):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, rc, stdout, ""))
    # `_info_plist` lists another image file (/tmp/v.dmg), so only the devices decide there.
    assert _still_attached("/dev/disk5", "/tmp/mine.dmg") is expect


def test_an_unreadable_info_never_ends_the_retries(monkeypatch):
    """Review r1 P3: info exits 0 with output that is not a plist; every try is
    made, `-force` last, and the teardown fails."""
    detaches = []

    def run(argv, **kwargs):
        if argv[:2] == ["hdiutil", "info"]:
            return subprocess.CompletedProcess(argv, 0, "not a plist", "")
        detaches.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", 'hdiutil: couldn\'t eject "disk5" - Resource busy')

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(AssertionError, match="Resource busy"):
        _detach("/dev/disk5", "/dev/disk5", "/tmp/v.dmg")
    assert len(detaches) == DETACH_TRIES + 1 and "-force" in detaches[-1]


@pytest.mark.parametrize("attach_stdout", [
    plistlib.dumps({"system-entities": [{"dev-entry": 5}]}).decode(),   # review r1 P2: a non-string device
    "not a plist",
    ATTACH_PLIST,
], ids=["device-not-a-string", "not-a-plist", "whole-disk"])
def test_the_image_is_detached_however_its_attach_output_reads(monkeypatch, tmp_path, attach_stdout):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, attach_stdout if argv[1] == "attach" else "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with _attached("HFS+", tmp_path / "v.dmg", tmp_path / "mount"):
        pass
    detaches = [argv for argv in calls if argv[1] == "detach"]
    assert detaches == [["hdiutil", "detach", "/dev/disk5" if attach_stdout == ATTACH_PLIST else str(tmp_path / "mount")]]


def test_the_image_is_detached_when_reading_its_device_raises(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(sys.modules[__name__], "_whole_disk", lambda out: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        with _attached("HFS+", tmp_path / "v.dmg", tmp_path / "mount"):
            pass
    assert [argv[1] for argv in calls] == ["attach", "detach"]


def test_the_image_is_detached_when_the_block_fails(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, ATTACH_PLIST if argv[1] == "attach" else "", ""))
    with pytest.raises(RuntimeError):
        with _attached("HFS+", tmp_path / "v.dmg", tmp_path / "mount"):
            raise RuntimeError("test body failed")
    assert calls[-1] == ["hdiutil", "detach", "/dev/disk5"]


def test_an_incomplete_info_record_never_ends_the_retries(monkeypatch):
    """Review r2: every detach busy, `info` exits 0 with `{"images": [{}]}`; the
    retries go on to `-force` and the teardown fails."""
    detaches = []

    def run(argv, **kwargs):
        if argv[:2] == ["hdiutil", "info"]:
            return subprocess.CompletedProcess(argv, 0, plistlib.dumps({"images": [{}]}).decode(), "")
        detaches.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", 'hdiutil: couldn\'t eject "disk5" - Resource busy')

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(AssertionError, match="Resource busy"):
        _detach("/dev/disk5", "/dev/disk5", "/tmp/mine.dmg")
    assert len(detaches) == DETACH_TRIES + 1 and "-force" in detaches[-1]
