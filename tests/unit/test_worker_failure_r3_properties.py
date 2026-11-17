"""C-5.10, C-23.14, C-25.7, C-27.1: review r3 of #308's three findings, as properties over
generated text.

- **The terminal readings keep bb118f63d's masks** (P1). Fix r2 read format characters and
  the information separators as invisible, which lost a mask bb118f63d's reading had
  (`x\\u200bBearer\\x1b[0m <token>`). bb118f63d's terminal layer is frozen below, driven
  by the live scrubber, and every character it masks stays masked on the handler, the tail
  and the follower.
- **The follower hides in any split what it hides whole** (P2). A key an invisible
  character splits, its value on the next feed, was written.
- **An approval masks every password character** (P1). A URL password holding `@`, `/`,
  `?`, `&` or `:` is masked whole, but for its shell metacharacters, which an approval
  never hides (IR-20), and a URL with a password never gives no span.

Every value is made up. Values are drawn from `POOL`, letters no other part of a generated
text, a placeholder or an escape holds, each used once a text, so a character the output
lacks is one the output masks.
"""
from __future__ import annotations

import copy
import logging
import re

from hypothesis import given, settings, strategies as st

from subfleet import daemon_log
from subfleet.conversations import redact
from subfleet.sessions.handoff import REDACTED, settled_end

# --- bb118f63d's terminal layer, frozen (`git show bb118f63d:subfleet/daemon_log.py`) -----
# Its lines 141-193, 351-355, 607-610 and 805-861, verbatim but for their names. What they
# call outside the terminal layer (the scrubber, the cut, the settle rule) is the live code,
# so the two sides differ only in how a text is read as a terminal shows it.

_B_RUN = re.compile(r"\S+")
_B_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_B_HIDDEN_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f]")
_B_TERMINAL_SEQUENCE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[ -/]*[0-~]?")


def _b_rendered(text: str) -> str:
    return _B_HIDDEN_CONTROL.sub("", _B_TERMINAL_SEQUENCE.sub("", text))


def _b_visible(text: str) -> str:
    return _B_CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _b_terminal_safe(source: str, scrubbed: str, scrub) -> str:
    if not _B_CONTROL.search(source):
        return scrubbed
    return _b_controls_shown(scrubbed, scrub(_b_rendered(source)))


def _b_controls_shown(text: str, rendered: str) -> str:
    shown = set(_B_RUN.findall(rendered))

    def run(match: re.Match) -> str:
        plain = _b_rendered(match.group())
        return match.group() if not plain or plain in shown else REDACTED

    return _b_visible(_B_RUN.sub(run, text))


class FrozenFormatter(daemon_log.ScrubbingFormatter):
    """bb118f63d's `ScrubbingFormatter._scrubbed`."""

    def _scrubbed(self, record: logging.LogRecord) -> str:
        view = copy.copy(record)
        view.message = view.getMessage()
        if self.usesTime():
            view.asctime = self.formatTime(view, self.datefmt)
        parts = [self.formatMessage(view), *self._blocks(record)]
        groups = [daemon_log._GROUP_MARK in part for part in parts]
        (line, *blocks), message = daemon_log._within(parts, daemon_log.RECORD_SCRUB_MAX), parts[0]
        text = self._scrubbed_parts(record, line, blocks, message, groups)
        if any(_B_CONTROL.search(part) for part in (line, *blocks)):
            rendered = self._scrubbed_parts(record, _b_rendered(line), [_b_rendered(block) for block in blocks],
                                            message, groups)
            text = _b_controls_shown(text, rendered)
        return text


def frozen_tail(text: str, count: int) -> list[str]:
    """bb118f63d's `scrubbed_tail`."""
    group = daemon_log._GROUP_MARK in text
    if len(text) > daemon_log._tail_limit(text, count):
        resolved = daemon_log._reminders_resolved(text)
        if resolved is not None:
            text = resolved
    cut = daemon_log.cut_whole(text, daemon_log._tail_limit(text, count))
    block = _b_terminal_safe(cut, daemon_log.scrubbed_block(cut, group=group, keep_lines=True),
                             lambda rendered: daemon_log.scrubbed_block(rendered, group=group, keep_lines=True))
    return daemon_log.neutralised(block).splitlines()[-count:]


class FrozenFollower(daemon_log.LogFollower):
    """bb118f63d's `LogFollower`."""

    def __init__(self, before: str):
        complete = before.rfind("\n") + 1
        start = max(0, complete - daemon_log.FOLLOW_CONTEXT - daemon_log.FOLLOW_HOLD_MAX)
        if start:
            newline = before.find("\n", start - 1, complete)
            start = complete if newline < 0 else newline + 1
        settled = start + settled_end(before[start:complete], 0)
        if complete - settled > daemon_log.FOLLOW_HOLD_MAX:
            settled = complete
        self.context = daemon_log._context_of(before[:settled])
        self.shown = before[settled:complete]
        self.pending = before[complete:]

    def feed(self, text: str) -> list[str]:
        self.pending += text
        complete = self.pending.rfind("\n") + 1
        if not complete:
            return []
        before = self.context + self.shown
        settled = settled_end(before + self.pending[:complete], len(self.context)) - len(before)
        if settled <= 0:
            if complete <= daemon_log.FOLLOW_HOLD_MAX:
                return []
            settled = complete
        return self._write(settled)

    def _write(self, end: int) -> list[str]:
        written, self.pending = self.pending[:end], self.pending[end:]
        before = self.context + self.shown
        scrubbed_block, neutralised, mark = daemon_log.scrubbed_block, daemon_log.neutralised, daemon_log._GROUP_MARK
        own = neutralised(scrubbed_block(written, group=mark in written, keep_lines=True))
        if before:
            around = before + written
            shown = neutralised(scrubbed_block(around, group=mark in around, keep_lines=True))
            own = daemon_log._only(own, set(_B_RUN.findall(shown)) | {"|", "+"})
        own = _b_terminal_safe(written, own, lambda rendered: neutralised(
            scrubbed_block(rendered, group=mark in rendered, keep_lines=True)))
        self.context, self.shown = daemon_log._context_of(before + written), ""
        return own.splitlines()


# --- generated text -----------------------------------------------------------------------

#: The letters a value is drawn from: none is in a key, a filler, `[REDACTED]`, an escape
#: (`\xNN`, lower-case hex), a reminder mark or an omission marker.
POOL = "FGHIJKLMNPQSUVWXYZ"
VALUE_LENGTH = 8                      # `Bearer`'s shortest token
KEYS = ["Bearer", "password", "Authorization: Bearer", "token", "secret", "api_key", "passwd", "Basic", "auth"]
#: A word before a key; one ending in a format character gives `Bearer` a word boundary in
#: bb118f63d's reading and none in fix r2's (review r3's P1).
BEFORE = ["x\u200b", "", "x", "login refused ", "x\u00ad", "x\u2060", "x\ufeff", "x\x1f", "x="]
#: Characters a terminal draws nothing for, or acts on: format characters, the information
#: separators, control sequences and other controls.
INVISIBLES = ["\x1b[0m", "\u200b", "\x1f", "\u00ad", "\x07", "\ufeff", "\x1c", "\u2060", "\x00", "\x9b",
              "\x7f", "\x08", "\x85", "\u202e", "\u180e", "\x1b", "\x1d", "\x1e"]
SEPARATORS = [" ", ":\n", "=", ": ", ":", "\n", "=\n", "\t", "='", '="']
ENDS = ["\n", "", " ", "'", '"', " for x", *INVISIBLES]
#: Nothing, half the time; else one of `INVISIBLES`.
MAYBE_INVISIBLE = st.one_of(st.just(""), st.sampled_from(INVISIBLES))


def _often(first, choices: list):
    """`first` about half the time, else any of `choices`."""
    return st.one_of(st.just(first), st.sampled_from(choices))


@st.composite
def keyed_texts(draw, key="Bearer", cut=0, before="x\u200b", inside="", after="\x1b[0m", separator=" ") -> str:
    """One or two values, each after a key: a word may come before the key, and an
    invisible or control character before it, inside it, after it and after its
    separator. Each part is its first choice (the arguments, review r3's P1 shape
    `x\u200bBearer\x1b[0m <value>` by default) about half the time, so that shape is
    drawn often and others around it."""
    letters = draw(st.permutations(POOL))
    parts = []
    for index in range(draw(st.integers(1, 2))):
        name = draw(_often(key, KEYS))
        at = min(draw(st.one_of(st.just(cut), st.integers(0, len(name)))), len(name))
        parts += [draw(_often(before, BEFORE)), draw(MAYBE_INVISIBLE), name[:at], draw(_often(inside, INVISIBLES)),
                  name[at:], draw(_often(after, ["", *INVISIBLES])), draw(_often(separator, SEPARATORS)),
                  draw(MAYBE_INVISIBLE), "".join(letters[index * VALUE_LENGTH:(index + 1) * VALUE_LENGTH]),
                  draw(st.sampled_from(ENDS)), draw(st.sampled_from(["", " ", "\n"]))]
    return "".join(parts)


@st.composite
def split_texts(draw) -> tuple[str, list[str]]:
    """A keyed text and the feeds it arrives in: cut after some of its lines, or at any
    points, or both. Its first choices are review r3's P2 shape, a key an invisible
    character splits, then its value on the next line (`pass\u200bword:\n<value>`)."""
    text = draw(keyed_texts(key="password", cut=4, before="", inside="\u200b", after="", separator=":\n")) + "\n"
    cuts = set(draw(st.lists(st.integers(0, len(text)), max_size=6)))
    if draw(st.booleans()):
        cuts |= {at + 1 for at, char in enumerate(text) if char == "\n"}
    cuts = sorted(cuts)
    return text, [text[start:end] for start, end in zip([0, *cuts], [*cuts, len(text)]) if end > start]


def _masked_letters(text: str, written: str) -> set[str]:
    return {letter for letter in POOL if letter in text and letter not in written}


def _runs(written: str) -> set[str]:
    return set(_B_RUN.findall(written))


def _follow(follower: daemon_log.LogFollower, chunks: list[str]) -> str:
    lines = []
    for chunk in chunks:
        lines += follower.feed(chunk)
    return "\n".join(lines + follower.flush())


def _routes(text: str) -> dict[str, tuple[str, str]]:
    """Each route's output, (bb118f63d's terminal layer, the head's), for `text`."""
    record = daemon_log._record(text, logging.INFO)
    return {
        "message": (FrozenFormatter().format(record), daemon_log.scrubbed(text)),
        "tail": ("\n".join(frozen_tail(text + "\n", 40)), "\n".join(daemon_log.scrubbed_tail(text + "\n", 40))),
        "follow": (_follow(FrozenFollower("earlier\n"), [text + "\n"]),
                   _follow(daemon_log.LogFollower("earlier\n"), [text + "\n"])),
    }


REVIEW_R3_P1 = "x​Bearer\x1b[0m FGHIJKLMNPQ"


def test_r3_the_frozen_oracle_masks_review_r3s_input():
    """The oracle is bb118f63d's reading: it hides review r3's P1 value on every route."""
    for route, (frozen, _) in _routes(REVIEW_R3_P1).items():
        assert "FGHIJKLMNPQ" not in frozen, (route, frozen)


@settings(max_examples=120, derandomize=True, database=None, deadline=None)
@given(text=keyed_texts())
def test_r3_no_character_bb118f63d_masked_is_written(text):
    """Invariant (review r3 of #308, P1): on the handler, the tail and the follower, every
    character bb118f63d's terminal layer masks stays masked, and every run the head writes
    that holds no `[REDACTED]` is one bb118f63d writes too."""
    for route, (frozen, head) in _routes(text).items():
        assert _masked_letters(text, frozen) <= _masked_letters(text, head), (route, text, frozen, head)
        assert {run for run in _runs(head) if REDACTED not in run} <= _runs(frozen), (route, text, frozen, head)


# --- the follower, fed in any split (review r3, P2) ----------------------------------------

REVIEW_R3_P2 = ["pass​word:\n", "FGHIJKLMNPQ\n"]


def test_r3_an_invisible_key_split_across_feeds_is_still_a_password():
    """Review r3's P2 input: the key, its line, then the value on the next feed."""
    assert "FGHIJKLMNPQ" not in _follow(daemon_log.LogFollower(""), REVIEW_R3_P2)
    assert "FGHIJKLMNPQ" not in _follow(daemon_log.LogFollower("earlier\n"), REVIEW_R3_P2)


#: A private key a format character splits in its first line: the text holds no key's first
#: line, so each line settled and its body was written before the key's end, which hides
#: it in the terminal reading, arrived.
PRIVATE_KEY_LINES = ["-----BEGIN RSA PRIV\u200bATE KEY-----\n", "MIIEFGHIJKLMNPQSUVWXYZ\n",
                     "-----END RSA PRIVATE KEY-----\n"]


def test_r3_a_private_key_an_invisible_character_splits_is_held_until_it_ends():
    """The follower settles a break in each terminal reading too (`_settled_end`)."""
    whole = "\n".join(daemon_log.scrubbed_tail("".join(PRIVATE_KEY_LINES), 10))
    assert "FGHIJKLMNPQSUVWXYZ" not in whole
    for before in ("", "earlier\n"):
        assert "FGHIJKLMNPQSUVWXYZ" not in _follow(daemon_log.LogFollower(before), PRIVATE_KEY_LINES)


@settings(max_examples=120, derandomize=True, database=None, deadline=None)
@given(case=split_texts())
def test_r3_a_follower_fed_in_any_split_hides_what_it_hides_whole(case):
    """Invariant (review r3 of #308, P2): fed the same text in any split, the follower
    masks every character it masks fed whole, and every one `daemon logs` masks."""
    text, chunks = case
    split = _follow(daemon_log.LogFollower(""), chunks)
    whole = _follow(daemon_log.LogFollower(""), [text])
    tail = "\n".join(daemon_log.scrubbed_tail(text, 40))
    assert _masked_letters(text, whole) | _masked_letters(text, tail) <= _masked_letters(text, split), (
        chunks, whole, tail, split)


# --- an approval's URL password (review r3, P1) --------------------------------------------

#: An approval's mask, as `_head_mask_approval` and release's masking write one.
MASK = r"\[masked [a-z-]+, \d+ chars\]"
#: The shell metacharacters an approval never hides (`redact._SHELL_META`, IR-20).
META = re.compile("|".join(re.escape(meta) for meta in redact._SHELL_META))
#: Review r2's and r3's URLs, each password's characters split at its `@` and `/`.
REVIEW_URLS = {
    "r2-at-then-slash": ("https://alice:", "p@ss/FGHIJKLMNPQ", "@host.test"),
    "r3-query-ampersand": ("redis://:", "FGHIJKLMNPQ", "@cache.test/x?x=2&owner=@me"),
}


def _masked_whole(command: str, prefix: str, password: str, pieces: list[str]) -> None:
    """`command`'s approval masks every character of `password`, which follows `prefix`:
    from the user's `:` there are only masks and shell metacharacters, then what follows
    the password or a suffix of it (masking past the password is allowed); no piece is
    shown; every shell metacharacter is shown, in order; and there is a span."""
    masked, spans = redact.mask_approval({"command": command})
    out = masked["command"]
    start = command.index(prefix + password) + len(prefix)
    after = command[start + len(password):]
    assert spans and out[:start] == command[:start], out
    region = re.match(f"(?:{MASK}|{META.pattern})+", out[start:])
    assert region is not None, out
    rest = out[start + region.end():]
    assert rest.startswith("@") and after.endswith(rest), out
    assert [piece for piece in pieces if piece in out] == [], out
    assert META.findall(out) == META.findall(command), out


def test_r3_review_urls_mask_their_whole_password():
    for prefix, password, rest in REVIEW_URLS.values():
        for wrap in ('curl "{}"', "curl {}", "{}"):
            _masked_whole(wrap.format(prefix + password + rest), prefix, password,
                          [piece for piece in re.split(r"[@/?&:]", password) if len(piece) > 2])


ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@st.composite
def password_urls(draw) -> tuple[str, str, str, list[str]]:
    """A command holding a URL whose password holds `@`, `/`, `?`, `&` and `:`, and whose
    query or fragment may hold `@`; with the prefix to its password, the password and
    its pieces."""
    pieces = draw(st.lists(st.text(ALNUM, min_size=6, max_size=9), min_size=1, max_size=5, unique=True))
    joins = draw(st.lists(st.sampled_from("@/?&:"), min_size=len(pieces) - 1, max_size=len(pieces) - 1))
    password = pieces[0] + "".join(join + piece for join, piece in zip(joins, pieces[1:]))
    scheme = draw(st.sampled_from(["https", "redis", "postgres", "my_db", "9p"]))
    user = draw(st.sampled_from(["", "alice", "svc@tenant"]))
    host = draw(st.sampled_from(["host.test", "cache.test:6379", "db.test/main"]))
    query = draw(st.sampled_from(["", "?x=2&owner=@me", "?a=@b", "?q=1&r=2"]))
    fragment = draw(st.sampled_from(["", "#@top", "#frag"]))
    prefix = f"{scheme}://{user}:"
    url = f"{prefix}{password}@{host}{query}{fragment}"
    command = draw(st.sampled_from(['curl "{}"', "curl {}", "{}", 'git clone {} && echo done'])).format(url)
    return command, prefix, password, pieces


@settings(max_examples=60, derandomize=True, database=None, deadline=None)
@given(case=password_urls())
def test_r3_an_approval_masks_every_password_character_but_shell_metacharacters(case):
    """Invariant (review r3 of #308, P1): no piece of a URL's password is shown, its
    masks run from the user's `:` to an `@` with only shell metacharacters between them,
    every shell metacharacter of the command is shown in order, and a URL with a password
    gives a span."""
    _masked_whole(*case)
