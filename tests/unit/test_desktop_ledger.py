"""C-20.5, C-21 (milestone 9): the desktop acceptance ledger stays honest.

Every row cites clauses that exist, uses a known status, and a row marked
`verified` names its evidence. The ledger may not claim more than the
contract binds.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = json.loads((ROOT / "docs" / "desktop" / "ledger.json").read_text())
CONTRACT = (ROOT / "docs" / "acceptance-contract.md").read_text()
CLAUSES = set(re.findall(r"^- \*\*(C-\d+\.\d+[a-z]?)\*\*", CONTRACT, re.M))


def test_every_cited_clause_exists():
    """C-21 a ledger row can only cite a clause the contract defines."""
    missing = {(row["id"], c) for row in LEDGER["rows"] for c in row["clauses"] if c not in CLAUSES}
    assert not missing, sorted(missing)


def test_statuses_and_evidence():
    """C-21 statuses come from the ledger's own list; `verified` and `implemented`
    rows name evidence."""
    for row in LEDGER["rows"]:
        assert row["status"] in LEDGER["statuses"], row["id"]
        if row["status"] in ("verified", "implemented"):
            assert row["evidence"], f"{row['id']} is {row['status']} without evidence"


def test_ids_are_unique():
    """C-21 one row per requirement."""
    ids = [row["id"] for row in LEDGER["rows"]]
    assert len(ids) == len(set(ids))


def test_milestone_9_clauses_are_all_cited():
    """C-21 no milestone 9 clause is orphaned: each is cited by some row or is
    infrastructure named in design §14."""
    cited = {c for row in LEDGER["rows"] for c in row["clauses"]}
    milestone9 = {c for c in CLAUSES if re.match(r"C-(2[4-9]|30)\.", c)}
    orphans = sorted(milestone9 - cited)
    # Clauses that bind mechanism rather than a user-visible requirement.
    infrastructure = {"C-24.4", "C-25.2", "C-26.5", "C-26.9", "C-26.12", "C-27.3", "C-27.4",
                      "C-29.4", "C-29.5"}
    assert set(orphans) <= infrastructure, orphans
