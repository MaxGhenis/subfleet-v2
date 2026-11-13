"""Read a review result saved with its accepted attempt's evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .contracts import JobState


def is_review_job(job: Mapping[str, Any]) -> bool:
    return job.get("task") == "review" or job.get("kind") == "gate-review"


def stored_review_result(job: Mapping[str, Any], evidence_json: str | None) -> dict[str, Any] | None:
    """Return stored review metadata; older records and other jobs have none.

    The caller must select evidence by ``accepted_attempt_id``. Reading never
    reparses an artifact: it may have changed since the attempt was accepted.
    """
    if not is_review_job(job) or not job.get("accepted_attempt_id"):
        return None
    if job.get("state") not in {state.value for state in JobState if state.terminal}:
        return None
    try:
        evidence = json.loads(evidence_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    review = evidence.get("review") if isinstance(evidence, dict) else None
    return review if isinstance(review, dict) else None
