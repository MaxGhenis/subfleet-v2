"""Taking reminders out never opens a quoted value, and following starts as `daemon logs -n N` (#233 review r5).

P2-1. The whole-text scrub runs its first quoted pass on the original text and strips
reminders only after it. Round 6's `daemon_log._reminders_resolved` handed the cut the
joined text unmasked, so where a reminder sat between a key's separator and a quote
(`password=<system-reminder>r</system-reminder>"…`), the first pass over what was left
read a value the join opened. It ran past a quote the original's first pass had masked
inside another value, or, with no closing quote, took a data URI's prefix or a private
key's first line to its line's end, so that rule no longer saw the block. The tail, `daemon
logs` and `daemon start`'s tail then printed secrets the whole-text scrub hides. Round 7
takes reminders out only where the first pass over what is left replaces exactly what the
original's replaces outside them, by the same words, and the same for the margin-free
reading; otherwise the file keeps round 5's marker rule.

P3-2. `daemon logs -f -n N` printed the lines before the first reminder tag in the file's
last 24 KiB (nothing after a tag settles), then held every new line until 16 KiB had
passed. It now first prints exactly what `daemon logs -n N` prints of the same file; the
follower's holds apply only to what arrives afterwards.

What is held here (the report's invariants 1 and 4):
1. every tail and `daemon logs` output hides every secret the whole-text scrub of the same
   text hides, over a grammar that puts reminders between a separator and a quote;
4. the first output of `daemon logs -f -n N` is `daemon logs -n N`'s on the same file.
"""
from __future__ import annotations

import random
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import cli, daemon_log
from subfleet.sessions import handoff
from tests import reference_tail_1cf653dff as round5
from tests.unit.test_daemon_log_reminders import MARK, TAG, admissions, secrets, tags
from tests.unit.test_daemon_log_tail import follow

BOUNDED = settings(max_examples=150, derandomize=True, database=None, deadline=None)
REMINDER = "<system-reminder>r</system-reminder>"

#: Review r5's three executed inputs (P2-1), each a reminder between a key's separator and
#: a quote: the join opens a value that ends at a quote the original's first pass masked;
#: one that takes a data URI's prefix to its line's end; one that takes a private key's
#: first line. Each is placed after 600 admission lines, then `after`.
JOINS = {
    "quote": f'password={REMINDER}"x \\"token\\": \\"v" y\nSECRET7quotejoin9\n"\n',
    "data-uri": f"token={REMINDER}'data:image/png;base64,\nQUJDREVG\nQUJDREVGSECRET7datajoin9QUJD\nQUJDREVG\n",
    "private-key": (f"token={REMINDER}'-----BEGIN RSA PRIVATE KEY-----\nQUJDREVG\nSECRET7pemjoin9\n"
                    "-----END RSA PRIVATE KEY-----\n"),
    # The same join read only without a group's margins: in the margin-free reading `token`
    # and `=` are one key, whose value the join opens past the masked quote.
    "margin-free": (f'  | token\n  | ={REMINDER}"x \\"token\\": \\"v" y\n  | SECRET7barejoin9\n  | "\n'),
}


def join_log(name: str) -> str:
    lead = "  +-+---------------- 1 ----------------\n" if name == "margin-free" else ""
    return lead + admissions(600) + JOINS[name] + "after\n"


def reference(text: str) -> set[str]:
    """The secrets the whole text's scrub shows: its block with its lines kept."""
    return secrets(daemon_log.scrubbed_block(text, group=daemon_log._GROUP_MARK in text, keep_lines=True))


# --- P2-1: the regressions, each failing at cb03e5a60 --------------------------------------

@pytest.mark.parametrize("name", sorted(JOINS))
def test_a_join_that_opens_a_value_keeps_the_marker_rule(name):
    """Review r5, P2-1, at the real bound: the whole log's scrub hides the secret, and so
    does the tail at 8 lines, which keeps round 5's rule for this file: only the marker.
    Round 6 printed `password="[REDACTED]"`, `SECRET7quotejoin9`, `"`, `after` (and the
    data URI's body line); for the private key, the text it cut showed the key's body."""
    text = join_log(name)
    assert len(text) > daemon_log._tail_limit(text, 8)
    assert reference(text) == set()
    assert daemon_log._reminders_resolved(text) is None
    tail = daemon_log.scrubbed_tail(text, 8)
    assert tail == round5.scrubbed_tail(text, 8) == [handoff.cut_marker(len(text))]
    assert not secrets("\n".join(tail)) and not tags(tail)


@pytest.mark.parametrize("name", sorted(JOINS))
def test_daemon_logs_and_daemon_start_s_tail_hide_what_the_whole_scrub_hides(name, root, capsys, tmp_path):
    """The same through `daemon logs -n 8` and `daemon start`'s tail (`cli._log_tail`)."""
    text = join_log(name)
    (root / "daemon.log").write_text(text)
    assert cli.main(["daemon", "logs", "-n", "8"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == daemon_log.scrubbed_tail(text, 8) and "SECRET7" not in "\n".join(printed)
    path = tmp_path / "daemon.log"
    path.write_text(text)
    shown = cli._log_tail(path, 8)
    assert "SECRET7" not in shown and shown.splitlines() == [f"  | {line}" for line in printed]


@pytest.mark.parametrize("name", sorted(JOINS))
def test_what_the_tail_would_cut_hides_what_the_whole_scrub_hides(name):
    """Whatever the resolution gives the cut, scrubbed whole it hides what the original
    scrubbed whole hides: round 6's resolved text showed each secret."""
    text = join_log(name)
    resolved = daemon_log._reminders_resolved(text)
    shown = set() if resolved is None else reference(resolved)
    assert shown <= reference(text), shown


@pytest.mark.parametrize("count", [1, 3, 40])
def test_a_join_that_opens_nothing_is_still_resolved(count):
    """The check falls back only where the join changes the first pass: a reminder between
    a separator and a plain value, or between two lines, is still taken out, and the tail
    keeps its last lines."""
    piece = f"password={REMINDER}SECRET7plainjoin9 rest\nnote {REMINDER} more\n"
    text = admissions(600) + piece + "after\n"
    resolved = daemon_log._reminders_resolved(text)
    assert resolved == text.replace(REMINDER, "")
    tail = daemon_log.scrubbed_tail(text, count)
    assert len(tail) == count and tail[-1] == "after" and not tags(tail)
    assert secrets("\n".join(tail)) <= reference(text) == set()
    if count == 40:
        assert "password=[REDACTED] rest" in tail and "note  more" in tail


def test_spans_are_what_the_scrub_s_first_pass_replaces():
    """`handoff.quoted_value_spans`, replaced by their words, is the first pass's output."""
    text = admissions(3) + "a='x b=' password='open\nSECRET7span9\n' token: \"q\" \\'k\\': \\'v\\' x\n"
    spans = handoff.quoted_value_spans(text)
    rebuilt, cursor = [], 0
    for start, end, out in spans:
        rebuilt.extend((text[cursor:start], out))
        cursor = end
    rebuilt.append(text[cursor:])
    assert "".join(rebuilt) == handoff._scrub_named_values(text, lambda _matched, out: out, quoted=True, multiline=True)
    assert spans and all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


# --- P2-1: the grammar, with reminders between a separator and a quote ---------------------

KEYS = ["password", "token", "api_key", "client_secret", "x.token", "db-password", "Authorization", "note"]
KEY_QUOTES = ["{}", '"{}"', "'{}'", '\\"{}\\"', "\\'{}\\'", '\\\\\\"{}\\\\\\"']
SEPARATORS = ["=", ":", " = ", ": ", "=\n", ":\n", "=\n  | ", ": \t"]
QUOTES = ['"', "'", '\\"', "\\'", '\\\\\\"', "\\\\\\'", '\\\\"']
REMINDERS = ["<system-reminder>r</system-reminder>", "<system-reminder>§</system-reminder>",
             "<system-reminder>a\nb</system-reminder>", "<SYSTEM-REMINDER>x</System-Reminder>",
             "<ſystem-reminder>'</system-remınder>", '<system-reminder>"</system-reminder>',
             "<system-reminder>", "</system-reminder>", "<system-remİnder>"]
PIECES = ["§", "§ more", "x", "v", "y", " ", "\n", "\n  | ", "  | ", "\t", "\\", "\\\\", "\\n", ",", ";", "--password ",
          "--auth-token\n", "deploy --db-token ", "Bearer ", "Authorization: ", "Cookie: a=", "https://u:§@host",
          "-----BEGIN RSA PRIVATE KEY-----\n", "-----BEGIN RSA\n", "PRIVATE KEY-----\n", "QUJDREVG\n",
          "-----END RSA PRIVATE KEY-----\n", "data:image/png;base64,", "data:image/png;base64,\n", "end.",
          "  +-+---------------- 1 ----------------\n", "\\\"token\\\": \\\"v", "<<", ">>"]


VALUES = ["§", "x §", "§\n§", "x \\\"token\\\": \\\"v\" y\n§\n", "data:image/png;base64,\nQUJDREVG\n§\n",
          "-----BEGIN RSA PRIVATE KEY-----\nQUJDREVG\n§\n-----END RSA PRIVATE KEY-----\n"]


def assignment(rng: random.Random) -> str:
    """A key, its separator, a reminder (or none), then a quote and a value that may close."""
    key = rng.choice(KEY_QUOTES).format(rng.choice(KEYS))
    quote = rng.choice(QUOTES)
    reminder = rng.choice(["", *REMINDERS])
    close = rng.choice(["", quote, '"', "'", "\n" + quote])
    return key + rng.choice(SEPARATORS) + reminder + quote + rng.choice(VALUES) + close


def grammar_text(rng: random.Random) -> str:
    """Review r5's grammar: keys, separators, every quote kind, escapes, flags, private-key and
    data-URI pieces, margins, reminders and orphans, with reminders between a separator and a
    quote (`assignment`), the shape round 6's generators never drew. Each `§` is a secret of
    its own. `tools/reminder_join_census.py` draws the same texts."""
    parts = []
    for _ in range(rng.randint(1, 14)):
        kind = rng.random()
        parts.append(assignment(rng) if kind < 0.4 else rng.choice(REMINDERS) if kind < 0.55 else rng.choice(PIECES))
    text = rng.choice(["", "plain words only\n" * 3, "  | member line\n" * 4]) + "".join(parts)
    pieces = (text + rng.choice(["", "\n", "\nafter\n"])).split("§")
    return "".join(piece + (f"SECRET7g{index}x9" if index < len(pieces) - 1 else "")
                   for index, piece in enumerate(pieces))


#: The grammar's texts, by seed (shrinking a seed finds a smaller seed, not a smaller text;
#: the failure message holds the text).
grammar_logs = st.integers(0, 2 ** 32 - 1).map(lambda seed: grammar_text(random.Random(seed)))
#: The join P2-1 needs: a separator, a whole reminder, then a quote.
BETWEEN = re.compile(r"[:=]\s*<[^\n>]*system-remi[^>]*>.*?</system-rem[^>]*>\\*[\"']", re.IGNORECASE | re.DOTALL)


def check(text: str, bound: int, count: int) -> list[str]:
    """The tail of `text` cut to `bound`: no tag, no secret the whole-text scrub hides."""
    saved = daemon_log.RECORD_SCRUB_MAX
    daemon_log.RECORD_SCRUB_MAX = bound
    try:
        tail = daemon_log.scrubbed_tail(text, count)
    finally:
        daemon_log.RECORD_SCRUB_MAX = saved
    assert not tags(tail), (text, bound, count, tail)
    assert secrets("\n".join(tail)) <= reference(text), (text, bound, count, tail)
    return tail


@BOUNDED
@given(text=grammar_logs, bound=st.sampled_from([60, 120, 200, 400]), count=st.sampled_from([1, 2, 4, 8, 40]))
def test_no_tail_shows_a_secret_the_whole_scrub_hides_over_review_r5_s_grammar(text, bound, count):
    """Invariant 1 over the grammar, at bounds small enough that most texts are cut, so the
    resolution runs: the tail hides every secret the whole text's scrub hides."""
    check(text, bound, count)


@BOUNDED
@given(text=grammar_logs, count=st.sampled_from([1, 8, 40]))
def test_daemon_logs_hides_what_the_whole_scrub_hides_over_the_grammar(text, count):
    """The same at the real bound, the grammar's text after 600 admission lines."""
    check(admissions(600) + text, daemon_log.RECORD_SCRUB_MAX, count)


def test_the_grammar_draws_reminders_between_a_separator_and_a_quote():
    """The grammar draws P2-1's shape often, and a share of its texts keep the marker rule."""
    texts = [grammar_text(random.Random(seed)) for seed in range(600)]
    between = [text for text in texts if BETWEEN.search(text)]
    assert len(between) >= 150, len(between)
    fallbacks = sum(daemon_log._reminders_resolved(admissions(600) + text) is None for text in between)
    assert 0 < fallbacks < len(between), (fallbacks, len(between))


# --- P3-2: the follower's start ------------------------------------------------------------

def first_output(root, monkeypatch, capsys, count: int) -> tuple[list[str], list[str]]:
    """`daemon logs -f -n COUNT`: what it prints before its first wait, and in all."""
    seen = []

    def wait(_seconds):
        seen.append(capsys.readouterr().out)
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", wait)
    assert cli.main(["daemon", "logs", "-f", "-n", str(count)]) == 0
    rest = capsys.readouterr().out
    return seen[0].splitlines(), (seen[0] + rest).splitlines()


def static_output(root, capsys, count: int) -> list[str]:
    assert cli.main(["daemon", "logs", "-n", str(count)]) == 0
    return capsys.readouterr().out.splitlines()


def test_following_a_tagged_log_starts_with_its_last_lines(root, monkeypatch, capsys):
    """Review r5, P3-2: 40 admission lines, a line an older daemon wrote with an orphan tag,
    8 more. `daemon logs -f -n 5` printed the five lines before the tagged one, ending at job
    39; it prints the file's last five, as `daemon logs -n 5` does. Lines written after it
    are held by the tag (up to `FOLLOW_HOLD_MAX`), and written at ^C."""
    text = (admissions(40) + "turn failed: model said: <system-reminder>The user's excerpt was cut\n"
            + admissions(8, 40))
    (root / "daemon.log").write_text(text)
    static = static_output(root, capsys, 5)
    assert static == [line.rstrip("\n") for line in admissions(5, 43).splitlines(True)]
    first, _ = first_output(root, monkeypatch, capsys, 5)
    assert first == static


@pytest.mark.parametrize("count", [3, 9, 40])
def test_following_starts_with_the_tag_s_line_marked(count, root, monkeypatch, capsys):
    """With the tagged line among the last lines, it is printed marked, as statically."""
    text = (admissions(40) + "turn failed: model said: <system-reminder>The user's excerpt was cut\n"
            + admissions(8, 40))
    (root / "daemon.log").write_text(text)
    static = static_output(root, capsys, count)
    first, everything = first_output(root, monkeypatch, capsys, count)
    assert first == static == everything
    assert not tags(first) and (MARK in "\n".join(first)) is (count > 8)


def test_lines_after_the_start_are_still_held_by_a_tag_and_scrubbed_with_it(root, monkeypatch, capsys):
    """What arrives afterwards is the follower's: a reminder whose closing tag comes later is
    stripped with what came between, as the whole-text scrub strips it."""
    (root / "daemon.log").write_text(admissions(40) + "note <system-reminder> opened\n" + admissions(3, 40))
    follow(root, monkeypatch, ["SECRET7insidereminder9\n", "</system-reminder> closed\n", "done\n"], lines=4)
    out = capsys.readouterr().out
    assert "SECRET7" not in out and not TAG.search(out)
    lines = out.splitlines()
    assert lines[:4] == [f"note {MARK} opened", *admissions(3, 40).splitlines()]
    assert lines[-1] == "done"


FOLLOW_LINES = st.lists(st.one_of(
    st.sampled_from(["admission: placed job 7 on lane codex-0\n", "plain words only\n", "x:\n", "--verbose\n",
                     "turn failed: model said: <system-reminder>The user's excerpt was cut\n",
                     "<system-reminder>note</system-reminder>\n", "</system-reminder>\n", "token:\n", "deploy --password\n",
                     "upstream said: password='open\n", "close'\n", "-----BEGIN RSA\n", "PRIVATE KEY-----\n",
                     "data:image/png;base64,QUJD\n", "  | member line\n", "SECRET7follow9 words\n"]),
    grammar_logs.map(lambda text: text + "\n")), min_size=1, max_size=30)


@settings(BOUNDED, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(lines=FOLLOW_LINES, count=st.integers(0, 50), last=st.sampled_from(["", "partial line, no break", "token:"]))
def test_following_starts_with_what_daemon_logs_prints(lines, count, last, root):
    """Invariant 4: on any file, tagged or not, ending in a held value or a line still being
    written, the first output of `daemon logs -f -n N` is `daemon logs -n N`'s."""
    (root / "daemon.log").write_text("".join(lines) + last)
    printed: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "out", lambda line="": printed.append(line))
        assert cli.main(["daemon", "logs", "-n", str(count)]) == 0
        static, printed[:] = list(printed), []
        first: list[list[str]] = []

        def wait(_seconds):
            first.append(list(printed))
            raise KeyboardInterrupt
        patch.setattr(cli.time, "sleep", wait)
        assert cli.main(["daemon", "logs", "-f", "-n", str(count)]) == 0
    assert first == [static], (first, static)
