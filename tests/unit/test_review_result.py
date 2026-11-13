"""D-REVIEW-CONTRACT1: total parsing and the review's explicit format rules."""

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.review_result import parse_review_result


@pytest.mark.parametrize(
    ("text", "verdict", "levels", "valid", "consistent"),
    [
        ("REQUEST CHANGES", None, (0, 0, 0, 0), False, True),
        ("REQUEST CHANGES\nP1: Wrong answer", None, (0, 1, 0, 0), False, True),
        ("VERDICT: REQUEST_CHANGES\n**P2 — Bound the lookup's read snapshot.**",
         "request_changes", (0, 0, 1, 0), True, True),
        ("VERDICT: APPROVE\nFINDINGS: none", "approve", (0, 0, 0, 0), True, True),
        ("VERDICT: APPROVE\nP1: Wrong answer", "approve", (0, 1, 0, 0), True, False),
        ("\r\n\t\r\nVERDICT: REQUEST_CHANGES\r\n- **P0: Live leak**\r\n",
         "request_changes", (1, 0, 0, 0), True, True),
        ("VERDICT: APPROVE   \t\nFINDINGS: none\n", "approve", (0, 0, 0, 0), True, True),
        ("VERDICT: REQUEST_CHANGES\nP3: Naming", "request_changes", (0, 0, 0, 1), True, False),
        ("VERDICT: REQUEST_CHANGES\nFINDINGS: none", "request_changes", (0, 0, 0, 0), True, False),
        ("VERDICT: APPROVE\nP2 - Missing test", "approve", (0, 0, 1, 0), True, True),
        ("VERDICT: APPROVE\nP3: Naming", "approve", (0, 0, 0, 1), True, True),
        ("VERDICT: APPROVE\nA paragraph mentions P1: regression.",
         "approve", (0, 0, 0, 0), False, True),
        ("VERDICT: APPROVE", "approve", (0, 0, 0, 0), False, True),
        ("", None, (0, 0, 0, 0), False, True),
    ],
)
def test_real_review_shapes(text, verdict, levels, valid, consistent):
    result = parse_review_result(text)
    assert result["verdict"] == verdict
    assert result["levels"] == dict(zip(("P0", "P1", "P2", "P3"), levels))
    assert result["valid"] is valid
    assert result["consistent"] is consistent
    assert (result["reason"] is None) is valid


@pytest.mark.parametrize("prefix", ["", "- ", "* ", "#", "## ", "**", "- **", "### **", "\t- **"])
@pytest.mark.parametrize("separator", [":", " —", " -", "**:", "** —", "** -"])
@pytest.mark.parametrize("level", ["P0", "P1", "P2", "P3"])
def test_markdown_line_start_findings(prefix, separator, level):
    result = parse_review_result(f"VERDICT: REQUEST_CHANGES\n{prefix}{level}{separator} Summary.\nDetail below.")
    assert result["levels"] == {key: int(key == level) for key in ("P0", "P1", "P2", "P3")}


@pytest.mark.parametrize("line", [
    "The finding is P0: Live leak.", "Fix P1 — Wrong answer.", "- See P2 - Missing test.",
    "**There is a P3: nit.**", "P10: Not a level.", "P2foo: Not a level.",
    "P2—No separating space.", "P2-No separating space.", "-P2: Not a list marker.",
    "FINDINGS: none in this paragraph", "# Heading mentioning P1: Wrong answer.",
])
def test_levels_in_prose_and_malformed_prefixes_are_not_findings(line):
    result = parse_review_result("VERDICT: APPROVE\n" + line)
    assert result["levels"] == {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    assert result["valid"] is False


@pytest.mark.parametrize("first_line", [
    "REQUEST CHANGES", "VERDICT: REQUEST CHANGES", "VERDICT: approve", "**VERDICT: APPROVE**",
    "Introduction", " VERDICT: APPROVE", "VERDICT: APPROVE and comments",
])
def test_first_nonempty_line_must_be_a_verdict_even_with_findings(first_line):
    result = parse_review_result(f"\n\t\n{first_line}\nVERDICT: APPROVE\nP2: Missing test")
    assert result["verdict"] is None
    assert result["valid"] is False
    assert "missing verdict" in result["reason"]


def test_findings_none_must_be_a_whole_line():
    for text in ("There are FINDINGS: none", "FINDINGS: none, except a note", "**FINDINGS: none**"):
        assert parse_review_result("VERDICT: APPROVE\n" + text)["valid"] is False


def test_two_megabyte_deliverable_and_long_markdown_prefix():
    header = "VERDICT: REQUEST_CHANGES\n"
    finding = "- **P2 — Bound the lookup's read snapshot.**\n"
    text = header + finding + "x" * (2 * 1024 * 1024 - len(header) - len(finding))
    assert len(text.encode()) >= 2 * 1024 * 1024
    result = parse_review_result(text)
    assert result["levels"] == {"P0": 0, "P1": 0, "P2": 1, "P3": 0}
    assert result["valid"] is True
    assert parse_review_result("VERDICT: APPROVE\n" + "*# \t" * 100_000)["valid"] is False


@settings(max_examples=300, deadline=None, database=None, derandomize=True)
@example(text="\ud800\x00\r\nP0: arbitrary Unicode")
@given(text=st.text())
def test_parser_is_total_for_generated_text(text):
    result = parse_review_result(text)
    assert set(result) == {"verdict", "levels", "valid", "reason", "consistent"}
    assert result["verdict"] in (None, "approve", "request_changes")
    assert set(result["levels"]) == {"P0", "P1", "P2", "P3"}
    assert all(type(count) is int and count >= 0 for count in result["levels"].values())
    assert type(result["valid"]) is bool
    assert type(result["consistent"]) is bool
    assert (result["reason"] is None) is result["valid"]


@settings(max_examples=200, deadline=None, database=None, derandomize=True)
@given(
    entries=st.lists(st.tuples(st.sampled_from(["P0", "P1", "P2", "P3"]), st.booleans()), max_size=40),
    prefix=st.sampled_from(["", "- ", "* ", "### ", "**", "- **"]),
    separator=st.sampled_from([":", " —", " -"]),
    newline=st.sampled_from(["\n", "\r\n", "\r"]),
)
def test_generated_counts_only_line_start_findings(entries, prefix, separator, newline):
    lines = ["VERDICT: REQUEST_CHANGES"]
    expected = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for level, is_finding in entries:
        lines.append((prefix if is_finding else "This paragraph mentions ") + level + separator + " Summary")
        expected[level] += int(is_finding)
    assert parse_review_result(newline.join(lines))["levels"] == expected


@settings(max_examples=200, deadline=None, database=None, derandomize=True)
@given(
    verdict=st.sampled_from(["VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES", "REQUEST CHANGES", ""]),
    finding=st.sampled_from(["P0: Live harm", "P1: Wrong result", "P2: Missing test", "P3: Nit", ""]),
    no_findings=st.booleans(),
    blank_lines=st.integers(min_value=0, max_value=5),
)
def test_valid_exactly_requires_a_verdict_and_a_findings_declaration(verdict, finding, no_findings, blank_lines):
    text = "\n" * blank_lines + verdict + "\n" + finding
    if no_findings:
        text += "\nFINDINGS: none"
    result = parse_review_result(text)
    expected = verdict in ("VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES") and (bool(finding) or no_findings)
    assert result["valid"] is expected


@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
@pytest.mark.parametrize("level", ["P0", "P1", "P2", "P3"])
def test_consistency_records_verdict_level_mismatches_without_changing_them(verdict, level):
    result = parse_review_result(f"VERDICT: {verdict.upper()}\n{level}: Summary")
    assert result["verdict"] == verdict
    assert result["levels"][level] == 1
    assert result["valid"] is True
    assert result["consistent"] is (level not in ("P0", "P1") if verdict == "approve" else level != "P3")
