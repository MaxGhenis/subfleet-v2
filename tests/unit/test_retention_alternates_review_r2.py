"""C-8.4: execute Git's remaining byte escapes and line-ending rules."""
import os
import subprocess

import pytest

from subfleet import dependencies, folders
from tests.unit.test_retention_dependency_units import repo


@pytest.mark.parametrize(("quoted", "raw"), [
    (br"\a", b"\a"), (br"\b", b"\b"), (br"\f", b"\f"), (br"\n", b"\n"),
    (br"\r", b"\r"), (br"\t", b"\t"), (br"\v", b"\v"), (br"\\", b"\\"),
    (br'\"', b'"'), (br"\157", b"o"), (br"\303\251", "é".encode()),
    (b"#inside", b"#inside"),
])
def test_c8_4_extended_git_c_escapes_resolve_the_same_objects(tmp_path, quoted, raw):
    source, borrower = repo(tmp_path / "source"), repo(tmp_path / "borrower")
    oid = subprocess.check_output(["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                                  input=b"r2 borrowed blob").strip()
    target = os.fsencode(tmp_path) + b"/alt-" + raw
    os.symlink(os.fsencode(source / ".git" / "objects"), target)
    base = borrower / ".git" / "objects"
    line = b'"' + os.fsencode(tmp_path) + b"/alt-" + quoted + b'"\n'
    (base / "info" / "alternates").write_bytes(line)
    result = subprocess.run(["git", "-C", str(borrower), "cat-file", "-p", os.fsdecode(oid)],
                            capture_output=True)
    assert result.returncode == 0 and result.stdout == b"r2 borrowed blob", result.stderr
    errors = []
    assert dependencies._alternates(str(base), incomplete=errors) == [folders.canonical(source / ".git" / "objects")]
    assert not errors


@pytest.mark.parametrize("tail", [b"\r\n", b" # comment\n", b"xignored\n"])
def test_c8_4_git_quoted_entry_trailing_text_and_crlf(tmp_path, tail):
    source, borrower = repo(tmp_path / "source"), repo(tmp_path / "borrower")
    oid = subprocess.check_output(["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                                  input=b"r2 trailing text").strip()
    base, target = borrower / ".git" / "objects", source / ".git" / "objects"
    (base / "info" / "alternates").write_bytes(b'"' + os.fsencode(target) + b'"' + tail)
    result = subprocess.run(["git", "-C", str(borrower), "cat-file", "-p", os.fsdecode(oid)],
                            capture_output=True)
    assert result.returncode == 0 and result.stdout == b"r2 trailing text"
    errors = []
    found = dependencies._alternates(str(base), incomplete=errors)
    assert str(target) in found and not errors
    if tail == b"xignored\n":
        assert str(base / "ignored") in found
