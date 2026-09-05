"""Strict revision-bound peer results and positive model attestation (C-23.9/43)."""

from __future__ import annotations

import json
from typing import Any

from .errors import GateError

SCHEMA_VERSION = 1
VERDICT_BEGIN = "---SUBFLEET-VERDICT-BEGIN---"
VERDICT_END = "---SUBFLEET-VERDICT-END---"
VERDICTS = frozenset({"approve", "changes_requested", "blocked"})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def parse_verdict(text: str, expected_revision: dict[str, Any]) -> dict[str, Any]:
    """Accept exactly v1's sentinel object with an unambiguous JSON interpretation."""
    if text.count(VERDICT_BEGIN) != 1 or text.count(VERDICT_END) != 1:
        raise GateError("peer output is missing or duplicates the verdict sentinel", 4)
    before, remainder = text.split(VERDICT_BEGIN, 1)
    if VERDICT_END not in remainder:
        raise GateError("peer verdict sentinel order is invalid", 4)
    payload, after = remainder.split(VERDICT_END, 1)
    if before.strip() or after.strip():
        raise GateError("peer output contains text outside the verdict sentinel", 4)
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except ValueError as exc:
        raise GateError(f"peer verdict is invalid JSON: {exc}", 4) from exc
    if not isinstance(value, dict):
        raise GateError("peer verdict must be a JSON object", 4)
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise GateError("peer verdict has an unsupported schema", 4)
    if value.get("artifact_revision") != expected_revision:
        raise GateError("peer verdict is bound to a different artifact revision", 4)
    verdict = value.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise GateError(f"peer verdict is invalid: {verdict!r}", 4)
    findings, notes, summary = value.get("findings"), value.get("notes"), value.get("summary")
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise GateError("peer findings must be an array of objects", 4)
    for finding in findings:
        if not all(isinstance(finding.get(field), str) and finding[field].strip()
                   for field in ("severity", "location", "description")):
            raise GateError("every peer finding needs nonempty severity, location, and description", 4)
    if (not isinstance(notes, list) or not all(isinstance(note, str) for note in notes)
            or not isinstance(summary, str) or not summary.strip()):
        raise GateError("peer verdict summary/notes have invalid types", 4)
    if verdict == "approve" and findings:
        raise GateError("peer claimed approval while returning actionable findings", 4)
    if verdict == "approve" and notes:
        raise GateError("peer claimed approval while returning nonempty notes", 4)
    if verdict == "changes_requested" and not findings:
        raise GateError("peer requested changes without an actionable finding", 4)
    return value


def validate_attestation(
    attestation: Any, requested_model: str, *, served_model: str | None = None,
    downgrade: Any = None,
) -> None:
    """Discard any round not positively attested to its pinned model (C-23.43)."""
    if isinstance(attestation, dict):
        served_model = attestation.get("served_model", served_model)
        downgrade = attestation.get("downgrade", downgrade)
        reported_request = attestation.get("requested_model", attestation.get("model_requested"))
        if reported_request is not None and reported_request != requested_model:
            raise GateError("peer attestation names a different requested model; not a verdict", 4)
        status = attestation.get("status", attestation.get("attestation"))
    else:
        status = getattr(attestation, "status", attestation)
        served_model = getattr(attestation, "served_model", served_model)
    status = getattr(status, "value", status)
    if downgrade is not None and downgrade is not False:
        raise GateError("peer carries a downgrade record; not a verdict", 4)
    if status != "attested":
        raise GateError(f"peer attestation is {status or 'unattested'}; not a verdict", 4)
    matches = served_model == requested_model or (
        requested_model.startswith("claude-") and isinstance(served_model, str)
        and served_model.startswith(requested_model + "-")
    )
    if not matches:
        raise GateError("peer lacks positive served-model attestation for the requested model; not a verdict", 4)
