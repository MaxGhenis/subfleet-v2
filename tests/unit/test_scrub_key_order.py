"""C-23.14 (D-SCR1, review r2 of #225): the `Bearer` and header rules never take a
credential key that a named-value rule would read.

The token rules run before the named-value passes. `Bearer`'s `\\s+` crossed a line
break and its token class read an identifier, and a header took the rest of its line
even when that was only a key whose value is on the next line. Either way the pass
that followed no longer saw the key and left its value in the text, in all three
modes: the whole text, an excerpt (alone, and cut out of a long block) and the
daemon.log line. Every regression here leaked at release/217, whose scrubber
`scrub_release217` keeps for the differential at the end."""

from __future__ import annotations

import contextlib
import itertools
import random
import re

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import refusals
from subfleet.sessions import handoff
from subfleet.sessions.handoff import scrub_bounded, scrub_secrets
from tests.scrub_work import scrub_work
from tests.unit import scrub_release217 as release217
from tests.unit.test_sessions_handoff import _scrubber_probe

#: The suite's ci profile: bounded and derandomized, never the deep search.
CI = settings(max_examples=200, derandomize=True, database=None, deadline=None)
SECRET = re.compile(r"SECRET7[A-Za-z0-9]*")
ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


def _lines_of_length(length: int) -> str:
    """Ordinary lines, `length` characters in all, ending in a line break."""
    line = "ordinary line\n"
    count, extra = divmod(length, len(line))
    return line * count + ("." * (extra - 1) + "\n" if extra else "")


@contextlib.contextmanager
def _excerpts_of(chars: int):
    """The scrubber's bound with excerpts of `chars` characters (EXCERPT_CHARS is half
    of MAX_SCRUB_CHARS), so that a short text can be cut through quickly. The rules
    and the cuts are the same code; only the two constants are smaller."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(handoff, "EXCERPT_CHARS", chars)
        patch.setattr(handoff, "MAX_SCRUB_CHARS", 2 * chars)
        yield


def _cut_through(text: str) -> list[str]:
    """`text` in long blocks whose head excerpt, then whose tail excerpt, would be cut
    at each of its line breaks and at its end: the break is the last before the head's
    bound, or the first in the tail's. `scrub_bounded` cuts there only if no match can
    span the break, and otherwise at an earlier (head) or later (tail) one."""
    excerpt = handoff.EXCERPT_CHARS
    filler = _lines_of_length(2 * excerpt)
    blocks = []
    for at in [i for i, char in enumerate(text) if char == "\n"] + [len(text)]:
        head = _lines_of_length(excerpt - 1 - at) + text + "\n" + filler
        tail = filler + text + "\n" + _lines_of_length(at + excerpt - len(text))
        assert head.rfind("\n", 0, excerpt) == excerpt - 1
        assert tail.find("\n", len(tail) - excerpt - 1) == len(filler) + at
        blocks += [head, tail]
    return blocks


def _modes(text: str, *, excerpt: int | None = None, detail: bool = True) -> dict[str, str]:
    """What each mode leaves of `text`: the excerpt rules, the whole-text scrub, long
    blocks whose excerpts are cut through it (`scrub_bounded`, with excerpts of
    `excerpt` characters, the real ones by default) and daemon.log's line, alone and
    (`detail`) as a refusal's detail."""
    found = {"excerpt": scrub_secrets(text)[0], "whole": scrub_secrets(text, whole=True)[0],
             "daemon.log line": refusals.scrubbed_line(text)}
    if detail:
        found["daemon.log"] = refusals.detail_line("submit", ValueError(text))
    with _excerpts_of(excerpt) if excerpt else contextlib.nullcontext():
        for n, block in enumerate(_cut_through(text)):
            found[f"cut {n // 2} {('head', 'tail')[n % 2]}"] = scrub_bounded(block, None)[0]
    return found


def _assert_kept_out(text: str, *, excerpt: int | None = None, detail: bool = True) -> dict[str, str]:
    """No mode keeps any of `text`'s `SECRET7` values."""
    secrets = SECRET.findall(text)
    assert secrets, text
    found = _modes(text, excerpt=excerpt, detail=detail)
    leaked = {mode: out for mode, out in found.items() if any(secret in out for secret in secrets)}
    assert leaked == {}, text
    return found


# --- the review's inputs --------------------------------------------------------

#: Review r2 of #225's four executed inputs, and what both scrubs make of them.
REVIEW_R2 = {
    "token-type-then-key": ("token_type: Bearer\naccess_token: SECRET7realaa2",
                            "token_type: Bearer\naccess_token: [REDACTED]"),
    "token-type-assignments": ("token_type=bearer refresh_token=SECRET7realaa7",
                               "token_type=bearer refresh_token=[REDACTED]"),
    "nested-scheme": ("auth:\n  scheme: bearer\n  client_secret: SECRET7realaa3",
                      "auth:\n  scheme: bearer\n  client_secret: [REDACTED]"),
    "header-key-value-next-line": ("upstream said:\nAuthorization: client_secret=\nSECRET7hdraa4",
                                   "upstream said:\nAuthorization: [REDACTED]\n[REDACTED]"),
}


@pytest.mark.parametrize("text,scrubbed", REVIEW_R2.values(), ids=REVIEW_R2.keys())
def test_the_reviews_inputs_lose_their_values_in_every_mode(text, scrubbed):
    """Each leaked at release/217 in all three modes; the last also in daemon.log once
    its lines were folded, where the header no longer starts a line. The excerpts are
    the real ones, cut at each of the text's line breaks."""
    found = _assert_kept_out(text)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert found["daemon.log"] == "request submit refused (ValueError): " + " ".join(scrubbed.split())
    assert release217.scrub_secrets(text, whole=True)[0] != scrubbed


# --- Bearer ------------------------------------------------------------------------

#: Shapes in which `Bearer` took a key: across a line break or on one line; a key the
#: token's own `.` or `/` precedes; a key's escaped closing quote, which the separator
#: reads; a flag; and a key whose name merely contains other letters first.
BEARER_TAKES_A_KEY = {
    "equals-next-line": ("scheme: bearer\nPASSWORD=\n  SECRET7bearaa1 tail",
                         "scheme: bearer\nPASSWORD=\n  [REDACTED] tail"),
    "separator-next-line": ("Bearer\nAccess-Key-Id\n: SECRET7bearaa2 tail", "Bearer\nAccess-Key-Id\n: [REDACTED] tail"),
    "spaced-colon": ("token_type: Bearer credential : SECRET7bearaa3,", "token_type: Bearer credential : [REDACTED],"),
    "parenthesised-value": ("BEARER secret_access_key=(SECRET7bearaa4)", "BEARER secret_access_key=[REDACTED]"),
    "dotted-key": ("auth.scheme=bearer\noauth.client_secret=SECRET7bearaa5",
                   "auth.scheme=bearer\noauth.client_secret=[REDACTED]"),
    "path-key": ("type: bearer\nsecrets/db_password: SECRET7bearaa6", "type: bearer\nsecrets/db_password: [REDACTED]"),
    "escaped-closing-quote": ('token_type: Bearer\naccess_token\\": SECRET7bearaa7',
                              'token_type: Bearer\naccess_token\\": [REDACTED]'),
    "flag": ("auth=bearer --client-secret SECRET7bearaa8", "auth=bearer --client-secret [REDACTED]"),
    "flag-after-path": ("Bearer x/--api-key SECRET7bearaa9", "Bearer x/--api-key [REDACTED]"),
    "prefixed-key": ("Bearer okPASSWORD=SECRET7bearab1", "Bearer okPASSWORD=[REDACTED]"),
}


@pytest.mark.parametrize("text,scrubbed", BEARER_TAKES_A_KEY.values(), ids=BEARER_TAKES_A_KEY.keys())
def test_bearer_never_takes_a_credential_key(text, scrubbed):
    """`Bearer`'s lookahead refuses a token that begins with a key a named-value rule
    reads, so that rule still sees the key and takes its value."""
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert any(secret in release217.scrub_secrets(text)[0] for secret in SECRET.findall(text))


#: A `Bearer` token where it is one: in a header, inline (the plain rules would read
#: `Authorization` as a key and `Bearer` as its value), in a dict, and padded.
INLINE_BEARER = {
    "header": ("Authorization: Bearer SECRET7tokenabc124", "Authorization: [REDACTED]"),
    "curl": ("curl -H 'Authorization: Bearer SECRET7tokenabc123' https://x",
             "curl -H 'Authorization: [REDACTED] [REDACTED]' https://x"),
    "inline-header": ("sent Authorization: Bearer SECRET7tokenabc125 upstream",
                      "sent Authorization: [REDACTED] [REDACTED] upstream"),
    "headers-dict": ("headers={'Authorization': 'Bearer SECRET7hdrdict9'}", "headers={'Authorization': '[REDACTED]'}"),
    "padded": ("auth: Bearer SECRET7padded0abc== then", "auth: Bearer [REDACTED] then"),
    "token-before-a-colon": ("Bearer SECRET7tokenabc126: unauthorized", "Bearer [REDACTED]: unauthorized"),
}


@pytest.mark.parametrize("text,scrubbed", INLINE_BEARER.values(), ids=INLINE_BEARER.keys())
def test_a_bearer_token_stays_redacted(text, scrubbed):
    """What release/217 redacted it still does, exactly: the lookahead refuses only a
    key, and the named-value rules still run after the token rules."""
    found = _assert_kept_out(text)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert release217.scrub_secrets(text) == scrub_secrets(text)


# --- headers -----------------------------------------------------------------------

#: Every header shape the reconstructed fuzz leaked at release/217: the header's value
#: on its own line or the next; the key alone or after another value; the key bare,
#: quoted or escape-quoted; and its separator ending the key's line or starting the next.
#: A quoted key alone is the header's own quoted value to the quoted rule, which masks
#: the key's value with it; it leaked when that union brought a later key onto the
#: header's line (`Cookie: "k"=\nSECRET… tail api-key=\nSECRET…`), so that is its case.
HEADER_SHAPES = {
    "colon": {"same-line": ": ", "colon-ends-line": ":\n  "},
    "before": {"key-alone": False, "after-a-value": True},
    "quote": {"bare": "{}", "quoted": '"{}"', "escaped": "\\'{}\\'"},
    "separator": {"separator-on-line": "=\n", "separator-next-line": "\n: "},
}
HEADER_KEYS = ("client_secret", "token", "Oauth_Token", "SERVICE_PASSWORD", "api-key", "Credentials")
#: A value before the key, itself a secret, in the form each header gives one.
HEADER_VALUES = {"Authorization": "Bearer SECRET7hdrpre{n}", "Cookie": "a=SECRET7hdrpre{n};",
                 "Set-Cookie": "id=SECRET7hdrpre{n}; Path=/;"}


def _header_cases():
    cases = []
    for n, (colon, before, quote, separator) in enumerate(itertools.product(*HEADER_SHAPES.values())):
        header = tuple(HEADER_VALUES)[n % 3]
        value = (HEADER_VALUES[header].format(n=n) + " ") if HEADER_SHAPES["before"][before] else ""
        key = HEADER_SHAPES["quote"][quote].format(HEADER_KEYS[n % len(HEADER_KEYS)])
        separator_text = HEADER_SHAPES["separator"][separator]
        if quote != "bare" and not value:
            key += f"{separator_text}SECRET7hdrunion{n} tail {HEADER_KEYS[(n + 1) % len(HEADER_KEYS)]}"
        text = (f"upstream said:\n{header}{HEADER_SHAPES['colon'][colon]}{value}{key}"
                f"{separator_text}SECRET7hdrval{n} tail")
        cases.append(pytest.param(text, id="/".join((colon, before, quote, separator))))
    return cases


@pytest.mark.parametrize("text", _header_cases())
def test_a_header_never_takes_a_key_whose_value_is_past_its_line(text):
    """The header leaves the key that ends its line to the named-value rules, which
    take its value on the next line; what precedes the key is redacted as before."""
    found = _assert_kept_out(text, excerpt=4096)
    assert any(secret in release217.scrub_secrets(text)[0] for secret in SECRET.findall(text))
    assert found["whole"].endswith("[REDACTED] tail")


def test_a_header_redacts_what_precedes_the_key_and_counts_each_value():
    """C-23.14 and the count rule (`_Tally`): the header still hides and counts its
    other values; the key and its value go to the named-value rules."""
    assert scrub_secrets("Cookie: a=SECRET7cookaa1; session_token=\nSECRET7cookaa2") == (
        "Cookie: [REDACTED] session_token=\n[REDACTED]", 2)
    assert scrub_secrets("Authorization: Basic SECRET7cookaa3 client_secret\n= SECRET7cookaa4") == (
        "Authorization: [REDACTED] client_secret\n= [REDACTED]", 2)
    # A header holding only the key is left to those rules whole: its own name is a
    # key, whose value is the key here, and the line's trailing whitespace goes.
    assert scrub_secrets("Cookie: token:   \nSECRET7cookaa5") == ("Cookie: [REDACTED]\n[REDACTED]", 2)
    for text in ("Cookie: [REDACTED] session_token=\n[REDACTED]", "Cookie: [REDACTED]\n[REDACTED]"):
        assert scrub_secrets(text) == (text, 0)


@pytest.mark.parametrize("text,scrubbed", [
    ('Authorization:a=SECRET7glueaa1; \\"x.credentials\\"\n: SECRET7glueaa2 tail',
     'Authorization:[REDACTED] credentials\\"\n: [REDACTED] tail'),
    ("Cookie: SECRET7glueaa3.token=\nSECRET7glueaa4", "Cookie: [REDACTED] token=\n[REDACTED]"),
    ('Cookie: x\\"token\\"=\nSECRET7glueaa5', 'Cookie: [REDACTED] \\"token\\"=\n[REDACTED]'),
], ids=["dotted-key", "value-glued-to-key", "quoted-key-glued"])
def test_a_kept_key_is_parted_from_the_redaction_before_it(text, scrubbed):
    """A key that adjoined what the header hides gets a space after the redaction:
    the header's own plain value ends there rather than running into the key, so the
    key and its value stay the named-value rules', and scrubbing again changes nothing."""
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert scrub_secrets(scrubbed) == (scrubbed, 0)


@pytest.mark.documented_limitation
@pytest.mark.parametrize("text", ["curl -H 'Authorization: Basic SECRET7inlineaa7' https://x",
                                  "sent Cookie: a=1 SECRET7inlineaa8 upstream"])
def test_a_header_that_does_not_start_a_line_keeps_its_second_word_documented_limitation(text):
    """Outside D-SCR1, at release/217 as here: the header rule reads a header only where
    it starts a line (C-23.14). Inline, the header's name is only a key, whose plain
    value is its first word, so `Basic` goes and the credential after it stays; so does
    a header first in a refusal's words once `refusals.detail_line` prefixes them. A
    follow-up must change this deliberately (it decides where an inline value ends)."""
    secret = SECRET.search(text).group()
    for out in (scrub_secrets(text)[0], scrub_secrets(text, whole=True)[0], refusals.scrubbed_line(text),
                refusals.detail_line("submit", ValueError(text.partition(" ")[2]))):
        assert secret in out
    assert secret in release217.scrub_secrets(text)[0]


@pytest.mark.documented_limitation
@pytest.mark.parametrize("text", ["sk-proj-abcdefghijklmnopq_token=\nSECRET7prefaa1",
                                  "eyJabcdefghij.klmnopqrstu.vwxyzabcd_token=\nSECRET7jwtaa2",
                                  "A" * 160 + "token=\nSECRET7b64aa3"], ids=["prefixed", "jwt", "base64"])
def test_a_key_shaped_like_a_token_is_read_as_one_documented_limitation(text):
    """Where C-23.14's key rule stops: the JWT, prefixed-token and long-base64 rules
    read only their own token shapes, and a key of that shape (an `sk-proj-…`, a
    JWT's three segments or 160 base64 characters, ending in a credential word) is
    read as a token, at release/217 as here. Guarding those rules as `Bearer` is
    would scan every identifier of every text; a follow-up must decide that."""
    secret = SECRET.search(text).group()
    assert secret in scrub_secrets(text)[0] and secret in scrub_secrets(text, whole=True)[0]
    assert secret in release217.scrub_secrets(text)[0]


#: Headers release/217 handled: their values on their line, a key without a separator
#: after it, a key whose value is on its line, and one whose separator ends the text.
ORDINARY_HEADERS = (
    "Authorization: Basic SECRET7basicaa1", "Authorization: Bearer SECRET7bearerab2",
    "Cookie: a=SECRET7cookab3; b=SECRET7cookab4", "Set-Cookie: id=SECRET7cookab5; Path=/; HttpOnly",
    "  cookie : session=SECRET7cookab6   \nnext", "Authorization: client_secret=SECRET7inline7",
    "Cookie: token\nnext line", "Cookie: a=SECRET7cookab8; token=", "Authorization:\n  SECRET7nextln9",
    "Cookie: [REDACTED]; session=SECRET7cookac1",
)


@pytest.mark.parametrize("text", ORDINARY_HEADERS)
def test_an_ordinary_header_is_redacted_and_counted_exactly_as_before(text):
    for whole in (False, True):
        assert scrub_secrets(text, whole=whole) == release217.scrub_secrets(text, whole=whole)


# --- the property ------------------------------------------------------------------

WORDS = ("api_key", "api-key", "apikey", "token", "secret", "password", "passwd", "authorization", "cookie",
         "credential", "credentials", "private_key", "signing-key", "secret_access_key", "access-key-id",
         "access_token", "refresh_token", "client_secret", "oauth_token", "auth-token")
CASES = (str, str.lower, str.upper, str.title)
KEYS = st.builds(lambda prefix, word, case: case(prefix + word), st.sampled_from(("", "db_", "service-", "x.")),
                 st.sampled_from(WORDS), st.sampled_from(CASES))
#: A flag's key is a sensitive word or a compound ending in one of `_SENSITIVE_KEY`'s
#: (`--db-password`, never `--db-authorization`).
FLAG_KEYS = st.builds(lambda key, case: case(key), st.one_of(st.sampled_from(WORDS), st.builds(
    lambda prefix, word: prefix + word, st.sampled_from(("db_", "service-")),
    st.sampled_from(("api_key", "token", "secret", "password", "passwd", "private_key", "signing-key")))),
    st.sampled_from(CASES))
SECRETS = st.text(alphabet=ALNUM, min_size=4, max_size=10).map(lambda tail: "SECRET7" + tail)
SEPARATORS = (":", "=", " : ", " = ", ":\n", "=\n", ":\n  ", "=\n  ", "\n: ", "\n= ")
KEY_QUOTES = ("{}", "'{}'", '"{}"', '\\"{}\\"')
VALUE_QUOTES = ("{}", "'{}'", '"{}"')
ASSIGNMENTS = st.builds(lambda key, kq, sep, vq, value: kq.format(key) + sep + vq.format(value),
                        KEYS, st.sampled_from(KEY_QUOTES), st.sampled_from(SEPARATORS),
                        st.sampled_from(VALUE_QUOTES), SECRETS)
FLAGS = st.builds(lambda key, gap, value: f"--{key}{gap}{value}", FLAG_KEYS, st.sampled_from((" ", "\t")), SECRETS)
BEARER_WORDS = st.sampled_from(("Bearer", "bearer", "BEARER"))
BEARERS = st.builds(lambda word, gap, then: word + gap + then, BEARER_WORDS, st.sampled_from((" ", "\n", "\n  ")),
                    st.one_of(ASSIGNMENTS, FLAGS, SECRETS, SECRETS.map(lambda value: value + "==")))
HEADER_VALUES_DRAWN = st.one_of(
    KEYS, ASSIGNMENTS, SECRETS, st.builds(lambda scheme, value: f"{scheme} {value}", st.sampled_from(
        ("Bearer", "Basic")), SECRETS), st.sampled_from(("ok", "see the log", "no value here")),
    st.builds(lambda value, then: f"a={value}; {then}", SECRETS, ASSIGNMENTS))
#: A header always starts its line: elsewhere `Cookie:` is only a key (C-23.14's header rule).
HEADER_PARTS = st.builds(lambda name, colon, value: "\n" + name + colon + value,
                         st.sampled_from(("Authorization", "Cookie", "Set-Cookie")),
                         st.sampled_from((": ", ":", ":\n", " : ")), HEADER_VALUES_DRAWN)
#: An inline header carries a `Bearer` token: inline, a header's other values are the
#: documented limitation below, and the name is a key whose plain value is `Bearer`.
INLINE_HEADERS = st.builds(lambda quote, word, value: f"curl -H {quote}Authorization: {word} {value}{quote} https://x",
                           st.sampled_from(("'", '"')), BEARER_WORDS, SECRETS)
FILLER = st.sampled_from(("ok", "see the log", "x=1", "auth:", "  scheme: bearer", "token_type: Bearer",
                          "path/to/file.py:12", "a.b", "Bearer", "upstream said:", "# note"))
PARTS = st.one_of(ASSIGNMENTS, FLAGS, BEARERS, HEADER_PARTS, INLINE_HEADERS, FILLER)
TEXTS = st.builds(lambda parts, joins: "".join(join + part for join, part in zip(joins, parts)).lstrip("\n"),
                  st.lists(PARTS, min_size=1, max_size=6), st.lists(st.sampled_from((" ", "\n", "\n  ")),
                                                                     min_size=6, max_size=6))


def _neutral_breaks(text: str) -> list[int]:
    """The line breaks an excerpt may be cut after (`_neutral_break`)."""
    return [at + 1 for at, char in enumerate(text) if char == "\n" and handoff._neutral_break(
        text, handoff._last_visible(text, at + 1), handoff._WHITESPACE_RUN_RE.match(text, at + 1).end())]


@CI
@given(text=TEXTS)
def test_no_credential_value_survives_and_excerpts_agree_with_the_whole(text):
    """Over keys, separators, `Bearer`, headers, flags, quoted and unquoted values and
    filler: no `SECRET7` value survives in any mode; scrubbing a scrubbed text changes
    nothing; and an excerpt is the whole text's scrub of the same lines, both where a
    cut is allowed and as `scrub_bounded` keeps it."""
    if not SECRET.search(text):
        return
    # A header that starts the text is inline once `detail_line` prefixes it: the
    # documented limitation above, not this rule.
    found = _assert_kept_out(text, excerpt=len(text) + 1, detail=not handoff._HEADER_RE.match(text))
    assert scrub_secrets(found["excerpt"])[0] == found["excerpt"]
    assert scrub_secrets(found["whole"], whole=True)[0] == found["whole"]
    assert refusals.scrubbed_line(found["daemon.log line"]) == found["daemon.log line"]
    whole = handoff._scrub(text, False)[0]
    for cut in _neutral_breaks(text):
        assert handoff._scrub(text[:cut], False)[0] + handoff._scrub(text[cut:], False)[0] == whole, cut
    with _excerpts_of(len(text) + 1):
        for block in _cut_through(text):
            head, _, tail = scrub_bounded(block, None)[0].partition(" characters omitted] …\n")
            head = head.rpartition("\n… [")[0]
            scrubbed = handoff._scrub(block, False)[0]
            assert scrubbed.lstrip().startswith(head) and scrubbed.rstrip().endswith(tail)


# --- the differential against release/217 ------------------------------------------

#: The fuzz's seed: the reviewer ran theirs at 225 (#225's number); this is the same
#: shape rebuilt (their generator was not kept), so its counts are its own.
FUZZ_SEED = 225
FUZZ_TEXTS = 4000
FUZZ_KEYS = ("api_key", "api-key", "apikey", "token", "secret", "password", "passwd", "authorization", "cookie",
             "credential", "credentials", "private_key", "signing-key", "secret_access_key", "access-key-id",
             "access_token", "refresh_token", "client_secret", "client-secret", "oauth_token", "auth_token",
             "SERVICE_PASSWORD", "db_password", "github-token", "mytoken")
#: Seven key quotings (one twice, as bare keys are the common case), eleven separators
#: (four crossing a line break) and sixteen value forms.
FUZZ_KEY_QUOTES = ("{}", "'{}'", '"{}"', "\\'{}\\'", '\\"{}\\"', '\\\\\\"{}\\\\\\"', "{}")
FUZZ_SEPARATORS = ("=", ": ", " = ", " : ", ":", "=\n", ":\n", ":\n  ", "=\n  ", "\n= ", "\n: ")
FUZZ_VALUES = ("{}", "'{}'", '"{}"', "\\'{}\\'", '\\"{}\\"', '\\\\\\"{}\\\\\\"', "'{}", '"{}', "\\'{}",
               "{},", "{};", "{})", "{} tail", "{}\n", "'{}' tail", "({})")
#: Mixed filler, with the extended fuzz's `Cookie:`, `Bearer`, `--token`,
#: `Authorization:`, reminder and backslash fillers.
FUZZ_FILLER = ("ok", "see logs", "error:", "x=1", "upstream said:", "token_type: Bearer", "Bearer", "bearer",
               "BEARER", "Cookie:", "Authorization:", "Set-Cookie:", "--token", "--verbose", "auth:",
               "  scheme: bearer", "token_type=bearer", "<system-reminder>r</system-reminder>", "\\\\", "\\",
               "headers={", "}", "curl -H", "# note", "path/to/file.py:12", "a.b")
FUZZ_JOINS = (" ", "\n", "\n  ", "", " \n")


def _fuzz_text(rng: random.Random) -> tuple[str, list[str]]:
    """One to five parts, each an assignment (a `SECRET7` value) or a filler."""
    parts, secrets = [], []
    for _ in range(rng.randint(1, 5)):
        if rng.random() < 0.55:
            key = rng.choice(CASES)(rng.choice(FUZZ_KEYS))
            while True:
                secret = "SECRET7" + "".join(rng.choice(ALNUM) for _ in range(rng.randint(4, 10))) + str(len(secrets))
                if not handoff._SENSITIVE_SUFFIX_RE.search(secret):
                    break
            secrets.append(secret)
            parts.append(rng.choice(FUZZ_KEY_QUOTES).format(key) + rng.choice(FUZZ_SEPARATORS)
                         + rng.choice(FUZZ_VALUES).format(secret))
        else:
            parts.append(rng.choice(FUZZ_FILLER))
    return parts[0] + "".join(rng.choice(FUZZ_JOINS) + part for part in parts[1:]), secrets


def _leaks(scrubber, scrubbed_line) -> tuple[set, dict]:
    """The (text, mode, secret) triples a scrubber leaves over the seeded fuzz, and the
    texts by index."""
    rng, triples, texts = random.Random(FUZZ_SEED), set(), {}
    for index in range(FUZZ_TEXTS):
        text, secrets = texts[index] = _fuzz_text(rng)
        for mode, out in (("whole", scrubber.scrub_secrets(text, whole=True)[0]),
                          ("excerpt", scrubber.clean(text, 4000)[0]), ("daemon.log", scrubbed_line(text))):
            triples.update((index, mode, secret) for secret in secrets if secret in out)
    return triples, texts


def _kind(text: str, secret: str) -> str:
    """`bearer` or `header` when one precedes the secret in its text (generously)."""
    before = text[:text.index(secret)].lower()
    if "bearer" in before:
        return "bearer"
    return "header" if re.search(r"(?:authorization|cookie)\s*:", before) else "other"


def test_the_head_leaks_nothing_release_217_keeps_out_and_no_bearer_or_header_value():
    """The differential (C-23.14): over the seeded fuzz, every triple the head leaves
    release/217 left too, and none follows a `Bearer` or a header."""
    head, texts = _leaks(handoff, refusals.scrubbed_line)
    base, _ = _leaks(release217, release217.scrubbed_line)
    kinds = {kind: len([1 for index, _, secret in base if _kind(texts[index][0], secret) == kind])
             for kind in ("bearer", "header", "other")}
    assert kinds["bearer"] > 50 and kinds["header"] > 20, kinds      # the fuzz exercises both
    assert head - base == set()
    assert [triple for triple in head if _kind(texts[triple[0]][0], triple[2]) != "other"] == []


# --- the lookahead's work ------------------------------------------------------------

#: The most a scrub of the texts below may read a character, every regex call counted
#: (`tests.scrub_work`): release/217 reads at most 43. A call past it is stopped before
#: `re` runs it, so a catastrophic lookahead fails here in seconds.
READS_PER_CHAR_MAX = 256
#: Four times the input: linear work grows about 4 times (its fixed part makes it
#: less), quadratic about 16. The counts are exact, so the bound can sit near 4.
WORK_GROWTH_MAX = 5


@pytest.mark.parametrize("token", ["a_", "token", "secret_", "--a", "x."])
def test_the_bearer_lookahead_keeps_scrubbing_linear(token):
    """C-23.14 / C-25.5: the lazy lookahead reads a token once per position; four times
    the input must not do quadratic work (`_SENSITIVE_KEY` there would). Counted, not
    timed (D-SCRUB-LINEAR: the CPU ratio failed shared runners at about 9x on code no
    PR had touched): what each regex call of the scrub reads, and the lines of
    `subfleet` it runs, are the same on every run."""
    measured = _scrubber_probe(f"token = {token!r}\nreads_per_char = {READS_PER_CHAR_MAX}\n" + '''
import json, sys
from tests.scrub_work import scrub_work
sys.setrecursionlimit(100_000)          # the emulator nests a generator per group repeat
work = {}
for size in (2048, 8192):
    work[size] = scrub_work("Bearer " + token * (size // len(token)), reads_per_char=reads_per_char)
    if work[size]["over"]:
        break
print(json.dumps(work))
''')
    assert [size["over"] for size in measured.values()] == [None, None], measured
    assert [size["same"] for size in measured.values()] == [True, True], measured
    assert [size["uncounted"] for size in measured.values()] == [[], []], measured
    small, large = measured["2048"], measured["8192"]
    assert small["lines"] > 0, measured                     # the trace saw the scrubber
    for count in ("reads", "lines"):
        assert large[count] < small[count] * WORK_GROWTH_MAX, (count, measured)


@CI
@given(text=st.one_of(TEXTS, st.randoms(use_true_random=False).map(lambda rng: _fuzz_text(rng)[0])))
def test_the_counted_scrub_is_the_scrub(text):
    """The count above changes nothing it counts (a differential, `tests.scrub_work`):
    over the key-order grammar and the seeded fuzz, the counted scrub gives the plain
    scrub's output and sees every regex call it makes, each call's matches `re`'s own.
    The budget only bounds the run: these short texts read up to about 100 a character."""
    work = scrub_work(text, reads_per_char=4096)
    assert (work["over"], work["same"], work["uncounted"]) == (None, True, []), (text, work)
