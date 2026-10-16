"""C-25.7/C-23.14: repr and incomplete assignments stay out of daemon.log."""

import configparser
import logging

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import host_push, refusals
from subfleet.sessions.handoff import scrub_secrets
from tests.unit.test_protocol_secrecy import wire_reply


def test_daemon_log_scrubs_config_errors_with_unterminated_repr_quotes(tmp_path):
    source = "password='SECRET7-left\n\""
    with pytest.raises(configparser.MissingSectionHeaderError) as caught:
        configparser.ConfigParser().read_string(source)
    error = host_push._workspace_refused(host_push.PushError(str(caught.value), kind="config-header"))
    # The real request responder writes its detail to the local log, never the wire.
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
    logged = (tmp_path / "daemon.log").read_text()
    assert "refused" in logged and "File contains no section headers" in logged
    assert "SECRET7-left" not in logged
    assert "SECRET7-left" not in str(response)


CREDENTIAL_KEYS = st.sampled_from([
    "api_key", "api-key", "token", "secret", "password", "passwd", "authorization", "cookie",
    "credential", "credentials", "private_key", "signing-key", "secret_access_key", "access-key-id",
    "access_token", "refresh_token", "client-secret", "oauth_token", "auth-token", "SERVICE_PASSWORD",
])
KEYS = st.one_of(CREDENTIAL_KEYS, st.tuples(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12),
    st.sampled_from(["_", "-"]), CREDENTIAL_KEYS).map(lambda parts: "".join(parts))).map(str.upper)


@pytest.mark.parametrize("style", ["none", "single", "double", "repr-single", "repr-double",
                                    "unterminated-single", "unterminated-double", "repr-unterminated"])
@settings(max_examples=40, derandomize=True, deadline=None)
@given(key=KEYS, suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=8, max_size=24),
       trailing=st.text(max_size=60))
def test_credential_assignment_scrubbing_never_keeps_the_value(style, key, suffix, trailing):
    secret = "SECRET7" + suffix
    value = {"none": secret, "single": f"'{secret}'", "double": f'"{secret}"',
             "repr-single": rf"\'{secret}\'", "repr-double": rf'\"{secret}\"',
             "unterminated-single": f"'{secret}", "unterminated-double": f'"{secret}',
             "repr-unterminated": repr(f"'{secret}\n\"")}[style]
    text = f"config error: {key}={value} {trailing}"
    assert secret not in scrub_secrets(text, whole=True)[0]
    assert secret not in refusals.scrubbed_words(ValueError(text))


def test_unterminated_value_stops_at_a_line_boundary():
    clean, _ = scrub_secrets("password='SECRET7left\nordinary diagnostic\n", whole=True)
    assert "SECRET7left" not in clean
    assert "ordinary diagnostic" in clean
