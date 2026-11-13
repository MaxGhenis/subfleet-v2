"""Review identity and transactional round reservations (C-6.21–22)."""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlsplit

from . import protocol, refusals
from .contracts import Exit


def normalize_subject(value: str) -> str:
    """Canonicalize GitHub PR links; keep other caller-chosen keys as text."""
    if type(value) is not str or not value.strip() or len(value) > 2000 or not value.isprintable():
        raise protocol.ProtocolError(refusals.HANDLER_MESSAGES["review-subject"][0], reason="review-subject")
    value = value.strip()
    try:
        url = urlsplit(value)
        match = re.fullmatch(r"/([^/]+)/([^/]+)/pull/([0-9]+)(?:/files)?/?", url.path, re.IGNORECASE)
        if url.scheme.lower() in ("http", "https") and url.netloc.lower() == "github.com" and match:
            owner, repo, number = match.groups()
            return f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{number}"
    except ValueError:
        pass                            # A caller's key need not be a URL.
    return value


def refuse_limit(subject: str) -> None:
    message, fix = refusals.render("review-round-limit", {"review_subject": subject})
    raise protocol.ProtocolError(message, Exit.REFUSED, fix, reason="review-round-limit",
                                 details={"review_subject": subject})


def assign_round(tx: sqlite3.Connection, subject: str, given: int | None = None) -> int:
    """Read accepted history and claim its next round under BEGIN IMMEDIATE.

    A queued/running/waiting job holds the next round without advancing it.
    Failed, cancelled and lost jobs release it; only a succeeded job with an
    accepted attempt advances history. Retirement carries accepted history
    into its durable audit event. Explicit rounds remain the caller's choice.
    """
    if given is not None:
        if given > 4:
            refuse_limit(subject)
        return given
    last = tx.execute(
        "SELECT MAX(review_round) FROM jobs WHERE task='review' AND review_subject=? "
        "AND state='succeeded' AND accepted_attempt_id IS NOT NULL", (subject,)).fetchone()[0]
    # Retention removes the indexed job row, but its committed audit event
    # preserves accepted history. The existing events_kind index restricts
    # this read to retirements, rather than the general event stream.
    archived = tx.execute(
        "WITH retired AS (SELECT CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END AS data "
        "FROM events WHERE kind='retention.pruned') "
        "SELECT MAX(json_extract(data,'$.review_round')) FROM retired "
        "WHERE json_extract(data,'$.review_subject')=? AND json_type(data,'$.review_round')='integer' "
        "AND json_extract(data,'$.review_round') BETWEEN 1 AND 4",
        (subject,)).fetchone()[0]
    candidate = max(last or 0, archived or 0) + 1
    if candidate > 4:
        refuse_limit(subject)
    held = tx.execute(
        "SELECT job_id FROM jobs WHERE task='review' AND review_subject=? AND review_round=? "
        "AND state IN ('queued','running','waiting') ORDER BY job_id LIMIT 1",
        (subject, candidate)).fetchone()
    if held is not None:
        details = {"job_id": held[0]}
        message, fix = refusals.render("review-round-held", details)
        raise protocol.ProtocolError(message, Exit.REFUSED, fix, reason="review-round-held", details=details)
    return candidate
