"""C-9.2, C-9.3, C-25.7: where the subscription refusal's words come from and go.

`tests/unit/test_claude_subscription_refusal.py` holds the recorded-stream tests and
the properties for quoting (1), inventing no refusal (7) and excerpts (8). This file
states the rest as properties over generated streams that mix Claude Code's own
frames, frames a model served, `api_retry` kinds, results and stderr:

2. Text only a model wrote never makes `enroll` or `classify` refuse, and is never
   quoted (it is marked `MODELSAID` on every line, so any quote of it shows).
3. Every `SubscriptionRefusal.line` is drawn from stderr plus `cli_texts()`.
4. A verified success is never `auth-dead` by the refusal.
5. What `protocol.refuse` sends for an enrolment refusal is one of the reason's
   template forms; it quotes a line only when the line is one Claude Code was
   recorded printing, and never Subfleet's old words or a generated line.
6. The template's fix is `claude.ORG_BLOCK_CAUSES`, and both equal a literal here.
7. (extended) Neither the words nor the kind, in any channel: no refusal.
9. Differential against release/217's predicate (b51f08f96): every run it called
   `auth-dead` still is, and every enrolment it refused with exit 5 on the CLI's
   words still exits 5.

`enroll` runs on a given turn (`_Turn`): no subprocess, no temporary directory and,
past the refusal and the `system/init` gate, no profile request (`_Passed`).
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon_log, protocol, refusals
from subfleet.adapters import claude
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import (
    AUTH_SIGNATURE_RE, CLI_TOO_OLD_RE, ORG_BLOCK_RE, ClaudeAdapter, _subscription_refusal,
)
from subfleet.adapters.claude_stream import ERROR_KINDS, parse_stream
from subfleet.contracts import Credential, OutcomeClass
from subfleet.sessions.handoff import scrub_secrets
from tests.conftest import NOW, exit_info, make_launch
from tests.unit.test_protocol_secrecy import wire_reply

QUIET = dict(deadline=None, suppress_health_check=[
    HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.filter_too_much])

# Written out here, not imported, so the module cannot change them unseen.
REFUSAL = (
    "Your organization has disabled Claude subscription access for Claude Code · "
    "Use an Anthropic API key instead, or ask your admin to enable access"
)
CAUSES = (
    "Either the account's subscription lapsed or was cancelled, or an org admin "
    "disabled Claude Code; once the account is subscribed with Claude Code allowed, "
    "run `subfleet lanes enroll` again."
)
BASE = "claude: Claude Code refused subscription access for this account"
CREDENTIAL_KINDS = ("authentication_failed", "oauth_org_not_allowed", "account_on_hold")
OTHER_KINDS = tuple(kind for kind in ERROR_KINDS if kind not in CREDENTIAL_KINDS)
OLD_WORDS = ("the organisation has disabled", "ask the account's admin", "enable claude code access")
PHRASES = (
    "organization has disabled Claude subscription access",
    "organization has disabled",
    "subscription access for Claude Code",
    "does not have access to Claude",
)
MODEL = "MODELSAID"
SENTINEL = "SENTINEL7-line"
CREDENTIAL = Credential(provider="claude", ref="claude-quota-max@policybench.org",
                        kind="keychain-token")
INIT = {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-haiku-4-5"}
ZERO = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0}


# --- streams ------------------------------------------------------------------

def _stream(*rows: dict) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def cli_frame(text: str, kind: str | None = None) -> dict:
    """Claude Code's own placeholder frame (`cli_placeholder`)."""
    row = {"type": "assistant", "session_id": "s", "is_api_error_message": True,
           "message": {"model": "<synthetic>", "role": "assistant", "type": "message",
                       "content": [{"type": "text", "text": text}], "usage": dict(ZERO)}}
    if kind:
        row["error"] = kind
    return row


def model_frame(text: str, kind: str | None = None) -> dict:
    """A frame a model served. A kind outside `AUTH_ERROR_KINDS` leaves it the model's."""
    row = {"type": "assistant", "session_id": "s",
           "message": {"model": "claude-opus-5-5", "role": "assistant", "type": "message",
                       "content": [{"type": "text", "text": text}],
                       "usage": {"input_tokens": 12, "output_tokens": 7}}}
    if kind:
        row["error"] = kind
    return row


def result(text: str = "", *, is_error: bool, errors: tuple[str, ...] = ()) -> dict:
    row = {"type": "result", "subtype": "error_during_execution" if errors else "success",
           "is_error": is_error, "session_id": "s", "result": text}
    if errors:
        row["errors"] = list(errors)
    return row


def retry(kind: str) -> dict:
    return {"type": "system", "subtype": "api_retry", "session_id": "s", "attempt": 1,
            "error": kind}


def marked(text: str) -> str:
    """Model prose, marked on every line so a quote of any of it shows."""
    return "\n".join(f"{MODEL} {line}" for line in text.split("\n"))


def _clean(text: str) -> str:
    # A file read back folds "\r" into a line break, and a lone surrogate cannot be
    # written as UTF-8: neither is a line Claude Code could print.
    return "".join(c for c in text if c != "\r" and not 0xD800 <= ord(c) <= 0xDFFF)


plain = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r"),
                max_size=80)
matching = st.from_regex(ORG_BLOCK_RE).map(_clean).filter(lambda text: ORG_BLOCK_RE.search(text))


@st.composite
def cli_line(draw, phrases: bool = True) -> str:
    """One or two lines, holding a refusal phrase in any case when `phrases`."""
    text = draw(plain)
    if phrases and draw(st.booleans()):
        phrase = draw(st.sampled_from(PHRASES))
        phrase = phrase.upper() if draw(st.booleans()) else phrase
        text = draw(plain) + phrase + text
    if draw(st.booleans()):
        text += "\n" + draw(plain)
    return text


@st.composite
def mixed_streams(draw, phrases: bool = True, oauth: bool = True, blank: bool = False):
    """(stdout, stderr): CLI frames, model frames, api_retry kinds, a result, stderr.

    Model frames always carry refusal phrases when `phrases` is false too: a model
    may quote them, and that must never matter. With `blank`, a result not marked
    `is_error` may have blank text, so an rc-0 run can be one that is not a verified
    success (no deliverable)."""
    cli_kinds = ERROR_KINDS if oauth else tuple(k for k in ERROR_KINDS if k != "oauth_org_not_allowed")
    rows = [INIT] if draw(st.booleans()) else []
    for _ in range(draw(st.integers(0, 4))):
        what = draw(st.sampled_from(("cli", "model", "retry")))
        if what == "cli":
            rows.append(cli_frame(draw(cli_line(phrases)), draw(st.sampled_from((None, *cli_kinds)))))
        elif what == "model":
            rows.append(model_frame(marked(draw(cli_line(True))), draw(st.sampled_from((None, *OTHER_KINDS)))))
        else:
            rows.append(retry(draw(st.sampled_from(cli_kinds))))
    if draw(st.booleans()):
        is_error = draw(st.booleans())
        if is_error:
            text = draw(cli_line(phrases))
        elif blank and draw(st.booleans()):
            text = draw(st.sampled_from(("", " ", "\n", " \t\n")))
        else:
            text = marked(draw(cli_line(True)))
        rows.append(result(text, is_error=is_error,
                           errors=tuple(draw(st.lists(cli_line(phrases), max_size=2)))))
    stderr = "\n".join(draw(st.lists(cli_line(phrases), max_size=3)))
    return _stream(*rows), stderr


def cli_corpus(stdout: str, stderr: str) -> str:
    """What the CLI and the provider wrote, built apart from the adapter (C-9.2)."""
    return "\n".join([stderr, *parse_stream(stdout).cli_texts()])


# --- the adapter, on a given turn ---------------------------------------------

class _Passed(Exception):
    """`enroll` got past the refusal and the `system/init` gate."""


class _Turn(ClaudeAdapter):
    def __init__(self, stdout: str, stderr: str = "", rc: int = 1):
        super().__init__(now=lambda: NOW, projects_dir=Path("/nonexistent"))
        self._turn = (rc, stdout, stderr)

    def credential_env(self, credential):
        return {}

    def _run_turn(self, env_add, model_id, timeout=0):
        return self._turn

    @staticmethod
    def account_from_reference(credential):
        raise _Passed()


def enroll(stdout: str, stderr: str = "", rc: int = 1) -> AdapterError | None:
    """The enrolment refusal for one turn's output, or None when there is none."""
    try:
        _Turn(stdout, stderr, rc).enroll(CREDENTIAL)
    except _Passed:
        return None
    except AdapterError as error:
        return error
    raise AssertionError("enroll finished without its account step")


def classify(attempt_dir: Path, stdout: str, stderr: str = "", rc: int = 1):
    attempt_dir.mkdir(exist_ok=True)
    (attempt_dir / "stream.jsonl").write_text(stdout, encoding="utf-8")
    (attempt_dir / "stderr").write_text(stderr, encoding="utf-8")
    adapter = ClaudeAdapter(now=lambda: NOW, projects_dir=attempt_dir)
    launch = make_launch(attempt_dir, session_id="s", model_id="claude-opus-5-5",
                         identity=None, label=None, projects_dir=attempt_dir)
    return adapter.classify(attempt_dir, launch, exit_info(rc))


def on_the_wire(error: AdapterError, log_name: str = "protocol-secrecy") -> tuple[dict, list[str]]:
    """The reply and daemon.log lines for an enrolment refusal: rewrapped as
    `Daemon._enroll_lane_locked` does, then answered by the daemon's own `_answer`."""
    wrapped = protocol.ProtocolError(str(error), error.code, error.fix, reason=error.reason,
                                     details=error.details)
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log, handler = logging.getLogger(log_name), Capture()
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        return wire_reply(op="submit", error=wrapped), lines
    finally:
        log.removeHandler(handler)


WIRE = re.compile(re.escape(BASE) + r'(?:: "(?P<line>.*)")?(?: \(error kind (?P<kind>[a-z_]+)\))?', re.S)


def written_to_daemon_log(error: AdapterError) -> tuple[dict, str]:
    """`on_the_wire`, with daemon.log as the daemon's own handler writes it
    (`daemon_log.ScrubbingStreamHandler`, which scrubs every line again)."""
    stream = io.StringIO()
    handler = daemon_log.ScrubbingStreamHandler(stream)
    log = logging.getLogger("protocol-secrecy")
    log.addHandler(handler)
    try:
        reply, _ = on_the_wire(error)
    finally:
        log.removeHandler(handler)
    return reply, stream.getvalue()


# --- 2. no model prose ----------------------------------------------------------

def test_a_model_quoting_the_refusal_does_not_refuse_enrolment():
    """Mutant: `enroll` reads `summary.texts()` again."""
    stdout = _stream(INIT, model_frame(f"{MODEL} {REFUSAL}"), result(f"{MODEL} {REFUSAL}", is_error=False))
    assert enroll(stdout, rc=0) is None


@settings(max_examples=60, **QUIET)
@given(prose=matching, kind=st.sampled_from((None, *OTHER_KINDS)), in_result=st.booleans(),
       init=st.booleans())
def test_model_prose_never_refuses_and_is_never_quoted(prose, kind, in_result, init, tmp_path):
    said = marked(prose)
    rows = ([INIT] if init else []) + [model_frame(said, kind)]
    if in_result:
        rows.append(result(said, is_error=False))
    stdout, rc = _stream(*rows), 0 if in_result else 1
    error = enroll(stdout, rc=rc)
    if init:
        assert error is None
    else:
        assert error.reason == "claude-authenticate" and MODEL not in str(error)
    outcome = classify(tmp_path / "a1", stdout, rc=rc)
    assert outcome.cls is not OutcomeClass.AUTH_DEAD
    assert "refusal" not in outcome.evidence and MODEL not in outcome.detail


# --- 3. the CLI's words only ----------------------------------------------------

#: A model frame's words, stamped `max_output_tokens`, beside a kind-only refusal: the
#: PR's `_provider_error_texts` quoted them (Step 0 item 4).
STAMPED_PROSE = (_stream(INIT, model_frame(f"{MODEL} the organization has disabled telemetry here",
                                           "max_output_tokens"),
                         retry("oauth_org_not_allowed")), "")


def test_a_model_frame_with_an_error_kind_is_not_the_provider_s_error_text(tmp_path):
    """Mutant: `_provider_error_texts` admits every frame with an `error`."""
    stdout, stderr = STAMPED_PROSE
    error = enroll(stdout, stderr)
    assert error.reason == "claude-subscription-refused" and MODEL not in str(error)
    assert "no words: the frame carrying oauth_org_not_allowed has none" in str(error)
    outcome = classify(tmp_path / "a1", stdout, stderr)
    assert outcome.cls is OutcomeClass.AUTH_DEAD and MODEL not in outcome.detail
    assert outcome.evidence["refusal"]["verbatim"] is None


@settings(max_examples=150, **QUIET)
@given(case=mixed_streams())
@example(case=STAMPED_PROSE)
@example(case=(_stream(model_frame(f"{MODEL} organization has disabled it", "unknown")),
               "organization has disabled Claude subscription access for Claude Code"))
def test_every_quoted_line_is_the_cli_s(case, tmp_path):
    stdout, stderr = case
    corpus = cli_corpus(stdout, stderr)
    refusal = _subscription_refusal(corpus, parse_stream(stdout))
    if refusal is not None and refusal.line is not None:
        # Quoted from its source scrubbed whole (review r1, P1): stderr and the CLI's
        # texts, as one corpus or each alone.
        body = refusal.line.removeprefix("…").removesuffix("…")
        sources = (corpus, *parse_stream(stdout).cli_texts())
        assert any(body in scrub_secrets(text, whole=True)[0] for text in sources)
        assert MODEL not in refusal.line
    error = enroll(stdout, stderr)
    if error is not None and error.reason == "claude-subscription-refused":
        assert MODEL not in str(error)
    outcome = classify(tmp_path / "a1", stdout, stderr)
    if "refusal" in outcome.evidence:
        assert MODEL not in outcome.detail


# --- 4. success supersedes ------------------------------------------------------

@settings(max_examples=80, **QUIET)
@given(channels=st.lists(st.sampled_from(("stderr", "frame", "retry", "errors")), min_size=1, unique=True),
       words=st.one_of(st.just(REFUSAL), matching), answer=plain)
def test_a_verified_success_is_never_refused(channels, words, answer, tmp_path):
    """Mutant: drop the `successful` gate on the refusal in `classify`."""
    rows, stderr = [INIT], ""
    if "stderr" in channels:
        stderr = words
    if "frame" in channels:
        rows.append(cli_frame(words, "oauth_org_not_allowed"))
    if "retry" in channels:
        rows.append(retry("oauth_org_not_allowed"))
    row = result(f"Done. {answer}", is_error=False)
    if "errors" in channels:
        row["errors"] = [words]
    rows.append(row)
    outcome = classify(tmp_path / "a1", _stream(*rows), stderr, rc=0)
    assert outcome.cls is not OutcomeClass.AUTH_DEAD
    assert "refusal" not in outcome.evidence


@pytest.mark.parametrize("carrier", ("stderr", "retry"))
def test_enroll_verified_success_contract(carrier, tmp_path):
    """C-9.3 as amended (review r1, P2): only classification excludes a verified
    success. The same rc-0 stream, with a result not marked `is_error` and a final
    text that is not blank, classifies `ok`, and enrolment still refuses it, exit 5."""
    rows, stderr = [INIT], ""
    if carrier == "stderr":
        stderr = REFUSAL
    else:
        rows.append(retry("oauth_org_not_allowed"))
    stdout = _stream(*rows, result("Done.", is_error=False))
    assert classify(tmp_path / "a1", stdout, stderr, rc=0).cls is OutcomeClass.OK
    error = enroll(stdout, stderr, rc=0)
    assert error.reason == "claude-subscription-refused" and error.code == 5


# --- 5. the template only, on the wire -------------------------------------------

def test_the_recorded_refusal_reaches_the_wire_whole_and_daemon_log_has_every_word():
    """Step 0 item 3: the client reads the template; daemon.log, the full statement."""
    error = enroll(_stream(INIT, cli_frame(REFUSAL, "oauth_org_not_allowed"),
                           result(REFUSAL, is_error=True)))
    reply, logged = on_the_wire(error)
    assert reply["error"]["message"] == f'{BASE}: "{REFUSAL}" (error kind oauth_org_not_allowed)'
    assert reply["error"]["fix"] == CAUSES and reply["error"]["code"] == 5
    assert not reply["error"].get("reason") and not reply["error"].get("details")
    refused = [line for line in logged if "refused (ProtocolError)" in line]
    assert len(refused) == 1 and f'verbatim: "{REFUSAL}"' in refused[0]
    assert "error_kinds: oauth_org_not_allowed" in refused[0] and f"fix: {CAUSES}" in refused[0]


def test_a_reworded_refusal_is_not_quoted_on_the_wire_but_is_in_daemon_log():
    reworded = "Claude subscriptions are not available for this account"
    error = enroll(_stream(INIT, cli_frame(reworded, "oauth_org_not_allowed")))
    reply, logged = on_the_wire(error)
    assert reply["error"]["message"] == f"{BASE} (error kind oauth_org_not_allowed)"
    assert reworded not in json.dumps(reply)
    assert any(f'verbatim: "{reworded}"' in line for line in logged)


def test_a_wordless_stamp_quotes_a_reworded_error_result():
    """Review r1's surviving mutant: `_provider_error_texts` without an error
    result's text. A stamped frame with no words quotes that text's first line."""
    reworded = "Subscription access is unavailable for this account"
    stdout = _stream(INIT, cli_frame("", "oauth_org_not_allowed"), result(reworded, is_error=True))
    refusal = _subscription_refusal(cli_corpus(stdout, ""), parse_stream(stdout))
    assert (refusal.line, refusal.source, refusal.quoted_from) == (
        reworded, "error-kind", "provider-error")


# --- 5b. daemon.log: excerpts come from text scrubbed whole (#104 review r1, P1) --

SECRET = "SYNTHETICSECRET7"


def _never_logged(stderr: str, tmp_path: Path) -> str:
    """Enrolment refuses with the template's least specific form, as it did before
    the fix, and no part of SECRET reaches daemon.log, the exception or the
    `auth-dead` record. Returns daemon.log's text."""
    error = enroll(_stream(INIT), stderr)
    assert error.reason == "claude-subscription-refused" and error.code == 5
    reply, logged = written_to_daemon_log(error)
    assert reply["error"]["message"] == BASE and reply["error"]["fix"] == CAUSES
    assert "refused (ProtocolError)" in logged
    outcome = classify(tmp_path / "a1", _stream(INIT), stderr)
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    for text in (logged, str(error), json.dumps(reply), outcome.detail, json.dumps(outcome.evidence)):
        assert SECRET[:4] not in text
    return logged


def test_log_scrubs_a_password_whose_label_is_outside_excerpt(tmp_path):
    """Review r1's case: the label lies 600 characters ahead of the words that
    matched, outside any excerpt of them. The source is scrubbed whole before the
    excerpt is taken, so the label still masks the value, and what matched lay
    inside it: the statement quotes nothing and says why."""
    stderr = 'password="' + "a" * 600 + "organization has disabled " + SECRET + "b" * 600 + '"'
    logged = _never_logged(stderr, tmp_path)
    assert f"no words: {claude.SCRUBBED_AWAY}" in logged


def test_scrubbed_never_sends_over_budget_text_to_whole_mode(monkeypatch):
    """C-23.14: count the input to the scrub, without a wall-clock assertion."""
    from subfleet.sessions import handoff

    calls = []

    def scrub(text, *, whole=False):
        calls.append((len(text), whole))
        return "clean", 0

    monkeypatch.setattr(handoff, "scrub_secrets", scrub)
    for size in (4096, handoff.MAX_SCRUB_CHARS + 1, 1_048_576):
        assert claude._scrubbed("x" * size) == "clean"
        length, whole = calls[-1]
        assert not whole or length <= handoff.MAX_SCRUB_CHARS
    assert len(calls) == 3
    assert calls[0] == (4096, True)


def test_a_match_lost_to_the_scrub_budget_is_not_quoted():
    """A raw match still refuses, but a discarded middle supplies no quote."""
    from subfleet.sessions.handoff import MAX_SCRUB_CHARS

    padding = "ordinary diagnostic\n" * (MAX_SCRUB_CHARS // 20)
    text = padding + "organization has disabled\n" + padding
    match = ORG_BLOCK_RE.search(text)
    assert match is not None and len(text) > MAX_SCRUB_CHARS
    assert claude._quoted(text, match) == claude.SCRUBBED_AWAY
    assert claude._first_auth_phrase(text) is None
    refusal = _subscription_refusal(text, parse_stream(""))
    assert refusal is not None and refusal.source == "text" and refusal.line is None
    assert claude.SCRUBBED_AWAY in refusal.statement()


@pytest.mark.parametrize("where", ("frame", "provider-error"))
def test_a_discarded_refusal_never_quotes_the_cut_marker(where):
    """The safe cut's synthetic omission marker is never Claude's evidence."""
    words = "reworded diagnostic " * 1000
    stdout = _stream(cli_frame(words if where == "frame" else "", "oauth_org_not_allowed"),
                     result(words if where == "provider-error" else "", is_error=True))
    refusal = _subscription_refusal(cli_corpus(stdout, ""), parse_stream(stdout))
    assert refusal is not None and refusal.source == "error-kind"
    assert refusal.line is None
    assert "characters omitted" not in refusal.statement()


@pytest.mark.parametrize(("phrase", "expected"), (
    pytest.param("Update Claude Code to use this model", OutcomeClass.CLI_TOO_OLD,
                 id="cli-too-old"),
    pytest.param("HTTP 401 Unauthorized", OutcomeClass.AUTH_DEAD, id="no-init-401"),
    pytest.param("out of usage credits", OutcomeClass.LIMITED, id="credits"),
    pytest.param("hit your limit", OutcomeClass.LIMITED, id="limit"),
    pytest.param("service unavailable", OutcomeClass.TRANSIENT, id="transient"),
))
def test_classify_quote_scrubs_a_password_before_excerpt(phrase, expected, tmp_path):
    """Review r2 F3: each quote site must scrub a label 600 characters ahead."""
    stderr = 'password="' + "a" * 600 + phrase + " " + SECRET + "b" * 600 + '"'
    outcome = classify(tmp_path / "attempt", "", stderr)
    assert outcome.cls is expected
    assert claude.SCRUBBED_AWAY in outcome.detail
    assert SECRET[:4] not in outcome.detail
    assert SECRET[:4] not in json.dumps(outcome.evidence)


@pytest.mark.parametrize("words", (48, 51, 52, 54, 120))
def test_log_scrubs_a_password_whose_label_follows_the_marker(words, tmp_path):
    """The label after the words that matched: the excerpt's cut at 300 characters
    falls before the label (120), inside it (54) or inside the value (51, 52), or
    the line is short enough to be quoted whole (48); no part of the value is logged."""
    stderr = "organization has disabled " + "word " * words + 'password="' + SECRET + '"'
    logged = _never_logged(stderr, tmp_path)
    assert 'verbatim: "organization has disabled word word' in logged


def test_the_recorded_line_reaches_the_wire_whole_beside_a_credential():
    """The client reply is unchanged: the scrub leaves the recorded line whole, so
    it still passes its closed check when a credential shares the stream."""
    for line in claude.SUBSCRIPTION_REFUSAL_LINES:
        assert scrub_secrets(line, whole=True)[0] == line
    for stdout, stderr in ((_stream(INIT, cli_frame(REFUSAL, "oauth_org_not_allowed")),
                            f'password="{SECRET}"'),
                           (_stream(INIT), f'password="{SECRET}"\n{REFUSAL}')):
        error = enroll(stdout, stderr)
        reply, logged = written_to_daemon_log(error)
        assert reply["error"]["message"].startswith(f'{BASE}: "{REFUSAL}"')
        assert f'verbatim: "{REFUSAL}"' in logged and SECRET not in logged


lines_to_quote = st.one_of(
    st.just(REFUSAL), st.just(SENTINEL), st.just(f"Error: {REFUSAL}"), st.just(f"{REFUSAL} {SENTINEL}"),
    st.just(f"{SENTINEL}\n{REFUSAL}"), st.just("the organisation has disabled Claude Code subscription access"),
    st.just("ask the account's admin to enable Claude Code access"), st.just(""),
    plain.map(lambda text: f"{SENTINEL} {text}"), matching.map(lambda text: f"{SENTINEL} {text}"))


@settings(max_examples=150, **QUIET)
@given(line=lines_to_quote, where=st.sampled_from(("frame", "stderr", "errors")),
       kinds=st.lists(st.sampled_from((*ERROR_KINDS, "not-a-kind")), max_size=3))
@example(line=SENTINEL, where="frame", kinds=[])
@example(line=REFUSAL, where="stderr", kinds=[])
@example(line=f"{SENTINEL} organization has disabled", where="stderr", kinds=["authentication_failed"])
def test_the_wire_reads_one_of_the_template_s_forms(line, where, kinds):
    """Mutants: the old template text or fix; `reason=` dropped at the raise; the line
    rendered without its closed check."""
    rows, stderr = [INIT], ""
    if where == "frame":
        rows.append(cli_frame(line, "oauth_org_not_allowed"))
    elif where == "stderr":
        stderr = line
    else:
        rows.append(result(is_error=True, errors=(line,)))
    rows += [retry(kind) for kind in kinds]
    stdout = _stream(*rows)
    summary = parse_stream(stdout)
    refusal = _subscription_refusal(cli_corpus(stdout, stderr), summary)
    error = enroll(stdout, stderr)
    if refusal is None:
        assert error is None
        return
    reply, _ = on_the_wire(error)
    message, fix = reply["error"]["message"], reply["error"]["fix"]
    shown = WIRE.fullmatch(message)
    assert shown is not None, message
    assert fix == CAUSES and reply["error"]["code"] == 5
    assert (shown["line"] is not None) == (refusal.line == REFUSAL)
    assert shown["line"] in (None, REFUSAL)
    present = [kind for kind in summary.error_kinds if kind in CREDENTIAL_KINDS]
    assert (shown["kind"] is not None) == bool(present)
    if "oauth_org_not_allowed" in present:
        assert shown["kind"] == "oauth_org_not_allowed"
    assert SENTINEL not in json.dumps(reply) and "not-a-kind" not in message
    for words in OLD_WORDS:
        assert words not in message.lower() and words not in fix.lower()


# --- 6. no cause asserted, no outcome promised ----------------------------------

def test_the_template_s_fix_is_the_adapter_s_cause_line():
    """Differential: every form's fix is `claude.ORG_BLOCK_CAUSES`, and both are the
    literal; the least specific form names nothing."""
    reason = "claude-subscription-refused"
    forms = (refusals.HANDLER_MESSAGES[reason], *refusals.LESS_SPECIFIC[reason])
    assert refusals.CLAUDE_REFUSAL_CAUSES == claude.ORG_BLOCK_CAUSES == CAUSES
    assert {fix for _, fix in forms} == {claude.ORG_BLOCK_CAUSES}
    assert forms[-1] == (BASE, CAUSES)
    assert "claude-org-disabled" not in refusals.HANDLER_MESSAGES
    for line in (REFUSAL, None):
        for kind in (*CREDENTIAL_KINDS, None):
            assert refusals.render(reason, {"claude_refusal": line, "error_kind": kind})[1] == CAUSES


def test_the_closed_values_are_the_recorded_line_and_the_credential_kinds():
    assert claude.SUBSCRIPTION_REFUSAL_LINES == (REFUSAL,)
    assert refusals._choices()["claude_refusal"] == (REFUSAL,)
    assert refusals._choices()["error_kind"] == CREDENTIAL_KINDS


# --- 7. no refusal invented (extends the PR's corpus-only property) -------------

@settings(max_examples=100, **QUIET)
@given(case=mixed_streams(phrases=False, oauth=False))
def test_no_refusal_is_invented_from_any_channel(case):
    stdout, stderr = case
    corpus = cli_corpus(stdout, stderr)
    if ORG_BLOCK_RE.search(corpus):
        return                              # a generated line can spell a phrase
    assert _subscription_refusal(corpus, parse_stream(stdout)) is None
    error = enroll(stdout, stderr)
    assert error is None or error.reason != "claude-subscription-refused"


# --- 9. the class is unchanged ----------------------------------------------------

def _release_217_auth_dead(stdout: str, stderr: str, rc: int) -> bool:
    """b51f08f96's `classify` up to its `auth-dead` returns, written out whole. A
    verified success (`successful`: rc 0, a result not marked `is_error`, and a final
    text, the result's or else the last assistant frame's, that is not blank) has no
    text failure. Otherwise the CLI version gate comes first; then ORG_BLOCK_RE on
    the CLI corpus, a credential kind, or, in a run that never reached
    `system/init`, AUTH_SIGNATURE_RE on the CLI corpus is `auth-dead`."""
    summary = parse_stream(stdout)
    corpus = cli_corpus(stdout, stderr)
    result_ = summary.result
    final = (result_.text if result_ is not None and result_.text
             else next((m.text for m in reversed(summary.assistants) if m.text), ""))
    successful = rc == 0 and result_ is not None and not result_.is_error and bool(final.strip())
    if successful or CLI_TOO_OLD_RE.search(corpus):
        return False
    return (ORG_BLOCK_RE.search(corpus) is not None
            or any(kind in CREDENTIAL_KINDS for kind in summary.error_kinds)
            or (not summary.has_init and AUTH_SIGNATURE_RE.search(corpus) is not None))


@settings(max_examples=150, **QUIET)
@given(case=mixed_streams(blank=True), rc=st.sampled_from((1, 0)))
@example(case=(_stream(INIT, retry("authentication_failed")), ""), rc=1)
@example(case=(_stream(cli_frame(REFUSAL)), ""), rc=0)
# rc 0 and a result not marked is_error, but blank: no deliverable, so no success.
@example(case=(_stream(INIT, result("", is_error=False)), REFUSAL), rc=0)
@example(case=(_stream(INIT, result(" \n", is_error=False, errors=(REFUSAL,))), ""), rc=0)
@example(case=(_stream(INIT, cli_frame(REFUSAL), result("", is_error=False)), ""), rc=0)
@example(case=(_stream(result("", is_error=False)), "HTTP 401 Unauthorized"), rc=0)
# rc 0 and a nonblank result not marked is_error: a verified success.
@example(case=(_stream(INIT, retry("oauth_org_not_allowed"), result("Done.", is_error=False)),
               REFUSAL), rc=0)
@example(case=(_stream(result("Done.", is_error=False)), "HTTP 401 Unauthorized"), rc=0)
# Empty streams, and the no-init signature branch.
@example(case=("", ""), rc=0)
@example(case=("", "HTTP 401 Unauthorized"), rc=1)
def test_what_release_217_called_auth_dead_still_is(case, rc, tmp_path):
    """Differential against b51f08f96's predicate (`_release_217_auth_dead`), over
    blank and nonblank rc-0 results too: classification is `auth-dead` exactly when
    it was. At enrolment, no `system/init`, or ORG_BLOCK_RE on the CLI's words, was
    exit 5, and still is. Mutant: gate the refusal on `non_error_result` instead of
    `successful` (a blank rc-0 result then hides the refusal)."""
    stdout, stderr = case
    summary = parse_stream(stdout)
    corpus = cli_corpus(stdout, stderr)
    outcome = classify(tmp_path / "a1", stdout, stderr, rc)
    assert (outcome.cls is OutcomeClass.AUTH_DEAD) == _release_217_auth_dead(stdout, stderr, rc), (
        outcome.cls, outcome.detail)
    if not summary.has_init or ORG_BLOCK_RE.search(corpus):
        error = enroll(stdout, stderr, rc)
        assert error is not None and error.code == 5
