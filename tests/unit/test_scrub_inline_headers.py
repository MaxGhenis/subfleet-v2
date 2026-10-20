"""C-23.14 (D-SCR2, review r1 of #231 and r3 of #233): inline credential headers are
scrubbed, a secret glued to a credential word stays hidden, `Bearer` decides once per
match, and a flag or key another rule's redaction encloses still owns its value.

Each regression leaked at the base this branch was cut from (release/217 after
#231, `16b24033a`), in the modes it names; the guards pin what must not change.
`scrub_release217` is release/217 before #231, for the cost comparisons."""

from __future__ import annotations

import itertools
import random
import re

import pytest

from subfleet import refusals
from subfleet.conversations.redact import mask_approval
from subfleet.sessions import handoff
from subfleet.sessions.handoff import scrub_secrets
from tests.unit import scrub_release217 as release217
from tests.unit.test_scrub_key_order import SECRET, _assert_kept_out, _modes
from tests.unit.test_sessions_handoff import _scrubber_probe


def _assert_stable(text: str, scrubbed: str) -> None:
    """Scrubbing a scrubbed text changes nothing, in either mode or daemon.log's."""
    assert scrub_secrets(scrubbed) == (scrubbed, 0)
    assert scrub_secrets(scrubbed, whole=True) == (scrubbed, 0)
    line = refusals.scrubbed_line(text)
    assert refusals.scrubbed_line(line) == line


# --- section 1: inline credential headers --------------------------------------

#: #231 review r1's inline headers, every quoting form, the names that end in
#: `authorization` or `cookie`, and a value on the next line.
INLINE_HEADERS = {
    "curl-basic": ("curl -H 'Authorization: Basic SECRET7inlnaa1' https://x",
                   "curl -H 'Authorization: [REDACTED]' https://x"),
    "curl-token": ('curl -H "Authorization: Token SECRET7inlnaa2" https://x',
                   'curl -H "Authorization: [REDACTED]" https://x'),
    "wget-header": ("wget --header='Authorization: Basic SECRET7inlnaa3' https://x",
                    "wget --header='Authorization: [REDACTED]' https://x"),
    "curl-cookie": ("curl -H 'Cookie: a=1; sid=SECRET7inlnaa4' https://x", "curl -H 'Cookie: [REDACTED]' https://x"),
    "prose": ("upstream sent Authorization: Basic SECRET7inlnaa5 and failed", "upstream sent Authorization: [REDACTED]"),
    "proxy-line-start": ("Proxy-Authorization: Basic SECRET7inlnaa6", "Proxy-Authorization: [REDACTED]"),
    "proxy-inline": ("curl -H 'Proxy-Authorization: Basic SECRET7inlnaa7' -v",
                     "curl -H 'Proxy-Authorization: [REDACTED]' -v"),
    "x-authorization": ("sent X-Authorization: SECRET7inlnaa8 ok", "sent X-Authorization: [REDACTED]"),
    "set-cookie": ("got Set-Cookie: id=SECRET7inlnaa9; Path=/; HttpOnly", "got Set-Cookie: [REDACTED]"),
    # An escaped closing quote ends the value at its backslashes; the plain rule, which
    # then reads the header's name as a key, takes those as its value's end, as it
    # always took a backslash (`api_key=x\\"` scrubs to `api_key=[REDACTED]"`).
    "json-escaped": ('{"cmd": "curl -H \\"Authorization: Basic SECRET7inlnab1\\" https://x"}',
                     '{"cmd": "curl -H \\"Authorization: [REDACTED]" https://x"}'),
    "repr-escaped": ("'curl -H \\'Cookie: sid=SECRET7inlnab2\\' https://x'",
                     "'curl -H \\'Cookie: [REDACTED]' https://x'"),
    "json-twice": ('"curl -H \\\\\\"Authorization: Basic SECRET7inlnab3\\\\\\" x"',
                   '"curl -H \\\\\\"Authorization: [REDACTED]" x"'),
    "inner-escaped-quote": ("-H 'Cookie: a=\\'SECRET7inlnab4\\' b' tail", "-H 'Cookie: [REDACTED]' tail"),
    "unclosed-quote": ("curl -H 'Authorization: Basic SECRET7inlnab5 -v\nnext", "curl -H 'Authorization: [REDACTED]\nnext"),
    "value-next-line": ("sent Authorization:\n  Basic SECRET7inlnab6 tail", "sent Authorization:\n  [REDACTED]"),
    "tab-before-colon": ("sent Cookie\t: sid=SECRET7inlnab7", "sent Cookie\t: [REDACTED]"),
    "lowercase": ("headers: cookie: sid=SECRET7inlnab8", "headers: cookie: [REDACTED]"),
    "two-on-a-line": ("-H 'Cookie: SECRET7inlnab9' -H 'Authorization: Basic SECRET7inlnac1' x",
                      "-H 'Cookie: [REDACTED]' -H 'Authorization: [REDACTED]' x"),
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
    assert found["daemon.log"] == "request submit refused (ValueError): Authorization: [REDACTED]"


@pytest.mark.parametrize("text,count", [
    ("curl -H 'Authorization: Basic SECRET7cntaa1' x", 1),
    ("curl -H 'Cookie: a=SECRET7cntaa2; b=SECRET7cntaa3' x", 2),
    ("curl -H 'Authorization: Bearer SECRET7cntaa4' x", 1),
    ("sent Cookie: [REDACTED] then SECRET7cntaa5", 0),
], ids=["one", "two-values", "bearer-inside", "already-redacted"])
def test_an_inline_header_counts_as_a_line_start_one(text, count):
    """Once per secret-looking run the value held (`_Tally`'s header rule). A value
    that already starts with `[REDACTED]` is left alone: the guard that keeps a
    scrubbed text stable (the last case's own secret is the documented cost)."""
    assert scrub_secrets(text)[1] == count


def test_daemon_logs_folded_line_keeps_the_words_after_a_redacted_header():
    """The guard: a folded line puts the next line's words after a redacted header,
    no longer at a line start; they are not the header's value."""
    text = "refused: upstream\nsent Authorization: Basic SECRET7foldaa1\nwhile reading job 20261020-a1b2c3"
    assert refusals.scrubbed_line(text) == (
        "refused: upstream sent Authorization: [REDACTED] while reading job 20261020-a1b2c3")


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
CREDENTIAL_WORD_ENDINGS = {
    "bearer-token-colon": ("invalid Bearer SECRET7a1b2c3d4_token: expired",
                           "invalid Bearer [REDACTED]_token: [REDACTED]"),
    "header-equals-underline": ("Cookie: sid=SECRET7abc123token\n====", "Cookie: [REDACTED] token\n=[REDACTED]"),
    "header-pseudo-header": ("Authorization: Basic SECRET7abc123secret\n:authority: example.com",
                             "Authorization: [REDACTED] secret\n:[REDACTED] example.com"),
    "bearer-flag": ("auth: Bearer SECRET7abcdef12--password hunter2x tail",
                    "auth: Bearer [REDACTED]--password [REDACTED] tail"),
    "inline-header-glued": ("sent Cookie: sid=SECRET7abc123token\n= next", "sent Cookie: [REDACTED] token\n= [REDACTED]"),
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


@pytest.mark.parametrize("text,twin,flag", _matrix())
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
    ("api_key=xyz--password SECRET7ownaa1 rest", "api_key=[REDACTED]--password [REDACTED] rest"),
    ("Authorization: Bearer --client-secret\nSECRET7ownaa2", "Authorization: [REDACTED] --client-secret\nSECRET7ownaa2"),
    ("Cookie: --auth-token\nSECRET7ownaa3", "Cookie: --auth-token\nSECRET7ownaa3"),
], ids=["plain-value-same-line", "header-and-bearer", "header-holding-only-the-flag"])
def test_a_flag_left_by_its_enclosure_is_read_by_the_flag_rule(text, scrubbed):
    """The mechanism: an enclosing match ends before a credential flag that ends it
    and may own a value past it (`_owning_flag`), and leaves the flag and what
    follows it in place. A plain value that ended in such a flag swallowed even a
    same-line value (the first case leaked in every mode at the base)."""
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
@pytest.mark.parametrize("text", ["x --password\nSECRET7flgaa1 rest", "intro\nAuthorization: x --password\nSECRET7flgaa2"])
def test_a_flags_next_line_value_shows_outside_the_folded_line_documented_limitation(text):
    """Outside D-SCR2, at the base as here: the flag rule reads a value on the flag's
    own line only (`_FLAG_VALUE_RE`), so a value on the next line shows in the
    whole-text, excerpt and cut modes, with an enclosing header or without one. Only
    daemon.log's folded line puts it on the flag's line. A follow-up must decide
    whether a flag that ends its line takes the next line's first word."""
    secret = SECRET.search(text).group()
    assert secret in scrub_secrets(text)[0] and secret in scrub_secrets(text, whole=True)[0]
    assert secret not in refusals.scrubbed_line(text)


@pytest.mark.documented_limitation
def test_curl_user_password_has_no_rule_documented_limitation():
    """`curl -u user:SECRET…` is no rule's: the next follow-up (D-SCR2's brief)."""
    text = "curl -u admin:SECRET7curlaa1 https://x"
    assert "SECRET7curlaa1" in scrub_secrets(text)[0]
    assert "SECRET7curlaa1" in release217.scrub_secrets(text)[0]


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


def test_a_mebibyte_refusal_costs_daemon_log_what_release_217_did():
    """#231 review r1: a 1,048,576-character refusal held the GIL 241 ms through
    `detail_line`, against 48.6 ms at release/217 (requests are capped at 1 MiB, and
    daemon.log scrubs a refusal whole before any bound)."""
    measured = _scrubber_probe('''
import json
from subfleet import refusals
from tests.cpu_scaling import best_cpu_ratio
from tests.unit import scrub_release217 as base
text = "Bearer " + "--s" * ((1 << 20) // 3)
error = ValueError(text)
def head():
    refusals.detail_line("submit", error)
def reference():
    base.scrubbed_line("request submit refused (ValueError): " + text)
print(json.dumps(best_cpu_ratio(head, reference, rounds=3)))
''')
    assert measured["ratio"] < 3, measured


@pytest.mark.parametrize("unit", ["~--a_", "--s"])
def test_the_bearer_rule_keeps_other_threads_responsive_at_the_bound(unit):
    """#231 review r1: the heartbeat gap under a whole `scrub_secrets` reached 84.9 ms
    on `~--a_` repeated (13 to 30 ms at release/217); the responsiveness bound is
    0.1 s of CPU. The best of three runs is kept, as the gap is one sample's maximum."""
    measured = _scrubber_probe(f"unit = {unit!r}\n" + '''
import json, threading, time
from subfleet.sessions import handoff
text = "Bearer " + unit * ((handoff.MAX_SCRUB_CHARS - 7) // len(unit))
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
        scrub_secrets(text)
    finally:
        stopped.set()
        thread.join()
    gaps.append(max(b - a for a, b in zip(samples, samples[1:])))
print(json.dumps({"longest_cpu_gap": min(gaps), "gaps": gaps}))
''')
    assert measured["longest_cpu_gap"] < 0.05, measured


#: Inputs that would make an inline-header or `Bearer` scan quadratic if a candidate
#: searched its line again: many headers on one line, guarded, quoted, unclosed or
#: escaped, a long name, long backslash runs, and `Bearer` runs ending in keys.
LINEAR_UNITS = ("x Cookie: [REDACTED] ", "-H 'Cookie: a' ", "'Cookie: ", "x Authorization:", "a Cookie:\n",
                "\\\\\\'Cookie: x ", "a_cookie_", "Bearer a1b2c3d4_token: x ", "Bearer x--password\n",
                "Bearer " + "--s" * 9, "Bearer ~--a_~--a_ ")


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


def test_bearer_refuses_exactly_what_the_lookahead_did():
    """The matches `_bearer_refusal` accepts are the lookahead's, span for span."""
    tried = 0
    for text in _bearer_texts():
        accepted = [match.span() for match in handoff._BEARER_RE.finditer(text) if handoff._bearer_refusal(match) is None]
        assert accepted == [match.span() for match in LOOKAHEAD_231.finditer(text)], text
        tried += 1
    assert tried > 45_000
