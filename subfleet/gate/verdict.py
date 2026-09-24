"""Strict revision-bound peer results and positive model attestation (C-23.9/43)."""

from __future__ import annotations

import json
import re
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

    It is the first form failure parse_verdict met, which can hide a revision-binding
    failure or a contradiction elsewhere in the output. The output may be re-asked,
    once within its round, only if rejected_output_evidence also finds no refusal.
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

    Envelope and field-shape failures raise VerdictFormatError. Once the single
    payload is a JSON object, its revision binding is checked before text outside
    the sentinels is reported, so that payload's binding failure (another revision,
    or none) is a plain GateError, as is a contradictory verdict. A failure found
    earlier (duplicate sentinels, invalid JSON) is reported first; see
    rejected_output_evidence for what else then forbids a re-ask.
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
_VERDICT_MEMBER = re.compile(r'"verdict"\s*:\s*"((?:[^"\\]|\\.)*)"')
_CONCERN_MEMBER = re.compile(r'"(?:findings|notes)"\s*:\s*\[\s*[^\s\]]')
_CLIP = 200


def clip(value: str, limit: int = _CLIP) -> str:
    """Bound peer-controlled text and keep it storable (no lone surrogates)."""
    value = value.encode("utf-8", "backslashreplace").decode("utf-8")
    return value if len(value) <= limit else value[:limit] + "..."


class _Members(list):
    """Every member of one JSON object, duplicates included (diagnosis only)."""


def _values(members: _Members, name: str) -> list[Any]:
    return [item for key, item in members if key == name]


def _plain(value: Any) -> Any:
    if isinstance(value, _Members):
        return {key: _plain(item) for key, item in value}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _duplicated(value: Any) -> bool:
    if isinstance(value, _Members):
        keys = [key for key, _ in value]
        return len(keys) != len(set(keys)) or any(_duplicated(item) for _, item in value)
    return isinstance(value, list) and any(_duplicated(item) for item in value)


def _identity(expected_revision: dict[str, Any]) -> list[str]:
    """The reviewed revision's digests: what an unreadable block must at least carry."""
    return [str(expected_revision[key]).lower() for key in ("sha256", "head_sha", "base_sha")
            if expected_revision.get(key)]


def rejected_output_evidence(text: str, expected_revision: dict[str, Any]) -> dict[str, Any]:
    """Diagnose output parse_verdict rejected, only to decide whether it may be re-asked.

    Nothing found here is ever a verdict or part of one: it can only forbid a
    re-ask (`refusal`) or keep the re-ask from approving (`outcome_lock`) (C-23.9).

    Each begin sentinel opens a block that runs to the next sentinel of either
    kind or to the end of the output. A block that parses as a JSON object must
    name the reviewed revision, in every artifact_revision member and without
    duplicated keys inside it; it must not approve with findings or notes or
    request changes without a finding. A block that does not parse, and any
    artifact_revision JSON outside the blocks, must at least contain the reviewed
    revision's digests. The lock reads the whole output, unparsed text included:
    any JSON "verdict" string other than approve (an empty or placeholder one
    names nothing) and any nonempty findings or notes. Prose is never read.
    """
    refusals, verdicts, lock = [], [], []
    identity, outside = _identity(expected_revision), []

    def carries_identity(fragment: str) -> bool:
        lowered = fragment.lower()
        return all(digest in lowered for digest in identity)

    cursor, begin, end = 0, text.find(VERDICT_BEGIN), None
    while begin != -1:
        outside.append(text[cursor:begin])
        body = begin + len(VERDICT_BEGIN)
        if end is None or -1 < end < body:  # once no end sentinel follows, none follows later
            end = text.find(VERDICT_END, body)
        following = text.find(VERDICT_BEGIN, body)
        stop = min((index for index in (end, following) if index != -1), default=len(text))
        segment = text[body:stop]
        try:
            value = json.loads(segment, object_pairs_hook=_Members)
            if not isinstance(value, _Members):
                raise ValueError("not a JSON object")
            refusals += _segment_refusals(value, expected_revision)
            verdicts += [item if isinstance(item, str) else json.dumps(_plain(item), sort_keys=True)
                         for item in _values(value, "verdict") if item is not None]
            if any(item not in _EMPTY for item in _values(value, "findings") + _values(value, "notes")):
                lock.append("listed findings or notes")
        except ValueError:
            if not carries_identity(segment):
                refusals.append("a verdict block the gate cannot read lacks the reviewed revision")
        except RecursionError:
            refusals.append("a verdict block in the output is too deeply nested to diagnose")
        cursor = stop + len(VERDICT_END) if stop == end else stop
        begin = following
    outside.append(text[cursor:])
    rest = "".join(outside)
    if '"artifact_revision"' in rest and not carries_identity(rest):
        refusals.append("revision JSON outside the verdict blocks lacks the reviewed revision")

    scan = text.replace(TEMPLATE_VERDICT, "")
    for match in _VERDICT_MEMBER.finditer(scan):
        try:
            verdicts.append(json.loads(f'"{match.group(1)}"'))
        except ValueError:
            verdicts.append(match.group(1))
    # An empty or placeholder verdict names nothing, like a missing one.
    verdicts = list(dict.fromkeys(clip(item) for item in verdicts if item not in ("", TEMPLATE_VERDICT)))
    lock = [f"named verdict {item!r}" for item in verdicts if item != "approve"] + lock
    if _CONCERN_MEMBER.search(scan):
        lock.append("listed findings or notes")
    return {"refusal": refusals[0] if refusals else None, "verdicts": verdicts,
            "outcome_lock": list(dict.fromkeys(lock))}


def _segment_refusals(members: _Members, expected_revision: dict[str, Any]) -> list[str]:
    refusals = []
    revisions, verdict = _values(members, "artifact_revision"), _values(members, "verdict")
    if (not revisions or any(_duplicated(item) or _plain(item) != expected_revision
                             for item in revisions)):
        refusals.append("a verdict block in the output is bound to a different artifact revision")
    findings = [item for item in _values(members, "findings") if item not in _EMPTY]
    if "approve" in verdict and (findings or any(item not in _EMPTY for item in _values(members, "notes"))):
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
