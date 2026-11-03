"""Real Git owns every packed-ref spelling outside the narrow absence proof."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import retention_git as rgit
from tests.unit.test_retention_git_common_dir import git, repository


PREFIX = "refs/subfleet-salvage/"
REF = (PREFIX + "job-a1").encode()
OTHER = b"refs/heads/main"
OID = b"1" * 40
HEADER = b"# pack-refs with: peeled fully-peeled sorted \n"


def _write_packed(common: Path, contents: bytes) -> None:
    common.mkdir(exist_ok=True)
    (common / "packed-refs").write_bytes(contents)


@pytest.mark.parametrize("contents", [
    b"", OID + b" " + OTHER,
    OID + b"\t" + OTHER + b"\n", OID + b"\r" + OTHER + b"\n",
    OID + b"\v" + OTHER + b"\n", OID + b"\f" + OTHER + b"\n",
    OID + b"  " + OTHER + b"\n", b" " + OID + b" " + OTHER + b"\n",
    OID + b" " + OTHER + b" \n", OID + b" " + OTHER + b"\r\n",
    b"# arbitrary comment\n", b"# pack-refs with: invented \n",
    OID + b" " + OTHER + b"\n" + HEADER,
    HEADER + HEADER + OID + b" " + OTHER + b"\n",
    b"^" + OID + b"\n",
    OID + b" " + OTHER + b"\n^" + OID + b"\n^" + OID + b"\n",
    OID + b" " + OTHER + b"\n^" + b"x" * 40 + b"\n",
    b"1" * 39 + b" " + OTHER + b"\n",
    b"1" * 41 + b" " + OTHER + b"\n",
    b"x" * 40 + b" " + OTHER + b"\n",
    b"A" * 40 + b" " + OTHER + b"\n",
    OID + b" " + OTHER + b" extra\n",
    OID + b" " + OTHER + b"\nnot a packed record\n",
    OID + b" refs/heads/caf\xc3\xa9\n",  # Valid Git name outside printable ASCII.
    OID + b" refs/heads/a\x7fb\n",
    OID + b" " + OTHER + b"\n" + b"1" * 64 + b" refs/heads/other\n",
], ids=[
    "empty-file", "missing-final-lf", "tab-separator", "cr-separator",
    "vt-separator", "ff-separator", "double-space", "leading-space",
    "trailing-space", "crlf", "arbitrary-comment", "unknown-header-capability",
    "misplaced-header", "repeated-header", "orphan-peel", "repeated-peel", "nonhex-peel",
    "short-oid", "long-oid", "nonhex-oid", "uppercase-oid", "extra-field",
    "arbitrary-line", "nonascii-refname", "delete-byte-refname", "mixed-hash-widths",
])
def test_no_refs_under_refuses_noncanonical_packed_format(tmp_path, contents):
    """Absence of our prefix does not make unsupported bytes a proof."""
    common = tmp_path / "common"
    _write_packed(common, contents)
    assert rgit.no_refs_under(common, PREFIX) is False


@pytest.mark.parametrize("width", [40, 64])
@pytest.mark.parametrize("header", [b"", HEADER, b"# pack-refs with: peeled \n"])
@pytest.mark.parametrize("peeled", [False, True])
def test_no_refs_under_accepts_supported_packed_format(tmp_path, width, header, peeled):
    oid = b"1" * width
    contents = header + oid + b" " + OTHER + b"\n"
    if peeled:
        contents += b"^" + oid + b"\n"
    common = tmp_path / "common"
    _write_packed(common, contents)
    assert rgit.no_refs_under(common, PREFIX) is True
    (common / "packed-refs").write_bytes(contents + oid + b" " + REF + b"\n")
    assert rgit.no_refs_under(common, PREFIX) is False


@pytest.mark.parametrize("name", [b"refs/tags/v2.1.12", b"refs/heads/a+b", b"refs/remotes/origin/x@{y}~^:?*[\\"],
                         ids=["dotted-tag", "plus", "punctuation"])
def test_no_refs_under_accepts_printable_ascii_refnames(tmp_path, name):
    """The live census's dotted tags keep the proof; the prefix test still decides."""
    contents = HEADER + OID + b" " + name + b"\n"
    common = tmp_path / "common"
    _write_packed(common, contents)
    assert rgit.no_refs_under(common, PREFIX) is True
    (common / "packed-refs").write_bytes(contents + OID + b" " + REF + b".tag\n")
    assert rgit.no_refs_under(common, PREFIX) is False


def test_git_packed_dotted_refnames_keep_the_proof_and_agree_with_git(tmp_path):
    """Git's own pack-refs output with dotted tags, before and after a salvage ref."""
    path = repository(tmp_path, commit=True)
    common = path / ".git"
    git(path, "tag", "v2.1.12")
    git(path, "branch", "fix/a+b.c")
    git(path, "pack-refs", "--all", "--prune")
    assert b"refs/tags/v2.1.12" in (common / "packed-refs").read_bytes()
    assert rgit.refs_under(common, PREFIX) == {}
    assert rgit.no_refs_under(common, PREFIX) is True
    git(path, "update-ref", PREFIX + "job-a1.v2", "HEAD")
    git(path, "pack-refs", "--all", "--prune")
    assert rgit.refs_under(common, PREFIX)
    assert rgit.no_refs_under(common, PREFIX) is False
    assert rgit.no_refs_under(common, PREFIX + "job-a1.v2") is False
    assert rgit.no_refs_under(common, PREFIX + "job-b1") is True


@pytest.mark.parametrize("prefix", ["--include-root-refs", "--ignore-case", "--count=1", "--points-at=HEAD"])
def test_no_refs_under_leaves_option_like_prefixes_to_git(tmp_path, prefix):
    """Git interprets these arguments as options and lists other packed refs."""
    path = repository(tmp_path, commit=True)
    git(path, "pack-refs", "--all", "--prune")
    common = path / ".git"
    packed = (common / "packed-refs").read_bytes()
    assert (" " + prefix).encode() not in packed
    assert rgit.refs_under(common, prefix)
    assert rgit.no_refs_under(common, prefix) is False


@pytest.mark.parametrize("tail", ["caf\u00e9", "cafe\u0301"], ids=["NFC", "NFD"])
def test_no_refs_under_leaves_unicode_normalization_with_ascii_store_to_git(tmp_path, tail):
    """Unsupported prefixes need Git even when stored names are ordinary ASCII."""
    common = tmp_path / "common"
    _write_packed(common, HEADER + OID + b" " + OTHER + b"\n")
    assert rgit.no_refs_under(common, PREFIX + tail) is False


def _listed_ref(common: Path, contents: bytes) -> bool:
    (common / "packed-refs").write_bytes(contents)
    result = subprocess.run(
        ["git", *rgit.GIT_CONFIG, f"--git-dir={common}", "for-each-ref",
         "--format=%(refname)", PREFIX],
        env=rgit.environment(), capture_output=True, timeout=10,
    )
    return REF in result.stdout.split(b"\n")


def test_no_refs_under_exhausts_all_256_packed_record_bytes(tmp_path):
    """Every separator, leading byte and trailing byte is checked against Git."""
    path = repository(tmp_path, commit=True)
    common = path / ".git"
    oid = git(path, "rev-parse", "HEAD").stdout.strip()
    accepted = {position: [] for position in ("separator", "before-oid", "after-refname")}
    for position in accepted:
        for value in range(256):
            byte = bytes([value])
            contents = {
                "separator": oid + byte + REF + b"\n",
                "before-oid": byte + oid + b" " + REF + b"\n",
                "after-refname": oid + b" " + REF + byte + b"\n",
            }[position]
            if _listed_ref(common, contents):
                accepted[position].append(value)
                assert rgit.no_refs_under(common, PREFIX) is False, (position, value)
    # Keep a useful, reproducible byte census in pytest -s output and require
    # this Git to exercise the ordinary packed record rather than a vacuous run.
    print("256-byte packed-ref oracle:", accepted)
    assert 32 in accepted["separator"]


@st.composite
def packed_files(draw):
    header = draw(st.sampled_from([b"", HEADER, b"# unknown header\n"]))
    records = []
    for _ in range(draw(st.integers(min_value=0, max_value=7))):
        ref = draw(st.sampled_from([REF, OTHER, b"refs/subfleet-salvage-other/job-a1"]))
        record = OID + b" " + ref
        position = draw(st.sampled_from(["plain", "separator", "before-oid", "after-refname", "peel", "junk"]))
        byte = bytes([draw(st.integers(min_value=0, max_value=255))])
        if position == "separator":
            record = OID + byte + ref
        elif position == "before-oid":
            record = byte + record
        elif position == "after-refname":
            record += byte
        elif position == "peel":
            record += b"\n^" + OID
        elif position == "junk":
            record = draw(st.binary(max_size=80))
        records.append(record + b"\n")
    contents = header + b"".join(records)
    return contents if draw(st.booleans()) else contents.rstrip(b"\n")


@settings(max_examples=60, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(contents=packed_files())
def test_no_refs_under_generated_packed_files_never_contradict_git(tmp_path, contents):
    # Reuse the same repository deliberately: each example replaces its only
    # packed-refs file completely, and no example writes a loose salvage ref.
    path = tmp_path / "repo"
    if not path.exists():
        repository(tmp_path, commit=True)
    common = path / ".git"
    (common / "packed-refs").unlink(missing_ok=True)
    oid = git(path, "rev-parse", "HEAD").stdout.strip()
    contents = contents.replace(OID, oid)
    if _listed_ref(common, contents):
        assert rgit.no_refs_under(common, PREFIX) is False
