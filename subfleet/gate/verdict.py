"""Strict revision-bound peer results and positive model attestation (C-23.9/43)."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .errors import GateError

SCHEMA_VERSION = 1
VERDICT_BEGIN = "---SUBFLEET-VERDICT-BEGIN---"
VERDICT_END = "---SUBFLEET-VERDICT-END---"
VERDICTS = frozenset({"approve", "changes_requested", "blocked"})
# D-GATE-CERT1: where either sentinel starts. The lookahead finds every start,
# overlapping ones too ("---SUBFLEET-VERDICT-BEGIN---SUBFLEET-VERDICT-END---").
_SENTINEL_START = re.compile("-(?=" + "|".join(re.escape(sentinel[1:])
                                               for sentinel in (VERDICT_BEGIN, VERDICT_END)) + ")")


def escape_sentinels(json_text: str) -> str:
    """JSON text holding no sentinel, decoding to the same value (D-GATE-CERT1).

    For `json.dumps` output only. A sentinel's letters can sit only inside a
    JSON string, where `\\u002d` decodes to the hyphen it replaces; and since the
    escape adds no hyphen and none of the sentinels' letters, no new sentinel forms.
    """
    return _SENTINEL_START.sub(lambda _: "\\u002d", json_text)


def wrap_verdict(value: Any, **dumps: Any) -> str:
    """`value` as the one sentinel-delimited object that parse_verdict reads back as `value`.

    A peer may quote a sentinel in a string by escaping it; the parsed value then
    holds the sentinel, and writing it back literally would make two (C-23.9).
    """
    return f"{VERDICT_BEGIN}\n{escape_sentinels(json.dumps(value, **dumps))}\n{VERDICT_END}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH_LIMIT = 200


def _path(node: tuple | None) -> str:
    """A JSON path (`$.findings[0]["a key"]`), ASCII and bounded, from linked steps."""
    steps = []
    while node is not None:
        step, node = node
        steps.append(f"[{step}]" if isinstance(step, int) else f".{step}" if _NAME.fullmatch(step)
                     else f"[{json.dumps(step)}]")
    path = "$" + "".join(reversed(steps))
    return path if len(path) <= _PATH_LIMIT else path[:_PATH_LIMIT] + "..."


def _non_finite(value: Any) -> str | None:
    """Where the first number that parsed as infinity sits, in document order.

    `json.loads` turns a literal past float range (`1e1000`) into infinity, and
    `parse_constant` sees only the literal constants (`Infinity`, `NaN`). Walked
    without recursion; each node keeps a link to its parent, never a path string.
    """
    pending: list[tuple[Any, tuple | None]] = [(value, None)]
    while pending:
        item, node = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return _path(node)
        if isinstance(item, dict):
            pending.extend(reversed([(child, (key, node)) for key, child in item.items()]))
        elif isinstance(item, list):
            pending.extend(reversed([(child, (index, node)) for index, child in enumerate(item)]))
    return None


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
    # D-GATE-CERT1: the certificate serializes what counts and parses it again, and
    # strict JSON has no infinity. Checked after the binding, so that stays its own refusal.
    try:
        json.dumps(value, allow_nan=False)
    except ValueError as exc:
        where = _non_finite(value)
        raise GateError(f"peer verdict holds a number outside the float range at {where}" if where
                        else f"peer verdict cannot be written as strict JSON: {exc}", 4) from None
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
