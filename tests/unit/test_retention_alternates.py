"""C-8.4: compare alternates byte decoding with the installed Git."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

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


# Git itself supplies the spelling written into alternates; the reader is not its own oracle.
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@example('éΐ')
@example('"\t\n\\')
@example(' tail ')
@given(st.text(alphabet='abcXYZ 0123"\\\t\n\a\b\v\f\réΐßﬁK', min_size=1, max_size=15))
def test_c8_4_git_quoted_generated_paths_resolve_to_the_same_canonical_objects(component):
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        root = Path(directory)
        source = repo(root / "source")
        borrower = root / "borrower"
        subprocess.run(["git", "init", "-q", str(borrower)], check=True, capture_output=True)
        base = borrower / ".git" / "objects"
        oid = subprocess.check_output(["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                                      input=b"generated borrowed blob\n").strip()
        name = "alt-" + component
        target = base / name
        shutil.copytree(source / ".git" / "objects", target)
        # A tiny nested repository lets ls-files quote a path relative to this objects directory.
        subprocess.run(["git", "init", "-q", str(base)], check=True, capture_output=True)
        quoted = subprocess.check_output(["git", "-C", str(base), "-c", "core.quotePath=true", "ls-files",
                                          "--others", "--directory", "--", name]).removesuffix(b"\n")
        assert quoted
        (base / "info" / "alternates").write_bytes(quoted + b"\n")
        resolved = subprocess.run(["git", "-C", str(borrower), "cat-file", "-p", os.fsdecode(oid)],
                                  capture_output=True)
        assert resolved.returncode == 0 and resolved.stdout == b"generated borrowed blob\n", resolved.stderr
        assert [folders.canonical(path) for path in dependencies._alternates(str(base))] == [folders.canonical(target)]


@pytest.mark.parametrize("line", [b'"bad\\z"', b'"short\\1"', b'"large\\400"', b'"unfinished', b'"unfinished\\'])
def test_c8_4_invalid_c_quotes_are_incomplete(line):
    with pytest.raises(ValueError):
        list(dependencies._alternate_paths(line))


def test_c8_4_alternates_after_many_comment_lines_are_read_to_eof(tmp_path):
    base = tmp_path / "objects"
    (base / "info").mkdir(parents=True)
    target = tmp_path / "owner-objects"
    target.mkdir()
    (base / "info" / "alternates").write_bytes(b"# ignored\n" * 10000 + os.fsencode(target) + b"\n")
    errors = []
    assert dependencies._alternates(str(base), incomplete=errors) == [str(target)]
    assert not errors
