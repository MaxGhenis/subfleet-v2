"""C-25.7/C-23.14: repr and incomplete assignments stay out of daemon.log, whole
texts and handoff excerpts alike."""

import configparser
import itertools
import json
import logging

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import host_push, refusals
from subfleet.sessions import handoff
from subfleet.sessions.handoff import scrub_secrets
from tests.unit.test_protocol_secrecy import wire_reply


def _responder_log(tmp_path, error):
    """What the real request responder writes to a temporary daemon.log, and its reply."""
    logger = logging.getLogger("protocol-secrecy")
    handler = logging.FileHandler(tmp_path / "daemon.log")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        response = wire_reply(op="submit", error=error)
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(handler)
        handler.close()
    return (tmp_path / "daemon.log").read_text(), response


def _config_refusal(source):
    with pytest.raises(configparser.Error) as caught:
        configparser.ConfigParser().read_string(source)
    return host_push._workspace_refused(host_push.PushError(str(caught.value), kind="config-header"))


def test_daemon_log_scrubs_config_errors_with_unterminated_repr_quotes(tmp_path):
    # The real request responder writes its detail to the local log, never the wire.
    logged, response = _responder_log(tmp_path, _config_refusal("password='SECRET7-left\n\""))
    assert "refused" in logged and "File contains no section headers" in logged
    assert "SECRET7-left" not in logged
    assert "SECRET7-left" not in str(response)


@pytest.mark.parametrize("source", [
    "opts = {'password': 'SECRET7quotedkey9', 'note': \"it's\"}\n",
    "'password': 'SECRET7quotedkey9' # \"note\"\n",
], ids=["dict-both-quotes", "quoted-key-both-quotes"])
def test_daemon_log_scrubs_a_repr_escaped_quoted_key(tmp_path, source):
    """Review r1 of #225, P2: configparser logs the line as a repr, `\\'password\\': \\'…\\'`,
    and the escaped closing quote of the key hid the separator from every rule."""
    logged, response = _responder_log(tmp_path, _config_refusal(source))
    assert "File contains no section headers" in logged and "password" in logged
    assert "SECRET7quotedkey9" not in logged
    assert "SECRET7quotedkey9" not in json.dumps(response)


@pytest.mark.parametrize("encode", [
    lambda value: json.dumps(json.dumps(value)),
    lambda value: json.dumps(json.dumps(json.dumps(value))),
    lambda value: json.dumps(value).replace('"', '\\"'),
], ids=["json-twice", "json-three-times", "escaped-json"])
def test_daemon_log_scrubs_json_encoded_more_than_once(tmp_path, encode):
    """Review r1 of #225, P2: `{\\"password\\": \\"…\\"}`, and JSON encoded twice, leaked."""
    text = "policy error: " + encode({"password": "SECRET7jsonkey9", "user": "fixture"})
    logged, response = _responder_log(tmp_path, host_push._workspace_refused(
        host_push.PushError(text, kind="config-header")))
    assert "policy error" in logged and "fixture" in logged
    assert "SECRET7jsonkey9" not in logged
    assert "SECRET7jsonkey9" not in json.dumps(response)


def _modes(text):
    """The three scrubbing modes: the whole text, a handoff excerpt, and daemon.log's line."""
    return {"whole": scrub_secrets(text, whole=True)[0], "excerpt": handoff.clean(text, 4000)[0],
            "daemon.log": refusals.scrubbed_words(ValueError(text))}


def test_a_repr_escaped_value_that_closes_keeps_the_rest_of_its_line():
    """Review r1 of #225, P3: a repr-escaped value closes at the next quote escaped by
    the same backslash run; it no longer takes the location after it."""
    text = "config error: password=\\'SECRET7x\\' at policy.json line 4; expected a string"
    for mode, scrubbed in _modes(text).items():
        assert "SECRET7x" not in scrubbed, mode
        assert "password=\\'[REDACTED]\\' at policy.json line 4; expected a string" in scrubbed, mode


def test_a_truly_unterminated_value_still_redacts_through_its_line():
    """Review r1 of #225's second location input has no closing quote at all, so where
    its value ends is unknown: C-23.14 keeps the value hidden, through the line, at the
    cost of the location after it on that line. A location on the next line survives."""
    for mode, scrubbed in _modes("invalid token='abc at /etc/subfleet/policy.json:12 (line 3)").items():
        assert scrubbed.endswith("invalid token=[REDACTED]"), mode
    for mode, scrubbed in _modes("bad value token='SECRET7y\nlocation: policy.json:4").items():
        assert "SECRET7y" not in scrubbed and "location: policy.json:4" in scrubbed, mode


CREDENTIAL_KEYS = st.sampled_from([
    "api_key", "api-key", "token", "secret", "password", "passwd", "authorization", "cookie",
    "credential", "credentials", "private_key", "signing-key", "secret_access_key", "access-key-id",
    "access_token", "refresh_token", "client-secret", "oauth_token", "auth-token", "SERVICE_PASSWORD",
])
KEY_CASES = {"as-written": lambda key: key, "lower": str.lower, "upper": str.upper, "title": str.title}
KEYS = st.tuples(
    st.one_of(CREDENTIAL_KEYS, st.tuples(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12),
        st.sampled_from(["_", "-"]), CREDENTIAL_KEYS).map(lambda parts: "".join(parts))),
    st.sampled_from(sorted(KEY_CASES))).map(lambda drawn: KEY_CASES[drawn[1]](drawn[0]))
#: How a key is quoted: bare, quoted, repr-quoted (`\'key\'`, and `\"key\"` as JSON
#: encoded twice writes it), and double-encoded (`\\\"key\\\"`, JSON encoded three times).
KEY_QUOTES = {"bare": "{}", "single": "'{}'", "double": '"{}"', "repr-single": "\\'{}\\'",
              "repr-double": '\\"{}\\"', "double-encoded": '\\\\\\"{}\\\\\\"'}
SEPARATORS = ("=", ": ", " = ")
VALUE_STYLES = {
    "none": "{}", "single": "'{}'", "double": '"{}"', "repr-single": "\\'{}\\'", "repr-double": '\\"{}\\"',
    "double-encoded": '\\\\\\"{}\\\\\\"', "unterminated-single": "'{}", "unterminated-double": '"{}',
    "repr-open": "\\'{}", "repr-unterminated": None,
}
SUFFIXES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=8, max_size=24)


def _assignment(key, key_quote, separator, style, secret):
    value = repr(f"'{secret}\n\"") if style == "repr-unterminated" else VALUE_STYLES[style].format(secret)
    return KEY_QUOTES[key_quote].format(key) + separator + value


def _assert_scrubbed(text, secrets):
    """No mode keeps any secret, and each mode's output is already scrubbed."""
    whole = scrub_secrets(text, whole=True)[0]
    excerpt = handoff.clean(text, 4000)[0]
    logged = refusals.scrubbed_line(text)
    for mode, scrubbed in (("whole", whole), ("excerpt", excerpt), ("daemon.log", logged),
                           ("daemon.log words", refusals.scrubbed_words(ValueError(text)))):
        assert not [secret for secret in secrets if secret in scrubbed], (mode, text, scrubbed)
    assert scrub_secrets(whole, whole=True)[0] == whole
    assert handoff.clean(excerpt, 4000)[0] == excerpt
    assert refusals.scrubbed_line(logged) == logged


def test_every_key_quoting_separator_case_and_value_style_is_scrubbed():
    """The cross product, exhaustively, for one key of each case."""
    for key_quote, separator, case, style in itertools.product(KEY_QUOTES, SEPARATORS, KEY_CASES, VALUE_STYLES):
        key = KEY_CASES[case]("service_password")
        secret = "SECRET7crossproduct9"
        _assert_scrubbed(f"config error: {_assignment(key, key_quote, separator, style, secret)} tail", [secret])


@pytest.mark.parametrize("style", sorted(VALUE_STYLES))
@pytest.mark.parametrize("key_quote", sorted(KEY_QUOTES))
@settings(max_examples=12, derandomize=True, deadline=None)
@given(key=KEYS, separator=st.sampled_from(SEPARATORS), suffix=SUFFIXES,
       leading=st.text(max_size=40), trailing=st.text(max_size=60))
def test_credential_assignment_scrubbing_never_keeps_the_value(key_quote, style, key, separator, suffix,
                                                               leading, trailing):
    """Any text around an assignment: whatever its key's quoting, separator and case,
    no mode keeps the value, and scrubbing again changes nothing."""
    secret = "SECRET7" + suffix
    _assert_scrubbed(f"{leading} {_assignment(key, key_quote, separator, style, secret)} {trailing}", [secret])


#: An opening quote, and its escaped forms, for two assignments of one kind on a line.
SAME_KIND_QUOTES = ("'", '"', "\\'", '\\"', '\\\\\\"')


@pytest.mark.parametrize("closes", ["neither", "second", "both"])
@pytest.mark.parametrize("quote", SAME_KIND_QUOTES)
@settings(max_examples=15, derandomize=True, deadline=None)
@given(keys=st.tuples(KEYS, KEYS), separator=st.sampled_from(SEPARATORS), suffixes=st.tuples(SUFFIXES, SUFFIXES),
       between=st.text(alphabet=" \t,;abc", max_size=6), leading=st.text(max_size=30),
       trailing=st.text(max_size=30))
def test_two_assignments_of_one_kind_on_a_line_both_lose_their_values(quote, closes, keys, separator, suffixes,
                                                                       between, leading, trailing):
    """Review r1 of #225, P2: when the first value has no closing quote, it pairs with
    the second's opening quote, and the second must not survive that in any mode."""
    first, second = ("SECRET7" + suffixes[0], "SECRET8" + suffixes[1])
    text = (f"{leading}\n{keys[0]}{separator}{quote}{first}{quote if closes == 'both' else ''}{between} "
            f"{keys[1]}{separator}{quote}{second}{quote if closes != 'neither' else ''} {trailing}")
    _assert_scrubbed(text, [first, second])


@settings(max_examples=200, derandomize=True, deadline=None)
@given(text=st.text(max_size=200))
def test_scrubbing_any_text_twice_changes_nothing_more(text):
    once = scrub_secrets(text, whole=True)[0]
    assert scrub_secrets(once, whole=True)[0] == once
    excerpt = handoff.clean(text, 4000)[0]
    assert handoff.clean(excerpt, 4000)[0] == excerpt
    logged = refusals.scrubbed_line(text)
    assert refusals.scrubbed_line(logged) == logged


def test_unterminated_value_stops_at_a_line_boundary():
    clean, _ = scrub_secrets("password='SECRET7left\nordinary diagnostic\n", whole=True)
    assert "SECRET7left" not in clean
    assert "ordinary diagnostic" in clean
