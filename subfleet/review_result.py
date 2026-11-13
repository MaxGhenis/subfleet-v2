"""Read the review answer contract without deciding whether a job succeeds."""

from __future__ import annotations


def _finding_level(line: str) -> str | None:
    """Recognize a finding at the start of a possibly Markdown-decorated line.

    Scan prefixes rather than searching for a level in prose. Advancing a
    cursor also keeps long, malformed runs of Markdown marks linear in size.
    """
    index = 0
    length = len(line)
    while index < length:
        mark = line[index]
        if mark in " \t*#":
            index += 1
        elif mark == "-" and index + 1 < length and line[index + 1] in " \t":
            index += 1
        else:
            break
    level = line[index:index + 2]
    if level not in ("P0", "P1", "P2", "P3"):
        return None
    index += 2
    # An author may bold just the level or the whole summary.
    while index < length and line[index] == "*":
        index += 1
    if index < length and line[index] == ":":
        return level
    separator = index
    while index < length and line[index] in " \t":
        index += 1
    if index > separator and index < length and line[index] in "—-":
        return level
    return None


def parse_review_result(text: str) -> dict:
    """Return the verdict and finding counts, including malformed reviews.

    Invalid format and contradictory severity are recorded separately. The
    caller, rather than the parser, decides whether a review counts as a round
    or blocks merging.
    """
    verdict = None
    first_line_seen = False
    levels = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    no_findings = False
    for line in text.splitlines():
        if not first_line_seen and line.strip():
            first_line_seen = True
            verdict = {
                "VERDICT: APPROVE": "approve",
                "VERDICT: REQUEST_CHANGES": "request_changes",
            }.get(line.rstrip())
        level = _finding_level(line)
        if level is not None:
            levels[level] += 1
        if line.strip() == "FINDINGS: none":
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
