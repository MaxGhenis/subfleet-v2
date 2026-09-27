"""v1-compatible gate evidence files and agreement certificates (C-23.8/9)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

from ..guardian import atomic_publish
from ..sessions.transcripts import read_regular
from ..store import utc_now
from .errors import GateError
from .revision import assert_expected
from .verdict import SCHEMA_VERSION, VERDICT_BEGIN, VERDICT_END, parse_verdict

GATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
Event = Callable[[str, dict[str, Any]], None]


def gate_dir(state_root: Path | str, gate_id: str) -> Path:
    if not GATE_ID_RE.fullmatch(gate_id):
        raise GateError(f"invalid gate id: {gate_id!r}")
    return Path(state_root) / "gates" / gate_id


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_bytes(path: Path, value: bytes) -> None:
    """Use the shared fsync/rename publisher; gate evidence stays private (C-8.1)."""
    private_dir(path.parent)
    atomic_publish(path, value)


def write_json(path: Path, value: Any) -> None:
    write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def load_state(directory: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_regular(directory / "gate.json").decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise GateError(f"cannot read gate state: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise GateError("gate state has an unsupported schema")
    return value


def save_state(
    directory: Path, state: dict[str, Any], *, event: Event | None = None,
) -> None:
    """Publish a file projection; its daemon caller supplies the events writer.

    This helper never opens a writable store. The daemon commits the authoritative
    transition before publishing its file projection, outside the SQL transaction.
    """
    state["updated_at"] = utc_now()
    if event is not None:
        event("gate.state", {"gate_id": state["id"], "state": copy.deepcopy(state)})
    write_json(directory / "gate.json", state)


def certificate(
    state: dict[str, Any], round_state: dict[str, Any], *, issued_at: str | None = None,
) -> dict[str, Any]:
    """Rebuild the exact v1 certificate content from one agreed revision."""
    approval = round_state.get("main_approval")
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        raise GateError("agreement certificate requires the main's explicit approval", 4)
    expected = round_state.get("revision")
    if not isinstance(expected, dict):
        raise GateError("agreement certificate has no artifact revision", 4)
    assert_expected(expected, approval.get("expected_revision"))
    verdict = parse_verdict(
        VERDICT_BEGIN + "\n" + json.dumps(round_state.get("verdict")) + "\n" + VERDICT_END,
        expected,
    )
    if round_state.get("status") != "approve" or verdict["verdict"] != "approve":
        raise GateError("agreement certificate requires peer approval", 4)
    if round_state.get("peer", state["peer"]) != state["peer"]:
        raise GateError("agreement certificate peer does not match its pinned peer", 4)
    if state.get("on_agreement") not in {"proceed", "merge"}:
        raise GateError("agreement certificate has an unsupported action", 4)
    if state["on_agreement"] == "merge" and expected.get("kind") != "pr":
        raise GateError("only a PR certificate can authorize merge", 4)
    return copy.deepcopy({
        "schema_version": SCHEMA_VERSION,
        "gate_id": state["id"],
        "issued_at": utc_now() if issued_at is None else issued_at,
        "artifact_revision": expected,
        "main_approval": approval,
        "peer": state["peer"],
        "peer_verdict": verdict,
        "authorized_action": state["on_agreement"],
    })


def replay(directory: Path) -> dict[str, Any] | None:
    """Reconstruct archived v1 evidence without dispatch, mutation, or new consent.

    The issuance timestamp is the only field recovered from the old certificate;
    approval and peer evidence are reconstructed from gate.json. A historical
    round count is evidence, not a request to bypass today's admission cap.
    """
    state = load_state(directory)
    if not (directory / "certificate.json").is_file():
        return None
    try:
        persisted = json.loads((directory / "certificate.json").read_text())
        issued_at = persisted["issued_at"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise GateError(f"cannot read gate certificate: {exc}", 4) from exc
    if not isinstance(issued_at, str) or not issued_at:
        raise GateError("gate certificate has no issuance timestamp", 4)
    rounds = state.get("rounds") or []
    if not rounds:
        raise GateError("gate certificate has no approval round", 4)
    return certificate(state, rounds[-1], issued_at=issued_at)
