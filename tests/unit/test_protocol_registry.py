"""The authoritative op registry also covers discovery, read routing and docs."""

from pathlib import Path
import re

import pytest

from subfleet import descriptors, protocol
from subfleet.conversations.service import CAPABILITIES


ROOT = Path(__file__).resolve().parents[2]


def test_conversation_status_is_advertised_and_read_only():
    assert "conversation.status" in protocol.CONVERSATION_OPS
    assert "conversation.status.v1" in CAPABILITIES
    assert descriptors.read_only("conversation.status", {"conversation_id": "fixture"})


@pytest.mark.parametrize("document", ["docs/acceptance-contract.md", "docs/desktop/design.md"])
def test_every_conversation_op_has_a_documented_contract(document):
    text = (ROOT / document).read_text()
    if document.endswith("acceptance-contract.md"):
        text = next(line for line in text.splitlines() if line.startswith("- **C-25.2**"))
        documented = set(re.findall(r"`([^`]+)`", text))
    else:
        documented = set(re.findall(r"^\| `([^`]+)`", text, re.MULTILINE))
    assert set(protocol.CONVERSATION_OPS) <= documented


def test_status_capability_is_documented_in_discovery_and_the_op_contract():
    text = (ROOT / "docs/acceptance-contract.md").read_text()
    for clause in ("C-25.1", "C-25.2"):
        line = next(line for line in text.splitlines() if line.startswith(f"- **{clause}**"))
        assert "`conversation.status.v1`" in line
