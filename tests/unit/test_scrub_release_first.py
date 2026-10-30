"""C-23.14 (#239 round 8): every mode runs release/217's pipeline for that mode, and writes its
output but for the runs the head's own pipeline for that mode does not hold, so no mode shows a
`SECRET7` value that release's same mode hides, or one the head's hides.

- **The frozen copy.** `subfleet/sessions/scrub_release.py` is release/217's scrubbing half at
  `2c981cba5`, verbatim but for its imports (`test_scrub_release_frozen` pins it).
- **The composition, mode by mode** (`handoff.shown_by_both`). Each mode's output is release's
  output of that mode with each whitespace-free run that the head's output of the same mode,
  of the same text, does not hold replaced by `[REDACTED]`. Each pipeline is the mode's
  whole one, fold and second scrub included (review r8, P2-1: composing each scrub loses what
  release's second pass reads in its own fold): the excerpt and whole scrubs, `scrub_bounded`
  and `clean` (release's pieces, each kept for the head's scrub of the same piece of text,
  then cut: within the budget one piece, the whole-text scrub, which `truncate` then cuts,
  C-23.36; past it each excerpt, cut as release cuts its own, review r9b P2-1), daemon.log's
  folded line, `detail_line` and `suggestions.scrubbed_line` (release's line kept for the
  head's, then cut, review r9b P2-2), a suggestion's brief (`normalize`) and daemon.log's
  block form. `mask_approval` masks what release's masking leaves
  (`redact._head_mask_approval`).
- **Bounded modes compose first, then cut** (#239 rounds 9 and 10). A cut piece holds the
  start or end of a run the head's scrub holds only whole, which the composition would hide,
  so each bounded mode keeps release's output for the head's runs before it cuts. Its cut
  can keep a character release's own cut left out, but none either scrub hides.
- **The argument, executed.** The second stage only replaces whole runs of release's output
  with the placeholder, so every character it writes outside a placeholder is release's, in
  order; and it keeps a run only if the head's output holds it, so every `SECRET7` value it
  shows the head shows too.
- **Measured.** Over every review's regression strings and r8's nine inputs, no mode shows a
  value release's same mode hides, nor one the head's hides."""

from __future__ import annotations

import contextlib
import functools
import importlib
import os
import random
import re

from hypothesis import given, strategies as st
import pytest

from subfleet import daemon_log, refusals
from subfleet.conversations import redact, suggestions
from subfleet.conversations.store import ConversationError
from subfleet.sessions import handoff, scrub_release
from tests.unit.test_scrub_key_order import CI, _revealed

SECRET = re.compile(r"SECRET7[A-Za-z0-9_]*")
PREFIX = "request submit refused (ValueError): "


# --- each mode: release's output, kept for the runs the head's output holds ----------------------

def _line(scrub, text: str) -> str:
    return suggestions._twice(scrub, text, suggestions._fold)[0]


def _block(scrub, text: str, keep_lines: bool) -> str:
    group = bool(daemon_log._GROUP_SEPARATOR.search(text) or daemon_log._GROUP_MARGIN_LINE.search(text))
    return daemon_log._block_form(scrub, text, group, keep_lines)


def _bounded(text: str, limit: int | None, strip: bool) -> str:
    pieces, outer_strip, _, heads, _ = handoff._released_pieces(text, limit, strip)
    out = "".join(piece if kept else handoff.shown_by_both(piece, head) for (piece, kept), head in zip(pieces, heads))
    out = out.strip() if outer_strip else out
    return handoff.truncate(out, limit) if limit is not None and len(out) > limit else out


def _cut_line(text: str, limit: int = 60, mark: str = "…") -> str:
    """`scrubbed_line`'s rule: release's line kept for the runs the head's line holds, then
    cut (`bounded(shown_by_both(released, head), limit, mark)`, #239 review r9b, P2-2)."""
    released, head = _line(scrub_release.scrub_secrets, text), _line(handoff._head_scrub_secrets, text)
    return suggestions.bounded(handoff.shown_by_both(released, head), limit, mark)


def _brief(text: str) -> str:
    """A suggestion's brief as `normalize` keeps it; "" where nothing of it is left."""
    try:
        return suggestions.normalize("title", "summary", text)[0]["brief"]
    except ConversationError as error:
        assert error.reason == "bad-brief", error
        return ""


R, H = scrub_release.scrub_secrets, handoff._head_scrub_secrets

#: Each mode: as the code runs it, release's pipeline for it, and the head's. `scrubbed_line`'s
#: are the two lines it composes before its cut (#239 review r9b, P2-2), so a value it shows
#: is one both lines show; a character past release's own cut may show.
MODES = {
    "excerpt": (lambda t: handoff.scrub_secrets(t)[0], lambda t: R(t)[0], lambda t: H(t)[0]),
    "excerpt, reminders": (lambda t: handoff.scrub_secrets(t, strip_reminders=True)[0],
                           lambda t: R(t, strip_reminders=True)[0], lambda t: H(t, strip_reminders=True)[0]),
    "whole": (lambda t: handoff.scrub_secrets(t, whole=True)[0], lambda t: R(t, whole=True)[0],
              lambda t: H(t, whole=True)[0]),
    "clean": (lambda t: handoff.clean(t, 4000)[0], lambda t: scrub_release.clean(t, 4000)[0],
              lambda t: handoff._head_scrub_bounded(t, 4000, strip_reminders=True)[0]),
    "daemon.log line": (lambda t: suggestions._scrub_line(t)[0], lambda t: _line(R, t), lambda t: _line(H, t)),
    "detail_line": (lambda t: suggestions._scrub_line(PREFIX + t)[0], lambda t: _line(R, PREFIX + t),
                    lambda t: _line(H, PREFIX + t)),
    "scrubbed_line": (lambda t: refusals.scrubbed_line(t, 60), lambda t: _line(R, t), lambda t: _line(H, t)),
    "brief": (lambda t: _brief(t),
              lambda t: suggestions._twice(R, t, suggestions._line_ends)[0],
              lambda t: suggestions._twice(H, t, suggestions._line_ends)[0]),
    "block": (lambda t: daemon_log.scrubbed_block(t), lambda t: _block(R, t, False), lambda t: _block(H, t, False)),
    "kept block": (lambda t: daemon_log.scrubbed_block(t, keep_lines=True), lambda t: _block(R, t, True),
                   lambda t: _block(H, t, True)),
    "mask_approval": (lambda t: redact.mask_approval({"c": t})[0]["c"],
                      lambda t: scrub_release.mask_approval({"c": t})[0]["c"],
                      lambda t: redact._head_mask_approval({"c": t})[0]["c"]),
}

#: Each mode's composition, from release's pipeline and the head's.
COMPOSED = {
    **{mode: (lambda release, head: lambda t: handoff.shown_by_both(release(t), head(t)))(release, head)
       for mode, (_, release, head) in MODES.items()},
    "clean": lambda t: _bounded(t, 4000, True),
    "scrubbed_line": _cut_line,
    "brief": lambda t: (lambda kept: kept if kept.strip() else "")(handoff.shown_by_both(
        suggestions._twice(R, t, suggestions._line_ends)[0], suggestions._twice(H, t, suggestions._line_ends)[0])),
    "mask_approval": lambda t: redact._head_mask_approval(scrub_release.mask_approval({"c": t})[0])[0]["c"],
}

#: Each bounded mode's limit.
LIMITS = {"clean": 4000, "scrubbed_line": 60}

#: Review r8's nine inputs (`test_scrub_review_r8`), which the head showed before this round.
R8 = importlib.import_module("tests.unit.test_scrub_review_r8").R8


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)


@functools.cache
def regression_strings() -> tuple[str, ...]:
    """Every `SECRET7` string the review modules hold, as review r7's corpus gathers them."""
    texts = []
    for name in ("test_scrub_review_r2", "test_scrub_review_r3", "test_scrub_review_r4", "test_scrub_review_r5",
                 "test_scrub_review_r6", "test_scrub_review_r7", "test_scrub_review_r8", "test_scrub_inline_headers",
                 "test_scrub_no_new_leak"):
        module = importlib.import_module(f"tests.unit.{name}")
        for attr, value in vars(module).items():
            if attr.startswith("__") or callable(value) and not isinstance(value, (dict, list, tuple)):
                continue
            texts += [s for s in _strings(value) if SECRET.search(s) and "{" not in s and len(s) < 4000]
    return tuple(dict.fromkeys(texts))


#: The default sample of the regression strings; `SF_SCRUB_FULL_MATRIX=1` reads them all.
SAMPLE = 300


def _texts() -> list[str]:
    strings = list(regression_strings())
    if os.environ.get("SF_SCRUB_FULL_MATRIX") != "1":
        strings = random.Random(8239).sample(strings, min(SAMPLE, len(strings)))
    return [*R8.values(), *strings]


@pytest.mark.parametrize("mode", MODES)
def test_each_mode_is_release_s_output_kept_for_the_runs_the_head_s_holds(mode):
    """The code's output of each mode is the composition's, on every text; a bounded mode's is
    the composition cut (`scrubbed_line`: `bounded(shown_by_both(released, head), limit,
    mark)`), within its limit."""
    run, composed = MODES[mode][0], COMPOSED[mode]
    assert [text for text in _texts() if run(text) != composed(text)] == []
    if mode in LIMITS:
        assert [text for text in _texts() if len(run(text)) > LIMITS[mode]] == []


def _new(values, out: str, other: str) -> list[str]:
    return sorted(value for value in values if value in out and value not in other)


@pytest.mark.parametrize("mode", MODES)
def test_no_mode_shows_a_value_release_s_or_the_head_s_mode_hides(mode):
    run, release, head = MODES[mode]
    shown = {}
    for text in _texts():
        values, out = set(SECRET.findall(text)), run(text)
        if (more := _new(values, out, release(text)) + _new(values, out, head(text))):
            shown[text] = more
    assert shown == {}


# --- the bounded modes: release's pieces ---------------------------------------------------------

@contextlib.contextmanager
def _bounds(chars: int):
    """Both scrubbers' bounds with excerpts of `chars` characters, so a short text is cut."""
    with pytest.MonkeyPatch.context() as patch:
        for module in (handoff, scrub_release):
            patch.setattr(module, "EXCERPT_CHARS", chars)
            patch.setattr(module, "MAX_SCRUB_CHARS", 2 * chars)
        yield


PIECE_LINES = ("ordinary line", "token: SECRET7a1", "Authorization: Basic SECRET7b2", "x --password",
               "curl -H 'Cookie: sid=SECRET7c3'", "<system-reminder>r</system-reminder>", "Bearer abcdefgh123",
               "api_key='x", "", "   ", "token:--SECRET7d4_token SECRET7e5")

#: A written marker, whose count `_uncounted` sets aside.
MARKER = re.compile(r"… \[([\d,]+) characters omitted\] …")


def _uncounted(out: str) -> str:
    return MARKER.sub("… [N characters omitted] …", out)


def _composed_release(text: str, limit: int | None, strip: bool) -> str:
    """Release's own `scrub_bounded`, each text it scrubs kept for the runs the head's scrub of
    that text holds (`shown_by_both`), each excerpt cut where both scrubbers allow: the bounded
    mode's rule, composed first, then cut as release cuts (#239 rounds 9 and 10). Release's
    code counts its marker from what it writes, the composed excerpts, so only that count can
    differ from the mode's, which is release's own."""
    scrub, head_cut, tail_cut = scrub_release._scrub, scrub_release._head_cut, scrub_release._tail_cut
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scrub_release, "_scrub", lambda part, strip_reminders: (handoff.shown_by_both(
            scrub(part, strip_reminders)[0], handoff._scrub(part, strip_reminders)[0]), 0))
        patch.setattr(scrub_release, "_head_cut", lambda part: min(head_cut(part), handoff._head_cut(part)))
        patch.setattr(scrub_release, "_tail_cut", lambda part: max(tail_cut(part), handoff._tail_cut(part)))
        return scrub_release.scrub_bounded(text, limit, strip_reminders=strip)[0]


@CI
@given(lines=st.lists(st.sampled_from(PIECE_LINES), min_size=1, max_size=24),
       limit=st.sampled_from((None, 30, 80, 200, 4000)), strip=st.booleans(), chars=st.sampled_from((40, 90, 400)))
def test_release_s_pieces_join_to_release_s_output(lines, limit, strip, chars):
    """`_released_pieces` is release's `scrub_bounded` as the pieces it joins, beside the head's
    scrub of the same text; an excerpt is cut where both scrubbers allow. Each piece is kept
    for the runs the head's scrub of the same text holds, then cut (#239 rounds 9 and 10):
    within the budget the bounded mode is the whole-text mode, cut (C-23.36); past it each
    excerpt is cut as release cuts its own, with release's marker between them, the count of
    what release's own cut of the same excerpts leaves out. So the mode writes release's own
    `scrub_bounded` with its scrubs composed first (`_composed_release`), and release's marker
    wherever both scrubbers cut alike; it shows no value that release's scrub of the text
    hides, or the head's; and it keeps to `limit`. Where both cut alike, the pieces join to
    release's output if nothing cuts them (within the budget, or no limit) or if both
    scrubbers scrub each excerpt alike."""
    text = "\n".join(lines)
    with _bounds(chars):
        pieces, outer_strip, count, heads, _ = handoff._released_pieces(text, limit, strip)
        joined = "".join(piece for piece, _ in pieces)
        joined = joined.strip() if outer_strip else joined
        released, released_count = scrub_release.scrub_bounded(text, limit, strip_reminders=strip)
        resolved = (scrub_release._resolve_blocks(text, strip, scrub_release._Tally())
                    if len(text) > scrub_release.MAX_SCRUB_CHARS else text)
        within = len(resolved) <= scrub_release.MAX_SCRUB_CHARS
        alike = within or (scrub_release._head_cut(resolved), scrub_release._tail_cut(resolved)) == (
            handoff._head_cut(resolved), handoff._tail_cut(resolved))
        uncut, _, _, uncut_heads, _ = handoff._released_pieces(text, None, strip)
        scrubbed_alike = all(kept or handoff.shown_by_both(piece, head) == piece
                             for (piece, kept), head in zip(uncut, uncut_heads))
        if alike and (within or limit is None or scrubbed_alike):
            bounded = joined if limit is None or len(joined) <= limit else handoff.truncate(joined, limit)
            assert (bounded, count) == (released, released_count)
        assert len(heads) == len(pieces)
        out = handoff.scrub_bounded(text, limit, strip_reminders=strip)[0]
        assert out == _bounded(text, limit, strip)
        assert _uncounted(out) == _uncounted(_composed_release(text, limit, strip))
        if alike and not within:
            assert MARKER.findall(out) == MARKER.findall(released)
        values = set(SECRET.findall(text))
        if within:
            whole = handoff.scrub_bounded(text, None, strip_reminders=strip)[0]
            assert out == (whole if limit is None else handoff.truncate(whole, limit))
        assert _new(values, out, scrub_release.scrub_bounded(text, None, strip_reminders=strip)[0]) == []
        assert _new(values, out, handoff._head_scrub_bounded(text, None, strip_reminders=strip)[0]) == []
        assert limit is None or len(out) <= limit


# --- the argument: the second stage only replaces release's runs ---------------------------------

ATOMS = ("token", "password", "api_key", "Authorization", "Cookie", "Set-Cookie", "Bearer", "bearer", "--password",
         "--1_token", "secret", ":", "=", " ", "\n", "\r", "\t", "'", '"', "\\", ";", ",", "x", "SECRET7a1", "SECRET7b",
         "abcdefgh12", "[REDACTED]", "-", "_", "a=1", "  | ", "+-+---------------- 1 ----------------",
         "… [12 characters omitted] …", "eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl", "ghp_abcdefghijklmnop1234",
         "<system-reminder>", "</system-reminder>")


@CI
@given(atoms=st.lists(st.sampled_from(ATOMS), max_size=40))
def test_each_mode_only_replaces_release_s_runs_and_shows_nothing_the_head_hides(atoms):
    """Every character a mode writes outside a placeholder is one of release's output of the
    mode, in order (`_revealed`; a mask's own words and `bounded`'s mark aside), every
    `SECRET7` run it writes is one that output holds, and every `SECRET7` value it shows
    the head's output shows. `scrubbed_line`'s are the lines it composes and then cuts, as
    `bounded(shown_by_both(released, head), limit, mark)`, within its limit (#239 review
    r9b, P2-2)."""
    text = "".join(atoms)
    values = set(SECRET.findall(text))
    for mode, (run, release, head) in MODES.items():
        out, released = run(text), release(text)
        bare = re.sub(r"\[masked [a-z-]+, \d+ chars\]", " ", out) if mode == "mask_approval" else out
        bare = bare.replace("…", " ") if mode == "scrubbed_line" else bare
        assert _revealed(released, bare) == "", (mode, text, out)
        assert [found for found in SECRET.findall(bare) if found not in released] == [], (mode, text, out)
        assert _new(values, out, head(text)) == [], (mode, text, out)
        if mode in LIMITS:
            assert out == COMPOSED[mode](text) and len(out) <= LIMITS[mode], (mode, text, out)


def test_an_approval_keeps_the_mask_release_writes():
    """Release masks the whole `Bearer` run that ends in a flag with nothing after it; the
    head's masking alone leaves the flag's name (`--secret`) shown, so the approval keeps
    release's mask."""
    request = {"command": "Bearer SECRET7--secret"}
    assert scrub_release.mask_approval(request)[0] == {"command": "[masked bearer, 22 chars]"}
    assert redact._head_mask_approval(request)[0] == {"command": "[masked bearer, 14 chars]--secret"}
    masked, spans = redact.mask_approval(request)
    assert masked == {"command": "[masked bearer, 22 chars]"} and [span["length"] for span in spans] == [22]
