"""One-way agreement with actual files, including initially absent names.

Disk images are local 20 MB fixtures, automatically detached. Run this file on
the hub when a sandbox denies hdiutil. No download or live state is involved.
"""
from pathlib import Path
import ctypes
import os
import subprocess
import sys

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
    attached = subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)],
                              capture_output=True, text=True)
    if attached.returncode:
        pytest.skip(f"hdiutil attach {request.param}: {attached.stderr.strip()}")
    try:
        yield request.param, mount
    finally:
        detached = subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True, text=True)
        assert detached.returncode == 0, detached.stderr


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
