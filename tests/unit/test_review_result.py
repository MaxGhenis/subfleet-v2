"""D-REVIEW-CONTRACT1: total parsing and the review's explicit format rules."""

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.review_result import (DEFINITION_OPENINGS, MAX_REVIEW_BYTES, parse_review_deliverable,
                                    parse_review_result)
from tests.unit.test_review_contract_prompt import DEFINITIONS

NONE = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}


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


@pytest.mark.parametrize("prefix", ["", "- ", "* ", "+ ", "#", "## ", "**", "- **", "### **", "   - **",
                                    "1. ", "12) **", "> ", "> - **", "_", "__"])
@pytest.mark.parametrize("separator", [":", " —", " –", " -", "**:", "** —", "** –", "** -"])
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


# A1 review r1, P2: shapes real reviews use for findings. Each left a levelled
# review invalid ("no levelled findings") under the first parser.
@pytest.mark.parametrize(("line", "level"), [
    ("1. P1: Gate rounds get the review preamble.", "P1"),
    ("1. **P2 — Bound the lookup's read snapshot.**", "P2"),
    ("### P2-1: A client that skips the CLI gets a generic refusal", "P2"),
    ("- **P3-1:** a nit in the report", "P3"),
    ("> P1: Quoted finding", "P1"),
    ("P2 – An en dash after the level", "P2"),
    ("**P2 – Bold, with an en dash**", "P2"),
    ("12. **P1 — Two-digit item**", "P1"),
    ("3) P0: Parenthesised ordered list", "P0"),
    ("> - **P3:** quoted list item", "P3"),
    ("P2 (change): `daemon.py:12006`, the parse holds the writer", "P2"),
    ("**P3-1 (UNPROVEN): a run can write more than its lock.**", "P3"),
    ("**P3-1. An unverifiable census still runs a protocol**", "P3"),
    ("P1: 2 tests fail under load", "P1"),
    ("P2: None of the callers check the result", "P2"),
])
def test_real_finding_shapes_are_counted(line, level):
    result = parse_review_result("VERDICT: REQUEST_CHANGES\n" + line + "\nDetail below.")
    assert result["levels"] == {key: int(key == level) for key in NONE}
    assert result["valid"] is True


# A1 review r1, P2: lines that are not findings. Each made an APPROVE review
# inconsistent under the first parser.
@pytest.mark.parametrize("text", [
    "```\nP1: inside a fenced code block\n```",
    "~~~~ text\nP0: inside a tilde fence\n~~~~",
    "- item\n\n  ```python\n  P1: fenced inside a list item\n  ```",
    "> ```\n> P1: fenced inside a quote\n> ```",
    "```\nP1: an unclosed fence runs to the end",
    "Paragraph:\n\n    P1: an indented code block",
    "\tP1: indented with a tab",
    "    - P2: nested four columns deep",
    "A sentence\u2028P1: after a line separator in mid-paragraph",
    "A sentence\x0cP1: after a form feed in mid-paragraph",
    "A sentence\x85P2: after a next-line character",
    "A sentence\rP1: after a bare carriage return",
    "- P0: harm that is live now or would be on merge (a leak, lost data, a guard that stops guarding).",
    "- P1: the change does something wrong (a wrong result, a false claim, a regression, a security hole).",
    "- **P2:** Right today but fragile or incomplete (a missing test).",
    "P3: Style and nits.",
    "P0: 0",
    "P1: 0, P2: 1, P3: 4",
    "- **P0:** none",
    "P2: no findings",
    "P3: 2.",
    "P0 – P1: none",
    "P0 - P3 are defined above",
    "**P3 · change · nits.**",
    "- **P2, stale expiry: closed.**",
    "3. **P3-1 closed:** acknowledged",
    "### P3",
    "`P1: in inline code`",
])
def test_code_separators_definitions_and_tallies_are_not_findings(text):
    result = parse_review_result("VERDICT: APPROVE\nFINDINGS: none\n" + text)
    assert result["levels"] == NONE
    assert result["valid"] is True and result["consistent"] is True


def test_the_preambles_own_definitions_echoed_are_not_findings():
    """Echoed definitions: a levelled line that opens with its own level's
    definition (DEFINITION_OPENINGS, case aside) repeats the legend."""
    for level, definition in zip(NONE, DEFINITIONS):
        assert definition.split(": ", 1)[1].casefold().startswith(DEFINITION_OPENINGS[level])
    text = "VERDICT: APPROVE\nFINDINGS: none\n\nLevels:\n" + "\n".join(f"- {line}" for line in DEFINITIONS)
    assert parse_review_result(text) == parse_review_result("VERDICT: APPROVE\nFINDINGS: none\n")
    # Another level's definition is not an echo of this one.
    assert parse_review_result("VERDICT: REQUEST_CHANGES\nP1: harm that is live now")["levels"]["P1"] == 1


def test_findings_none_inside_code_does_not_declare_none():
    for text in ("```\nFINDINGS: none\n```", "    FINDINGS: none", "\tFINDINGS: none"):
        result = parse_review_result("VERDICT: APPROVE\n" + text)
        assert result["valid"] is False and "no levelled findings" in result["reason"]


def test_a_fence_closes_only_on_its_own_marker():
    text = "VERDICT: REQUEST_CHANGES\n````\n```\nP1: still fenced\n````\nP2: after the fence"
    assert parse_review_result(text)["levels"] == {**NONE, "P2": 1}
    text = "VERDICT: REQUEST_CHANGES\n```\nP1: fenced\n~~~\nP1: still fenced\n```\nP2: after"
    assert parse_review_result(text)["levels"] == {**NONE, "P2": 1}
    # Backticks in a backtick fence's info string make it inline code, not a fence.
    assert parse_review_result("VERDICT: REQUEST_CHANGES\n``` a`b\nP1: counted")["levels"] == {**NONE, "P1": 1}


@pytest.mark.parametrize("prefix", ["- ", "* ", "+ ", "1. ", "12) ", "> 1. - ", "- > "])
@pytest.mark.parametrize("marker", ["```", "~~~~"])
def test_list_item_fences_hide_examples_and_close_before_real_findings(prefix, marker):
    """A1 review r2 P1: a list marker must not hide the opening fence."""
    text = (f"VERDICT: APPROVE\nFINDINGS: none\n\nExample:\n{prefix}{marker}text\n"
            f"  P0: only an example\n  {marker}\n\nP1: Actual regression\n")
    result = parse_review_result(text)
    assert result == {"verdict": "approve", "levels": {**NONE, "P1": 1},
                      "valid": True, "reason": None, "consistent": False}


@pytest.mark.parametrize(("prefix", "p1"), [
    (">     ", 0), ("> >     ", 0), (">\t", 1),
])
def test_indented_code_in_a_quote_is_not_a_finding(prefix, p1):
    result = parse_review_result(f"VERDICT: APPROVE\nFINDINGS: none\n{prefix}P1: code example")
    assert result["levels"] == {**NONE, "P1": p1}
    assert result["consistent"] is (p1 == 0)


@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES"])
@pytest.mark.parametrize(("line", "p1"), [
    pytest.param(">\t  P1: x", 0, id="tab-then-two-spaces"),
    pytest.param("> \tP1: x", 1, id="space-then-tab"),
    pytest.param(">   \tP1: x", 0, id="three-spaces-then-tab"),
])
def test_quoted_findings_use_four_column_tab_stops(verdict, line, p1):
    approval = verdict == "APPROVE"
    declaration = "FINDINGS: none\n" if approval else ""
    result = parse_review_result(f"VERDICT: {verdict}\n{declaration}{line}")
    assert result["levels"] == {**NONE, "P1": p1}
    assert result["valid"] is (approval or p1 == 1)
    assert result["consistent"] is (p1 == (0 if approval else 1))


@pytest.mark.parametrize(("line", "level"), [
    ("P0: 0 (none found)", None),
    ("- P1: 2 (both fixed in round 2)", None),
    ("P1: 1,000 jobs are dropped when the index rolls over", "P1"),
    ("P1: 500 (Internal Server Error) on every runs request", "P1"),
    ("P1: Zero (0) requests reach the handler after restart", "P1"),
    ("P1: 1. See below.", "P1"),
    ("P1: 500 (Internal Server Error)", None),
    ("P1: 2 (both fixed).", None),
    ("P1: 2 (both fixed) (more detail)", "P1"),
])
def test_only_a_complete_count_with_one_final_parenthetical_is_a_tally(line, level):
    """A1 review r3 P2: a trailing comment is allowed only after a complete count."""
    result = parse_review_result("VERDICT: APPROVE\nFINDINGS: none\n" + line)
    assert result == {"verdict": "approve", "levels": {**NONE, **({level: 1} if level else {})},
                      "valid": True, "reason": None, "consistent": level is None}


@pytest.mark.parametrize("summary", [
    "1,000 jobs are dropped when the index rolls over",
    "500 (Internal Server Error) on every runs request",
    "Zero (0) requests reach the handler after restart",
    "None, because every request fails",
    "2. Calls fail after restart",
    "0, jobs disappear",
    "0, P2: 2 jobs disappear",
])
def test_numeric_or_count_word_summaries_are_findings(summary):
    """A1 review r2 P1: only a complete tally expression is a tally."""
    result = parse_review_result(f"VERDICT: REQUEST_CHANGES\nP1: {summary}")
    assert result == {"verdict": "request_changes", "levels": {**NONE, "P1": 1},
                      "valid": True, "reason": None, "consistent": True}


@pytest.mark.parametrize("prefix", ["\ufeff", "\u200b", "\ufeff\u200b\u2060", "\u200b\n", "\ufeff\n\u200c\u200d\n",
                                    "\n\u200b \t\n"])
@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_a_byte_order_mark_and_zero_width_characters_before_the_verdict_are_ignored(prefix, verdict):
    """A1 review r1, P3: `\\ufeffVERDICT: APPROVE` and a zero-width first line gave no verdict."""
    result = parse_review_result(f"{prefix}VERDICT: {verdict.upper()}\u200b\nP2: Missing test")
    assert result["verdict"] == verdict and result["valid"] is True
    assert parse_review_deliverable(f"{prefix}VERDICT: {verdict.upper()}\nP2: x".encode())["verdict"] == verdict


def test_zero_width_characters_do_not_rescue_a_wrong_first_line():
    for text in ("\u200bREQUEST CHANGES", "\ufeff VERDICT: APPROVE", "\u200bIntro\nVERDICT: APPROVE"):
        assert parse_review_result(text + "\nP2: x")["verdict"] is None


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
@example(text="```\n\ufeff\u2028P1: x\x0c\x85~~~")
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
    newline=st.sampled_from(["\n", "\r\n"]),
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


@settings(max_examples=200, deadline=None, database=None, derandomize=True)
@given(payload=st.binary(max_size=4096))
def test_deliverable_parse_is_total_for_generated_bytes(payload):
    """I3: the daemon's entry point takes any captured bytes."""
    assert parse_review_deliverable(payload) == parse_review_result(payload.decode("utf-8", "replace"))


def test_a_deliverable_at_the_bound_is_read_whole():
    finding = b"P2: Missing test\n"
    payload = b"VERDICT: REQUEST_CHANGES\n" + b"x" * (MAX_REVIEW_BYTES - 26 - len(finding)) + b"\n" + finding
    assert len(payload) == MAX_REVIEW_BYTES
    result = parse_review_deliverable(payload)
    assert result["levels"] == {**NONE, "P2": 1} and result["valid"] is True


def test_a_deliverable_over_the_bound_is_read_to_a_line_break_and_recorded_invalid():
    """A1 review r1, P2: at most MAX_REVIEW_BYTES are decoded and parsed; what
    lies past the bound was never read, so the round is recorded invalid."""
    head = b"VERDICT: REQUEST_CHANGES\nP1: Counted before the bound\n"
    filler = b"x" * 100 + b"\n"
    body = head + filler * ((MAX_REVIEW_BYTES - len(head)) // len(filler))
    payload = body + b"P2: straddles the bound" + b"y" * 200 + b"\nP0: past the bound\n"
    result = parse_review_deliverable(payload)
    assert result["verdict"] == "request_changes"
    assert result["levels"] == {**NONE, "P1": 1}
    assert result["valid"] is False and result["consistent"] is True
    assert result["reason"] == (f"deliverable is {len(payload)} bytes, over the {MAX_REVIEW_BYTES}-byte bound; "
                                f"only its first {len(body)} bytes were read")
    no_verdict = parse_review_deliverable(b"intro\n" + payload)
    assert no_verdict["reason"].startswith("missing verdict on first non-empty line; deliverable is ")
    one_line = parse_review_deliverable(b"*# " * (MAX_REVIEW_BYTES // 3 + 1))
    assert one_line["valid"] is False and one_line["levels"] == NONE
