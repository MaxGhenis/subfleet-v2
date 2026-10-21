"""No reminder tag blanks what a person reads of daemon.log, or reaches it (C-25.7, #233 review r4, P3-2).

Round 5's cut keeps only its marker for any text that holds a reminder tag, and
`scrubbed_tail` cut daemon.log with it. daemon.log is never rewritten, and the handler
wrote an orphan tag through (`turn failed: model said: <system-reminder>…`), so one such
line left `daemon start`'s tail and `daemon logs` at its default 40 lines only the marker,
for good. Round 6:

- the handler writes `daemon_log.REMINDER_MARK` in place of every tag the scrub leaves, so
  daemon.log this daemon writes holds no tag;
- the tail and `daemon logs` (both modes) read a file that must be cut with its reminders
  resolved first (`daemon_log._reminders_resolved`): each reminder the whole-text scrub
  would strip taken out, as that scrub finds it, and every other tag marked. The cut then
  meets no tag, and a tagged log keeps its last N lines;
- `daemon logs --follow` also holds a private key's first line until it can no longer
  become one (review r4's note: `-----BEGIN RSA`, then `PRIVATE KEY-----`).

What is held: no tag in any output; no output shows a secret the whole-text scrub hides;
and on a log with no tag the output is round 5's, byte for byte
(`tests/reference_tail_1cf653dff.py`).
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
from tests.unit import test_daemon_log_cut_windows as windows
from tests.unit import test_daemon_log_handler as handler_tests

#: The marker (`daemon_log.REMINDER_MARK`); named here too so these tests run, and fail on
#: their own assertions, against round 5's code.
MARK = getattr(daemon_log, "REMINDER_MARK", "[reminder-tag]")
#: Either tag, in any case and fold: the scrubber's own `handoff._REMINDER_TAG_RE`.
TAG = re.compile(r"</?system-reminder>", re.IGNORECASE)
SECRET_RUN = re.compile(r"SECRET7[a-z0-9]*")
BOUNDED = settings(max_examples=120, derandomize=True, database=None, deadline=None)


def secrets(text: str) -> set[str]:
    return set(SECRET_RUN.findall(text))


def tags(lines) -> list[str]:
    return [match.group() for line in lines for match in TAG.finditer(line)]


def admissions(count: int, start: int = 0) -> str:
    return "".join(f"2026-10-20 admission: placed job {n} on lane codex-0\n" for n in range(start, start + count))


#: The tag forms the brief names, each as a first line would hold it: a whole reminder,
#: an orphan opening and closing tag, other cases, and the folds the regex reads (`ſ` for
#: `s`, `ı` and `İ` for `i`); and review r4's handler line.
TAG_FORMS = {
    "complete-block": "<system-reminder>note</system-reminder>",
    "complete-block-folded": "<ſystem-reminder>note</SYSTEM-REMİNDER>",
    "orphan-open": "<system-reminder>",
    "orphan-close": "</system-reminder>",
    "upper-case-open": "<SYSTEM-REMINDER>",
    "mixed-case-close": "</System-Reminder>",
    "long-s-open": "<ſystem-reminder>",
    "dotless-i-close": "</system-remınder>",
    "dotted-capital-i-open": "<system-remİnder>",
    "handler-line": "turn failed: model said: <system-reminder>The user's excerpt was cut",
}
#: What the tail shows of each form: a whole reminder is stripped, as the scrub strips it;
#: every other tag is the marker.
SHOWN = {name: TAG.sub(MARK, re.sub(r"(?is)<system-reminder>.*?</system-reminder>", "", form))
         for name, form in TAG_FORMS.items()}

#: What a daemon before D-LOG1 left raw (`test_daemon_log_tail.PRE_D_LOG1`'s shapes).
OLDER = ("2026-10-01 an older daemon said: password=SECRET7older9\n"
         "2026-10-01 its next record: token:\nSECRET7nextline9 and more\n"
         "Authorization: Bearer SECRET7header9value\n"
         "guardian argv: subfleet-guardian --auth-token\nSECRET7flagline9 --lane codex-0\n")


def tagged_log(first: str) -> str:
    """Review r4's log, about 25 KB: `first` as its first line, then admission lines and
    raw records of an older daemon among them."""
    return first + "\n" + admissions(300) + OLDER * 3 + admissions(230, 300) + OLDER * 3 + admissions(25, 530)


# --- the regressions: each fails at 1cf653dff ----------------------------------------------

@pytest.mark.parametrize("count", [5, 14, 40])
@pytest.mark.parametrize("form", sorted(TAG_FORMS))
def test_a_tag_on_the_first_line_no_longer_blanks_the_tail(form, count):
    """Review r4, P3-2: with any tag form on line 1 of a 25 KB log, round 5's tail was one
    line, the marker. It is the log's last lines, scrubbed as the same log with that line
    as the tail shows it gives them."""
    text = tagged_log(TAG_FORMS[form])
    assert len(text) > 24_000
    tail = daemon_log.scrubbed_tail(text, count)
    assert tail == round5.scrubbed_tail(tagged_log(SHOWN[form]), count)
    assert len(tail) == count and tail[-1] == "2026-10-20 admission: placed job 554 on lane codex-0"
    assert not tags(tail) and not secrets("\n".join(tail))


@pytest.mark.parametrize("count", [5, 14, 40])
@pytest.mark.parametrize("form", ["complete-block", "orphan-open", "mixed-case-close", "long-s-open", "handler-line"])
def test_daemon_logs_keeps_its_lines_after_a_tagged_first_line(form, count, root, capsys):
    """The same through the CLI: `daemon logs -n 5`, `-n 14` and `-n 40` each printed only
    `… [22,884 characters omitted] …` (review r4). They print the last lines."""
    (root / "daemon.log").write_text(tagged_log(TAG_FORMS[form]))
    assert cli.main(["daemon", "logs", "-n", str(count)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == round5.scrubbed_tail(tagged_log(SHOWN[form]), count)
    assert len(lines) == count and not tags(lines) and "SECRET7" not in "\n".join(lines)


@pytest.mark.parametrize("form", sorted(TAG_FORMS))
def test_a_tag_among_the_last_lines_is_its_marker_and_a_whole_reminder_is_stripped(form, tmp_path):
    """A tag in the lines the tail shows: the line is kept, the tag the marker, a whole
    reminder taken out as the scrub takes it out. `daemon start`'s tail too."""
    text = admissions(600) + f"before {TAG_FORMS[form]} after\n" + admissions(3, 600)
    tail = daemon_log.scrubbed_tail(text, 5)
    assert tail[0] == "2026-10-20 admission: placed job 599 on lane codex-0"
    assert tail[1] == f"before {SHOWN[form]} after" and not tags(tail)
    path = tmp_path / "daemon.log"
    path.write_text(text)
    assert cli._log_tail(path, 5).splitlines() == [f"  | {line}" for line in tail]


@pytest.mark.parametrize("form", sorted(TAG_FORMS))
def test_a_log_too_short_to_cut_shows_each_tag_as_the_marker(form, root, capsys):
    """A log short enough that the tail scrubs it whole is not resolved before a cut: the
    scrub strips a whole reminder, and every tag it leaves is marked after the scrub (round
    5 showed an orphan tag as it was). `daemon logs` prints the same lines."""
    text = admissions(3) + f"before {TAG_FORMS[form]} after\n" + admissions(2, 3)
    assert len(text) < daemon_log.RECORD_SCRUB_MAX
    tail = daemon_log.scrubbed_tail(text, 40)
    assert len(tail) == 6 and tail[3] == f"before {SHOWN[form]} after" and not tags(tail)
    (root / "daemon.log").write_text(text)
    assert cli.main(["daemon", "logs", "-n", "40"]) == 0
    assert capsys.readouterr().out.splitlines() == tail


@pytest.mark.parametrize("form", sorted(TAG_FORMS))
def test_the_handler_writes_no_reminder_tag(form):
    """Review r4, P3-2: the handler wrote an orphan tag through. Every tag its scrub leaves
    is now the marker, so daemon.log this daemon writes holds none; a whole reminder is
    stripped, as before."""
    log = handler_tests.Written("subfleet.daemon.test-reminders")
    try:
        log.log.error("turn failed: model said: %s", TAG_FORMS[form])
        log.log.error("several lines:\n%s\nand a value token=SECRET7tagline9", TAG_FORMS[form])
        written = log.text()
    finally:
        log.close()
    assert not TAG.search(written), written
    assert f"turn failed: model said: {SHOWN[form]}".rstrip() in written
    assert "SECRET7" not in written
    assert (MARK in written) is (MARK in SHOWN[form])


def test_an_orphan_tag_reaches_daemon_log_as_the_marker_through_a_real_handler(tmp_path):
    """Through a file, as daemon.log is written: review r4's line, then its tail."""
    import logging
    path = tmp_path / "daemon.log"
    handler = daemon_log.ScrubbingStreamHandler(path.open("a"))
    log = logging.getLogger("subfleet.daemon.test-reminder-file")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        log.error("turn failed: model said: <system-reminder>The user's excerpt was cut")
        for n in range(600):
            log.info("admission: placed job %d on lane codex-0", n)
    finally:
        log.removeHandler(handler)
        handler.stream.close()
    text = path.read_text()
    assert not TAG.search(text) and text.splitlines()[0].endswith(f"model said: {MARK}The user's excerpt was cut")
    assert daemon_log.scrubbed_tail(text, 40) == text.splitlines()[-40:]


def follow_feed(chunks: list[str]) -> list[str]:
    follower = daemon_log.LogFollower("start\n")
    written = []
    for chunk in chunks:
        written += follower.feed(chunk)
    return written + follower.flush()


def test_a_private_key_s_first_line_is_held_until_it_can_no_longer_become_one():
    """Review r4's note: `-----BEGIN RSA` settled before `PRIVATE KEY-----` arrived, so only
    the context limit covered it. The line is held, as the other holds are."""
    follower = daemon_log.LogFollower("start\n")
    assert follower.feed("-----BEGIN RSA\n") == []
    assert follower.feed("PRIVATE KEY-----\nSECRET7pemsplit9\n") == []
    written = follower.feed("-----END RSA PRIVATE KEY-----\nnext\n")
    assert written[-1] == "next" and "SECRET7" not in "\n".join(written)
    # A first line that cannot become one any more is written when the next line comes.
    follower = daemon_log.LogFollower("start\n")
    assert follower.feed("-----BEGIN RSA\n") == []
    assert follower.feed("lower case words\n") == ["-----BEGIN RSA", "lower case words"]


def test_a_split_first_line_past_the_context_still_hides_its_key():
    """Review r4's limit: with 8,300 or more blank lines between `-----BEGIN RSA` and
    `PRIVATE KEY-----`, the key showed (only the 8,192-character context covered it). Held
    now, the whole key is written together, hidden."""
    written = follow_feed(["-----BEGIN RSA\n", "\n" * 9000, "PRIVATE KEY-----\nSECRET7farpem9\n",
                           "-----END RSA PRIVATE KEY-----\n", "next\n"])
    assert written[-1] == "next" and "SECRET7" not in "\n".join(written)


def test_daemon_logs_follow_holds_a_split_first_line(root, monkeypatch, capsys):
    """Through `daemon logs -f`, the pieces written one at a time."""
    from tests.unit.test_daemon_log_tail import follow
    (root / "daemon.log").write_text(admissions(5))
    follow(root, monkeypatch, ["-----BEGIN RSA\n", "\n" * 9000, "PRIVATE KEY-----\nSECRET7clipem9\n",
                               "-----END RSA PRIVATE KEY-----\n", "done\n", "a <system-reminder> line\n"])
    out = capsys.readouterr().out
    assert "SECRET7" not in out and not TAG.search(out)
    assert out.splitlines()[-1] == f"a {MARK} line"


# --- where the scrub masks before it strips --------------------------------------------------

#: A reminder the whole-text scrub reads only after its quoted pass: the pass masks the first
#: closing tag inside `token='…'`, so the reminder runs to the second, over the secret.
MASKED_CLOSE = "<system-reminder> token='x</system-reminder>y' SECRET7masked9 </system-reminder> after\n"
#: And one it never strips: the opening tag is inside a masked value, so it is no tag there.
MASKED_OPEN = "password='x <system-reminder>' kept SECRET7plain9 </system-reminder> after\n"


@pytest.mark.parametrize("line", [MASKED_CLOSE, MASKED_OPEN])
@pytest.mark.parametrize("where", ["first", "last"])
def test_a_tag_a_quoted_value_masks_is_none_to_the_strip(line, where):
    """Review r4: the whole-text scrub masks quoted values across lines before it strips
    reminders, so stripping before the cut must read them in that order. The tail hides
    what the whole text's scrub hides, and shows no tag."""
    text = line + admissions(600) if where == "first" else admissions(600) + line
    # Three lines of a 601-line log: the tail must cut, so it resolves the reminders first
    # (a count that covers the whole log would scrub it whole, and test nothing of that): the
    # log is longer than what the tail cuts it to, the bound and twice its last three lines.
    assert len(text) > daemon_log.RECORD_SCRUB_MAX + 2 * len("".join(text.splitlines(True)[-3:]))
    whole = daemon_log.scrubbed_block(text, keep_lines=True)
    tail = daemon_log.scrubbed_tail(text, 3)
    assert len(tail) == 3 and secrets("\n".join(tail)) <= secrets(whole) and not tags(tail)
    assert ("SECRET7masked9" in whole) is False
    if where == "last":
        assert tail[-1] == TAG.sub(MARK, whole.splitlines()[-1])


def test_where_the_margin_free_reading_strips_other_reminders_the_marker_rule_stays():
    """The block form also reads a group's text without its margins. There, `token` and
    the `=` the margin parted are one key, whose quoted value masks the opening tag, so that
    reading strips no reminder where the other strips one. The tail cannot say which, and
    keeps round 5's rule for it: only the marker, never the text."""
    line = "  | token\n  | ='<system-reminder>' SECRET7group9 </system-reminder>\n"
    text = "  +-+---------------- 1 ----------------\n" + line + admissions(600)
    assert daemon_log._reminders_resolved(text) is None
    tail = daemon_log.scrubbed_tail(text, 5)
    assert len(tail) == 1 and tail[0].startswith("… [") and not tags(tail)


def test_where_taking_a_reminder_out_makes_a_tag_the_marker_rule_stays():
    """`<sys` and `tem-reminder>` either side of a reminder: stripped, they join into a tag
    the scrub's second pass would read. The tail keeps round 5's rule for it."""
    text = "<sys<system-reminder>x</system-reminder>tem-reminder>\n" + admissions(600) + "</system-reminder>\n"
    assert daemon_log._reminders_resolved(text) is None
    tail = daemon_log.scrubbed_tail(text, 5)
    assert len(tail) == 1 and tail[0].startswith("… [") and not tags(tail)


# --- the C scans against the regex ---------------------------------------------------------

SMALL = st.text(alphabet=list("<>/system-reminder") + ["ſ", "ı", "İ", "S", "R", "E", "\n", " "], max_size=60)


@settings(max_examples=2000, derandomize=True, database=None, deadline=None)
@given(text=SMALL)
def test_reminder_tags_are_the_tag_regex_s_matches(text):
    """`handoff.reminder_tags`, C scans, finds exactly what `_REMINDER_TAG_RE` finds; and
    after `neutralised` no tag is left, whatever the text."""
    assert handoff.reminder_tags(text) == [(m.start(), m.end(), m.group()[1] != "/")
                                           for m in handoff._REMINDER_TAG_RE.finditer(text)]
    assert not TAG.search(daemon_log.neutralised(text))


@BOUNDED
@given(lines=st.lists(st.one_of(st.sampled_from(handler_tests.FUZZ_FILLER).map(lambda line: line + "\n"),
                                handler_tests.SECRETS.flatmap(handler_tests.lines_with)), min_size=1, max_size=20))
def test_the_quoted_mask_is_the_scrub_s_first_pass(lines):
    """`handoff.quoted_value_mask` masks, character for character, the spans the whole-text
    scrub's first pass replaces, and keeps every other character as it is."""
    text = "".join(lines)
    masked, mark = handoff.quoted_value_mask(text)
    assert len(masked) == len(text) and mark not in text
    spans = []

    def replaced(matched, out):
        spans.append(matched)
        return out
    handoff._scrub_named_values(text, replaced, quoted=True, multiline=True)
    assert all(kept == char for kept, char in zip(masked, text) if kept != mark)
    runs = [text[run.start():run.end()] for run in re.finditer(re.escape(mark) + "+", masked)]
    assert "".join(runs) == "".join(spans)


# --- the differential and the properties ----------------------------------------------------

def reference(text: str) -> set[str]:
    """The secrets the whole text's scrub shows: its block with its lines kept."""
    return secrets(daemon_log.scrubbed_block(text, group=daemon_log._GROUP_MARK in text, keep_lines=True))


#: Review r4's edge generator, as its report describes it (its code was not kept): pieces a
#: window's edge may fall inside.
EDGE_PIECES = [
    "  |\n", "  | \n", "    |\n", "      | \n", "  +\n", "  + more\n", "   |\n", "  |　\n", "　 |\n",
    "  |\xa0\n", "\t  |\n", "| key | value |\n", "| token | SECRET7table9 |\n", "a+b=c\n",
    "x | password=SECRET7pipe9 y\n", "<ſystem-reminder>\n", "</system-remınder>\n",
    "<system-remİnder>x</SYSTEM-REMINDER>\n", "<system-reminder\n", "system-reminder>\n",
    "<system-reminder>\nSECRET7inside9\n</system-reminder>\n", "note <system-reminder>\n", "</System-Reminder> note\n",
    "　Authorization: Bearer SECRET7hdr9\n", "\xa0Cookie: a=SECRET7cookie9\n", " Set-Cookie:\n",
    "data:image/png;base64,QUJDREVG\nQUJDREVG\nQUJDREVG\nSECRET7data9\nQUJD\n", "password='open\nSECRET7q9\nclose'\n",
    "deploy --password\nSECRET7flag9 rest\n", "token:\nSECRET7key9\n", "said: '<system-reminder>' ok\n",
    "-----BEGIN RSA\nPRIVATE KEY-----\nSECRET7pem9\n-----END RSA\nPRIVATE KEY-----\n", "plain words only\n",
]


def edge_text(rng: random.Random) -> tuple[str, int, int]:
    """A text, a bound and a count, with runs of margin lines and the pieces around where
    the cut's two windows end."""
    bound, count = rng.randint(40, 600), rng.randint(1, 4)
    segments = []
    for _ in range(rng.randint(2, 30)):
        piece = rng.choice(EDGE_PIECES)
        segments.append(piece * (rng.randint(1, 60) if piece.lstrip(" \t　\xa0").startswith(("|", "+")) else 1))
    text = "".join(segments)
    if rng.random() < 0.3:
        text = "plain words only\n" * rng.randint(1, 200) + text
    if rng.random() < 0.2:
        text = text.replace("\n", "\r\n")
    return text, bound, count


def tagged_fuzz_text(rng: random.Random) -> tuple[str, int]:
    """`fuzz_text` with reminder tags among its lines."""
    text, limit = handler_tests.fuzz_text(rng)
    lines = text.splitlines(True)
    for _ in range(rng.randint(1, 3)):
        lines.insert(rng.randint(0, len(lines)), rng.choice(EDGE_PIECES[15:23] + ["said: '<system-reminder>' ok\n"]))
    return "".join(lines), limit


CORPORA = {**windows.CORPORA, "fuzz_text-with-tags": tagged_fuzz_text}


def check_tail(text: str, bound: int, count: int, monkeypatch) -> None:
    monkeypatch.setattr(daemon_log, "RECORD_SCRUB_MAX", bound)
    tail = daemon_log.scrubbed_tail(text, count)
    if not TAG.search(text):
        assert tail == round5.scrubbed_tail(text, count), (text, bound, count)
    else:
        assert not tags(tail), (text, bound, count)
        assert secrets("\n".join(tail)) <= reference(text), (text, bound, count, tail)


@pytest.mark.parametrize("corpus", sorted(CORPORA))
@pytest.mark.parametrize("seed", range(3))
def test_the_tail_is_round_5_s_without_a_tag_and_hides_what_the_whole_scrub_hides_with_one(corpus, seed, monkeypatch):
    """Differential over the fuzz corpora (`fuzz_text`, with #231's headers, with tags), each
    text's limit as the tail's bound: on a text with no tag the tail is round 5's byte for
    byte; on a tagged one it shows no tag and no secret the whole text's scrub hides."""
    rng = random.Random(6000 + seed)
    for _ in range(handler_tests.FUZZ_TEXTS // 3 + (seed < handler_tests.FUZZ_TEXTS % 3)):
        text, limit = CORPORA[corpus](rng)
        check_tail(text, limit, rng.randint(1, 6), monkeypatch)


@pytest.mark.parametrize("seed", range(3))
def test_the_tail_on_review_r4_s_window_edges(seed, monkeypatch):
    """The same over the edge generator: margin runs, tables, tag folds and near misses,
    whole reminders, leading headers after Unicode whitespace, data URI bodies, CRLF."""
    rng = random.Random(7000 + seed)
    for _ in range(handler_tests.FUZZ_TEXTS // 3 + (seed < handler_tests.FUZZ_TEXTS % 3)):
        check_tail(*edge_text(rng), monkeypatch)


LOG_LINES = st.one_of(
    st.sampled_from(EDGE_PIECES),
    st.sampled_from(list(TAG_FORMS.values())).map(lambda form: f"x {form} y\n"),
    handler_tests.SECRETS.flatmap(handler_tests.lines_with),
    st.sampled_from(["  | member said: api_key:\n  | SECRET7member9\n", "  +-+---------------- 1 ----------------\n",
                     "Authorization: Bearer SECRET7auth9\n", "deploy --db-token \n  SECRET7dbtoken9\n"]),
)


@BOUNDED
@given(lines=st.lists(LOG_LINES, min_size=1, max_size=40), count=st.integers(1, 60),
       before=st.sampled_from([0, 2000, 20_000]), bound=st.sampled_from([200, 2000, daemon_log.RECORD_SCRUB_MAX]))
def test_no_tag_reaches_a_tail_and_no_secret_the_whole_scrub_hides(lines, count, before, bound):
    """Property: over logs that mix tags in every form, flags, headers and margins, the tail
    holds no tag and shows no secret the whole log's scrub hides."""
    text = "plain words only\n" * (before // 17) + "".join(lines)
    saved = daemon_log.RECORD_SCRUB_MAX
    daemon_log.RECORD_SCRUB_MAX = bound
    try:
        tail = daemon_log.scrubbed_tail(text, count)
    finally:
        daemon_log.RECORD_SCRUB_MAX = saved
    assert not tags(tail)
    assert secrets("\n".join(tail)) <= reference(text)


@BOUNDED
@given(lines=st.lists(LOG_LINES, min_size=1, max_size=30), seed=st.integers(0, 2 ** 16))
def test_no_tag_reaches_what_daemon_logs_follow_writes(lines, seed):
    """Property: followed in any pieces, `daemon logs -f` writes no tag, and no secret the
    whole log's scrub hides."""
    from tests.unit.test_daemon_log_tail import fed
    text = "".join(lines)
    written = fed(text, random.Random(seed))
    assert not tags(written)
    assert secrets("\n".join(written)) <= reference(text), text
