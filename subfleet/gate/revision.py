"""Caller-attested artifact identities; fresh reads never grant approval (C-23.8)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..sessions.transcripts import read_regular
from .errors import GateError

GIT_OID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def plan(path: Path | str) -> tuple[dict[str, Any], bytes]:
    """Capture a plan once, retaining exactly the bytes that were hashed."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        body = read_regular(resolved)               # the file checked is the file read
    except OSError as exc:
        raise GateError(f"cannot read plan {path}: {exc}") from exc
    return {
        "kind": "plan", "path": str(resolved),
        "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
    }, body


def revision(subject: dict[str, Any]) -> dict[str, Any]:
    """The v1 revision shape excludes moving metadata and a plan's local path."""
    if subject.get("kind") == "pr":
        return {key: subject[key] for key in (
            "kind", "repository", "number", "base_sha", "head_sha",
        )}
    if subject.get("kind") == "plan":
        return {key: subject[key] for key in ("kind", "sha256", "bytes")}
    raise GateError("artifact kind must be pr or plan")


def expected_revision(args: Any, subject: dict[str, Any]) -> dict[str, Any]:
    """Build only the fingerprint supplied by the main, never one just observed."""
    if subject["kind"] == "pr":
        head = str(getattr(args, "expect_head", "") or "").lower()
        base = str(getattr(args, "expect_base", "") or "").lower()
        if not GIT_OID_RE.fullmatch(head) or not GIT_OID_RE.fullmatch(base):
            raise GateError(
                "PR approval requires --expect-head and --expect-base with full commit OIDs"
            )
        return {
            "kind": "pr", "repository": subject["repository"],
            "number": subject["number"], "base_sha": base, "head_sha": head,
        }
    if subject["kind"] != "plan":
        raise GateError("artifact kind must be pr or plan")
    digest = str(getattr(args, "expect_sha256", "") or "").lower()
    if not SHA256_RE.fullmatch(digest):
        raise GateError("plan approval requires --expect-sha256 with the full digest")
    return {"kind": "plan", "sha256": digest, "bytes": subject["bytes"]}


def assert_expected(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if actual != expected:
        raise GateError(
            "captured artifact does not match the main agent's expected revision: "
            f"expected {json.dumps(expected, sort_keys=True)}, "
            f"got {json.dumps(actual, sort_keys=True)}", 4,
        )


def assert_optional_expected(
    args: Any, subject: dict[str, Any], approved: dict[str, Any],
) -> None:
    supplied = (
        getattr(args, "expect_sha256", None) if subject["kind"] == "plan"
        else getattr(args, "expect_head", None) or getattr(args, "expect_base", None)
    )
    if supplied:
        assert_expected(approved, expected_revision(args, subject))


def fingerprint(subject: dict[str, Any]) -> str:
    """A printable digest of the complete revision; OIDs remain visible beside it."""
    canonical = json.dumps(revision(subject), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
