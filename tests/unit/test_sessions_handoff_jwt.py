"""Any JWT is scrubbed, however its header's JSON is written (C-23.14).

Defect verified 2026-10-18 at release/217 3fbc4b7c5: the JWT rule matched only tokens
that start `eyJ`, the encoding of `{"`. A header whose JSON opens any other way (a
space or line break, a BOM, UTF-16: `{ "alg":"RS256","typ":"JWT"}` encodes to
`eyAi…`) makes a token JWT readers still accept (`adapters.codex._claims` reads its claims), and it
passed `scrub_secrets` and `scrub_bounded` with 0 replacements: into handoff briefs,
conversation events, suggestions and refusal details, and past the approval masker
(C-27.1). #171 worked around it for timer failure messages only.

Every token here is a MOCK built in-process from made-up JSON and bytes; none is, or
was, a credential.
"""

from __future__ import annotations

import base64
import json
import re
import string

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet import ids, refusals
from subfleet.conversations import diff, redact
from subfleet.sessions import handoff

REDACTED = handoff.REDACTED
#: Token characters: one of these before a JWT makes it part of a longer token (C-1.5).
TOKEN_CHARS = string.ascii_letters + string.digits + "_-"


def b64(raw: bytes | str, *, pad: bool = False) -> str:
    raw = raw.encode() if isinstance(raw, str) else raw
    encoded = base64.urlsafe_b64encode(raw).decode()
    return encoded if pad else encoded.rstrip("=")


def mock_jwt(header: bytes | str = '{"alg":"RS256","typ":"JWT"}', *, pad: bool = False,
             extra: tuple[bytes, ...] = ()) -> str:
    """A MOCK token: the header given, a made-up payload and signature bytes."""
    segments = [b64(header, pad=pad), b64('{"sub":"MOCK-1234567890"}', pad=pad),
                b64(b"MOCK signature bytes, not a key", pad=pad)]
    return ".".join(segments + [b64(part, pad=pad) for part in extra])


#: The token the defect report gives, its parts kept apart so none is a literal key.
MOCK_EYA = ("eyAiYWxnIjoiUlMyNTYiLCJ0eXAiOiJKV1QifQ" + "." + "eyJzdWIiOiJNT0NLLTEyMzQ1Njc4OTAifQ"
            + "." + "MoCkSiGnAtUrE0123456789abcdefGHIJ")


def test_the_reported_token_is_scrubbed_and_counted():
    """C-23.14: the token of the defect report passed every scrub with 0 replacements."""
    assert MOCK_EYA.startswith("eyAi")
    text = "provider rejected " + MOCK_EYA
    assert handoff.scrub_secrets(text) == ("provider rejected " + REDACTED, 1)
    assert handoff.scrub_secrets(text, whole=True) == ("provider rejected " + REDACTED, 1)
    assert handoff.scrub_bounded(text, None) == ("provider rejected " + REDACTED, 1)
    assert handoff.scrub_bounded(text, 30) == ("provider rejected " + REDACTED, 1)


@pytest.mark.parametrize("header", [
    pytest.param(b'{ "alg":"RS256","typ":"JWT"}', id="space-before-first-member"),
    pytest.param(b'\n{"alg":"HS256"}', id="leading-line-break"),
    pytest.param(b'\t{"alg":"HS256"}', id="leading-tab"),
    pytest.param(b'\r\n{"alg":"ES256"}', id="leading-crlf"),
    pytest.param(b'{\n  "alg": "RS256",\n  "typ": "JWT"\n}', id="indented"),
    pytest.param(b'{"\\u0061lg":"RS256"}', id="escaped-member-name"),
    pytest.param(b'{"typ":"JWT","kid":"2026-10","alg":"PS512"}', id="alg-last"),
    pytest.param(b'{"alg":null}', id="alg-null"),
    pytest.param(b'{"alg":0}', id="shortest-header"),
    pytest.param('﻿{"alg":"RS256"}'.encode(), id="utf-8-bom"),
    pytest.param('{"alg":"RS256"}'.encode("utf-16"), id="utf-16"),
    pytest.param('{"alg":"RS256"}'.encode("utf-16-le"), id="utf-16-le"),
    pytest.param('{"alg":"RS256"}'.encode("utf-32-be"), id="utf-32-be"),
    pytest.param(b'{"alg":"RS256","jwk":{"kty":"RSA","e":"AQAB","n":"' + b"x" * 400 + b'"}}',
                 id="embedded-key"),
])
def test_a_header_however_its_json_is_written_is_found(header):
    """C-23.14: a header is a segment that decodes to a JSON object with an `alg`
    member, as `json` reads one (and `adapters.codex._claims` reads a payload)."""
    assert json.loads(base64.urlsafe_b64decode(b64(header, pad=True)))["alg"] in ("RS256", "HS256", "ES256",
                                                                                "PS512", None, 0)
    token = mock_jwt(header)
    for text in (token, f"failed: {token}", f'{{"token_hint": "{token}"}}', f"x={token}&y=1",
                 f"(see {token}).", f"<{token}>", f"Authorization rejected:\n{token}\n"):
        out, count = handoff.scrub_secrets(text)
        assert token not in out and REDACTED in out and count == 1, (text, out, count)


@pytest.mark.parametrize("token, kept", [
    pytest.param(mock_jwt(pad=True), "", id="padded"),
    pytest.param(mock_jwt('{ "alg":"dir","enc":"A256GCM"}', extra=(b"iv-bytes-mock", b"tag-bytes-mock")), "",
                 id="jwe-five-segments"),
    pytest.param(b64('{ "alg":"none"}') + "." + b64('{"sub":"MOCK"}') + ".", ".", id="unsecured-empty-signature"),
    pytest.param(b64('{ "alg":"HS256"}') + ".." + b64(b"MOCK detached signature"), "", id="detached-payload"),
    pytest.param(b64('{ "alg":"HS256"}') + ".x.y", "", id="short-segments-after-a-header"),
])
def test_every_jwt_serialization_goes_whole(token, kept):
    """C-23.14: a JWT runs from its header to its run's end, so a JWE's five
    segments go too; a trailing dot holds nothing and is kept."""
    out, count = handoff.scrub_secrets(f"value {token} end")
    assert out == f"value {REDACTED}{kept} end" and count == 1


def test_text_joined_to_a_jwt_by_a_dot_is_kept():
    """C-23.14: the JWT starts at its header, wherever that is in its run."""
    assert handoff.scrub_secrets(f"see config.{MOCK_EYA}.") == (f"see config.{REDACTED}.", 1)
    assert handoff.scrub_secrets(f"a.b.{MOCK_EYA}") == (f"a.b.{REDACTED}", 1)


def test_each_jwt_counts_once_as_the_other_rules_do():
    """C-23.14: a replacement counts one value; one two rules match counts once."""
    assert handoff.scrub_secrets(f"{MOCK_EYA} and {mock_jwt()}") == (f"{REDACTED} and {REDACTED}", 2)
    assert handoff.scrub_secrets(f"Authorization: Bearer {MOCK_EYA}")[1] == 1
    assert handoff.scrub_secrets(f"curl -H 'X-Auth: Bearer {MOCK_EYA}'") == (f"curl -H 'X-Auth: Bearer {REDACTED}'", 1)
    assert handoff.scrub_secrets(f'access_token = "{MOCK_EYA}"') == (f'access_token = "{REDACTED}"', 1)
    assert handoff.scrub_secrets(f"https://user:{MOCK_EYA}@example.test/") == (
        f"https://user:{REDACTED}@example.test/", 1)


@pytest.mark.parametrize("text", [
    "subfleet.sessions.handoff.scrub_secrets(text)",
    "from subfleet.conversations.redact import mask_approval",
    "report.2026.10.18.tar.gz and README.md.bak",
    "v1.2.3-rc.4, 10.0.0.1, 2001.db8.0.1",
    "self._pending.rfind(x) and os.path.join(a, b)",
    "api.example.test/v1.2/items.json",
    "e.g. i.e. etc. a.b.c x.y.z.w.v.u",
    "com.example.app.MainActivity.onCreate",
    "QUJDREVGR0hJSktM.QUJDREVGR0hJSktM.QUJDREVGR0hJSktM",                  # not JSON
    b64('{ "typ":"JWT","kid":"k1"}') + "." + b64('{"sub":"MOCK"}') + "." + b64(b"MOCK sig"),  # no `alg`
    b64('["alg","RS256"]') + "." + b64('{"sub":"MOCK"}') + "." + b64(b"MOCK sig"),           # not an object
    b64('{ "alg":"RS256"}') + "." + b64('{"sub":"MOCK"}'),                                    # two segments
])
def test_dotted_names_without_a_header_are_kept(text):
    """C-23.14: only a run with a JOSE header and two segments after it holds a JWT."""
    assert handoff.scrub_secrets(text) == (text, 0)
    assert handoff.scrub_secrets(text, whole=True) == (text, 0)


def test_the_eyJ_shape_is_still_a_jwt():
    """C-23.14: a segment that starts `eyJ` followed by two of at least 8 characters
    is a JWT whether or not it decodes, as before."""
    shape = "eyJ" + "A" * 8 + "." + "B" * 8 + "." + "C" * 8
    assert handoff.scrub_secrets(f"t {shape} t") == (f"t {REDACTED} t", 1)
    assert handoff.scrub_secrets(f"t {shape}.more t") == (f"t {REDACTED} t", 1)


def test_every_scrubbing_caller_removes_it():
    """C-23.14, C-25.5, C-25.7, C-26.14, C-27.1, C-1.5: the callers inherit the rule."""
    assert MOCK_EYA not in redact.scrub(f"answer {MOCK_EYA}")
    assert MOCK_EYA not in redact.bounded(f"answer {MOCK_EYA}", 40)
    assert MOCK_EYA not in handoff.clean(f"tool said {MOCK_EYA}", 200)[0]
    patch, count = diff.scrub_diff(f"diff --git a/f b/f\n@@ -1 +1 @@\n-old\n+header {MOCK_EYA}\n")
    assert MOCK_EYA not in patch and f"+header {REDACTED}" in patch and count == 1
    line = refusals.detail_line("show", RuntimeError(f"provider rejected {MOCK_EYA}"))
    assert line is not None and MOCK_EYA not in line and REDACTED in line
    masked, spans = redact.mask_approval({"command": f"curl -H 'X-Auth: {MOCK_EYA}' https://example.test"})
    assert masked["command"] == f"curl -H 'X-Auth: [masked jwt, {len(MOCK_EYA)} chars]' https://example.test"
    assert [span["rule"] for span in spans] == ["jwt"]
    # C-1.5: an id is read from after each joiner, where a token can start.
    assert ids.holds_credential(f"run-{MOCK_EYA}") and ids.holds_credential(MOCK_EYA)
    assert not ids.holds_credential("subfleet.sessions.handoff")


def test_a_jwt_in_the_excerpts_of_an_oversized_text_is_scrubbed():
    """C-23.14: the rule matches within a line, so each excerpt scrubs it."""
    prose = "ordinary words in a line of prose, with nothing in it worth hiding\n"
    text = f"first {MOCK_EYA}\n" + prose * 6_000 + f"last {mock_jwt()}\n" + prose * 10
    assert len(text) > handoff.MAX_SCRUB_CHARS
    out, count = handoff.scrub_bounded(text, None)
    assert MOCK_EYA not in out and mock_jwt() not in out and count == 2
    assert out.startswith(f"first {REDACTED}") and f"last {REDACTED}" in out


def test_a_segment_json_cannot_read_is_no_header_and_never_raises():
    """C-23.14: a segment nested past `json`'s recursion limit, or holding an integer
    longer than `int` reads, is not a header; the scrub still finishes."""
    for body in ("[" * 100_000, '{ "alg":' * 100_000, '{ "alg":' + "9" * 5_000 + "}"):
        text = f"x {b64(body)}.payload.signature y"
        assert handoff._linear_sub(handoff._JWT_RUN_RE, REDACTED, text) == text
        handoff.scrub_secrets(text)          # other rules may omit the long run; none raises


def test_each_segment_is_decoded_at_most_once(monkeypatch):
    """C-23.14: the rule's work is linear: a run of n segments is decoded at most n
    times, however its segments are arranged."""
    calls = []
    real = base64.urlsafe_b64decode
    monkeypatch.setattr(handoff.base64, "urlsafe_b64decode", lambda value: (calls.append(1), real(value))[1])
    for unit in ("A" * 12 + ".", b64('{"a":"' + "x" * 30) + ".", b64("[" * 40) + ".", "eyJ" + "A" * 9 + ".x."):
        calls.clear()
        run = unit * 2_000
        handoff.scrub_secrets(run)
        assert len(calls) <= run.count(".") + 1, (unit, len(calls))


# --- properties -------------------------------------------------------------------------

JSON_SCALARS = (st.none() | st.booleans() | st.integers(-10**9, 10**9)
                | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=12))
JSON_VALUES = st.recursive(JSON_SCALARS, lambda inner: st.lists(inner, max_size=3)
                           | st.dictionaries(st.text(max_size=8), inner, max_size=3), max_leaves=6)
JSON_SPACE = st.text(" \t\n\r", max_size=3)


@st.composite
def headers(draw) -> bytes:
    """A JOSE header: any JSON object with an `alg` member, in any member order,
    spacing and encoding `json` reads (C-23.14)."""
    members = draw(st.dictionaries(st.text(max_size=10), JSON_VALUES, max_size=4))
    members["alg"] = draw(JSON_VALUES)
    order = draw(st.permutations(sorted(members)))
    text = json.dumps({key: members[key] for key in order},
                      indent=draw(st.sampled_from([None, 0, 2, "\t"])),
                      separators=draw(st.sampled_from([(",", ":"), (", ", ": "), (" ,", " : "), (",\n", ":\t")])),
                      ensure_ascii=draw(st.booleans()))
    encoding = draw(st.sampled_from(["utf-8", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-32"]))
    return (draw(JSON_SPACE) + text + draw(JSON_SPACE)).encode(encoding)


@st.composite
def jwts(draw) -> str:
    """A JWT from a random header, a random payload (JSON or bytes) and a random
    signature, perhaps padded, perhaps a JWE's five segments."""
    payload = draw(st.one_of(JSON_VALUES.map(lambda value: json.dumps(value).encode()), st.binary(max_size=48)))
    parts = [draw(headers()), payload, draw(st.binary(max_size=64))]
    parts += draw(st.sampled_from([[], [b"", b""]]) | st.lists(st.binary(max_size=24), min_size=2, max_size=2))
    return ".".join(b64(part, pad=draw(st.booleans())) for part in parts)


#: Text the other rules act on, so the JWT is tried inside them too. A data URI is
#: left out: its body swallows the head of a token after it, a defect of that rule
#: which predates this one (follow-up of 2026-10-18).
FRAGMENTS = st.sampled_from([
    "password = ", "access_token=", '"', "'", "\\", "\\'", "Bearer ", "Authorization: ", "Cookie: a=",
    "token", ":", "=", ".", "..", "-", " ", "\n", "\r\n", "\t", "-----BEGIN PRIVATE KEY-----\n",
    "-----END PRIVATE KEY-----", "<system-reminder>", "</system-reminder>", "https://user:", "@host/",
    "eyJ", "eyJAAAAAAAA", "QUJD" * 3, "sk-ant-" + "A" * 20, "--password ", "os.path", "ordinary words ",
])
SURROUNDINGS = st.lists(st.one_of(st.text(st.characters(blacklist_characters="["), max_size=10), FRAGMENTS),
                        max_size=6).map("".join)
#: What sets a JWT apart from the text before it: any character outside the token
#: alphabet. After a letter, digit, `-` or `_` it is part of a longer token (C-1.5).
SEPARATORS = st.characters(blacklist_characters=TOKEN_CHARS)


@hypothesis.settings(max_examples=600, deadline=None)
@hypothesis.given(token=jwts(), before=SURROUNDINGS, separator=SEPARATORS, after=SURROUNDINGS,
                  whole=st.booleans())
def test_any_jwt_is_scrubbed_whatever_surrounds_it(token, before, separator, after, whole):
    """C-23.14 (property): a JWT built from any JSON header with an `alg` member, any
    payload and any signature is gone from every scrub and from an approval, and the
    scrub counts it, whatever text is around it."""
    text = before + separator + token + after
    out, count = handoff.scrub_secrets(text, whole=whole)
    assert token not in out and count >= 1
    for segment in token.split("."):
        body = segment.rstrip("=")
        if len(body) >= 8 and body not in before and body not in after:
            assert body not in out, (body, out)
    masked, spans = redact.mask_approval({"command": text})
    assert token not in masked["command"] and spans


@hypothesis.settings(max_examples=300, deadline=None)
@hypothesis.given(token=jwts(), before=st.text(max_size=20), after=st.text(max_size=20),
                  separator=st.sampled_from(" \n\t\"'=:(,.<"))
def test_the_rule_is_idempotent(token, before, after, separator):
    """C-23.14 (property): what the rule leaves holds no JWT it would replace."""
    once = handoff._linear_sub(handoff._JWT_RUN_RE, REDACTED, before + separator + token + after)
    assert token not in once
    assert handoff._linear_sub(handoff._JWT_RUN_RE, REDACTED, once) == once


#: The rule as it stood at release/217 3fbc4b7c5: the reference the widened rule is
#: checked against.
EYJ_RULE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
                      r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
PIECES = st.sampled_from(["eyJ", "eyA", "A" * 8, "abc", "x", ".", "..", "=", "==", "-", "_", " ", "\n", ":",
                          '"', "x.y", MOCK_EYA, "eyJ" + "A" * 8, "B" * 8, "Zq9"])


@hypothesis.settings(max_examples=2_000, deadline=None)
@hypothesis.given(st.lists(PIECES, max_size=30).map("".join))
def test_the_rule_replaces_everything_the_eyJ_rule_did(text):
    """C-23.14 (differential): every span the old `eyJ` pattern matched lies inside
    a span the widened rule replaces."""
    spans = []
    handoff._linear_sub(handoff._JWT_RUN_RE, lambda match: spans.append(match.span()) or REDACTED, text)
    for old in EYJ_RULE.finditer(text):
        assert any(start <= old.start() and old.end() <= end for start, end in spans), (text, old.span(), spans)


IDENTIFIER = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True)


@hypothesis.settings(max_examples=1_000, deadline=None)
@hypothesis.given(st.lists(st.lists(IDENTIFIER, min_size=1, max_size=8).map(".".join), max_size=5),
                  st.sampled_from([" ", "\n", "(", ", ", "/"]))
def test_dotted_identifiers_are_kept(names, joiner):
    """C-23.14 (property): module paths, attribute chains and file names, none of
    whose parts is a JOSE header, are not touched."""
    hypothesis.assume(not any(part.startswith("eyJ") for name in names for part in name.split(".")))
    text = joiner.join(names)
    assert handoff._linear_sub(handoff._JWT_RUN_RE, REDACTED, text) == text
