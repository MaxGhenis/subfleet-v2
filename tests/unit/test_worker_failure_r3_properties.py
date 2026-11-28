"""C-5.10, C-23.14, C-25.7, C-27.1: review r3 of #308's three findings, as properties over
generated text.

- **The terminal readings keep bb118f63d's masks** (P1). Fix r2 read format characters and
  the information separators as invisible, which lost a mask bb118f63d's reading had
  (`x\\u200bBearer\\x1b[0m <token>`). bb118f63d's terminal layer is frozen below, driven
  by the live scrubber, and every character it masks stays masked on the handler, the tail
  and the follower.
- **The follower hides in any split what it hides whole** (P2). A key an invisible
  character splits, its value on the next feed, was written.
- **An approval masks exactly the password a client is sent** (P1; fix r4, after review
  r4). Fix r3 masked to the last `@` before whitespace and kept shell metacharacters in
  view, which hid commands and the real host. Now the password is found on the request as
  submitted: the authority ends at the first `/`, `?`, `#`, whitespace, unquoted shell
  operator, or `$`, backtick or backslash outside single quotes, the userinfo at its last
  `@`, and the password is the userinfo after the user's first `:`. Inside quotes a shell
  operator is a password character. So review r2's `https://alice:p@ss/FGHIJKLMNPQ@host.test`
  has the password `p` and the host `ss`, and the rest is a path that shows.

Every value is made up. Values are drawn from `POOL`, letters no other part of a generated
text, a placeholder or an escape holds, each used once a text, so a character the output
lacks is one the output masks.
"""
from __future__ import annotations

import copy
import logging
import re
import urllib.parse

from hypothesis import event, example, given, settings, strategies as st

from subfleet import daemon_log
from subfleet.conversations import redact
from subfleet.sessions import scrub_release
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
#: Nothing, or one of `INVISIBLES`: one of two branches.
MAYBE_INVISIBLE = st.one_of(st.just(""), st.sampled_from(INVISIBLES))


def _often(first, choices: list):
    """`first`, or any of `choices`: one of two branches."""
    return st.one_of(st.just(first), st.sampled_from(choices))


@st.composite
def keyed_texts(draw, key="Bearer", cut=0, before="x\u200b", inside="", after="\x1b[0m", separator=" ") -> str:
    """One or two values, each after a key: a word may come before the key, and an
    invisible or control character before it, inside it, after it and after its
    separator. Each part is its first choice (the arguments, review r3's P1 shape
    `x\u200bBearer\x1b[0m <value>` by default) or any choice, one of two branches, so
    that shape is drawn often and others around it."""
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


# --- an approval's URL password (review r3, P1; the rule fix r4 made) -----------------------

#: An approval's mask, as `_head_mask_approval` and release's masking write one.
MASK = r"\[masked [a-z-]+, (\d+) chars\]"
#: Review r2's and r3's URLs: what comes before the password, the password, what follows.
#: Review r2's authority is `alice:p@ss` (it ends at the `/`): the password is `p`, the host
#: `ss`, and `/FGHIJKLMNPQ@host.test` is a path that shows (fix r4).
REVIEW_URLS = {
    "r2-at-then-slash": ("https://alice:", "p", "@ss/FGHIJKLMNPQ@host.test"),
    "r3-query-ampersand": ("redis://:", "FGHIJKLMNPQ", "@cache.test/x?x=2&owner=@me"),
}


def _masked_whole(command: str, prefix: str, password: str, pieces: list[str]) -> None:
    """`command`'s approval masks exactly `password`, which follows `prefix`: the output is
    the command with the password's characters replaced by masks (release's and the
    head's, standing for exactly its length) and every other character as written; no
    piece of it shows; and there is a span."""
    masked, spans = redact.mask_approval({"command": command})
    out = masked["command"]
    start = command.index(prefix + password) + len(prefix)
    end = start + len(password)
    assert spans and out[:start] == command[:start] and out.endswith(command[end:]), out
    region = out[start:len(out) - (len(command) - end)]
    assert re.fullmatch(f"(?:{MASK})+", region), out
    assert sum(int(count) for count in re.findall(MASK, region)) == len(password), out
    assert [piece for piece in pieces if piece in out] == [], out


def test_r3_review_urls_mask_their_whole_password():
    """Each review URL, in `curl "…"`, `curl '…'`, `curl …` and bare, masks its password
    and nothing else: review r2's path after the host shows (fix r4)."""
    for prefix, password, rest in REVIEW_URLS.values():
        for wrap in ('curl "{}"', "curl '{}'", "curl {}", "{}"):
            _masked_whole(wrap.format(prefix + password + rest), prefix, password,
                          [piece for piece in re.split(r"[@/?&:]", password) if len(piece) > 2])
            assert rest in redact.mask_approval({"command": wrap.format(prefix + password + rest)})[0]["command"]


ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#: How each wrap quotes the URL it holds: "double", "single" or "none".
WRAPS = {'curl "{}"': "double", "curl '{}'": "single", "curl {}": "none", "{}": "none",
         "git clone {} && echo done": "none"}
#: Commands after the host, as review r4's table has them, each with a later `@x.test`.
TAILS = ["", ";rm${IFS}-rf${IFS}/nonexistent;@x.test", "&&{rm,-rf,nonexistent.d}&&echo@x.test",
         "|{sh,-c,id}|echo@x.test", "$(rm${IFS}nonexistent)@x.test", "`rm${IFS}nonexistent`@x.test",
         ">nonexistent.out;echo@x.test", "${IFS}@x.test"]
#: What ends the authority, by how the URL is quoted: whitespace always; a `$`, a backtick
#: or a backslash outside single quotes; a shell operator unquoted.
ENDS_AUTHORITY = {"single": "", "double": "$`\\", "none": "$`\\;&|<>()"}


def client_password(command: str, quoting: str) -> str | None:
    """The oracle: the password a client is sent, computed from what the generator placed and
    how it quoted it, never by the code under test. `urlsplit`'s password of the text after
    `://` up to the first character that ends the authority by the shell's rules, its quote
    characters removed (`urlsplit` ends the authority at `/`, `?` or `#`, and the userinfo
    at its last `@`)."""
    word = []
    for char in command[command.index("://") + 3:]:
        if char.isspace() or char in ENDS_AUTHORITY[quoting]:
            break
        word.append(char)
    return urllib.parse.urlsplit("http://" + "".join(word).replace('"', "").replace("'", "")).password


@st.composite
def password_urls(draw) -> tuple[str, str, str | None, list[str]]:
    """A command holding a URL whose password holds `@ / ? # & ; | : $`, whose user is
    empty, plain or holds an `@`, whose query or fragment may hold `@`, with a command after
    the host or none, in `curl "…"`, `curl '…'`, `curl …`, bare or `git clone … && echo
    done`. Each part but the user (review r3's empty one, or review r4's plain one) is
    review r4's P2 shape or review r3's redis URL (a double-quoted password with `;`,
    `?x=2&owner=@me`) or any choice, one of two branches.
    With the prefix before the password, the expected password (`client_password`) and the
    generated pieces it holds."""
    pieces = draw(st.lists(st.text(ALNUM, min_size=6, max_size=9), min_size=1, max_size=5, unique=True))
    joins = draw(st.lists(_often(";", list("@/?#&;|:$")), min_size=len(pieces) - 1, max_size=len(pieces) - 1))
    placed = pieces[0] + "".join(join + piece for join, piece in zip(joins, pieces[1:]))
    scheme = draw(_often("redis", ["https", "redis", "postgres", "my_db", "9p"]))
    user = draw(st.sampled_from(["", "alice", "svc@tenant"]))
    host = draw(st.sampled_from(["cache.test", "host.test", "cache.test:6379", "db.test/main"]))
    tail = draw(st.sampled_from(TAILS))
    query = draw(_often("?x=2&owner=@me", ["", "?x=2&owner=@me", "?a=@b", "?q=1&r=2"]))
    fragment = draw(st.sampled_from(["", "#@top", "#frag"]))
    wrap = draw(_often('curl "{}"', list(WRAPS)))
    prefix = f"{scheme}://{user}:"
    command = wrap.format(f"{prefix}{placed}@{host}{tail}{query}{fragment}")
    password = client_password(command, WRAPS[wrap])
    assert password is None or placed.startswith(password) or password.startswith(placed), (command, password)
    return command, prefix, password, [piece for piece in pieces if piece in (password or "")]


def _example(command: str, quoting: str) -> tuple[str, str, str | None, list[str]]:
    """A case as `password_urls` draws one, for `command`, whose URL `quoting` quotes."""
    return command, command[:command.index(":", command.index("://") + 3) + 1], client_password(command, quoting), []


#: Release's frozen rule crosses `#`, `?` and `$` to the first `@`, past where the authority
#: ends; with no password, the display is release's output and the head adds nothing.
RELEASE_PAST_THE_AUTHORITY = [("curl https://alice:AAAAAA#BBBBBB@host.test@x.test", "none"),
                              ('curl "https://alice:AAAAAA$BBBBBB@host.test"', "double"),
                              ("curl 'https://alice:AAAAAA?BBBBBB@cc@host.test/x'", "single")]


@settings(max_examples=150, derandomize=True, database=None, deadline=None)
@given(case=password_urls())
@example(case=_example(*RELEASE_PAST_THE_AUTHORITY[0]))
@example(case=_example(*RELEASE_PAST_THE_AUTHORITY[1]))
@example(case=_example(*RELEASE_PAST_THE_AUTHORITY[2]))
def test_an_approval_masks_exactly_the_password_a_client_is_sent(case):
    """Invariant (fix r4 of #308, after review r4): the display is the command with exactly
    its URL's password replaced by masks (release's and the head's), quote characters kept
    and every other character as written, so the real host and every command show. A
    non-empty password gives a span and shows none of its pieces. With no password the
    head adds no mask: the display is release's output, whose frozen rule can mask past
    the authority (`u:aa#bb@evil.test`), which this property does not judge."""
    command, prefix, password, pieces = case
    if password:
        _masked_whole(command, prefix, password, pieces)
        return
    released = scrub_release.mask_approval({"command": command})
    assert redact.mask_approval({"command": command}) == released
    event("no password" + ("; release masks past the authority" if released[0]["command"] != command else ""))
