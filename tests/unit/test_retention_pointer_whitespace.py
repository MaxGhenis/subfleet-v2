"""C-8.4: pointer pathname whitespace must agree with the installed Git.

For every pointer Git follows, discovery records the target Git reads, or
reports incomplete. Git is the oracle, including for rejected pointer syntax.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import dependencies, folders, retention_git


def git(*args, data=None):
    return subprocess.run(["git", *map(str, args)], input=data, capture_output=True,
                          env=retention_git.environment(), timeout=30)


def pointer_repository(base, layout, name, *, absolute=False, ending=b"\n", prefix=b"gitdir: "):
    owner = base / "owner.git"
    assert git("init", "-q", "--bare", owner).returncode == 0
    blob = git("--git-dir", owner, "hash-object", "-w", "--stdin", data=b"pointer-only blob\n")
    assert blob.returncode == 0, blob.stderr
    checkout = base / "checkout"
    checkout.mkdir()
    admin = checkout / ".git"
    if layout == "gitfile":
        parent = checkout
    else:
        admin.mkdir()
        (admin / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        parent = admin
    if name:
        alias = parent / name
        alias.symlink_to(owner, target_is_directory=True)
        target = os.fsencode(alias if absolute else name)
    else:
        target = b""
    pointer = admin if layout == "gitfile" else admin / "commondir"
    pointer.write_bytes((prefix if layout == "gitfile" else b"") + target + ending)
    resolved = git("-C", checkout, "cat-file", "-p", os.fsdecode(blob.stdout.strip()))
    return owner, checkout, resolved


@pytest.mark.parametrize("layout,name,ending,prefix,accepted", [
    ("gitfile", " metadata ", b"\n", b"gitdir: ", True),
    ("gitfile", "metadata", b"\n", b"gitdir:", False),
    ("gitfile", "", b"\n", b"gitdir: ", False),
    ("gitfile", " metadata ", b"\r\n", b"gitdir: ", True),
    ("commondir", " metadata ", b"\n", b"", True),
    ("commondir", "   ", b"\n", b"", True),
    ("commondir", "", b"\n", b"", False),
    ("commondir", " metadata ", b"\r\n", b"", True),
], ids=["gitfile-spaces", "gitfile-no-space", "gitfile-empty", "gitfile-crlf",
        "commondir-spaces", "commondir-only-spaces", "commondir-empty", "commondir-crlf"])
def test_pointer_syntax_and_whitespace_match_real_git(tmp_path, layout, name, ending, prefix, accepted):
    owner, checkout, resolved = pointer_repository(tmp_path, layout, name, ending=ending, prefix=prefix)
    assert (resolved.returncode == 0) == accepted, resolved.stderr
    recorded = dependencies.record(str(checkout))
    if accepted:
        assert resolved.stdout == b"pointer-only blob\n"
        assert folders.canonical(owner / "objects") in recorded["git"]
        assert not recorded.get("incomplete"), recorded
    elif layout == "gitfile":
        # Rejected gitfiles name no usable Git storage, including no syntactic
        # target from the colon-only spelling. This also kills a lax prefix.
        assert not recorded.get("git"), recorded
    else:
        # Git rejects an empty commondir. Keeping its admin and objects paths
        # is conservative; no readable repository storage is omitted.
        assert folders.canonical(checkout / ".git") in recorded["git"]
        assert folders.canonical(owner / "objects") not in recorded["git"]


WHITESPACE = st.text(alphabet=" \t\v\f\r\n\u0085\u00a0\u2003", max_size=5)


@settings(max_examples=100, deadline=None, derandomize=True)
@example("gitfile", " ", " ", False, b"\n")
@example("commondir", "\t", "\r\n", True, b"\r\n")
@given(st.sampled_from(["gitfile", "commondir"]), WHITESPACE, WHITESPACE,
       st.booleans(), st.sampled_from([b"\n", b"\r\n"]))
def test_pointer_pathname_whitespace_preserves_the_storage_real_git_reads(layout, leading, trailing,
                                                                         absolute, ending):
    # The final space keeps CR/LF in the pathname interior, where Git preserves
    # them; only the separately written record ending may be removed.
    with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as directory:
        owner, checkout, resolved = pointer_repository(
            Path(directory), layout, leading + "metadata" + trailing + " ",
            absolute=absolute, ending=ending)
        assert resolved.returncode == 0 and resolved.stdout == b"pointer-only blob\n", resolved.stderr
        recorded = dependencies.record(str(checkout))
        assert (folders.canonical(owner / "objects") in recorded.get("git", [])
                or recorded.get("incomplete")), recorded
