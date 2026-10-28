"""C-23.14 (D-SCR2, review r1 of #231 and r3 of #233): inline credential headers are
scrubbed, a secret glued to a credential word stays hidden, `Bearer` decides once per
match, and a flag or key another rule's redaction encloses still owns its value.

Each regression leaked at the base this branch was cut from (release/217 after
#231, `16b24033a`), in the modes it names; the guards pin what must not change.
`scrub_release217` is release/217 before #231, for the cost comparisons.

Fix round 1 (#239 review r1) adds regressions that failed at this branch's
reviewed head `d3a19518b`, each checked against `scrub_473fe2941`, the base kept
whole: no mode of the head shows a value that base or release/217 hides."""

from __future__ import annotations

import contextlib
import itertools
import random
import re

import pytest

from subfleet import refusals
from subfleet.conversations import suggestions
from subfleet.conversations.redact import mask_approval
from subfleet.sessions import handoff, scrub_release
from subfleet.sessions.handoff import scrub_bounded, scrub_secrets
from tests.cpu_scaling import COST_RATIO_MAX
from tests.unit import scrub_473fe2941 as base473
from tests.unit import scrub_release217 as release217
from tests.unit.test_scrub_key_order import SECRET, _assert_kept_out, _cut_through, _lines_of_length, _modes, _revealed
from tests.unit.test_sessions_handoff import _scrubber_probe


def _assert_stable(text: str, scrubbed: str) -> None:
    """Scrubbing a scrubbed text shows nothing the first scrub hid, in either mode or
    daemon.log's (C-23.14: monotone, not idempotent). Since #239 round 8 a second scrub
    can join placeholders the first wrote, as release's header rule reads
    `Cookie: [REDACTED] [REDACTED]` as one value (`handoff.shown_by_both`), and can hide a
    `SECRET7` run the first showed (`x Cookie:\nx Cookie:\n;v SECRET7c3`, review r9b), so
    its runs are a subset of the first's."""
    for whole in (False, True):
        again = scrub_secrets(scrubbed, whole=whole)[0]
        assert _revealed(scrubbed, again) == "" and set(SECRET.findall(again)) <= set(SECRET.findall(scrubbed))
    line = refusals.scrubbed_line(text)
    assert _revealed(line, refusals.scrubbed_line(line)) == ""


# --- section 1: inline credential headers --------------------------------------

#: #231 review r1's inline headers, every quoting form, the names that end in
#: `authorization` or `cookie`, and a value on the next line.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
INLINE_HEADERS = {
    "curl-basic": ("curl -H 'Authorization: Basic SECRET7inlnaa1' https://x",
                   "curl -H 'Authorization: [REDACTED] [REDACTED] https://x"),
    "curl-token": ('curl -H "Authorization: Token SECRET7inlnaa2" https://x',
                   'curl -H "Authorization: [REDACTED] [REDACTED] https://x'),
    "wget-header": ("wget --header='Authorization: Basic SECRET7inlnaa3' https://x",
                    "wget --header='Authorization: [REDACTED] [REDACTED] https://x"),
    "curl-cookie": ("curl -H 'Cookie: a=1; sid=SECRET7inlnaa4' https://x", "curl -H 'Cookie: [REDACTED] [REDACTED] https://x"),
    "prose": ("upstream sent Authorization: Basic SECRET7inlnaa5 and failed", 'upstream sent Authorization: [REDACTED] [REDACTED] [REDACTED] [REDACTED]'),
    "proxy-line-start": ("Proxy-Authorization: Basic SECRET7inlnaa6", 'Proxy-Authorization: [REDACTED] [REDACTED]'),
    "proxy-inline": ("curl -H 'Proxy-Authorization: Basic SECRET7inlnaa7' -v",
                     "curl -H 'Proxy-Authorization: [REDACTED] [REDACTED] -v"),
    "x-authorization": ("sent X-Authorization: SECRET7inlnaa8 ok", 'sent X-Authorization: [REDACTED] [REDACTED]'),
    "set-cookie": ("got Set-Cookie: id=SECRET7inlnaa9; Path=/; HttpOnly", 'got Set-Cookie: [REDACTED] [REDACTED] [REDACTED]'),
    # An escaped closing quote ends the value at its backslashes; the plain rule, which
    # then reads the header's name as a key, takes those as its value's end, as it
    # always took a backslash (`api_key=x\\"` scrubs to `api_key=[REDACTED]"`).
    "json-escaped": ('{"cmd": "curl -H \\"Authorization: Basic SECRET7inlnab1\\" https://x"}',
                     '{"cmd": "curl -H \\"Authorization: [REDACTED] [REDACTED] https://x"}'),
    "repr-escaped": ("'curl -H \\'Cookie: sid=SECRET7inlnab2\\' https://x'",
                     "'curl -H \\'Cookie: [REDACTED]' https://x'"),
    "json-twice": ('"curl -H \\\\\\"Authorization: Basic SECRET7inlnab3\\\\\\" x"',
                   '"curl -H \\\\\\"Authorization: [REDACTED] [REDACTED] x"'),
    "inner-escaped-quote": ("-H 'Cookie: a=\\'SECRET7inlnab4\\' b' tail", "-H 'Cookie: [REDACTED] [REDACTED] tail"),
    "unclosed-quote": ("curl -H 'Authorization: Basic SECRET7inlnab5 -v\nnext", "curl -H 'Authorization: [REDACTED] [REDACTED] [REDACTED]\nnext"),
    "value-next-line": ("sent Authorization:\n  Basic SECRET7inlnab6 tail", 'sent Authorization:\n  [REDACTED] [REDACTED] [REDACTED]'),
    "tab-before-colon": ("sent Cookie\t: sid=SECRET7inlnab7", "sent Cookie\t: [REDACTED]"),
    "lowercase": ("headers: cookie: sid=SECRET7inlnab8", "headers: cookie: [REDACTED]"),
    "two-on-a-line": ("-H 'Cookie: SECRET7inlnab9' -H 'Authorization: Basic SECRET7inlnac1' x",
                      "-H 'Cookie: [REDACTED]' -H 'Authorization: [REDACTED] [REDACTED] x"),
}


@pytest.mark.parametrize("text,scrubbed", INLINE_HEADERS.values(), ids=INLINE_HEADERS.keys())
def test_an_inline_header_loses_its_value_in_every_mode(text, scrubbed):
    """The value runs from the first visible character after the colon to the quote
    that matches one opening right before the name, or to its line's end, and is
    redacted whole; before D-SCR2 the plain rule took only its first word."""
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert found["daemon.log"] == "request submit refused (ValueError): " + " ".join(scrubbed.split())
    _assert_stable(text, scrubbed)


def test_a_line_start_header_is_inline_once_detail_line_prefixes_it():
    """#231 review r1: a refusal whose words begin with a header kept the credential
    in daemon.log, where `detail_line`'s prefix puts the header mid-line."""
    text = "Authorization: Basic SECRET7inlnac2"
    found = _assert_kept_out(text)
    assert found["whole"] == "Authorization: [REDACTED]"
    assert found["daemon.log"] == "request submit refused (ValueError): Authorization: [REDACTED] [REDACTED]"


@pytest.mark.parametrize("text,count", [
    ("curl -H 'Authorization: Basic SECRET7cntaa1' x", 1),
    ("curl -H 'Cookie: a=SECRET7cntaa2; b=SECRET7cntaa3' x", 2),
    ("curl -H 'Authorization: Bearer SECRET7cntaa4' x", 2),   # release's count (`Bearer`, then the key)
    ("sent Cookie: [REDACTED] then SECRET7cntaa5", 0),
], ids=["one", "two-values", "bearer-inside", "already-redacted"])
def test_an_inline_header_counts_as_a_line_start_one(text, count):
    """Once per secret-looking run the value held (`_Tally`'s header rule). A value
    that already starts with `[REDACTED]` is left alone: the guard that keeps a
    scrubbed text stable (the last case's own secret is the documented cost). Since #239
    round 8 the count is release's or the head's, whichever replaced more."""
    assert scrub_secrets(text)[1] == count


def test_daemon_logs_folded_line_keeps_the_words_after_a_redacted_header():
    """The guard: a folded line puts the next line's words after a redacted header,
    no longer at a line start; they are not the header's value."""
    text = "refused: upstream\nsent Authorization: Basic SECRET7foldaa1\nwhile reading job 20261020-a1b2c3"
    assert refusals.scrubbed_line(text) == (          # since #239 round 8 (`handoff.shown_by_both`)
        "refused: upstream sent Authorization: [REDACTED] [REDACTED] while reading job 20261020-a1b2c3")


#: What an inline header must leave: a quoted key (the quoted rule's), a name that does
#: not end in a header word, `=` for the colon, and the closing quote's own place.
NOT_INLINE_HEADERS = {
    "dict-key": "headers={'Authorization': 'Basic SECRET7notaa1'}",
    "json-key": '{"Cookie": "sid=SECRET7notaa2"}',
    "longer-name": "authorizations: ok, cookies: fine, Cookie-Jar: kept",
    "equals": "sent Authorization=SECRET7notaa3 tail",
    "empty-value": "-H 'Cookie:' https://x",
}


@pytest.mark.parametrize("text", NOT_INLINE_HEADERS.values(), ids=NOT_INLINE_HEADERS.keys())
def test_what_is_not_an_inline_header_scrubs_as_before(text):
    assert scrub_secrets(text) == release217.scrub_secrets(text)
    assert scrub_secrets(text, whole=True) == release217.scrub_secrets(text, whole=True)


@pytest.mark.parametrize("text,scrubbed", [
    ("sent Authorization: x client_secret=\nSECRET7keyaa1 tail", "sent Authorization: [REDACTED] client_secret=\n[REDACTED] tail"),
    ("curl -H 'Cookie: a=1; token:\nSECRET7keyaa2' x", "curl -H 'Cookie: [REDACTED] token:\n[REDACTED]' x"),
    ("sent Cookie: token=\n  SECRET7keyaa3", "sent Cookie: [REDACTED]\n  [REDACTED]"),
], ids=["after-a-value", "quote-unclosed-on-its-line", "key-alone"])
def test_an_inline_header_leaves_a_key_whose_value_is_past_its_line(text, scrubbed):
    """D-SCR1's rule over the inline value's span (`_key_past_line`)."""
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    _assert_stable(text, scrubbed)


# --- section 2: a secret that ends in a credential word -------------------------

#: #231 review r1's P3-1: #231 read these secrets as keys, so they showed where
#: release/217 hid them. The header keeps only the credential word for the
#: named-value rules; `Bearer` redacts the token before it.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
CREDENTIAL_WORD_ENDINGS = {
    "bearer-token-colon": ("invalid Bearer SECRET7a1b2c3d4_token: expired",
                           'invalid Bearer [REDACTED] [REDACTED]'),
    "header-equals-underline": ("Cookie: sid=SECRET7abc123token\n====", 'Cookie: [REDACTED] [REDACTED]\n=[REDACTED]'),
    "header-pseudo-header": ("Authorization: Basic SECRET7abc123secret\n:authority: example.com",
                             'Authorization: [REDACTED] [REDACTED]\n:[REDACTED] example.com'),
    "bearer-flag": ("auth: Bearer SECRET7abcdef12--password hunter2x tail",
                    'auth: Bearer [REDACTED] [REDACTED] tail'),
    "inline-header-glued": ("sent Cookie: sid=SECRET7abc123token\n= next", 'sent Cookie: [REDACTED]\n= [REDACTED]'),
}


@pytest.mark.parametrize("text,scrubbed", CREDENTIAL_WORD_ENDINGS.values(), ids=CREDENTIAL_WORD_ENDINGS.keys())
def test_a_secret_ending_in_a_credential_word_stays_hidden(text, scrubbed):
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    _assert_stable(text, scrubbed)


def test_the_approval_masker_masks_a_token_before_a_credential_word():
    """It shares `_BEARER_RE` and has no named-value pass after it: #231 showed this
    token unmasked; release/217 masked it with the key."""
    masked, spans = mask_approval({"command": "curl -H 'Authorization: Bearer SECRET7a1b2c3d4_token:' https://x"})
    assert masked == {"command": "curl -H 'Authorization: [masked bearer, 22 chars]_token:' https://x"}
    assert [(span["path"], span["rule"], span["length"]) for span in spans] == [("$.command", "bearer", 22)]


@pytest.mark.parametrize("command", [
    "deploy --auth-scheme Bearer --client-secret x",
    "echo Bearer access_token=%s",
    "curl -H 'Authorization: Bearer secrets/db_password=' https://x",
], ids=["flag-without-a-token", "key-on-the-next-line", "key-shaped-path"])
def test_the_approval_masker_still_leaves_a_key_shaped_run(command):
    """#231's refusal: a run that is a key, with no token before its word, is shown."""
    assert mask_approval({"command": command}) == ({"command": command}, [])


def test_a_key_after_bearer_keeps_its_name():
    """The run before a refused key's word is redacted only when it reads as a token
    (`_token_like`): eight or more characters with a letter and a digit."""
    assert scrub_secrets("type: bearer\nservice_account_password: SECRET7namaa1")[0] == (
        "type: bearer\nservice_account_password: [REDACTED]")
    assert scrub_secrets("Bearer abc123_token: SECRET7namaa2")[0] == "Bearer abc123_token: [REDACTED]"


# --- section 5: what another rule's redaction encloses still owns its value -------

#: #233 review r3c: an enclosing rule's redaction took a credential flag whose value
#: is on the next line, and daemon.log's folded line then showed that value. Each
#: enclosure has a twin without its rule, which the result must equal mode by mode.
ENCLOSURES = {
    "line-start-header": ("intro\nAuthorization: x {item}", "intro\nx {item}"),
    "inline-header": ("intro\nsent Authorization: x {item}", "intro\nsent x {item}"),
    "quoted-inline-header": ("intro\ncurl -H 'Authorization: x {item}", "intro\ncurl -H 'x {item}"),
    "bearer": ("intro\nBearer x.y.z{item}", "intro\nx.y.z{item}"),
    "named-value": ("intro\napi_key=xyz{item}", "intro\nxyz{item}"),
}
FLAGS = ("--password", "--auth-token", "--client-secret")
KEYS = ("token:", "password=")
PLACEMENTS = {"lf": "\n", "crlf": "\r\n", "lf-indented": "\n  ", "crlf-indented": "\r\n  "}
#: The modes daemon.log's fold puts a next-line value on the flag's line in.
FOLDED = ("daemon.log line", "daemon.log")


def _matrix():
    for n, ((enclosure, (text, twin)), item, (placement, gap)) in enumerate(itertools.product(
            ENCLOSURES.items(), FLAGS + KEYS, PLACEMENTS.items())):
        tail = f"{gap}SECRET7mx{n} rest\ntail"
        yield pytest.param(text.format(item=item) + tail, twin.format(item=item) + tail, item in FLAGS,
                           id=f"{enclosure}/{item.strip(':=')}/{placement}")


@pytest.mark.parametrize("text,twin,flag", tuple(_matrix()))
def test_a_swallowed_flag_or_key_still_owns_its_next_line_value(text, twin, flag):
    """The hub's rule: its next-line value is redacted exactly as it would be without
    the enclosing rule. A key's is hidden in every mode. A flag's value is read on the
    flag's own line only, so it is hidden where daemon.log folds the lines; in the
    other modes it shows with the enclosure as without it (documented below)."""
    secret = SECRET.search(text).group()
    found, bare = _modes(text, excerpt=4096), _modes(twin, excerpt=4096)
    assert {mode: secret in out for mode, out in found.items()} == {mode: secret in out for mode, out in bare.items()}
    if not flag:
        assert not any(secret in out for out in found.values())
    for mode in FOLDED:
        assert secret not in found[mode], mode


@pytest.mark.parametrize("text,scrubbed", [
    ("api_key=xyz--password SECRET7ownaa1 rest", "[REDACTED] [REDACTED] rest"),
    ("Authorization: Bearer --client-secret\nSECRET7ownaa2", "Authorization: [REDACTED]\nSECRET7ownaa2"),
    ("Cookie: --auth-token\nSECRET7ownaa3", "Cookie: [REDACTED]\n[REDACTED]"),
], ids=["plain-value-same-line", "header-and-bearer", "header-holding-only-the-flag"])
def test_a_flag_left_by_its_enclosure_is_read_by_the_flag_rule(text, scrubbed):
    """The mechanism: an enclosing match ends before a credential flag that ends it
    and may own a value past it (`_owning_flag`), and leaves the flag and what
    follows it in place. A plain value that ended in such a flag swallowed even a
    same-line value (the first case leaked in every mode at the base). Since round 2
    a header whose whole value is the flag takes it, and the value daemon.log's
    folded line gives it: kept, daemon.log's folded line read the header's value to
    the record's end, past any cut `handoff.cut_whole` makes (C-25.7). Since #239
    round 8, pinned as every mode writes them: release/217's output, kept for the runs
    the head's output holds (`handoff.shown_by_both`)."""
    assert scrub_secrets(text, whole=True)[0] == scrub_secrets(text)[0] == scrubbed
    assert SECRET.search(text).group() not in refusals.scrubbed_line(text)
    _assert_stable(text, scrubbed)


@pytest.mark.parametrize("text", ["sent Authorization: ok Bearer\nSECRET7beraa1", "Authorization: ok Bearer\nSECRET7beraa2",
                                  "curl -H 'Cookie: x Bearer\n  SECRET7beraa3' https://x"])
def test_a_bearer_that_ends_an_inline_value_keeps_its_next_line_token(text):
    """The inline rule runs before `Bearer`; a `Bearer` that ends its value is left
    to that rule, which reads a token across the line break, as it would without
    the header (the hypothesis search found the second through `detail_line`)."""
    _assert_kept_out(text, excerpt=4096)


@pytest.mark.documented_limitation
@pytest.mark.parametrize("text", ["x --password\nSECRET7flgaa1 rest", "intro\nAuthorization: x --password\nSECRET7flgaa2",
                                  "x --password\n\n  \nSECRET7flgaa3 rest"])
def test_a_flags_next_line_value_shows_outside_the_folded_line_documented_limitation(text):
    """Outside D-SCR2, at the base as here: the flag rule reads a value on the flag's
    own line only (`_FLAG_VALUE_RE`), so a value on a later line shows in the
    whole-text, excerpt and cut modes, with an enclosing header or without one. Only
    daemon.log's folded line puts it on the flag's line, past any blank lines (the
    fold joins every run of whitespace). A follow-up must decide whether a flag that
    ends its line takes the next line's first word."""
    secret = SECRET.search(text).group()
    assert secret in scrub_secrets(text)[0] and secret in scrub_secrets(text, whole=True)[0]
    assert secret not in refusals.scrubbed_line(text)


@pytest.mark.documented_limitation
def test_curl_user_password_has_no_rule_documented_limitation():
    """`curl -u user:SECRET…` is no rule's: the next follow-up (D-SCR2's brief)."""
    text = "curl -u admin:SECRET7curlaa1 https://x"
    assert "SECRET7curlaa1" in scrub_secrets(text)[0]
    assert "SECRET7curlaa1" in release217.scrub_secrets(text)[0]


# --- fix round 1 (#239 review r1): no mode shows a value 473fe2941 hides -----------

#: The three scrubbers the differential compares, mode by mode.
TREES = {"head": handoff, "473fe2941": base473, "release/217": release217}
PREFIX = "request submit refused (ValueError): "


@contextlib.contextmanager
def _all_excerpts_of(chars: int):
    """`_excerpts_of` for every tree at once, so that one set of cut blocks serves all."""
    with pytest.MonkeyPatch.context() as patch:
        for scrubber in (*TREES.values(), scrub_release):   # release's copy cuts the head's excerpts
            patch.setattr(scrubber, "EXCERPT_CHARS", chars)
            patch.setattr(scrubber, "MAX_SCRUB_CHARS", 2 * chars)
        yield


def _fold(scrubber, text: str) -> str:
    """daemon.log's line without its 2,000-character bound, as the frozen trees keep it:
    scrubbed whole, folded, scrubbed whole again (`suggestions._scrub_line`)."""
    return suggestions._scrub_line(text)[0] if scrubber is handoff else scrubber.scrubbed_line(text)


def _tree_modes(text: str, *, cuts: bool = True) -> dict[str, dict[str, str]]:
    """What each mode of each tree leaves of `text`: the excerpt and whole-text scrubs,
    `clean`, daemon.log's line alone and as a refusal's detail, and (`cuts`) blocks
    whose head and tail excerpts are cut at each of its line breaks."""
    found = {name: {"excerpt": scrubber.scrub_secrets(text)[0], "whole": scrubber.scrub_secrets(text, whole=True)[0],
                    "clean": scrubber.clean(text, 4000)[0], "daemon.log line": _fold(scrubber, text),
                    "daemon.log": _fold(scrubber, PREFIX + text)} for name, scrubber in TREES.items()}
    if cuts:
        with _all_excerpts_of(len(text) + 1):
            blocks = _cut_through(text)
            for name, scrubber in TREES.items():
                for n, block in enumerate(blocks):
                    found[name][f"cut {n // 2} {('head', 'tail')[n % 2]}"] = scrubber.scrub_bounded(block, None)[0]
    return found


def _new_leaks(text: str, *, cuts: bool = True) -> set[tuple[str, str, str]]:
    """The (reference, mode, secret) triples where the head shows a `SECRET7` value
    that 473fe2941 or release/217 hides."""
    secrets = set(SECRET.findall(text))
    found = _tree_modes(text, cuts=cuts)
    shown = {name: {(mode, secret) for mode, out in modes.items() for secret in secrets if secret in out}
             for name, modes in found.items()}
    return {(name, mode, secret) for name in ("473fe2941", "release/217")
            for mode, secret in shown["head"] - shown[name]}


#: P2-1: a line-start header whose whole value is the next line's header name. The
#: line-start rule left the value whole (it is a key whose value is past its line),
#: the inline rule's second scan skipped a name that starts its line, and the plain
#: rule took one word of the name's own value. The first two leaked from D-SCR2's
#: wider header names; the third already leaked at 473fe2941 (#231).
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
SWALLOWED_HEADER_NAMES = {
    "x-authorization": ("X-Authorization:\nAuthorization:\n  Basic SECRET7xa1",
                        "X-Authorization:\n[REDACTED]\n  [REDACTED]"),
    "proxy-then-cookie": ("Proxy-Authorization:\nCookie:\n  sid=1; s2=SECRET7lone2",
                          "Proxy-Authorization:\n[REDACTED]\n  [REDACTED]"),
    "authorization-then-cookie": ("Authorization:\nCookie:\n  sid=1; s2=SECRET7lone3",
                                  'Authorization:\n[REDACTED]\n  [REDACTED] [REDACTED]'),
    "yaml-proxy-authorization": ("proxy_authorization:\n  cookie:\n    sid=1; s2=SECRET7yml1",
                                 "proxy_authorization:\n  [REDACTED]\n    [REDACTED]"),
    "yaml-session-cookie": ("session_cookie:\n  cookie:\n    a=1; b=SECRET7yml2",
                            "session_cookie:\n  [REDACTED]\n    [REDACTED]"),
}


@pytest.mark.parametrize("text,scrubbed", SWALLOWED_HEADER_NAMES.values(), ids=SWALLOWED_HEADER_NAMES.keys())
def test_a_header_name_a_line_start_header_swallows_takes_a_headers_value(text, scrubbed):
    """The inline rule's second scan reads names at a line start too, and a header
    whose whole value is a header's name stops there (`_header_redaction`'s `rescan`),
    so the name takes a header's value, as it would on its own line."""
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert _new_leaks(text) == set()
    _assert_stable(text, scrubbed)


#: P3-1: the quoted pass gives a header's name it swallows a header's value to its
#: line's end. It looked for a `Bearer` or flag that ends the value at the line break,
#: so it found neither, and the item's value on the next line went to no rule; the
#: second leaked in every mode (from #239 review r1's seed 9001), the third only
#: through daemon.log, its trailing spaces hiding the flag.
LINE_ENDING_ITEMS = {
    "bearer": ("secret:'Cookie:\nx Bearer\nSECRET7k3bcd", "secret:[REDACTED] Bearer [REDACTED]"),
    "bearer-after-a-carriage-return": ("token:'Authorization:\r2 Bearer\nSECRET70lrvoqzo6q3",
                                       "token:[REDACTED] Bearer [REDACTED]"),
    "flag-trailing-spaces": ("secret:'Cookie:\nx --password  \nSECRET7k3cde",
                             "secret:[REDACTED] --password  \nSECRET7k3cde"),
}


@pytest.mark.parametrize("text,scrubbed", LINE_ENDING_ITEMS.values(), ids=LINE_ENDING_ITEMS.keys())
def test_a_bearer_or_flag_ending_a_swallowed_headers_line_keeps_its_value(text, scrubbed):
    """The check reads the line's last visible character, as `_header_redaction` does:
    the `Bearer` takes its token, and the flag its value where daemon.log folds it onto
    the flag's line. Elsewhere the flag's next-line value shows, as at 473fe2941 (the
    documented limitation below)."""
    assert scrub_secrets(text, whole=True)[0] == scrub_secrets(text)[0] == scrubbed
    assert _new_leaks(text) == set()
    secret = SECRET.search(text).group()
    assert secret not in refusals.scrubbed_line(text)
    assert secret not in refusals.detail_line("submit", ValueError(text))
    _assert_stable(text, scrubbed)


#: P3-2: the plain rule leaves a value that is a credential flag to the flag rule, and
#: the flag rule's value swallowed a credential flag glued to its end, so that flag's
#: own value showed. The third also leaked at 473fe2941; release/217 hid it.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
GLUED_FLAGS = {
    "plain-value-is-a-flag": ("api_key=--password x--token SECRET7l4d", '[REDACTED] [REDACTED] [REDACTED]'),
    "tab": ("token:--passwd 1--token\tSECRET7b1", '[REDACTED] [REDACTED]\t[REDACTED]'),
    "bearer-then-two-flags": ("BEARER SECRET7i8b11q0credentials--password z--client-secret  SECRET7qq1api-key",
                              'BEARER [REDACTED] [REDACTED]  [REDACTED]'),
}


@pytest.mark.parametrize("text,scrubbed", GLUED_FLAGS.values(), ids=GLUED_FLAGS.keys())
def test_a_flag_glued_to_another_flags_value_keeps_its_own_value(text, scrubbed):
    """A flag's value ends before a credential flag glued to its end (`_owning_flag`),
    and the flag rule skips only a flag inside the previous flag's value."""
    found = _assert_kept_out(text, excerpt=4096)
    assert found["whole"] == found["excerpt"] == scrubbed
    assert _new_leaks(text) == set()
    _assert_stable(text, scrubbed)


def test_an_excerpt_agrees_with_the_whole_scrub_at_the_real_bound():
    """P3-3: whether the inline rule read a line again after `Bearer` merged it with
    its token's depended on whether a line-start header anywhere else in the text
    changed, so the tail excerpt (no such header) showed what the whole scrub (with
    `Cookie: a` in its head) hid. The second scan now always runs."""
    tail = "sent Authorization: Bearer\nSECRET7tok123 SECRET7oth456 rest\n" + "closing line\n" * 50
    head = "Cookie: a\n"
    text = head + _lines_of_length(handoff.MAX_SCRUB_CHARS + 46_000 - len(head) - len(tail)) + tail
    assert len(text) > handoff.MAX_SCRUB_CHARS and text.index("sent Authorization") > len(text) - handoff.EXCERPT_CHARS
    whole = scrub_secrets(text, whole=True)[0]
    excerpt = scrub_bounded(text, None)[0]
    assert "SECRET7" not in whole and "SECRET7" not in excerpt
    # `scrub_bounded` strips each excerpt; the tail is the whole scrub's last lines.
    assert whole.rstrip().endswith(excerpt.partition(" characters omitted] …\n")[2])
    assert "SECRET7" not in refusals.detail_line("submit", ValueError(text))


def test_a_header_name_a_partial_bearer_cut_exposes_is_read_in_the_same_scrub():
    """P3-4: `Bearer` redacts a token before the credential word its run ends in, which
    leaves `Authorization:` after a placeholder: a header no rule had read, which a
    second scrub then took. The inline rule's second scan reads it the first time."""
    text = "Bearer\nabc1pi_keyAuthorization:--secret x"
    for whole in (False, True):
        once = scrub_secrets(text, whole=whole)[0]
        assert once == "Bearer\n[REDACTED] [REDACTED]"     # since #239 round 8 (`handoff.shown_by_both`)
        assert scrub_secrets(once, whole=whole) == (once, 0)
    line = refusals.scrubbed_line(text)
    assert refusals.scrubbed_line(line) == line


#: P3-5: a quoted value with no closing quote is redacted to its line's end, which
#: took a `Bearer` or credential flag ending the line; leaked at release/217,
#: 473fe2941 and `d3a19518b` alike. Each has its twin without the opening quote.
UNCLOSED = {
    "single-bearer": ("api_key='xyz Bearer\nSECRET7p5a rest", "api_key=xyz Bearer\nSECRET7p5a rest"),
    "single-flag": ("api_key='xyz --password\nSECRET7p5b rest", "api_key=xyz --password\nSECRET7p5b rest"),
    "double-bearer": ('password: "abc Bearer\nSECRET7p5c rest', "password: abc Bearer\nSECRET7p5c rest"),
    "double-flag": ('password: "abc --password\nSECRET7p5d rest', "password: abc --password\nSECRET7p5d rest"),
    "escaped-bearer-crlf": ("token=\\'xyz Bearer\r\n  SECRET7p5e rest", "token=xyz Bearer\r\n  SECRET7p5e rest"),
}


def _new(out: str, other: str) -> list[str]:
    """The whitespace-free runs `out` shows that `other` does not."""
    return sorted(set(out.split()) - set(other.split()) - {"[REDACTED]"})


@pytest.mark.parametrize("text,twin", UNCLOSED.values(), ids=UNCLOSED.keys())
def test_an_unclosed_quoted_value_leaves_a_line_ending_bearer_or_flag_its_value(text, twin):
    """The unclosed value ends before the `Bearer` or flag at its line's last visible
    character (`_owned_item`), so the item's own rule reads it, and every mode gives
    what the twin gives."""
    # Since #239 round 8 each mode writes release's output kept for the runs the head's
    # holds (`handoff.shown_by_both`), and release's unclosed value takes the item with the
    # line, so the twin may show the item's name where the text hides it; the secret shows
    # in the same modes in both.
    for whole in (False, True):
        assert _new(scrub_secrets(text, whole=whole)[0], scrub_secrets(twin, whole=whole)[0]) == []
    secret = SECRET.search(text).group()
    found, bare = _modes(text, excerpt=4096), _modes(twin, excerpt=4096)
    assert {mode: secret in out for mode, out in found.items()} == {mode: secret in out for mode, out in bare.items()}
    for mode in FOLDED:
        assert secret not in found[mode], mode
    assert _new_leaks(text) == set()
    _assert_stable(text, scrub_secrets(text)[0])


#: Section 5's matrix for the enclosures P3-5 found: an unclosed quoted value, single,
#: double and escaped, with a `Bearer` among the swallowed items.
UNCLOSED_ENCLOSURES = {
    "unclosed-single": ("intro\napi_key='xyz {item}", "intro\napi_key=xyz {item}"),
    "unclosed-double": ('intro\npassword: "abc {item}', "intro\npassword: abc {item}"),
    "unclosed-escaped": ("intro\ntoken=\\'xyz {item}", "intro\ntoken=xyz {item}"),
}


def _unclosed_matrix():
    for n, ((enclosure, (text, twin)), item, (placement, gap)) in enumerate(itertools.product(
            UNCLOSED_ENCLOSURES.items(), FLAGS + KEYS + ("Bearer",), PLACEMENTS.items())):
        tail = f"{gap}SECRET7ux{n} rest\ntail"
        yield pytest.param(text.format(item=item) + tail, twin.format(item=item) + tail, item in FLAGS,
                           id=f"{enclosure}/{item.strip(':=')}/{placement}")


@pytest.mark.parametrize("text,twin,flag", tuple(_unclosed_matrix()))
def test_an_unclosed_quote_does_not_take_what_a_swallowed_item_owns(text, twin, flag):
    """As `test_a_swallowed_flag_or_key_still_owns_its_next_line_value`: mode by mode,
    what the twin without the opening quote gives; a key's and a `Bearer`'s value is
    hidden in every mode, a flag's in the folded ones."""
    secret = SECRET.search(text).group()
    found, bare = _modes(text, excerpt=4096), _modes(twin, excerpt=4096)
    assert {mode: secret in out for mode, out in found.items()} == {mode: secret in out for mode, out in bare.items()}
    if not flag:
        assert not any(secret in out for out in found.values())
    for mode in FOLDED:
        assert secret not in found[mode], mode


@pytest.mark.documented_limitation
def test_a_refused_bearers_short_run_before_a_credential_word_shows_documented_limitation():
    """P3-6, the converse of D-SCR2's section 2 decision, stated in C-23.14: a refused
    `Bearer` redacts the run before the credential word only when that run reads as a
    token (eight or more characters, a letter and a digit). `SECRET7` is seven, so it
    shows, at 473fe2941 as here; release/217 hid it with the key."""
    text = "invalid Bearer SECRET7_token: expired"
    for scrubber in (handoff, base473):
        assert scrubber.scrub_secrets(text)[0] == "invalid Bearer SECRET7_token: [REDACTED]"
    assert release217.scrub_secrets(text)[0] == "invalid Bearer [REDACTED]: expired"


@pytest.mark.documented_limitation
def test_a_shell_continuation_after_a_flag_shows_its_value_documented_limitation():
    """P3-6: `--password \\` and the value on the next line. The flag rule takes the
    backslash as the flag's value, so the next line's word shows in every mode that
    keeps the line, the folded daemon.log line included, at 473fe2941 and release/217
    as here (follow-up 2)."""
    text = "mysql -u root --password \\\n  SECRET7c4c -h db"
    for name, modes in _tree_modes(text).items():
        assert all("SECRET7c4c" in out for mode, out in modes.items() if not mode.startswith("cut")), name


# --- section 3: Bearer's cost ----------------------------------------------------

#: #231 review r1's worst cases for the lookahead at the 262,144-character bound
#: (`--s`: 63.6 ms against 1.2 to 2.4 ms at release/217), and the units #231 measured.
COST_UNITS = ("--s", "~--a_", "--client_", "--a", "a_", "token", "x.")


@pytest.mark.parametrize("unit", COST_UNITS)
def test_the_bearer_rule_costs_what_release_217s_did(unit):
    """C-23.14 / C-25.5: `Bearer` decides once per match (`_bearer_refusal`), so at the
    work bound its rule costs what release/217's pattern costs on the same text,
    measured alongside it in one child (best-of-five CPU batches, so machine speed
    cancels). #231's lookahead took 25 to 40 times as long."""
    measured = _scrubber_probe(f"unit = {unit!r}\n" + '''
import json
from subfleet.sessions import handoff
from tests.cpu_scaling import best_cpu_ratio
from tests.unit import scrub_release217 as base
text = "Bearer " + unit * ((handoff.MAX_SCRUB_CHARS - 7) // len(unit))
# A tree without `_bearer_value` (#231's lookahead) replaces with the string.
replacement = getattr(handoff, "_bearer_value", "Bearer " + handoff.REDACTED)
def head():
    handoff._apply_rules(text, ((handoff._BEARER_RE, replacement),), handoff._Tally())
def reference():
    base._apply_rules(text, ((base._BEARER_RE, "Bearer " + base.REDACTED),), base._Tally())
print(json.dumps(best_cpu_ratio(head, reference)))
''')
    assert measured["ratio"] < 4, measured


def test_a_mebibyte_refusal_costs_daemon_log_at_most_the_cost_bound_over_release_s():
    """#231 review r1: a 1,048,576-character refusal held the GIL 241 ms through
    `detail_line`, against 48.6 ms at release/217 before #231 (requests are capped at
    1 MiB, and daemon.log scrubs a refusal whole before any bound). Since #239 round 8
    `detail_line` runs release/217's own line first (`scrub_release`, which has #231's
    lookahead), then the head's, so the reference is release's line through the frozen
    copy and the bound `COST_RATIO_MAX`."""
    measured = _scrubber_probe('''
import json
from subfleet import refusals
from subfleet.conversations import suggestions
from subfleet.sessions import scrub_release
from tests.cpu_scaling import best_cpu_ratio
text = "Bearer " + "--s" * ((1 << 20) // 3)
error = ValueError(text)
def head():
    refusals.detail_line("submit", error)
def reference():
    suggestions.bounded(suggestions._twice(scrub_release.scrub_secrets, "request submit refused (ValueError): " + text,
                                           suggestions._fold)[0], refusals.LOG_LINE_MAX, "…")
print(json.dumps(best_cpu_ratio(head, reference, rounds=3)))
''')
    assert measured["ratio"] < COST_RATIO_MAX, measured


@pytest.mark.parametrize("unit", ["~--a_", "--s"])
def test_the_bearer_rule_keeps_other_threads_responsive_at_the_bound(unit):
    """#231 review r1: the heartbeat gap under a whole `scrub_secrets` reached 84.9 ms
    on `~--a_` repeated (13 to 30 ms at release/217 before #231); the responsiveness
    bound is 0.1 s of CPU. The best of three runs is kept, as the gap is one sample's
    maximum. Since #239 round 8 the scrub runs release/217's own first (`scrub_release`),
    whose `Bearer` lookahead (#231's) is one regex call of about 80 to 100 ms here: the
    head's own pass keeps the 50 ms gap, and the whole scrub's gap is within
    `COST_RATIO_MAX` of release's own."""
    measured = _scrubber_probe(f"unit = {unit!r}\n" + '''
import json, threading, time
from subfleet.sessions import handoff, scrub_release
text = "Bearer " + unit * ((handoff.MAX_SCRUB_CHARS - 7) // len(unit))
def longest_gap(scrub):
    gaps = []
    for _ in range(3):
        ready, stopped, samples = threading.Event(), threading.Event(), []
        def heartbeat():
            samples.append(time.process_time())
            ready.set()
            while not stopped.wait(0.001):
                samples.append(time.process_time())
            samples.append(time.process_time())
        thread = threading.Thread(target=heartbeat)
        thread.start()
        ready.wait()
        try:
            scrub(text)
        finally:
            stopped.set()
            thread.join()
        gaps.append(max(b - a for a, b in zip(samples, samples[1:])))
    return min(gaps)
print(json.dumps({"head": longest_gap(handoff._head_scrub_secrets), "whole": longest_gap(scrub_secrets),
                  "release": longest_gap(scrub_release.scrub_secrets)}))
''')
    assert measured["head"] < 0.05, measured
    assert measured["whole"] < COST_RATIO_MAX * max(measured["release"], 0.05), measured


#: Inputs that would make an inline-header or `Bearer` scan quadratic if a candidate
#: searched its line again: many headers on one line, guarded, quoted, unclosed or
#: escaped, a long name, long backslash runs, and `Bearer` runs ending in keys.
LINEAR_UNITS = ("x Cookie: [REDACTED] ", "-H 'Cookie: a' ", "'Cookie: ", "x Authorization:", "a Cookie:\n",
                "\\\\\\'Cookie: x ", "a_cookie_", "Bearer a1b2c3d4_token: x ", "Bearer x--password\n",
                "Bearer " + "--s" * 9, "Bearer ~--a_~--a_ ",
                # Fix round 1: each of A to E's paths, and the reversed scan's colons.
                "secret:'Cookie:\nx Bearer  \n", "api_key='x --password   \n", "--password x--token ",
                "X-Authorization:\nAuthorization:\n", "Bearer\nabc1pi_keyAuthorization:--secret x\n", "x\t: ")


def test_an_unclosed_value_reads_its_lines_end_once():
    """Fix round 1: an unclosed value looks for a flag or `Bearer` at its line's last
    visible character (`_owned_item`). Each kind of escaped quote, by its run of
    backslashes, can leave one value unclosed on a line, so a line can hold hundreds;
    each walking back over the line's trailing whitespace again would be quadratic.
    The walk is kept per position, so this costs what release/217's scrub does."""
    measured = _scrubber_probe('''
import json
from subfleet.sessions import handoff
from tests.cpu_scaling import best_cpu_ratio
from tests.unit import scrub_release217 as base
values = "".join("api_key=" + "\\\\" * run + "'x " for run in range(1, 500))
text = values + " " * (handoff.MAX_SCRUB_CHARS - len(values) - 1) + "\\n"
def head():
    handoff.scrub_secrets(text)
def reference():
    base.scrub_secrets(text)
print(json.dumps(best_cpu_ratio(head, reference, rounds=3)))
''')
    assert measured["ratio"] < 4, measured


@pytest.mark.parametrize("unit", LINEAR_UNITS)
def test_inline_headers_and_bearer_keep_scrubbing_linear(unit):
    """C-23.14 / C-25.5: four times the input takes about four times the CPU."""
    measured = _scrubber_probe(f"unit = {unit!r}\n" + '''
import json
from tests.cpu_scaling import best_cpu_samples

def make_call(size):
    text = unit * (size // len(unit))
    def call():
        scrub_secrets(text)
    return call

print(json.dumps(best_cpu_samples(make_call, (32768, 131072))))
''')
    assert measured["ratio"] < 8, measured


# --- the callback agrees with the lookahead it replaces --------------------------

#: #231's `_BEARER_RE`, the lookahead D-SCR2 replaced, with its flag branch widened
#: as D-SCR2 widens `_bearer_refusal`'s (a flag followed by any whitespace or the
#: text's end may own a value), so that the two decide the same texts.
LOOKAHEAD_231 = re.compile(
    r"(?i)\bBearer\s+(?![A-Za-z0-9._~+/-]*?(?:" + handoff._SENSITIVE_WORD + handoff._ASSIGN_SEPARATOR_RE.pattern
    + r"|--" + handoff._SENSITIVE_KEY + r"(?:\s|\Z)))[A-Za-z0-9._~+/-]{8,}=*")
BEARER_PIECES = ("Bearer ", "bearer\n", "BEARER  ", "x", "SECRET7a1", "token", "_token", "--password", "--x",
                 "secret_access_key", ".", "/", "~", "-", "==", ": ", "=", "\\\": ", " v", "\n", "\t-v", "'", ",")


def _bearer_texts():
    """Every text of up to three pieces after each scheme word, then 20,000 seeded
    ones of four to seven, each ending in ` tail` or at a piece."""
    for size in range(1, 4):
        for parts in itertools.product(BEARER_PIECES[3:], repeat=size):
            for scheme in BEARER_PIECES[:3]:
                yield scheme + "".join(parts) + " tail"
    rng = random.Random(231)
    for _ in range(20_000):
        parts = [rng.choice(BEARER_PIECES) for _ in range(rng.randint(4, 7))]
        yield rng.choice(BEARER_PIECES[:3]) + "".join(parts) + rng.choice(("", " tail"))


def _secret_named_flag_run(match) -> bool:
    """Whether a `Bearer` run ends in a credential flag whose name reads as a secret,
    which owns nothing since round 2 (#239 review r2, P4), so the rule takes the run."""
    text, start, end = match.string, match.start(1), match.end(1)
    return handoff._ending_flag(text, start, end) is not None and handoff._owning_flag(text, start, end) is None


def test_bearer_refuses_exactly_what_the_lookahead_did():
    """The matches `_bearer_refusal` accepts are the lookahead's, span for span, and
    since round 2 those whose run ends in a secret-named flag (P4): the lookahead
    refused `Bearer --passwordSECRET7a1_token tail`, which is now redacted, the value
    daemon.log's folded line gives the flag too (`Bearer [REDACTED] [REDACTED]`)."""
    tried = reread = 0
    for text in _bearer_texts():
        matches = list(handoff._BEARER_RE.finditer(text))
        accepted = [match.span() for match in matches if handoff._bearer_refusal(match) is None]
        expected = {match.span() for match in LOOKAHEAD_231.finditer(text)}
        expected |= {match.span() for match in matches if _secret_named_flag_run(match)}
        assert accepted == sorted(expected & {match.span() for match in matches}), text
        reread += any(_secret_named_flag_run(match) for match in matches)
        tried += 1
    assert tried > 45_000 and reread >= 10
    assert scrub_secrets("Bearer --passwordSECRET7a1_token tail")[0] == "Bearer [REDACTED] [REDACTED]"
