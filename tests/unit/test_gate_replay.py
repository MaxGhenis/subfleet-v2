"""Recorded v1 gate state reconstructs the same agreement certificate."""

import copy
import json
import re
import shutil
from pathlib import Path

import pytest

from subfleet.gate.certificate import (
    certificate, gate_dir, load_state, replay, save_state, write_json,
)
from subfleet.gate.errors import GateError
from subfleet.gate.verdict import parse_verdict, validate_attestation

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gates" / "v1"
GATES = sorted(path for path in FIXTURES.iterdir() if path.is_dir())


@pytest.mark.parametrize("directory", GATES, ids=lambda path: path.name)
def test_v1_gate_replays_identical_certificate_content(directory):
    """C-23.8, C-23.9: all three v1 certificates reconstruct from recorded approvals."""
    before = {path.relative_to(directory): path.read_bytes()
              for path in directory.rglob("*") if path.is_file()}
    expected = json.loads((directory / "certificate.json").read_text())
    assert replay(directory) == expected
    after = {path.relative_to(directory): path.read_bytes()
             for path in directory.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("directory", GATES, ids=lambda path: path.name)
def test_archived_peer_output_attests_the_certificate_revision(directory):
    """C-23.9, C-23.43: raw sentinel output and Fable sidecar support each certificate."""
    state = load_state(directory)
    last_round = state["rounds"][-1]
    round_dir = directory / "rounds" / Path(last_round["peer_output"]).parent.name
    artifact = json.loads((round_dir / "artifact.json").read_text())
    assert artifact == last_round["revision"]
    verdict = parse_verdict((round_dir / "peer-output.md").read_text(), artifact)
    assert verdict == last_round["verdict"]
    fields = dict(line.split(": ", 1) for line in
                  (round_dir / "peer-output.MODEL_ATTESTED").read_text().splitlines())
    assert fields["session"]
    assert not (round_dir / "peer-output.DOWNGRADED").exists()
    validate_attestation("attested", fields["requested"], served_model=fields["served"])


def test_replay_preserves_historical_rounds_without_admitting_new_ones():
    """C-23.53, C-23.8: historical eight-round evidence replays without new admission."""
    directory = FIXTURES / "20260905-093217-pr-aff2fcda"
    state = load_state(directory)
    assert len(state["rounds"]) == 8 and state["max_rounds"] == 0
    result = replay(directory)
    assert result["artifact_revision"] == state["rounds"][-1]["revision"]
    assert result["artifact_revision"] != state["rounds"][0]["revision"]


@pytest.mark.parametrize("mutation,message", [
    (lambda row: row["main_approval"].update(approved=False), "explicit approval"),
    (lambda row: row["main_approval"].update(expected_revision={}), "expected revision"),
    (lambda row: row.update(status="changes_requested"), "peer approval"),
    (lambda row: row["verdict"].update(artifact_revision={}), "different artifact"),
    (lambda row: row["verdict"].update(notes=["fix this"]), "nonempty notes"),
    (lambda row: row.update(peer="astra"), "pinned peer"),
])
def test_certificate_rejects_inconsistent_agreement_evidence(mutation, message):
    """C-23.8, C-23.9: a certificate cannot launder an inconsistent main/peer pair."""
    state = load_state(GATES[0])
    last_round = copy.deepcopy(state["rounds"][-1])
    mutation(last_round)
    with pytest.raises(GateError, match=message) as error:
        certificate(state, last_round, issued_at="2026-09-05T00:00:00Z")
    assert error.value.code == 4


def test_replay_without_certificate_cannot_infer_agreement(tmp_path):
    """C-23.8: archived state with no issued certificate grants no fresh authority."""
    state = load_state(GATES[0])
    write_json(tmp_path / "gate.json", state)
    assert replay(tmp_path) is None


def test_replay_does_not_trust_old_certificate_fields(tmp_path):
    """C-23.8, C-23.9: reconstruct approval from gate evidence, not certificate copying."""
    shutil.copytree(GATES[0], tmp_path / "archive")
    directory = tmp_path / "archive"
    expected = replay(directory)
    existing = json.loads((directory / "certificate.json").read_text())
    existing["peer"] = "bogus-peer"
    existing["artifact_revision"] = {"kind": "unrelated"}
    write_json(directory / "certificate.json", existing)
    assert replay(directory) == expected


def test_state_projection_audits_before_private_atomic_publication(tmp_path):
    """C-3.2, C-3.3, C-8.1: state publication follows its supplied daemon audit callback."""
    directory = gate_dir(tmp_path, "20260905-000000-plan-test")
    state = load_state(GATES[0])
    state["id"] = directory.name
    seen = []

    def event(kind, data):
        assert not (directory / "gate.json").exists()
        seen.append((kind, data))

    save_state(directory, state, event=event)
    assert load_state(directory) == state
    assert seen[0][0] == "gate.state" and seen[0][1]["state"] == state
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "gate.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/b", "", ".hidden", "a" * 129])
def test_gate_ids_cannot_escape_the_state_root(tmp_path, name):
    """C-2.1, C-23.8: gate state is addressed only within the named state root."""
    with pytest.raises(GateError, match="invalid gate id"):
        gate_dir(tmp_path, name)


def test_redacted_fixtures_have_no_credentials_or_original_home_paths():
    """C-10.5, C-23.8: replay evidence contains no credential or live home reference."""
    pattern = re.compile(r"\b(?:sk-(?:ant-(?:api\d+-|oat\d+-))?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}\b"
                         r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
                         r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    assert len(GATES) == 3
    for directory in GATES:
        for path in directory.rglob("*"):
            if path.is_file():
                content = path.read_text()
                assert "/Users/maxghenis" not in content, path
                assert pattern.search(content) is None, path
