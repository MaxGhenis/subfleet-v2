"""Coverage assertions over the adjudicated v1 invariant ledger.

`docs/invariants.json` is the machine-readable half of `docs/invariants.md`: one row per
ledger id in `docs/reports/A-invariants.md`, carrying the disposition milestone 0 requires
(plan B rev 4, "Test strategy": "every row of appendix A is adjudicated before milestone 1
as keep, replace (with the replacement named), or drop (with the reason), and each keep or
replace names its acceptance owner").

These tests pin the index itself, not the invariants: that every ledger row is present
exactly once, that every disposition is legal, that every kept or replaced row names a
clause, and that the clause it names exists — a `C-x.y` clause in
`docs/acceptance-contract.md`, or a `P-23.<n>` clause proposed in `docs/invariant-gaps.md`
for the rows the contract does not yet cover. No row may be left `GAP`: the milestone-0
requirement is that every surviving invariant has somewhere to be accepted, and a proposed
clause is that somewhere until the integrator folds section 23 into the contract.
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
PROPOSALS = REPO / "docs" / "invariant-gaps.md"

LEDGER_ROWS = 220

#: Rows the contract did not cover when `docs/invariants.md` was first written; each now
#: carries a clause proposed in `docs/invariant-gaps.md`.
PROPOSED_ROWS = 92

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
PROPOSAL_RE = re.compile(r"^P-23\.\d+$")
#: The folded form: section 23 of the contract carries each P-23.<n> as C-23.<n>.
FOLDED_RE = re.compile(r"^C-23\.\d+$")


def as_proposal(clause: str) -> str:
    """C-23.<n> (the folded clause a row cites) to P-23.<n> (its provenance section)."""
    return "P-23." + clause.split(".")[1]


#: What a dropped row puts in the columns that only a surviving invariant can fill.
NOT_APPLICABLE = "n/a"

#: The flag `docs/invariants.md` version 1 used for a row no clause covered. Nothing may
#: carry it now: it is either a contract clause or a proposal.
UNRESOLVED = "GAP"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return json.loads(INDEX.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def clauses() -> set[str]:
    """Clause ids the contract defines, parsed from its `**C-x.y**` markers."""
    return set(re.findall(r"\*\*(C-\d+\.\d+)\*\*", CONTRACT.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def proposals() -> dict[str, list[int]]:
    """Proposed clause ids, each mapped to the ledger rows its own text says it covers.

    A proposal is a `### P-23.<n> — <title>` section whose clause text carries the same
    `**P-23.<n>**` marker the contract uses, and whose `- **Ledger rows:**` line names the
    rows. Both halves are required: a heading with no clause text defines nothing.
    """
    text = PROPOSALS.read_text(encoding="utf-8")
    sections = re.split(r"^### (P-23\.\d+) — ", text, flags=re.MULTILINE)[1:]
    found: dict[str, list[int]] = {}
    for name, body in zip(sections[::2], sections[1::2]):
        assert f"**{name}**" in body, f"{name} has a heading but no clause text"
        line = re.search(r"^- \*\*Ledger rows:\*\* (.+)$", body, flags=re.MULTILINE)
        assert line, f"{name} names no ledger rows"
        ids = [int(m) for m in re.findall(r"(?:^|,\s*)(\d+) \(", line.group(1))]
        assert ids, f"{name}'s ledger rows do not parse: {line.group(1)!r}"
        assert name not in found, f"{name} is defined twice"
        found[name] = ids
    return found


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


def test_kept_and_replaced_rows_name_a_clause(rows: list[dict]) -> None:
    """Every surviving invariant maps to a contract clause or a proposed one."""
    for row in rows:
        if row["disposition"] in {"keep", "replace"}:
            value = row["contract_clause"]
            assert CLAUSE_RE.match(value) or PROPOSAL_RE.match(value), (
                f"row {row['id']} cites {value!r}, which is neither a `C-x.y` clause nor a "
                "`P-23.<n>` proposal"
            )


def test_no_row_is_left_unresolved(rows: list[dict]) -> None:
    """No row still reads `GAP`: milestone 0 wants a clause for every surviving invariant."""
    stranded = sorted(row["id"] for row in rows if row["contract_clause"] == UNRESOLVED)
    assert not stranded, (
        f"rows still flagged {UNRESOLVED}: {stranded}. Map each to a contract clause, or "
        f"propose one in {PROPOSALS.name} and cite it as `P-23.<n>`."
    )


def test_every_cited_contract_clause_exists(rows: list[dict], clauses: set[str]) -> None:
    """No row cites a clause `docs/acceptance-contract.md` does not define (C-20.5 spirit)."""
    assert clauses, "no `**C-x.y**` markers parsed from the contract"
    unknown = sorted(
        {row["contract_clause"] for row in rows if CLAUSE_RE.match(row["contract_clause"])}
        - clauses
    )
    assert not unknown, f"clauses cited but not defined: {unknown}"


def test_every_cited_proposal_is_defined(rows: list[dict], proposals: dict[str, list[int]]) -> None:
    """A `P-23.<n>` is legal only where `docs/invariant-gaps.md` writes the clause text."""
    assert proposals, f"no `P-23.<n>` proposals parsed from {PROPOSALS.name}"
    unknown = sorted(
        {row["contract_clause"] for row in rows if PROPOSAL_RE.match(row["contract_clause"])}
        - set(proposals)
    )
    assert not unknown, f"proposals cited but not written in {PROPOSALS.name}: {unknown}"


def test_no_proposal_is_orphaned(rows: list[dict], proposals: dict[str, list[int]]) -> None:
    """Every proposed clause is cited by at least one row; the file invents nothing."""
    cited = {as_proposal(c) if FOLDED_RE.match(c) else c
             for c in (row["contract_clause"] for row in rows)}
    orphans = sorted(set(proposals) - cited)
    assert not orphans, f"proposals no row cites: {orphans}"


def test_proposals_and_index_agree_on_which_rows_each_covers(
    rows: list[dict], proposals: dict[str, list[int]],
) -> None:
    """A proposal's `Ledger rows:` list is exactly the set of rows citing it, both ways."""
    from_index: dict[str, set[int]] = {}
    for row in rows:
        clause = row["contract_clause"]
        if PROPOSAL_RE.match(clause) or FOLDED_RE.match(clause):
            from_index.setdefault(as_proposal(clause), set()).add(row["id"])
    from_file = {name: set(ids) for name, ids in proposals.items()}
    assert from_file == from_index


def test_proposals_are_numbered_from_one_without_gaps(proposals: dict[str, list[int]]) -> None:
    """`P-23.<n>` runs 1..N so the integrator can fold the set in as one section."""
    numbers = sorted(int(name.split(".")[1]) for name in proposals)
    assert numbers == list(range(1, len(proposals) + 1)), f"non-contiguous proposals: {numbers}"


def test_every_proposal_names_an_owner_and_a_module() -> None:
    """Each proposal carries the acceptance owner and v2 module the ledger rows assign."""
    text = PROPOSALS.read_text(encoding="utf-8")
    sections = re.split(r"^### (P-23\.\d+) — ", text, flags=re.MULTILINE)[1:]
    for name, body in zip(sections[::2], sections[1::2]):
        for field in ("Milestone", "Acceptance owner", "v2 module", "Rationale"):
            assert f"- **{field}:**" in body, f"{name} names no {field.lower()}"
        owners = re.search(r"^- \*\*Acceptance owner:\*\* (.+)$", body, flags=re.MULTILINE)
        assert owners and owners.group(1).strip() in OWNERS, (
            f"{name} owner {owners.group(1) if owners else None!r} is not one of {sorted(OWNERS)}"
        )


def test_the_proposed_rows_are_the_ninety_two(
    rows: list[dict], proposals: dict[str, list[int]],
) -> None:
    """The set the proposals cover is the set `docs/invariants.md` version 1 left uncovered."""
    covered = sorted(i for ids in proposals.values() for i in ids)
    assert len(covered) == len(set(covered)), "a ledger row is claimed by two proposals"
    assert len(covered) == PROPOSED_ROWS
    assert covered == sorted(
        row["id"] for row in rows
        if PROPOSAL_RE.match(row["contract_clause"]) or FOLDED_RE.match(row["contract_clause"])
    )


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
