"""Caller-attested PR pairs and plan snapshots (C-23.8)."""

import hashlib
from argparse import Namespace

import pytest

from subfleet.gate.errors import GateError
from subfleet.gate.revision import (
    assert_expected, assert_optional_expected, expected_revision, fingerprint, plan, revision,
)


@pytest.mark.parametrize("length", [40, 64])
def test_full_oid_pair_is_supplied_by_caller(length):
    """C-23.8: fresh remote heads never replace caller-attested full OIDs."""
    subject = {"kind": "pr", "repository": "example/repo", "number": 42,
               "head_sha": "c" * length, "base_sha": "d" * length}
    expected = expected_revision(Namespace(expect_head="A" * length, expect_base="B" * length), subject)
    assert expected["head_sha"] == "a" * length
    assert expected["base_sha"] == "b" * length
    with pytest.raises(GateError) as error:
        assert_expected(revision(subject), expected)
    assert error.value.code == 4


@pytest.mark.parametrize("head,base", [(None, "b" * 40), ("a" * 40, None),
                                      ("a" * 7, "b" * 40), ("a" * 40, "b" * 41),
                                      ("z" * 40, "b" * 40), ("a" * 40, " b" * 20)])
def test_missing_or_partial_oids_are_usage_errors(head, base):
    """C-23.8, C-17.1: both full commit OIDs are mandatory, with exit 2."""
    subject = {"kind": "pr", "repository": "example/repo", "number": 42}
    with pytest.raises(GateError, match="full commit OIDs") as error:
        expected_revision(Namespace(expect_head=head, expect_base=base), subject)
    assert error.value.code == 2


def test_plan_hash_uses_exact_snapshot_bytes_and_excludes_path(tmp_path):
    """C-23.8, C-23.1: a plan fingerprint binds bytes, including line endings."""
    path = tmp_path / "plan.md"
    body = b"# Plan\r\n\xc3\xa9\n"
    path.write_bytes(body)
    subject, snapshot = plan(path)
    assert snapshot == body == path.read_bytes()
    expected = expected_revision(Namespace(expect_sha256=hashlib.sha256(body).hexdigest().upper()), subject)
    assert revision(subject) == expected
    assert "path" not in expected and expected["bytes"] == len(body)


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "z" * 64])
def test_plan_approval_is_not_inferred_from_snapshot(digest):
    """C-23.8: reading the current hash does not attest main approval."""
    with pytest.raises(GateError, match="--expect-sha256") as error:
        expected_revision(Namespace(expect_sha256=digest), {"kind": "plan", "sha256": "a" * 64, "bytes": 2})
    assert error.value.code == 2


def test_changed_plan_blocks_prior_approval(tmp_path):
    """C-23.8, C-17.1: a mutation between approval and round completion exits 4."""
    path = tmp_path / "plan.md"
    path.write_text("first\n")
    subject, _ = plan(path)
    approved = expected_revision(Namespace(expect_sha256=subject["sha256"]), subject)
    path.write_text("later\n")
    changed, _ = plan(path)
    with pytest.raises(GateError, match="expected revision") as error:
        assert_expected(revision(changed), approved)
    assert error.value.code == 4


def test_fingerprint_covers_identity_base_head_and_length():
    """C-23.8: fingerprints ignore moving metadata but include the complete revision."""
    subject = {"kind": "pr", "repository": "example/repo", "number": 42,
               "base_sha": "a" * 40, "head_sha": "b" * 40}
    assert fingerprint(subject) == fingerprint({**subject, "state": "MERGED", "checks": []})
    for field, value in [("base_sha", "c" * 40), ("head_sha", "c" * 40),
                         ("repository", "example/other"), ("number", 43)]:
        assert fingerprint(subject) != fingerprint({**subject, field: value})
    assert fingerprint({"kind": "plan", "sha256": "a" * 64, "bytes": 1}) != fingerprint(
        {"kind": "plan", "sha256": "a" * 64, "bytes": 2})


def test_completed_gate_optional_expectation_cannot_replace_approval():
    """C-23.8: an explicit wrong fingerprint is rejected even on completed gates."""
    subject = {"kind": "plan", "sha256": "a" * 64, "bytes": 2}
    assert_optional_expected(Namespace(), subject, subject)
    with pytest.raises(GateError) as error:
        assert_optional_expected(Namespace(expect_sha256="b" * 64), subject, subject)
    assert error.value.code == 4
