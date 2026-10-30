"""C-23.14 (D-SCR2 fix round 2, #239 review r2): no mode of the head's scrubber shows a
`SECRET7` value that 473fe2941 (`scrub_473fe2941`) or release/217 at c2e080e44 hides.

Release/217 at c2e080e44 holds 473fe2941's scrubber with #233's (D-LOG1) changes, none of
which changes what a scrub returns: this round measured both trees' outputs equal in every
mode on every text below and on the sweeps (the report's round-2 section), so
`scrub_473fe2941` stands for both here. The pre-#231 reference (`scrub_release217`,
3fbc4b7c5) is checked too, where the round-1 differential checked it.

Review r2's executed inputs are regressions, each failing at 6bda95d54. Its three
generators were not kept; they are rebuilt from what the review states of them, at the
sizes it gives: the flag matrix (16 enclosures x 6 flag-shaped values x 8 endings, 768
cells), the union matrix (7 enclosures x 90, 630 cells) and the next-line-header matrix
(7 openers x 6 x 6 names x 16 values and tails, 4,032 cells). Over each the head shows no
value either reference hides, an excerpt cut where `_neutral_break` allows scrubs as the
whole text does there, and a second scrub shows nothing the first hid."""

from __future__ import annotations

import itertools
import re
import time

import pytest

from subfleet import refusals
from subfleet.conversations import suggestions
from subfleet.sessions import handoff
from tests.unit import scrub_473fe2941 as base473
from tests.unit import scrub_release217 as release217
from tests.unit.test_scrub_key_order import _neutral_breaks, _revealed

PREFIX = "request submit refused (ValueError): "
SECRET = re.compile(r"SECRET7[A-Za-z0-9_]*")
REFERENCES = {"473fe2941 and c2e080e44": base473, "3fbc4b7c5": release217}


def _line(scrubber, text: str) -> str:
    """daemon.log's line without its 2,000-character bound (`suggestions._scrub_line`)."""
    return suggestions._scrub_line(text)[0] if scrubber is handoff else scrubber.scrubbed_line(text)


def _outputs(scrubber, text: str) -> dict[str, str]:
    return {"excerpt": scrubber.scrub_secrets(text)[0], "whole": scrubber.scrub_secrets(text, whole=True)[0],
            "clean": scrubber.clean(text, 4000)[0], "daemon.log line": _line(scrubber, text),
            "detail_line": _line(scrubber, PREFIX + text)}


def _shown(outputs: dict[str, str], secrets) -> set[tuple[str, str]]:
    return {(mode, secret) for mode, out in outputs.items() for secret in secrets if secret in out}


def _secrets(text: str) -> set[str]:
    return set(SECRET.findall(text))


def _new_leaks(text: str, references=REFERENCES) -> dict[str, list]:
    """What the head shows of `text`'s secrets that each reference hides, by mode."""
    secrets = _secrets(text)
    head = _shown(_outputs(handoff, text), secrets)
    return {name: sorted(head - _shown(_outputs(scrubber, text), secrets)) for name, scrubber in references.items()
            if head - _shown(_outputs(scrubber, text), secrets)}


def _unstable(text: str) -> dict[str, str]:
    """The modes whose second scrub shows what their first hid (invariant 2)."""
    found = {}
    for mode, scrub in (("excerpt", lambda t: handoff.scrub_secrets(t)[0]),
                        ("whole", lambda t: handoff.scrub_secrets(t, whole=True)[0]),
                        ("daemon.log line", refusals.scrubbed_line)):
        first = scrub(text)
        if (revealed := _revealed(first, scrub(first))):
            found[mode] = revealed
    return found


def _splits(text: str) -> list[int]:
    """The line breaks an excerpt may be cut at where the two excerpts' scrubs differ
    from the whole text's (invariant 3)."""
    whole = handoff._scrub(text, False)[0]
    return [cut for cut in _neutral_breaks(text)
            if handoff._scrub(text[:cut], False)[0] + handoff._scrub(text[cut:], False)[0] != whole]


def _assert_hidden(text: str) -> dict[str, str]:
    """No mode of the head shows any of `text`'s `SECRET7` values."""
    secrets = _secrets(text)
    assert secrets, text
    found = _outputs(handoff, text)
    assert {mode: out for mode, out in found.items() if any(secret in out for secret in secrets)} == {}, text
    return found


# --- review r2's executed inputs -------------------------------------------------------

#: P2-1: a value shaped like a credential flag, which a rule deferring to the flag left
#: whole (`_owning_flag`); P4 makes a flag whose name reads as a secret own nothing, and
#: the hub's decision redacts the value daemon.log's fold gives it too.
P2_1 = {
    "plain-value": "token:--SECRET7abc1_token",
    "bearer-flag": "Bearer --SECRET7abc1_token",
    "bearer-glued": "Bearer SECRET7--secret",
    "header": "Authorization: --SECRET7abc1_token",
    "E-unclosed": "api_key='xyz --SECRET7abc1_token",
    "B-glued-flag": "--password x--SECRET7abc1_token",
    "A-swallowed-header": "secret:'Cookie: x --SECRET7abc1_token",
}
#: P2-1's partial input with a dummy value: the value before the glued flag is hidden;
#: the flag's name, `--super-secret`, is a flag's (C-23.14) and the head's rules show it.
#: Since #239 round 8 every mode writes release/217's output kept for the runs the head's
#: holds (`handoff.shown_by_both`), and release's key rule takes the whole run.
P2_1_PARTIAL = ("token: SECRET7h2--super-secret", 'token: [REDACTED]')
#: P2-2: a `Bearer` a quoted range's union took from the `Bearer` rule.
P2_2 = {
    "A-next-line": "secret:'Cookie:\nx token:Bearer\nSECRET7un361x rest",
    "seed-239": "passwd:'Cookie:\nh\ttoken:\nBearer SECRET7k",
    "seed-2392": 'Authorization:"Authorization:\ny passwd:Bearer\nSECRET7d12345',
}
#: P2-3: a quoted inline header whose value starts on the next line, at that line's
#: own header.
P2_3 = {
    "curl": 'curl -H "Cookie:\nCookie: sid=1" SECRET7abc124 -v',
    "json": '{"cmd": "curl -H \\"Authorization:\nAuthorization: Basic x\\" SECRET7abc123 -v"}',
}
#: P3-2: a line-ending `Bearer` that is a key's value inside an unclosed quote, and its
#: twin without the quote.
P3_2 = ("api_key='x token:Bearer\nSECRET7abc12345", "api_key=x token:Bearer\nSECRET7abc12345")
#: Check 2's two regressions against release/217 (both shared by 473fe2941).
CHECK_2 = {
    "bearer-flag-converse": "Bearer SECRET7--secret x",
    "refused-bearer-header-line": "Authorization: Bearer\nclient_secret=x SECRET7abc13 tail",
}
#: The cells P4 alone opens (review r2 counted 10 in its matrix, `sent Cookie: x
#: --SECRET7abc1_token\nSECRET7nxt9` among them): the 20 of this round's rebuilt flag
#: matrix that show a value 473fe2941 hides once the fold redaction is dropped (the
#: value daemon.log's line joins to a secret-named flag an inline header or `Bearer`
#: took), and the same at a line start.
P4_OPENED = (
    *(f"Bearer {flag} SECRET7nxt9" for flag in ("--SECRET7abc1_token", "x--SECRET7abc1_token",
                                                "--passwordSECRET7a1_token", "--db-SECRET7k9-password")),
    *(f"sent Cookie: x {flag}{ending}" for flag in ("--SECRET7abc1_token", "x--SECRET7abc1_token",
                                                    "--passwordSECRET7a1_token", "--db-SECRET7k9-password")
      for ending in ("\nSECRET7nxt9", "  \nSECRET7nxt9 rest", "\n\nSECRET7nxt9", "\r\nSECRET7nxt9")),
    "Authorization: x --SECRET7abc1_token\nSECRET7nxt9",
)


@pytest.mark.parametrize("text", [*P2_1.values(), *P2_2.values(), *P2_3.values(), P3_2[0], *CHECK_2.values(),
                                  *P4_OPENED],
                         ids=[*(f"P2-1-{k}" for k in P2_1), *(f"P2-2-{k}" for k in P2_2), *(f"P2-3-{k}" for k in P2_3),
                              "P3-2", *(f"check-2-{k}" for k in CHECK_2), *(f"P4-opened-{n}" for n in range(len(P4_OPENED)))])
def test_review_r2_s_inputs_are_hidden_in_every_mode(text):
    """Every executed input of review r2 that showed a value 473fe2941 or release/217
    hides (each failed at 6bda95d54), check 2's two, and the cells P4 alone opens:
    no mode shows the value, and no excerpt cut disagrees with the whole text."""
    _assert_hidden(text)
    assert _splits(text) == [], text


def test_p2_1_s_partial_input_hides_the_value_before_the_glued_flag():
    text, scrubbed = P2_1_PARTIAL
    assert handoff.scrub_secrets(text)[0] == scrubbed
    _assert_hidden(text)


def test_p3_2_s_quoted_value_scrubs_as_its_twin_hides():
    """Invariant 4 for P3-2's input: the unclosed quote hides what its twin hides."""
    quoted, twin = P3_2
    assert _assert_hidden(twin)
    assert _assert_hidden(quoted)


#: What the fix makes of the inputs, pinned (fail closed: a secret-named flag goes with
#: the value the fold gives it; the header a refused `Bearer` ends takes its next line).
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
PINNED = {
    "sent Cookie: x --SECRET7abc1_token\nSECRET7nxt9": 'sent Cookie: [REDACTED] [REDACTED]\n[REDACTED]',
    "token:--SECRET7abc1_token SECRET7nxt9": "token:[REDACTED] [REDACTED]",
    "Bearer SECRET7--secret x": 'Bearer [REDACTED] [REDACTED]',
    "Bearer --passwordSECRET7a1_token tail": "Bearer [REDACTED] [REDACTED]",
    "Authorization: Bearer\nclient_secret=x SECRET7abc13 tail": 'Authorization: [REDACTED]\n[REDACTED] [REDACTED] [REDACTED]',
    'curl -H "Cookie:\nCookie: sid=1" SECRET7abc124 -v': 'curl -H "Cookie:\n[REDACTED] [REDACTED]',
    "sent Cookie: --password\nSECRET7a1 x": "sent Cookie: [REDACTED]\n[REDACTED] x",
    # The stated converse (C-23.14): a run before a refused key that is not a token.
    "invalid Bearer SECRET7_token: expired": "invalid Bearer SECRET7_token: [REDACTED]",
}


@pytest.mark.parametrize("text,scrubbed", PINNED.items())
def test_the_fix_s_outputs_are_pinned(text, scrubbed):
    assert handoff.scrub_secrets(text)[0] == scrubbed
    assert handoff.scrub_secrets(text, whole=True)[0] == scrubbed


def test_the_stated_converse_is_the_one_exception():
    """`invalid Bearer SECRET7_token: expired` keeps `SECRET7` (C-23.14), as 473fe2941
    does; only the pre-#231 scrubber hid it, by taking the key too."""
    text = "invalid Bearer SECRET7_token: expired"
    assert _new_leaks(text, {"473fe2941 and c2e080e44": base473}) == {}
    assert set(_new_leaks(text)) == {"3fbc4b7c5"}


def test_a_second_scrub_never_reveals_what_the_first_hid():
    """Invariant 2 (monotone) on review r2's P3-3 inputs: a second scrub removes more
    (`cookie:AUTHORIZATION :` loses its `:` line), never shows anything."""
    for text in ("cookie:AUTHORIZATION :\nE", "-Authorization:cookie :\no", "cookie:AUTHORIZATION :\nSECRET7e1"):
        assert _unstable(text) == {}, text


# --- P3-1: one look per line end -------------------------------------------------------

def _adversary(values: int) -> str:
    """Review r2's P3-1 input: `values` unclosed values on one line, one per run of
    backslashes before the quote, the line ending in a credential word."""
    line = " ".join("api_key=" + "\\" * run + "'x" for run in range(1, values + 1))
    return line + " token\n"


def _cpu(function, *args) -> float:
    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        function(*args)
        best = min(best, time.process_time() - started)
    return best


def test_unclosed_values_sharing_a_line_end_read_it_once():
    """At 6bda95d54 each unclosed value searched back to its opening for a flag
    (`_owning_flag`'s `rfind`), O(n^1.5) on a line of such values: growth at 4x input
    was 6.3. Now the line's end is read once (`owned_before`): the quoted pass grows
    at most about 4x, measured in CPU time, best of three (6bda95d54: 6.1 here)."""
    small, large = _adversary(360), _adversary(720)
    assert len(large) > 3.8 * len(small)

    def quoted(text):
        handoff._scrub_named_values(text, lambda matched, out: out, quoted=True)
    ratio = _cpu(quoted, large) / max(_cpu(quoted, small), 1e-6)
    assert ratio < 5.5, ratio


def test_owned_before_reads_each_line_end_once(monkeypatch):
    """The count behind the cost: `_owned_item` runs once per line end, however many
    unclosed values share it."""
    calls = []
    original = handoff._owned_item
    monkeypatch.setattr(handoff, "_owned_item", lambda text, start, last: calls.append(last) or original(text, start, last))
    handoff._scrub_named_values(_adversary(60), lambda matched, out: out, quoted=True)
    assert len(calls) == len(set(calls)) <= 2, calls


# --- the rebuilt generators ------------------------------------------------------------

#: Flag-shaped values: a secret-named flag, a secret glued before a flag, a glued
#: secret-named flag, a flag name holding a secret, a secret before a compound flag, a
#: compound secret-named flag.
FLAG_VALUES = ("--SECRET7abc1_token", "SECRET7--secret", "x--SECRET7abc1_token", "--passwordSECRET7a1_token",
               "SECRET7ab--super-secret", "--db-SECRET7k9-password")
#: What follows the value: the end, words, the next line's value (the fold's), blank
#: lines, trailing spaces, a closing quote, CRLF.
FLAG_ENDINGS = ("", " tail", "\nSECRET7nxt9", " SECRET7nxt9", "  \nSECRET7nxt9 rest", "\n\nSECRET7nxt9", "'",
                "\r\nSECRET7nxt9")
#: Where the value sits: plain values, `Bearer`, headers at a line start and inline,
#: quoted, unclosed and JSON values, a flag's value and a glued one, a swallowed header,
#: a value on the line after its header, and bare.
FLAG_ENCLOSURES = ("token:{v}", "token: {v}", "password={v}", "Bearer {v}", "Authorization: {v}",
                   "sent Cookie: x {v}", "sent Cookie: {v}", "curl -H 'Authorization: {v}", "api_key='xyz {v}",
                   'api_key="{v}', "--password y{v}", "--password {v}", "secret:'Cookie: x {v}",
                   '{{"token": "{v}', "X-Authorization:\n  {v}", "deploy {v}")


def flag_matrix() -> list[str]:
    return [enclosure.format(v=value) + ending
            for enclosure in FLAG_ENCLOSURES for value in FLAG_VALUES for ending in FLAG_ENDINGS]


#: P2-2's shape: a quoted range (an unclosed quote around a header's name, or a value)
#: whose union reaches a later key whose plain value is a line-ending `Bearer`.
UNION_ENCLOSURES = ("secret:'Cookie:\nx ", "passwd:'Cookie:\nh\t", 'Authorization:"Authorization:\ny ',
                    'api_key="X-Authorization:\nz ', "token='open\nw ", "secret:'x ", "curl -H 'Cookie: a ")
UNION_KEYS = ("token", "passwd", "secret", "api_key", "client_secret", "Authorization")
UNION_JOINS = (":", ": ", ":\n")
UNION_BEARERS = ("Bearer\n{s}", "Bearer {s}", "Bearer\n  {s}", "\nBearer {s}", "Bearer\r\n{s}")


def union_matrix() -> list[str]:
    return [enclosure + key + join + bearer.format(s=f"SECRET7un{n}x") + " rest"
            for n, (enclosure, key, join, bearer) in enumerate(itertools.product(
                UNION_ENCLOSURES, UNION_KEYS, UNION_JOINS, UNION_BEARERS))]


#: P2-3's shape: a header (quoted or not) whose value starts on the next line, at that
#: line's own header, with a secret after the closing quote.
NEXT_LINE_OPENERS = (("curl -H \"", "\""), ("curl -H '", "'"), ('{"cmd": "curl -H \\"', '\\"'), ("curl -H ", ""),
                     ('{"c": "x \\\\\\"', '\\\\\\"'), ("--header=\"", "\""), ("wget --header='", "'"))
NEXT_LINE_NAMES = ("Cookie", "Authorization", "Set-Cookie", "Proxy-Authorization", "X-Authorization", "cookie")
NEXT_LINE_VALUES = ("sid=1", "Basic x", "Bearer SECRET7in{n}abc", "")
NEXT_LINE_TAILS = ("", "\nnext", " more", "\r\n")


def next_line_matrix() -> list[str]:
    cells = []
    for n, ((opening, closing), outer, inner, value, tail) in enumerate(itertools.product(
            NEXT_LINE_OPENERS, NEXT_LINE_NAMES, NEXT_LINE_NAMES, NEXT_LINE_VALUES, NEXT_LINE_TAILS)):
        cells.append(f"{opening}{outer}:\n{inner}: {value.format(n=n)}{closing} SECRET7nl{n}x -v{tail}")
    return cells


#: P3-2's class (invariant 4): a line-ending `Bearer` that is a key's value inside an
#: unclosed single, double or escaped quote, or a header name a quoted match swallows on
#: the same line, and the same text without the opening quote.
OWNERSHIP_ENCLOSURES = (("api_key='x ", "api_key=x "), ('api_key="x ', "api_key=x "), ("api_key=\\'x ", "api_key=x "),
                        ("secret:'Cookie: x ", "secret:Cookie: x "))


def ownership_matrix() -> list[tuple[str, str]]:
    return [(quoted + key + join + bearer.format(s=f"SECRET7ow{n}x") + " rest",
             twin + key + join + bearer.format(s=f"SECRET7ow{n}x") + " rest")
            for n, ((quoted, twin), key, join, bearer) in enumerate(itertools.product(
                OWNERSHIP_ENCLOSURES, UNION_KEYS, UNION_JOINS, UNION_BEARERS))]


def test_a_quoted_bearer_value_is_hidden_where_its_twin_is():
    """Invariant 4 over P3-2's 360 cells (review r2: 300 showed what the twin hides, at
    every tree): each mode hides every secret the twin's same mode hides."""
    cells = ownership_matrix()
    assert len(cells) == 360
    failures = []
    for quoted, twin in cells:
        secrets = _secrets(quoted)
        if (shown := _shown(_outputs(handoff, quoted), secrets) - _shown(_outputs(handoff, twin), secrets)):
            failures.append((quoted, sorted(shown)))
    assert failures == []


MATRICES = {"flag": (flag_matrix, 768), "union": (union_matrix, 630), "next-line": (next_line_matrix, 4032)}


@pytest.mark.parametrize("name", MATRICES)
def test_the_rebuilt_matrices_have_the_review_s_sizes(name):
    """And the flag matrix holds every cell P4 alone opens (`P4_OPENED`), but the last."""
    build, size = MATRICES[name]
    cells = build()
    assert len(cells) == size == len(set(cells))
    assert all(_secrets(cell) for cell in cells)
    if name == "flag":
        assert set(P4_OPENED[:-1]) <= set(cells)


@pytest.mark.parametrize("name", MATRICES)
def test_no_cell_of_a_rebuilt_matrix_shows_what_a_reference_hides(name):
    """Invariants 1 to 3 over every cell: no new leak against either reference, no
    excerpt cut that disagrees with the whole text, and no second scrub that shows what
    the first hid."""
    failures = {"leaks": [], "splits": [], "unstable": []}
    for cell in MATRICES[name][0]():
        if (leaks := _new_leaks(cell)):
            failures["leaks"].append((cell, leaks))
        if (splits := _splits(cell)):
            failures["splits"].append((cell, splits))
        if (unstable := _unstable(cell)):
            failures["unstable"].append((cell, unstable))
    assert failures == {"leaks": [], "splits": [], "unstable": []}


# --- the merge with D-LOG1 (#233): a record's cut and the inline header rule -----------

#: Headers whose value the line form's folded rescrub reads to the record's end unless
#: the first scrub redacts it: at the merge (8a9f217fc) a header whose whole value was a
#: credential flag kept it, so with that header in the part `cut_whole` leaves out, the
#: cut record showed what the uncut one hid (C-25.7).
CUT_HEADERS = ("sent {n}: --password\n{s} x\n", "{n}: --password\n{s} x\n", "sent {n}: --api-key  \n\n{s}\n",
               "sent {n}: --password\n", "sent {n}:\n{m}: Basic {s}\n", "{n}:\n{m}: Basic {s}\n",
               "sent {n}: Bearer\n{s}abcdefgh x\n", "sent {n}: token=\n{s}\n", "sent {n}: x --SECRET7f1_token\n{s} y\n",
               "curl -H '{n}: --password'\n{s}\n", "sent {n}: Bearer\nclient_secret=x {s} tail\n", "x {n}: {m}:\n{s}\n")
CUT_NAMES = ("Cookie", "Authorization", "X-Authorization", "Set-Cookie", "cookie")
CUT_PLAIN = ("plain words only\n", "w\n", "it's here\n", "x:\n", "a = 'one'\n", "\n")
_CUT_SECRET = re.compile(r"SECRET7[a-z0-9]*")


def _cut_record(rng) -> tuple[str, int]:
    lines = []
    for _ in range(rng.randint(5, 60)):
        secret = "SECRET7" + "".join(rng.choice("abcdef0123") for _ in range(6))
        draw = rng.random()
        if draw < 0.2:
            lines.append(rng.choice(CUT_HEADERS).format(n=rng.choice(CUT_NAMES), m=rng.choice(CUT_NAMES), s=secret))
        else:
            lines.append(f"tail {secret} here\n" if draw < 0.35 else rng.choice(CUT_PLAIN))
    return "".join(lines), rng.randint(40, 400)


def _cut_forms():
    from subfleet import daemon_log
    return (daemon_log.scrubbed_block, lambda text: refusals.scrubbed_line(text, 10 ** 9))


@pytest.mark.parametrize("shape", CUT_HEADERS)
def test_a_header_left_out_of_a_cut_hides_no_more_of_the_tail_than_the_cut_does(shape):
    """With each header shape in the part the cut leaves out, neither daemon.log form of
    the cut record shows a secret the uncut record's form hides (at the merge, a header
    whose whole value was a flag showed the tail's `SECRET7tail9`)."""
    for name in CUT_NAMES[:3]:
        piece = shape.format(n=name, m="Cookie", s="SECRET7a1")
        text = "plain words\n" * 30 + piece + "plain words\n" * 30 + "tail SECRET7tail9 here\n" + "w\n" * 5
        cut = handoff.cut_whole(text, 200)
        assert "characters omitted" in cut and "SECRET7a1" not in cut
        for form in _cut_forms():
            assert set(_CUT_SECRET.findall(form(cut))) <= set(_CUT_SECRET.findall(form(text))), (piece, form)


def test_no_cut_record_shows_a_secret_its_whole_record_hides():
    """Differential over seeded records holding those headers among plain lines (seed
    233, 400 records; the merge failed 332 of 1,500 in this round's probe)."""
    import random
    rng = random.Random(233)
    failures = []
    for _ in range(400):
        text, limit = _cut_record(rng)
        cut = handoff.cut_whole(text, limit)
        for form in _cut_forms():
            if not set(_CUT_SECRET.findall(form(cut))) <= set(_CUT_SECRET.findall(form(text))):
                failures.append((text, limit))
                break
    assert failures == []


# --- found by this round's combined generator --------------------------------------------

#: Joining cells of the matrices above found two shapes the first version of this round's
#: fix still showed where 473fe2941 hid them: a credential flag whose name reads as a
#: secret with no rule around it (473fe2941 hid it only where a union or header took the
#: whole line), and a line-start header whose value an unclosed escaped quote had already
#: redacted to a bare carriage return, so neither header scan read past the line.
COMBINED_FOUND = {
    "bare-secret-flag": ("--password --db-SECRET7k9-password\nSECRET7nxt9", "--password [REDACTED]\n[REDACTED]"),
    "bare-secret-flag-same-line": ("deploy --SECRET7abc1_token value", "deploy [REDACTED] [REDACTED]"),
    "header-after-carriage-return": ('x\nAuthorization: \\" SECRET7nl1276x -v\r--password --passwordSECRET7a1_token\nSECRET7nxt9',
                                     "x\nAuthorization: [REDACTED]\n[REDACTED]"),
    "union-then-bare-flag": ('Authorization:"Authorization:\ny api_key: Bearer\r\nSECRET7un234x rest--password '
                             "--db-SECRET7k9-password\nSECRET7nxt9", None),
}


@pytest.mark.parametrize("text,scrubbed", COMBINED_FOUND.values(), ids=COMBINED_FOUND.keys())
def test_what_the_combined_generator_found_is_hidden(text, scrubbed):
    _assert_hidden(text)
    assert _splits(text) == [] and _unstable(text) == {}
    if scrubbed is not None:
        assert handoff.scrub_secrets(text)[0] == scrubbed


def _combined_text(rng) -> str:
    pool = _COMBINED_POOL
    parts = [rng.choice(pool) for _ in range(rng.randint(2, 4))]
    return parts[0] + "".join(rng.choice((" ", "\n", "\n  ", "", "\r\n", "\r", "  \n", "\t", "\n\n")) + part
                              for part in parts[1:])


_COMBINED_POOL = [*flag_matrix(), *union_matrix(), *next_line_matrix()[::4], *(quoted for quoted, _ in ownership_matrix()),
                  *P4_OPENED, *P2_1.values(), *CHECK_2.values(),
                  *(shape.format(n="Cookie", m="Authorization", s="SECRET7cx1") for shape in CUT_HEADERS)]


def test_joined_cells_show_nothing_a_reference_hides():
    """Invariants 1 to 3 over 400 texts of two to four cells joined (seed 2392); the
    round's scratch run took 4,500 over three seeds, all clean once the two shapes
    above were fixed."""
    import random
    rng = random.Random(2392)
    failures = []
    for _ in range(400):
        text = _combined_text(rng)
        if (leaks := _new_leaks(text)) or _splits(text) or _unstable(text):
            failures.append((text, leaks))
    assert failures == []
