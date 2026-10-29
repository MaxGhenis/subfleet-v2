"""C-8.4: compare alternates byte decoding with the installed Git."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from subfleet import dependencies, folders
from tests.unit.test_retention_dependency_units import repo


@pytest.mark.parametrize("case", ["absolute", "relative", "octal-ascii", "octal-utf8",
                                  "quote", "backslash", "tab", "newline", "comment", "blank",
                                  "leading-outside", "trailing-outside", "leading-inside", "trailing-inside",
                                  "after-quote"])
def test_c8_4_alternates_match_git_bytes(tmp_path, case):
    source = repo(tmp_path / "source")
    borrower = repo(tmp_path / "borrower")
    # This blob is absent from the borrower, so cat-file proves Git resolved the alternate.
    oid = subprocess.check_output(["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                                  input=b"only in alternate\n").strip()
    names = {"quote": 'with"quote', "backslash": 'with\\slash', "tab": 'with\ttab',
             "newline": 'with\nnewline', "octal-utf8": "non-é", "octal-byte": os.fsdecode(b"non-\xff"),
             "leading-inside": " leading", "trailing-inside": "trailing "}
    target = tmp_path / names.get(case, "ordinary")
    shutil.copytree(source / ".git" / "objects", target)
    base = borrower / ".git" / "objects"
    path = os.fsencode(target)
    encoded = path
    if case == "relative": encoded = os.fsencode(os.path.relpath(target, base))
    if case == "octal-ascii": encoded = path.replace(b"ordinary", b"\\157rdinary")
    if case == "octal-utf8": encoded = path.replace(b"\xc3\xa9", b"\\303\\251")
    if case == "octal-byte": encoded = path.replace(b"\xff", b"\\377")
    for raw, escape in ((b'"', b'\\"'), (b"\\", b"\\\\"), (b"\t", b"\\t"), (b"\n", b"\\n")):
        if case == {b'"': "quote", b"\\": "backslash", b"\t": "tab", b"\n": "newline"}[raw]:
            encoded = path.replace(raw, escape)
    line = b'"' + encoded + b'"'
    expected = [folders.canonical(target)]
    reads = True
    if case == "comment": line, expected, reads = b"#" + path, [], False
    if case == "blank": line, expected, reads = b"", [], False
    if case == "leading-outside":
        line, reads = b" " + path, False
        expected = [os.path.realpath(os.path.join(base, os.fsdecode(line)))]
    if case == "trailing-outside":
        line, reads = path + b" ", False
        expected = [os.path.realpath(os.fsdecode(line))]
    if case == "after-quote":
        line += b" ignored-missing"
        expected.append(os.path.realpath(base / "ignored-missing"))
    (base / "info" / "alternates").write_bytes(line + b"\n")
    result = subprocess.run(["git", "-C", str(borrower), "cat-file", "-e", os.fsdecode(oid)],
                            capture_output=True)
    assert (result.returncode == 0) == reads, result.stderr
    assert dependencies._alternates(str(base)) == expected


def test_c8_4_c_quotes_decode_raw_bytes_before_fsdecode():
    assert dependencies._unquote(b'"non-\\377"') == (b"non-\xff", 10)
