"""Coverage assertions over the adjudicated v1 invariant ledger.

`docs/invariants.json` is the machine-readable half of `docs/invariants.md`: one row per
ledger id in `docs/reports/A-invariants.md`, carrying the disposition milestone 0 requires
(plan B rev 4, "Test strategy": "every row of appendix A is adjudicated before milestone 1
as keep, replace (with the replacement named), or drop (with the reason), and each keep or
replace names its acceptance owner").

These tests pin the index itself, not the invariants: that every ledger row is present
exactly once, that every disposition is legal, that every kept or replaced row names a
clause of `docs/acceptance-contract.md` or is explicitly flagged `GAP`, and that no row
cites a clause the contract does not define.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "docs" / "invariants.json"
CONTRACT = REPO / "docs" / "acceptance-contract.md"
LEDGER = REPO / "docs" / "reports" / "A-invariants.md"
TABLE = REPO / "docs" / "invariants.md"

LEDGER_ROWS = 220

FIELDS = (
    "id",
    "invariant",
    "v1_location",
    "class",
    "disposition",
    "replacement",
    "contract_clause",
    "acceptance_owner",
    "v2_module",
    "test_name",
    "notes",
)

DISPOSITIONS = {"keep", "replace", "drop"}
OWNERS = {"unit", "fake", "process", "live"}
CLASSES = {
    "safety-guard",
    "capacity-truth",
    "ops-hygiene",
    "session-continuity",
    "routing-policy",
    "provenance/attestation",
    "identity",
    "UX-contract",
    "work-salvage",
    "process-survival",
}

CLAUSE_RE = re.compile(r"^C-\d+\.\d+$")

#: What a dropped row puts in the columns that only a surviving invariant can fill.
NOT_APPLICABLE = "n/a"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return json.loads(INDEX.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def clauses() -> set[str]:
    """Clause ids the contract defines, parsed from its `**C-x.y**` markers."""
    return set(re.findall(r"\*\*(C-\d+\.\d+)\*\*", CONTRACT.read_text(encoding="utf-8")))


def test_index_is_a_json_array_of_objects(rows: list[dict]) -> None:
    """The index is a JSON array, one object per ledger row."""
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)


def test_every_ledger_row_is_adjudicated(rows: list[dict]) -> None:
    """All 220 rows of `docs/reports/A-invariants.md` appear (plan B, Appendix A)."""
    assert len(rows) == LEDGER_ROWS


def test_ids_are_unique_and_complete(rows: list[dict]) -> None:
    """Ids are exactly 1..220, each once: no gaps, no duplicates, no invented rows."""
    ids = [row["id"] for row in rows]
    assert all(isinstance(i, int) for i in ids)
    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    assert not duplicates, f"duplicate ids: {duplicates}"
    assert sorted(ids) == list(range(1, LEDGER_ROWS + 1))


def test_rows_are_in_ledger_order(rows: list[dict]) -> None:
    """The index reads in ledger order so it can be diffed against the ledger by eye."""
    assert [row["id"] for row in rows] == list(range(1, LEDGER_ROWS + 1))


def test_every_row_has_every_field(rows: list[dict]) -> None:
    """Every row carries the full column set of `docs/invariants.md`."""
    for row in rows:
        missing = [f for f in FIELDS if f not in row]
        assert not missing, f"row {row.get('id')} missing {missing}"
        extra = [k for k in row if k not in FIELDS]
        assert not extra, f"row {row.get('id')} has unexpected fields {extra}"
        assert all(isinstance(row[f], str) for f in FIELDS if f != "id")


def test_dispositions_are_in_the_allowed_set(rows: list[dict]) -> None:
    """A disposition is `keep`, `replace`, or `drop` and nothing else."""
    bad = [(row["id"], row["disposition"]) for row in rows if row["disposition"] not in DISPOSITIONS]
    assert not bad, f"illegal dispositions: {bad}"


def test_classes_match_the_ledger_vocabulary(rows: list[dict]) -> None:
    """Classes are the ten the ledger uses (plan B, Appendix A counts by class)."""
    bad = [(row["id"], row["class"]) for row in rows if row["class"] not in CLASSES]
    assert not bad, f"unknown classes: {bad}"


def test_replaced_rows_name_their_replacement(rows: list[dict]) -> None:
    """`replace` names the v2 rule; nothing else carries a replacement."""
    for row in rows:
        if row["disposition"] == "replace":
            assert row["replacement"].strip(), f"row {row['id']} is `replace` with no replacement"
        else:
            assert not row["replacement"].strip(), (
                f"row {row['id']} is `{row['disposition']}` but carries a replacement"
            )


def test_dropped_rows_give_a_reason(rows: list[dict]) -> None:
    """A drop is only legitimate with the reason recorded beside it."""
    for row in rows:
        if row["disposition"] == "drop":
            assert row["notes"].strip(), f"row {row['id']} is dropped with no reason"


def test_kept_and_replaced_rows_name_a_clause_or_a_gap(rows: list[dict]) -> None:
    """Every surviving invariant maps to a contract clause or is flagged `GAP`."""
    for row in rows:
        if row["disposition"] in {"keep", "replace"}:
            value = row["contract_clause"]
            assert value == "GAP" or CLAUSE_RE.match(value), (
                f"row {row['id']} cites {value!r}, which is neither `GAP` nor a `C-x.y` clause"
            )


def test_every_cited_clause_exists_in_the_contract(rows: list[dict], clauses: set[str]) -> None:
    """No row cites a clause `docs/acceptance-contract.md` does not define (C-20.5 spirit)."""
    assert clauses, "no `**C-x.y**` markers parsed from the contract"
    unknown = sorted(
        {row["contract_clause"] for row in rows if row["contract_clause"] not in clauses}
        - {"GAP", NOT_APPLICABLE}
    )
    assert not unknown, f"clauses cited but not defined: {unknown}"


def test_dropped_rows_do_not_claim_a_clause(rows: list[dict]) -> None:
    """A dropped invariant owns no clause; the clause column reads `n/a`."""
    for row in rows:
        if row["disposition"] == "drop":
            assert row["contract_clause"] == NOT_APPLICABLE, (
                f"row {row['id']} is dropped but cites {row['contract_clause']!r}"
            )


def test_surviving_rows_name_an_acceptance_owner(rows: list[dict]) -> None:
    """Plan B: each keep or replace names an acceptance owner (unit, fake, process, live)."""
    for row in rows:
        if row["disposition"] in {"keep", "replace"}:
            assert row["acceptance_owner"] in OWNERS, (
                f"row {row['id']} owner {row['acceptance_owner']!r} is not one of {sorted(OWNERS)}"
            )
            assert row["v2_module"].strip(), f"row {row['id']} names no v2 module"
            assert re.match(r"^test_[a-z0-9_]+$", row["test_name"]), (
                f"row {row['id']} test name {row['test_name']!r} is not a pytest function name"
            )


def test_dropped_rows_carry_no_owner_module_or_test(rows: list[dict]) -> None:
    """A dropped invariant has nothing to accept, so it names no owner, module, or test."""
    for row in rows:
        if row["disposition"] == "drop":
            assert row["acceptance_owner"] == NOT_APPLICABLE
            assert row["v2_module"] == NOT_APPLICABLE
            assert row["test_name"] == NOT_APPLICABLE


def test_test_names_are_unique(rows: list[dict]) -> None:
    """Proposed test names are distinct so a later coverage test can key on them."""
    names = [row["test_name"] for row in rows if row["test_name"] != NOT_APPLICABLE]
    duplicates = sorted(n for n, c in Counter(names).items() if c > 1)
    assert not duplicates, f"duplicate test names: {duplicates}"


def test_mandated_dispositions_hold(rows: list[dict]) -> None:
    """Plan B and the lane brief fix ten rows: eight `replace`, two `drop`."""
    by_id = {row["id"]: row for row in rows}
    for rid in (44, 57, 58, 116, 125, 126, 127, 171):
        assert by_id[rid]["disposition"] == "replace", f"row {rid} must be `replace`"
    for rid in (219, 220):
        assert by_id[rid]["disposition"] == "drop", f"row {rid} must be `drop`"
    assert by_id[133]["disposition"] == "keep", "plan B keeps row 133 as the Codex ordering key"


def test_markdown_table_and_json_agree(rows: list[dict]) -> None:
    """`docs/invariants.md` and `docs/invariants.json` carry the same ids and dispositions."""
    from_table: dict[int, str] = {}
    for line in TABLE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(FIELDS) or not cells[0].isdigit():
            continue
        from_table[int(cells[0])] = cells[4]
    assert len(from_table) == LEDGER_ROWS, (
        f"parsed {len(from_table)} table rows from docs/invariants.md, expected {LEDGER_ROWS}"
    )
    assert from_table == {row["id"]: row["disposition"] for row in rows}


def test_ledger_still_has_two_hundred_and_twenty_rows() -> None:
    """The index is pinned to the ledger it adjudicates; a new ledger row fails here first."""
    text = LEDGER.read_text(encoding="utf-8")
    ids = [int(m) for m in re.findall(r"^\| (\d+) \| ", text, flags=re.MULTILINE)]
    assert ids == list(range(1, LEDGER_ROWS + 1))
