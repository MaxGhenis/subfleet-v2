"""A margin file that ends in a reminder keeps its last lines (#233 review r6, P3-A).

`daemon_log._reminders_resolved` reads a file that holds a group's margin twice: as it is,
and without its margins (`_margin_free`), as the block form also reads it. It used what is
left only where the margin-free reading with its reminders taken out was exactly the
margin-free reading of what is left. `_margin_free` strips a text's trailing line breaks, as
`_group_block` reads text. So where reminders end the file, the first keeps the line break
before them and the second does not, and the file kept only the cut's marker for that alone
(600 admission lines, `  | member line`, then `<system-reminder>note</system-reminder>`).

They are now compared with their trailing line breaks stripped (`daemon_log._alike`), except
where the margin-free reading of what is left ends in a data URI's body short enough for
those breaks to count. Only `_DATA_URI_RE` reads such a break: its body is 32 or more of
`[A-Za-z0-9+/=\\s]`, line breaks included. With the breaks stripped alone (review r6's fix
direction), a body of 31 characters before a reminder that ends the file was hidden by the
whole-text scrub, which counts the break before the reminder, and shown by the tail, whose
block form strips it: `SECRET7dataend9` at `daemon logs -n 2`. That file keeps the marker.

What is held: no tail shows a secret the whole-text scrub hides; a margin file ending in a
reminder is refused only where the quoted pass's masks differ or a data URI's body reaches
the break, never for a trailing break alone.
"""
from __future__ import annotations

import random
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet import cli, daemon_log
from subfleet.sessions import handoff
from tests import reference_tail_1cf653dff as round5
from tests.unit.test_daemon_log_reminders import TAG, admissions, secrets, tags

BOUNDED = settings(max_examples=150, derandomize=True, database=None, deadline=None)
MEMBER = "  | member line\n"
#: How the whole-text scrub strips a reminder, for what the tail should show.
WHOLE_REMINDER = re.compile(r"(?is)<system-reminder>.*?</system-reminder>")

#: Review r6's three endings (P3-A), each after 600 admission lines and a member line, and
#: two more of the same shape: a reminder that ends the file with no break after it, and CRLF.
ENDINGS = {
    "multi-line": "<system-reminder>\nnote\n</system-reminder>\n",
    "one-line": "<system-reminder>note</system-reminder>\n",
    "one-line-blank-line": "<system-reminder>note</system-reminder>\n\n",
    "no-break": "<system-reminder>note</system-reminder>",
    "crlf": "<system-reminder>note</system-reminder>\r\n",
}


def margin_log(ending: str) -> str:
    return admissions(600) + MEMBER + ending


def reference(text: str) -> set[str]:
    """The secrets the whole text's scrub shows: its block with its lines kept."""
    return secrets(daemon_log.scrubbed_block(text, group=daemon_log._GROUP_MARK in text, keep_lines=True))


# --- the regressions: each kept only the marker at c2e080e44 -----------------------------

@pytest.mark.parametrize("count", [1, 3, 8])
@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_a_margin_file_ending_in_a_reminder_keeps_its_last_lines(ending, count):
    """Review r6, P3-A: each resolved to None, and `scrubbed_tail(text, 3)` was `['… [31,748
    characters omitted] …']`. The reminder is taken out, and the tail is the last lines, as
    round 5 shows the same file with the reminder stripped."""
    text = margin_log(ENDINGS[ending])
    assert len(text) > daemon_log._tail_limit(text, count)
    stripped = WHOLE_REMINDER.sub("", text)
    assert daemon_log._reminders_resolved(text) == stripped
    tail = daemon_log.scrubbed_tail(text, count)
    assert tail == round5.scrubbed_tail(stripped, count)
    assert len(tail) == count and tail[-1] == "  | member line"
    if count > 1:
        assert tail[-2] == "2026-10-20 admission: placed job 599 on lane codex-0"
    assert not tags(tail) and not secrets("\n".join(tail))


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_daemon_logs_and_daemon_start_s_tail_print_the_last_lines(ending, root, capsys, tmp_path):
    """The same through `daemon logs -n 3` and `daemon start`'s tail (`cli._log_tail`)."""
    text = margin_log(ENDINGS[ending])
    (root / "daemon.log").write_text(text, newline="")
    assert cli.main(["daemon", "logs", "-n", "3"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == daemon_log.scrubbed_tail(text, 3)
    assert printed == ["2026-10-20 admission: placed job 598 on lane codex-0",
                       "2026-10-20 admission: placed job 599 on lane codex-0", "  | member line"]
    path = tmp_path / "daemon.log"
    path.write_text(text, newline="")
    assert cli._log_tail(path, 3).splitlines() == [f"  | {line}" for line in printed]


@pytest.mark.parametrize("text", [admissions(600) + ENDINGS["one-line"],
                                  margin_log(ENDINGS["one-line"]) + "after\n"], ids=["no-margin", "after"])
def test_the_shapes_round_7_resolved_still_resolve(text):
    """Review r6's controls: no margin line, or a line after the reminder. Resolved at
    c2e080e44 and now, the reminder taken out."""
    assert daemon_log._reminders_resolved(text) == WHOLE_REMINDER.sub("", text)
    assert daemon_log.scrubbed_tail(text, 3) == round5.scrubbed_tail(WHOLE_REMINDER.sub("", text), 3)


# --- the exception: a data URI's body the break before the reminder completes ------------

#: 20 and 10 body characters on two member lines: 31 read without margins, 32 with the line
#: break before a reminder that ends the file, which the whole-text scrub's margin-free
#: reading keeps (`_DATA_URI_RE`: `[A-Za-z0-9+/=\s]{32,}`).
BODY = "  | data:image/png;base64,SECRET7dataend9QUJDR\n  | QUJDREVGQU\n"
DATA_ENDINGS = {
    "lf": BODY + "<system-reminder>r</system-reminder>\n",
    "no-break": BODY + "<system-reminder>r</system-reminder>",
    "two-reminders": BODY.replace("QUJDREVGQU", "QUJDREVGQ") + "<system-reminder>r</system-reminder>\n"
                     "<system-reminder>s</system-reminder>\n",
    "crlf": (BODY.replace("QUJDREVGQU", "QUJDREVGQ") + "<system-reminder>r</system-reminder>\n").replace("\n", "\r\n"),
}


@pytest.mark.parametrize("count", [2, 3, 8])
@pytest.mark.parametrize("ending", sorted(DATA_ENDINGS))
def test_a_data_uri_body_the_trailing_break_completes_keeps_the_marker_rule(ending, count):
    """The whole-text scrub hides `SECRET7dataend9`: its margin-free reading, the reminder
    stripped, ends in the body and the break before the reminder, 32 characters. The block
    form of what is left reads it without that break, 31, and shows it. With the trailing
    breaks stripped from both sides alone, each tail here showed `SECRET7dataend9` (the
    probe's run at c2e080e44); the file keeps round 5's rule: only the marker."""
    text = admissions(600) + MEMBER + DATA_ENDINGS[ending]
    assert reference(text) == set()
    assert daemon_log._reminders_resolved(text) is None
    tail = daemon_log.scrubbed_tail(text, count)
    assert tail == [handoff.cut_marker(len(text))] and not tags(tail)


def test_a_data_uri_body_too_long_for_the_break_to_count_is_still_resolved():
    """Only a body the break could complete is refused: one of 40 characters matches with
    and without it, and the file keeps its last lines, the body hidden."""
    text = admissions(600) + MEMBER + "  | data:image/png;base64,SECRET7longbody9QUJDREVGQUJDREVGQUJDREVGQU\n" \
        "<system-reminder>r</system-reminder>\n"
    assert daemon_log._reminders_resolved(text) == WHOLE_REMINDER.sub("", text)
    tail = daemon_log.scrubbed_tail(text, 3)
    assert len(tail) == 3 and not secrets("\n".join(tail)) and reference(text) == set()


@pytest.mark.parametrize("left,kept,alike", [
    ("a\nmember line\n", "a\nmember line", True),
    ("a\nmember line\n\r\n", "a\nmember line", True),
    ("a\nmember line\n", "a\nmember line\n", True),
    ("a\nmember line\n", "a\nmember lin", False),
    ("a\n\nmember line", "a\nmember line", False),
    ("x data:a/b;base64,QUJD\n", "x data:a/b;base64,QUJD", False),
    ("x data:a/b;BASE64,QUJD\nQUJD\n", "x data:a/b;BASE64,QUJD\nQUJD", False),
    ("x data:a/b;base64," + "Q" * 31 + "\n", "x data:a/b;base64," + "Q" * 31, False),
    ("x data:a/b;base64," + "Q" * 32 + "\n", "x data:a/b;base64," + "Q" * 32, True),
    ("x data:a/b;base64,QUJD!\n", "x data:a/b;base64,QUJD!", True),
    ("x data:a/b;base64,QUJD", "x data:a/b;base64,QUJD", True),
])
def test_alike_strips_trailing_breaks_unless_a_data_uri_body_could_count_them(left, kept, alike):
    """`_alike`: equal, or equal but for trailing line breaks where no data URI body within
    31 characters of the end could count them."""
    assert daemon_log._alike(left, kept) is alike


# --- the property: refused only where the masks differ or a body reaches the break --------

#: Lines of an exception group's member, as a margin file holds them: values a quoted pass
#: masks or leaves, keys the margins part (a value only the margin-free reading opens, which a
#: quote in the final reminder closes there alone), orphan closing tags, and plain lines.
MARGIN_LINES = [
    "  | member line\n", "  +-+---------------- 1 ----------------\n", "    | Traceback (most recent call last):\n",
    "  | token: '§'\n", "  | password='§\n", "  | api_key=§ rest\n", "  | note 'x\n", "  | said: \"§\"\n",
    "  | Authorization: Bearer §\n", "  | --auth-token §\n", "  | token\n  | ='§'\n", "  | x=\"§\n",
    "  | token\n  | ='§\n", "  | api_key\n  | =\"§\n",
    "  | </system-reminder>\n", "plain words only\n", "  |\n", "  | \n", "    + more\n",
]
#: The reminder that ends the file, each on its own line: quotes, margins and folds inside.
FINAL_REMINDERS = [
    "<system-reminder>note</system-reminder>", "<system-reminder>\nnote\n</system-reminder>",
    "<SYSTEM-REMINDER>x</System-Reminder>", "<ſystem-reminder>'</system-remınder>",
    '<system-reminder>"</system-reminder>', "<system-reminder>a\n  | b</system-reminder>",
    "<system-reminder>§</system-reminder>",
]
TRAILING = ["", "\n", "\n\n", "\r\n", "\n\n\n", "\n \n"]
FILLER = "QUJDREVGQUJDREVGQUJDREVGQUJDREVGQUJDREVG"


def data_lines(rng: random.Random) -> str:
    """A data URI on member lines whose body, read without margins, is 20 to 40 characters,
    the 32 `_DATA_URI_RE` needs among them, one secret at its start."""
    body = ("SECRET7tail9" + FILLER)[:rng.randint(20, 40)]
    split = rng.randint(1, len(body) - 1)
    return f"  | data:image/png;base64,{body[:split]}\n  | {body[split:]}\n" if rng.random() < 0.7 \
        else f"  | data:image/png;base64,{body}\n"


def margin_reminder_text(rng: random.Random) -> str:
    """A margin file that ends in a reminder: member lines (`MARGIN_LINES`, a data URI's
    body among them half the time, often last), then a reminder on its own line and trailing
    line breaks, CRLF a fifth of the time. Each `§` is a secret of its own.
    `tools/reminder_join_census.py` draws the same texts."""
    lines = [MEMBER] + [rng.choice(MARGIN_LINES) for _ in range(rng.randint(0, 8))]
    if rng.random() < 0.5:
        lines.insert(rng.randint(1, len(lines)) if rng.random() < 0.4 else len(lines), data_lines(rng))
    text = "".join(lines) + rng.choice(FINAL_REMINDERS) + rng.choice(TRAILING)
    if rng.random() < 0.2:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    pieces = text.split("§")
    return "".join(piece + (f"SECRET7m{index}x9" if index < len(pieces) - 1 else "")
                   for index, piece in enumerate(pieces))


margin_reminder_logs = st.integers(0, 2 ** 32 - 1).map(lambda seed: margin_reminder_text(random.Random(seed)))
#: A body character as `_DATA_URI_RE` reads one, and its `;base64,`.
BODY_CHAR = re.compile(r"(?i)[A-Za-z0-9+/=\s]")
BASE64_MARK = re.compile(r"(?i);base64,")


def body_reaches_the_end(text: str) -> bool:
    """Whether `text` ends in `;base64,` and at most 31 body characters, read back a
    character at a time: a body the line breaks after it could bring to 32."""
    run = 0
    while run < len(text) and run < 32 and BODY_CHAR.fullmatch(text[len(text) - run - 1]):
        run += 1
    return run < 32 and BASE64_MARK.fullmatch(text, max(0, len(text) - run - 8), len(text) - run) is not None


def refusal_reasons(text: str) -> set[str]:
    """Why `text`, a margin file, would keep only the marker, read from the guard's parts:
    `masks`, where the quoted pass over what is left (as it is, or read without margins)
    replaces other than what it replaces in the file outside the reminders, or the two
    readings take out other reminders (`_taken_out`); `data-uri`, where the margin-free
    reading of what is left ends in a body the trailing breaks could complete; `texts`,
    where the two readings of what is left differ by more than trailing line breaks; and
    `tag`, where taking reminders out makes one."""
    tags = handoff.reminder_tags(text)
    taken = daemon_log._taken_out(text, tags)
    bare = daemon_log._margin_free(text)
    bare_tags = handoff.reminder_tags(bare)
    bare_taken = daemon_log._taken_out(bare, bare_tags)
    if taken is None or bare_taken is None or len(bare_tags) != len(tags) or bare_taken[1] != taken[1]:
        return {"masks"}
    kept, pairs = taken
    left, read = bare_taken[0], daemon_log._margin_free(kept)
    reasons = set()
    if left.rstrip("\r\n") != read.rstrip("\r\n"):
        reasons.add("texts")
    elif left != read and body_reaches_the_end(read.rstrip("\r\n")):
        reasons.add("data-uri")
    if len(handoff.reminder_tags(kept)) != len(tags) - sum(closing - opening + 1 for opening, closing in pairs):
        reasons.add("tag")
    return reasons


@BOUNDED
@given(text=margin_reminder_logs, count=st.sampled_from([1, 3, 8]))
def test_a_margin_file_ending_in_a_reminder_is_refused_only_where_the_masks_differ(text, count):
    """Over margin files that end in a reminder on its own line: the resolution is refused
    exactly where the quoted pass's masks differ or a data URI's body reaches the trailing
    break, never for a trailing line break alone (at c2e080e44, also for that). Taking out
    a reminder that ends the file changes neither reading but at its end, and makes no tag.
    Whatever is resolved, no tail shows a secret the whole-text scrub hides."""
    whole = admissions(600) + text
    reasons = refusal_reasons(whole)
    assert reasons <= {"masks", "data-uri"}, (reasons, text)
    resolved = daemon_log._reminders_resolved(whole)
    assert (resolved is None) == bool(reasons), (reasons, text)
    tail = daemon_log.scrubbed_tail(whole, count)
    assert not tags(tail), (text, count, tail)
    assert secrets("\n".join(tail)) <= reference(whole), (text, count, tail)
    if resolved is not None:
        assert reference(resolved) <= reference(whole), text


def test_the_margin_files_resolve_and_fall_back_for_each_reason():
    """The property's texts are not vacuous: most resolve, and each reason for keeping the
    marker occurs; at c2e080e44 every one whose readings differ only at its end fell back."""
    reasons = [refusal_reasons(admissions(10) + margin_reminder_text(random.Random(seed))) for seed in range(400)]
    resolved = sum(not found for found in reasons)
    assert resolved >= 200, resolved
    assert sum("masks" in found for found in reasons) >= 5
    assert sum("data-uri" in found for found in reasons) >= 10
    assert not any(found - {"masks", "data-uri"} for found in reasons)
    differ = 0
    for seed in range(400):
        text = admissions(10) + margin_reminder_text(random.Random(seed))
        taken = daemon_log._taken_out(text, handoff.reminder_tags(text))
        bare = daemon_log._margin_free(text)
        bare_taken = daemon_log._taken_out(bare, handoff.reminder_tags(bare))
        if taken and bare_taken and bare_taken[0] != daemon_log._margin_free(taken[0]):
            differ += 1
    assert differ >= 100, differ


def test_no_tag_is_left_in_what_is_resolved():
    """What the resolution gives the cut holds no tag: the reminder taken out, any orphan
    marked."""
    for seed in range(200):
        text = admissions(10) + margin_reminder_text(random.Random(seed))
        resolved = daemon_log._reminders_resolved(text)
        assert resolved is None or not TAG.search(resolved), text
