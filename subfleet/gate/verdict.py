"""Strict revision-bound peer results and positive model attestation (C-23.9/43)."""

from __future__ import annotations

import json
from typing import Any

from .errors import GateError

SCHEMA_VERSION = 1
VERDICT_BEGIN = "---SUBFLEET-VERDICT-BEGIN---"
VERDICT_END = "---SUBFLEET-VERDICT-END---"
VERDICTS = frozenset({"approve", "changes_requested", "blocked"})
# The prompt template's placeholder: a block that still carries it chose no verdict.
TEMPLATE_VERDICT = "approve | changes_requested | blocked"


class VerdictFormatError(GateError):
    """Peer output that is not a well-formed verdict envelope (C-23.9).

    Only this failure may be re-asked, once, within its round. A payload bound to
    another revision, or a contradictory verdict, raises a plain GateError instead.
    """

    def __init__(self, message: str):
        super().__init__(message, 4)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def decode_output(body: bytes) -> str:
    """Peer output is UTF-8 text; anything else is a malformed envelope."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerdictFormatError(f"peer output is not UTF-8 text: {exc}") from exc


def parse_verdict(text: str, expected_revision: dict[str, Any]) -> dict[str, Any]:
    """Accept exactly v1's sentinel object with an unambiguous JSON interpretation.

    Envelope and field-shape failures raise VerdictFormatError. The revision binding
    is checked as soon as the payload is an object, before any text outside the
    sentinels is reported, so a payload bound to another revision (or to none) is
    never classed as a format failure. Contradictory verdicts are not format failures.
    """
    if text.count(VERDICT_BEGIN) != 1 or text.count(VERDICT_END) != 1:
        raise VerdictFormatError("peer output is missing or duplicates the verdict sentinel")
    before, remainder = text.split(VERDICT_BEGIN, 1)
    if VERDICT_END not in remainder:
        raise VerdictFormatError("peer verdict sentinel order is invalid")
    payload, after = remainder.split(VERDICT_END, 1)
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise VerdictFormatError(f"peer verdict is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise VerdictFormatError("peer verdict must be a JSON object")
    if value.get("artifact_revision") != expected_revision:
        raise GateError("peer verdict is bound to a different artifact revision", 4)
    if before.strip() or after.strip():
        raise VerdictFormatError("peer output contains text outside the verdict sentinel")
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise VerdictFormatError("peer verdict has an unsupported schema")
    verdict = value.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise VerdictFormatError(f"peer verdict is invalid: {verdict!r:.80}")
    findings, notes, summary = value.get("findings"), value.get("notes"), value.get("summary")
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise VerdictFormatError("peer findings must be an array of objects")
    for finding in findings:
        if not all(isinstance(finding.get(field), str) and finding[field].strip()
                   for field in ("severity", "location", "description")):
            raise VerdictFormatError("every peer finding needs nonempty severity, location, and description")
    if (not isinstance(notes, list) or not all(isinstance(note, str) for note in notes)
            or not isinstance(summary, str) or not summary.strip()):
        raise VerdictFormatError("peer verdict summary/notes have invalid types")
    if verdict == "approve" and findings:
        raise GateError("peer claimed approval while returning actionable findings", 4)
    if verdict == "approve" and notes:
        raise GateError("peer claimed approval while returning nonempty notes", 4)
    if verdict == "changes_requested" and not findings:
        raise GateError("peer requested changes without an actionable finding", 4)
    return value


_EMPTY = (None, [], {}, "")


class _Members(list):
    """Every member of one JSON object, duplicates included (diagnosis only)."""


def _plain(value: Any) -> Any:
    if isinstance(value, _Members):
        return {key: _plain(item) for key, item in value}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def rejected_output_evidence(text: str, expected_revision: dict[str, Any]) -> dict[str, Any]:
    """Diagnose output parse_verdict rejected, only to decide whether it may be re-asked.

    Nothing found here is ever a verdict or part of one: it can only forbid a
    re-ask or constrain its outcome (C-23.9). Every sentinel-delimited segment is
    scanned, duplicate JSON members included. A segment that is a JSON object
    naming another artifact revision, or none, is a revision-binding failure; one
    that approves with findings or notes, or requests changes without a finding,
    is contradictory. Either forbids the re-ask (`refusal`). Every verdict such a
    segment names, other than the template's placeholder, is kept, so that a
    re-ask cannot turn a rejected non-approval into an approval.
    """
    refusals, verdicts = [], []
    start = text.find(VERDICT_BEGIN)
    while start != -1:
        body = start + len(VERDICT_BEGIN)
        end = text.find(VERDICT_END, body)
        if end == -1:
            break
        try:
            value = json.loads(text[body:end], object_pairs_hook=_Members)
            if isinstance(value, _Members):
                refusals += _segment_refusals(value, expected_revision)
                verdicts += [item if isinstance(item, str) else json.dumps(_plain(item), sort_keys=True)
                             for key, item in value
                             if key == "verdict" and item is not None and item != TEMPLATE_VERDICT]
        except ValueError:
            pass  # not JSON: no binding or verdict can be read from this segment
        except RecursionError:
            refusals.append("a verdict block in the output is too deeply nested to diagnose")
        start = text.find(VERDICT_BEGIN, body)
    return {"refusal": refusals[0] if refusals else None, "verdicts": verdicts}


def _segment_refusals(members: _Members, expected_revision: dict[str, Any]) -> list[str]:
    def values(name):
        return [_plain(item) for key, item in members if key == name]
    refusals = []
    revisions, verdict = values("artifact_revision"), values("verdict")
    if not revisions or any(item != expected_revision for item in revisions):
        refusals.append("a verdict block in the output is bound to a different artifact revision")
    findings = [item for item in values("findings") if item not in _EMPTY]
    if "approve" in verdict and (findings or any(item not in _EMPTY for item in values("notes"))):
        refusals.append("a verdict block in the output approves with findings or notes")
    if "changes_requested" in verdict and not findings:
        refusals.append("a verdict block in the output requests changes without a finding")
    return refusals


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
    if status == "mismatch":
        raise GateError("peer attestation is mismatch; not a verdict", 4)
    if status != "attested":
        # C-23.43 as amended: a Claude round must be positively attested, because
        # the provider can silently serve another model. A Codex round pins its
        # model at launch and the CLI has no such fallback; when no rollout was
        # persisted (Codex 0.153.3 does not keep one for an isolated ephemeral
        # run) the round counts and the certificate records it as unattested.
        if requested_model.startswith("claude-"):
            raise GateError(f"peer attestation is {status or 'unattested'}; not a verdict", 4)
        return
    matches = served_model == requested_model or (
        requested_model.startswith("claude-") and isinstance(served_model, str)
        and served_model.startswith(requested_model + "-")
    )
    if not matches:
        raise GateError("peer lacks positive served-model attestation for the requested model; not a verdict", 4)
