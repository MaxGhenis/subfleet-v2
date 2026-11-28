"""Read the review answer contract without deciding whether a job succeeds."""

from __future__ import annotations

import re

#: The most of an accepted deliverable the daemon reads (C-12.11, review r1 P2).
#: Hub reviews run to tens of kilobytes; a larger answer is parsed up to its
#: last line break inside this bound and recorded invalid, never read whole.
MAX_REVIEW_BYTES = 4 * 1024 * 1024

#: A byte-order mark and zero-width characters an editor or model can put
#: before the verdict line (review r1 P3). They never make a line non-empty.
_ZERO_WIDTH = "\ufeff\u200b\u200c\u200d\u2060"
_VISIBLE = str.maketrans("", "", _ZERO_WIDTH)

#: A finding's level at the start of a line (review r1 P2). Up to three spaces
#: of indentation (four columns or a tab is an indented code block, or content
#: nested under something else), then any run of containers: a blockquote `>`,
#: a list marker (`-`, `*`, `+`, `1.`, `1)`), a heading's `#`s, emphasis
#: (`**`, `*`, `__`, `_`). Then the level, an optional finding number (`P2-1`),
#: optional closing emphasis and an optional short parenthetical (`P2 (change)`),
#: then `:`, a `.` before a space, or a space and an em dash, en dash or hyphen.
#: Possessive repeats keep a run of marks linear in its length, and only a
#: line's first FINDING_PREFIX_CHARS characters are matched, so no line costs
#: more than that: a level that far into a line is not at its start.
FINDING_PREFIX_CHARS = 256
_FINDING = re.compile(
    r" {0,3}"
    r"(?:(?:>|[-*+](?=[ \t])|\d{1,9}[.)](?=[ \t])|#{1,6}|\*{1,2}|_{1,2})[ \t]*)*+"
    r"(P[0-3])(?:-\d{1,4})?(?![0-9A-Za-z])"
    r"[*_]*+"
    r"(?:[ \t]*\([^()\n]{1,80}\)[*_]*+)?"
    r"(?P<separator>:|\.(?=[ \t])|[ \t]+[\u2014\u2013-])"
)
#: What follows a level in a count or tally, not a finding: `P0: 0`,
#: `P1: none`, `P0: 0, P1: 2`, `P2: no findings`. A number that starts a
#: sentence (`P1: 2 tests fail`) is a finding's summary. Match the complete
#: expression, optionally ending in one parenthetical comment: punctuation
#: alone cannot turn a numeric summary into a tally.
_COUNT = r"(?:(?:\d++|none|zero)(?:[ \t]++findings?)?|no[ \t]++findings?)\b"
_TALLY = re.compile(
    rf"{_COUNT}[ \t*_]*+"
    rf"(?:(?:[,;|/\u00b7][ \t*_]*+)?P[0-3][ \t*_]*+:[ \t*_]*+{_COUNT}[ \t*_]*+)*"
    r"(?:\([^()\n]*\)[ \t*_]*+)?[.,;]?[ \t*_]*+", re.IGNORECASE,
)
#: A dash between two levels is a range (`P0 - P1: none`), not a finding.
_RANGE = re.compile(r"P[0-3]\b")
#: The opening of each level's own definition (C-12.11). A levelled line that
#: begins with it, case aside, repeats the preamble's legend: it is not a finding.
DEFINITION_OPENINGS = {
    "P0": "harm that is live now",
    "P1": "the change does something wrong",
    "P2": "right today but fragile",
    "P3": "style and nits",
}
#: A fenced code block's opener can follow quote or list markers. A closer
#: allows only whitespace and quote markers before its backticks or tildes:
#: a list marker inside a fence is content, as in a diff's removed closer.
_FENCE = re.compile(r"(?:[ \t>]|[-*+][ \t]++|\d{1,9}[.)][ \t]++)*+(`{3,}|~{3,})(.*)$")
#: Preserve the base closer rule independently of the opening containers.
_FENCE_CLOSE = re.compile(r"[ \t>]*(`{3,}|~{3,})(.*)$")
#: On a tab-expanded line, a quote marker removes at most one separator
#: column. Four further columns introduce code, just as outside a quote.
_INDENTED_CODE = re.compile(r"(?: {0,3}> ?)*+ {4}")
_NO_FINDINGS = re.compile(r" {0,3}FINDINGS: none[ \t]*$")


def _finding_level(line: str) -> str | None:
    """The level a line opens a finding with, or None.

    A level in the middle of a sentence, a tally, a range and an echoed
    definition are not findings.
    """
    prefix = line[:FINDING_PREFIX_CHARS]
    if _INDENTED_CODE.match(prefix.expandtabs(4)):
        return None
    match = _FINDING.match(prefix)
    if match is None:
        return None
    level = match.group(1)
    rest = line[match.end():].lstrip(" \t*_")
    if _TALLY.fullmatch(rest):
        return None
    if match.group("separator") not in (":", ".") and _RANGE.match(rest):
        return None
    if rest.casefold().startswith(DEFINITION_OPENINGS[level]):
        return None
    return level


def parse_review_result(text: str) -> dict:
    """Return the verdict and finding counts, including malformed reviews.

    Invalid format and contradictory severity are recorded separately. The
    caller, rather than the parser, decides whether a review counts as a round
    or blocks merging. Lines end at `\\n` alone (a `\\r` before it is dropped):
    a form feed, U+2028 or U+0085 in a paragraph does not start a line. Nothing
    inside a fenced code block is a finding or a `FINDINGS: none`.
    """
    verdict = None
    first_line_seen = False
    levels = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    no_findings = False
    fence = None
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if not first_line_seen and line.translate(_VISIBLE).strip():
            first_line_seen = True
            verdict = {
                "VERDICT: APPROVE": "approve",
                "VERDICT: REQUEST_CHANGES": "request_changes",
            }.get(line.lstrip(_ZERO_WIDTH).rstrip().rstrip(_ZERO_WIDTH).rstrip())
        if fence is not None:
            marker = _FENCE_CLOSE.match(line)
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) \
                    and not marker.group(2).strip():
                fence = None
            continue
        marker = _FENCE.match(line)
        if marker and not (marker.group(1)[0] == "`" and "`" in marker.group(2)):
            fence = marker.group(1)
            continue
        level = _finding_level(line)
        if level is not None:
            levels[level] += 1
        elif _NO_FINDINGS.match(line):
            no_findings = True

    reasons = []
    if verdict is None:
        reasons.append("missing verdict on first non-empty line")
    if not any(levels.values()) and not no_findings:
        reasons.append("no levelled findings or FINDINGS: none")
    consistent = not (
        verdict == "approve" and (levels["P0"] or levels["P1"])
        or verdict == "request_changes" and not (levels["P0"] or levels["P1"] or levels["P2"])
    )
    return {
        "verdict": verdict,
        "levels": levels,
        "valid": not reasons,
        "reason": "; ".join(reasons) if reasons else None,
        "consistent": consistent,
    }


def parse_review_deliverable(payload: bytes) -> dict:
    """Parse an accepted deliverable's bytes, reading at most MAX_REVIEW_BYTES.

    A larger deliverable is read up to its last line break inside the bound
    (all of the bound if it has none), counted from that, and recorded
    invalid with the reason, since findings past the bound were never read.
    """
    if len(payload) <= MAX_REVIEW_BYTES:
        return parse_review_result(payload.decode("utf-8", "replace"))
    head = payload[:MAX_REVIEW_BYTES]
    cut = head.rfind(b"\n")
    if cut >= 0:
        head = head[:cut + 1]
    result = parse_review_result(head.decode("utf-8", "replace"))
    over = (f"deliverable is {len(payload)} bytes, over the {MAX_REVIEW_BYTES}-byte bound; "
            f"only its first {len(head)} bytes were read")
    result["reason"] = f"{result['reason']}; {over}" if result["reason"] else over
    result["valid"] = False
    return result
