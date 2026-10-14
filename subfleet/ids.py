"""Stable identifiers and submission fingerprints (C-1, C-6.2)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Collection, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import JOB_ID_SLUG_MAX, REQUEST_ID_MAX


def canonical_json(value: Any) -> bytes:
    """Return unambiguous UTF-8 JSON with sorted keys and no insignificant space."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def job_id(name: str | None = None, *, now: datetime | None = None,
           existing: Collection[str] = ()) -> str:
    """Make a C-1.1 local-time id; allocate under the store transaction."""
    stamp = (now or datetime.now().astimezone()).astimezone().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "job").lower()).strip("-")[:JOB_ID_SLUG_MAX].rstrip("-") or "job"
    candidate = f"{stamp}-{slug}"
    seq = 1
    while candidate in existing:
        suffix = f"-{seq}"
        candidate = f"{stamp}-{slug[:JOB_ID_SLUG_MAX - len(suffix)].rstrip('-')}{suffix}"
        seq += 1
    return candidate


#: A job id's part before its slug (C-1.1): `YYYYMMDD-HHMMSS-`.
_JOB_ID_STAMP = re.compile(r"[0-9]{8}-[0-9]{6}-")


def holds_credential(value: str) -> bool:
    """Whether the C-23.14 scrubber, reading `value` whole, would change it: what
    C-1.5 refuses as a request id and C-25.7 as a correlation id."""
    from .sessions.handoff import scrub_secrets
    clean, count = scrub_secrets(value, strip_reminders=True, whole=True)
    return bool(count) or clean != value


def job_id_holds_credential(value: str) -> bool:
    """`holds_credential` for a job id, whose slug (C-1.1: the caller's `-n` name) is
    also read alone. Read whole, the hyphen the timestamp ends with is a token boundary
    the scrubber does not cross, so `20261013-000000-sk-…`, `…-glpat-…` and `…-xoxb-…`
    passed as ids a refusal could name (review r6 of #181, P2)."""
    if holds_credential(value):
        return True
    stamp = _JOB_ID_STAMP.match(value)
    return stamp is not None and holds_credential(value[stamp.end():])


def attempt_id(job: str, seq: int) -> str:
    """Return the C-1.2 sequence id."""
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ValueError("attempt sequence must be an integer starting at 1")
    return f"{job}/a{seq}"


def request_id(value: str | None = None) -> str:
    """Validate a caller request id or generate the C-1.5 UUID4 default."""
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not value or len(value) > REQUEST_ID_MAX:
        raise ValueError(f"request id must contain 1 to {REQUEST_ID_MAX} characters")
    # Submission keys are opaque caller strings, unlike the reflected wire id.
    # Keep credential refusal without imposing the correlation token alphabet.
    if holds_credential(value):
        raise ValueError("request id cannot be a credential")
    return value


def request_id_arg(value: str) -> str:
    """Validate a CLI id without argparse quoting a rejected caller value."""
    import argparse
    try:
        return request_id(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"request id must contain 1 to {REQUEST_ID_MAX} characters "
            "and cannot be a credential") from None


def payload_digest(prompt: bytes | Mapping[str, Any], *, workdir: str | Path | None = None,
                   workdir_head: str | None = None, task: str | None = None,
                   tier: str | None = None, pinned_model: str | None = None,
                   pinned_lane: str | None = None, sandbox: str = "read-only",
                   exclusions: Collection[str] = (), out_path: str | Path | None = None,
                   allow_desktop: bool = False, policy_hash: str = "",
                   isolated_review: bool = False, review_root: str | None = None,
                   round_lease: str | None = None, resume: Mapping[str, Any] | None = None,
                   unmeasured_reserve_reason: str | None = None,
                   mcp_servers: Collection[str] = (), push_branch: str | None = None,
                   push_remote: str | None = None) -> str:
    """Hash the exact C-6.2 payload, excluding caller identity and display name.

    A mapping is accepted for callers that have already assembled these canonical
    fields. Git inspection belongs to validation, outside the store transaction.
    """
    if isinstance(prompt, Mapping):
        payload = dict(prompt)
    else:
        if workdir is None:
            raise ValueError("workdir is required")
        payload = {
            "prompt": base64.b64encode(prompt).decode("ascii"),
            "workdir": str(Path(workdir).expanduser().resolve()),
            "workdir_head": workdir_head,
            "task": task, "tier": tier, "pinned_model": pinned_model,
            "pinned_lane": pinned_lane, "sandbox": sandbox,
            "exclusions": sorted(exclusions),
            "out_path": str(Path(out_path).expanduser().resolve()) if out_path is not None else None,
            "allow_desktop": allow_desktop, "policy_hash": policy_hash,
        }
        if isolated_review or review_root or round_lease:
            payload.update(isolated_review=isolated_review, review_root=review_root,
                           round_lease=round_lease)
        if resume is not None:
            payload["resume"] = dict(resume)
        if unmeasured_reserve_reason is not None:
            # Default submissions retain their old digest across this additive
            # upgrade; explicit authorization is part of the exact request.
            payload["unmeasured_reserve_reason"] = unmeasured_reserve_reason
        if mcp_servers:
            # C-12.9: likewise, a job that names no MCP server keeps its old digest.
            payload["mcp_servers"] = sorted(mcp_servers)
        if push_branch is not None:
            payload.update(push_branch=push_branch, push_remote=push_remote)
    return hashlib.sha256(canonical_json(payload)).hexdigest()


new_job_id = job_id
new_attempt_id = attempt_id
new_request_id = request_id
