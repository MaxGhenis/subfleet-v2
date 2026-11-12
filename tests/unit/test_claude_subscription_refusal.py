"""C-9.3, C-10.2: Claude Code refusing subscription access is quoted, never explained.

On 2026-10-01 `subfleet lanes enroll` refused five accounts with Subfleet's own
sentence, "the organisation has disabled Claude Code subscription access", and the
fix "ask the account's admin to enable Claude Code access". Their subscriptions had
been cancelled and had expired. The refusal's recorded text names an organisation
setting all the same. So enrolment and every auth-dead outcome carry Claude Code's
refusal line verbatim with the stream's error kinds, and name both known causes
without choosing one.

The recorded stream is `tests/fixtures/claude/org-block-recorded/` (a real attempt
on 2026-09-30, Claude Code 2.1.284).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import (
    LINE_EXCERPT_MAX, ORG_BLOCK_CAUSES, ORG_BLOCK_RE, AUTH_SIGNATURE_RE, LIMIT_RE,
    ClaudeAdapter, _first_line_containing, _subscription_refusal,
)
from subfleet.adapters.claude_stream import parse_stream
from subfleet.contracts import Credential, OutcomeClass
from tests.conftest import (
    FIXTURES, NOW, exit_info, load_expected, make_lane, make_launch, stage_case,
)

QUIET = dict(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

RECORDED = "org-block-recorded"
REFUSAL = (
    "Your organization has disabled Claude subscription access for Claude Code · "
    "Use an Anthropic API key instead, or ask your admin to enable access"
)
CREDENTIAL = Credential(provider="claude", ref="claude-quota-max@policybench.org",
                        kind="keychain-token")

#: Subfleet's own words from before this fix: each asserted one cause.
OLD_ASSERTIONS = (
    "the organisation has disabled",
    "ask the account's admin",
    "enable claude code access",
)


class _Runner:
    """A `subprocess.run` stand-in: the keychain answers a token, the turn answers
    the given stream."""

    def __init__(self, *, stdout="", stderr="", rc=1):
        self.stdout, self.stderr, self.rc = stdout, stderr, rc

    def __call__(self, argv, **kwargs):
        if argv[0].endswith("security") or argv[1:2] == ["get"]:
            return subprocess.CompletedProcess(argv, 0, "sk-ant-oat01-REDACTED", "")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def _recorded_stdout() -> str:
    return (FIXTURES / RECORDED / "stdout").read_text(encoding="utf-8")


def _enroll_error(stdout: str, stderr: str = "", rc: int = 1, tmp: Path | None = None,
                  ) -> AdapterError:
    adapter = ClaudeAdapter(runner=_Runner(stdout=stdout, stderr=stderr, rc=rc),
                            now=lambda: NOW, projects_dir=tmp or Path("/nonexistent"))
    with pytest.raises(AdapterError) as caught:
        adapter.enroll(CREDENTIAL)
    return caught.value


def _ours(text: str, quoted: str | None) -> str:
    """Everything Subfleet wrote, with Claude Code's quoted words taken out."""
    return (text.replace(quoted, "") if quoted else text).lower()


def _stream(*rows: dict) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


INIT = {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-haiku-4-5"}


# --- enrolment (C-10.2) ------------------------------------------------------


def test_enroll_quotes_the_recorded_refusal_and_its_error_kind(tmp_path):
    """The recorded stream's refusal reaches the AdapterError verbatim, with the
    stream's error kind, and the fix names both causes without choosing."""
    error = _enroll_error(_recorded_stdout(), tmp=tmp_path)
    message = str(error)
    assert error.code == 5
    assert f'verbatim: "{REFUSAL}"' in message
    assert "error_kinds: oauth_org_not_allowed" in message
    assert error.fix == ORG_BLOCK_CAUSES
    assert "subscription lapsed or was cancelled" in error.fix
    assert "an org admin disabled Claude Code" in error.fix
    assert "`subfleet lanes enroll` succeeds" in error.fix
    for claim in OLD_ASSERTIONS:
        assert claim not in _ours(message, REFUSAL)
        assert claim not in _ours(error.fix, None)


def test_enroll_quotes_the_refusal_without_system_init(tmp_path):
    """C-9.3 the refusal is auth-dead with or without `system/init`: a missing init
    no longer turns it into 'did not authenticate' with a setup-token fix."""
    stdout = _stream({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "session_id": "s", "errors": [REFUSAL]})
    error = _enroll_error(stdout, tmp=tmp_path)
    assert error.code == 5
    assert f'verbatim: "{REFUSAL}"' in str(error)
    assert "did not authenticate" not in str(error)
    assert error.fix == ORG_BLOCK_CAUSES


def test_enroll_quotes_the_refusal_from_stderr(tmp_path):
    """Claude Code's words count wherever it printed them, stderr included."""
    stdout = _stream(INIT, {"type": "result", "subtype": "success", "is_error": False,
                            "result": "ok", "session_id": "s"})
    error = _enroll_error(stdout, stderr=f"Error: {REFUSAL}\n", rc=1, tmp=tmp_path)
    assert f'verbatim: "Error: {REFUSAL}"' in str(error)
    assert "error_kinds" not in str(error)      # none in the stream, none claimed


def test_enroll_quotes_a_reworded_refusal_carried_only_by_its_error_kind(tmp_path):
    """A refusal whose words ORG_BLOCK_RE does not know is still refused at
    enrolment when Claude Code stamps `oauth_org_not_allowed` on it, and its own
    first line is what gets quoted."""
    reworded = "Claude subscriptions are not available for this account"
    assert not ORG_BLOCK_RE.search(reworded)
    stdout = _stream(INIT, {
        "type": "assistant", "session_id": "s", "error": "oauth_org_not_allowed",
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": f"\n  {reworded}\nsecond line"}]},
    })
    error = _enroll_error(stdout, tmp=tmp_path)
    assert error.code == 5
    assert f'verbatim: "{reworded}"' in str(error)
    assert "second line" not in str(error)
    assert "error_kinds: oauth_org_not_allowed" in str(error)
    assert error.fix == ORG_BLOCK_CAUSES


def test_enroll_says_so_when_the_error_kind_came_without_words(tmp_path):
    stdout = _stream(INIT, {"type": "assistant", "session_id": "s",
                            "error": "oauth_org_not_allowed",
                            "message": {"model": "<synthetic>", "content": []}})
    error = _enroll_error(stdout, tmp=tmp_path)
    assert "no refusal text in the stream; error_kinds: oauth_org_not_allowed" in str(error)


def test_enroll_lists_every_distinct_error_kind_once(tmp_path):
    stdout = _stream(
        INIT,
        {"type": "system", "subtype": "api_retry", "session_id": "s", "attempt": 1,
         "error": "server_error"},
        {"type": "assistant", "session_id": "s", "error": "oauth_org_not_allowed",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": REFUSAL}]}},
        {"type": "assistant", "session_id": "s", "error": "oauth_org_not_allowed",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": REFUSAL}]}},
    )
    error = _enroll_error(stdout, tmp=tmp_path)
    assert "error_kinds: oauth_org_not_allowed, server_error" in str(error)


# --- the turn classifier (C-9.3): runs, probes, heals --------------------------


def _classify_recorded(adapter, tmp_path):
    expected = load_expected(RECORDED)
    attempt_dir, rc = stage_case(RECORDED, tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id=expected["session_id"],
                         model_id=expected["requested_model"],
                         projects_dir=adapter._config_projects_dir())
    return adapter.classify(attempt_dir, launch, exit_info(rc))


def test_classify_quotes_the_recorded_refusal_with_its_error_kind(adapter, tmp_path):
    outcome = _classify_recorded(adapter, tmp_path)
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert outcome.detail.startswith("auth-dead: ")
    assert f'verbatim: "{REFUSAL}"' in outcome.detail
    assert "error_kinds: oauth_org_not_allowed" in outcome.detail
    assert outcome.detail.endswith(ORG_BLOCK_CAUSES)
    assert outcome.evidence["refusal"] == {"verbatim": REFUSAL, "source": "text"}
    assert outcome.evidence["error_kinds"] == ["oauth_org_not_allowed"]
    assert outcome.evidence["answered"]["auth"] == "subscription access refused (text)"
    for claim in OLD_ASSERTIONS:
        assert claim not in _ours(outcome.detail, REFUSAL)


def test_the_refusal_detail_never_reads_as_a_revoked_token(adapter, tmp_path):
    """`Timers` treats a heal turn whose detail says 'revoked' as a revoked token;
    the words Subfleet adds to a refusal must not trip that."""
    outcome = _classify_recorded(adapter, tmp_path)
    assert "revoked" not in outcome.detail.lower()
    assert "revoked" not in ORG_BLOCK_CAUSES.lower()


def test_classify_quotes_a_reworded_refusal_carried_only_by_its_error_kind(adapter, tmp_path):
    """Before, the kind alone gave 'the provider reported error oauth_org_not_allowed'
    and dropped what Claude Code said."""
    reworded = "Claude subscriptions are not available for this account"
    attempt_dir = tmp_path / "a1"
    attempt_dir.mkdir()
    stream = _stream(INIT, {
        "type": "assistant", "session_id": "s", "error": "oauth_org_not_allowed",
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": reworded}]},
    })
    (attempt_dir / "stream.jsonl").write_text(stream, encoding="utf-8")
    (attempt_dir / "stdout").write_text(stream, encoding="utf-8")
    (attempt_dir / "stderr").write_text("", encoding="utf-8")
    launch = make_launch(attempt_dir, session_id="s", model_id="claude-opus-5-5",
                         projects_dir=adapter._config_projects_dir())
    outcome = adapter.classify(attempt_dir, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert f'verbatim: "{reworded}"' in outcome.detail
    assert "error_kinds: oauth_org_not_allowed" in outcome.detail
    assert outcome.evidence["refusal"] == {"verbatim": reworded, "source": "error-kind"}


def test_other_auth_error_kinds_keep_their_own_detail(adapter, tmp_path):
    """Only the subscription refusal names its causes; `authentication_failed` is a
    different fault and keeps the plain provider-error detail."""
    attempt_dir = tmp_path / "a1"
    attempt_dir.mkdir()
    stream = _stream(INIT, {
        "type": "assistant", "session_id": "s", "error": "authentication_failed",
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": "Invalid bearer token"}]},
    })
    (attempt_dir / "stream.jsonl").write_text(stream, encoding="utf-8")
    (attempt_dir / "stderr").write_text("", encoding="utf-8")
    launch = make_launch(attempt_dir, session_id="s", model_id="claude-opus-5-5",
                         projects_dir=adapter._config_projects_dir())
    outcome = adapter.classify(attempt_dir, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert outcome.detail == "auth-dead: the provider reported error authentication_failed"
    assert ORG_BLOCK_CAUSES not in outcome.detail


def test_probe_outcome_quotes_the_recorded_refusal(tmp_path):
    """C-11.4 a probe turn is classified by the same `classify`, so a probe that
    meets the refusal records the same verbatim detail as a run."""
    adapter = ClaudeAdapter(runner=_Runner(stdout=_recorded_stdout(), rc=1),
                            now=lambda: NOW, projects_dir=tmp_path)
    outcome = adapter.probe_outcome(make_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "x"},
                                    "claude-opus-5-5")
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert f'verbatim: "{REFUSAL}"' in outcome.detail
    assert "error_kinds: oauth_org_not_allowed" in outcome.detail
    assert outcome.detail.endswith(ORG_BLOCK_CAUSES)


# --- properties ---------------------------------------------------------------

#: Every alternative ORG_BLOCK_RE knows, written as Claude Code would.
PHRASES = (
    "organization has disabled Claude subscription access",
    "organization has disabled",
    "subscription access for Claude Code",
    "does not have access to Claude",
)

# Fragments of a line: anything but a newline (the corpus's line separator) and
# lone surrogates (which cannot be written to a file as UTF-8).
fragments = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n"),
    max_size=400,
)


def _any_case(draw, phrase: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(phrase), max_size=len(phrase)))
    return "".join(c.swapcase() if flip else c for c, flip in zip(phrase, flips))


def _around(draw, phrase: str) -> str:
    """Arbitrary lines around one line that holds `phrase`, anywhere in a line of
    any length."""
    # Padding makes long lines, with the phrase early, late or in the middle,
    # common rather than rare: they are where an excerpt can lose the match.
    pads = st.one_of(st.just(0), st.integers(0, 2 * LINE_EXCERPT_MAX))
    lead, trail = "-" * draw(pads), "-" * draw(pads)
    indent = draw(st.sampled_from(" \t")) * draw(pads)     # trimmed off the quote
    line = indent + draw(fragments) + lead + phrase + trail + draw(fragments)
    before = draw(st.lists(fragments, max_size=3))
    after = draw(st.lists(fragments, max_size=3))
    return "\n".join([*before, line, *after])


@st.composite
def refusal_corpora(draw):
    """A known refusal phrase, in any letter case, inside arbitrary text."""
    return _around(draw, _any_case(draw, draw(st.sampled_from(PHRASES))))


#: A phrase each classifying pattern knows, so every pattern's excerpt is exercised.
CLASSIFIERS = (
    *((ORG_BLOCK_RE, phrase) for phrase in PHRASES),
    (AUTH_SIGNATURE_RE, "401"),
    (AUTH_SIGNATURE_RE, "refresh token was revoked"),
    (LIMIT_RE, "usage limit"),
    (LIMIT_RE, "hit your limit"),
    # No real pattern matches this much, but the helper must still hold a match
    # longer than the limit whole.
    (re.compile(r"z{350}", re.IGNORECASE), "z" * 350),
)


@st.composite
def classified_corpora(draw):
    pattern, phrase = draw(st.sampled_from(CLASSIFIERS))
    return pattern, _around(draw, f" {_any_case(draw, phrase)} ")


def _matched_line(corpus: str) -> tuple[str, str]:
    """The line holding ORG_BLOCK_RE's first match, trimmed, and the match itself,
    found independently of the code under test."""
    match = ORG_BLOCK_RE.search(corpus)
    assert match is not None
    lines, start = corpus.split("\n"), 0
    for line in lines:
        if start <= match.start() < start + len(line) + 1:
            return line.strip(), match.group(0)
        start += len(line) + 1
    raise AssertionError("unreachable")


def _assert_quotes(text: str, corpus: str) -> None:
    line, found = _matched_line(corpus)
    if len(line) <= LINE_EXCERPT_MAX:
        assert line in text
    else:
        # A longer line is excerpted, and the excerpt still holds the match.
        assert found in text


@settings(max_examples=300, **QUIET)
@given(corpus=refusal_corpora())
def test_any_matching_corpus_yields_a_refusal_quoting_its_line(corpus):
    refusal = _subscription_refusal(corpus, parse_stream(""))
    assert refusal is not None and refusal.source == "text"
    _assert_quotes(refusal.statement(), corpus)


@settings(max_examples=150, **QUIET)
@given(corpus=refusal_corpora())
def test_any_matching_corpus_is_quoted_by_enroll(corpus):
    """Whatever surrounds it, a refusal on stderr reaches the AdapterError."""
    stdout = _stream(INIT, {"type": "result", "subtype": "success", "is_error": False,
                            "result": "ok", "session_id": "s"})
    error = _enroll_error(stdout, stderr=corpus, rc=1)
    assert error.code == 5 and error.fix == ORG_BLOCK_CAUSES
    _assert_quotes(str(error), corpus)


@settings(max_examples=150, **QUIET)
@given(corpus=refusal_corpora(), in_stream=st.booleans())
def test_any_matching_corpus_is_quoted_by_the_classifier(corpus, in_stream):
    """Whether Claude Code printed it on stderr or in the stream's error text, the
    auth-dead detail quotes the refusal line and names both causes."""
    adapter = ClaudeAdapter(now=lambda: NOW, projects_dir=Path("/nonexistent"))
    stream = _stream(INIT)
    stderr = corpus
    if in_stream:
        stream = _stream(INIT, {"type": "result", "subtype": "error_during_execution",
                                "is_error": True, "session_id": "s", "errors": [corpus]})
        stderr = ""
    with tempfile.TemporaryDirectory() as tmp:
        attempt_dir = Path(tmp)
        (attempt_dir / "stream.jsonl").write_text(stream, encoding="utf-8")
        (attempt_dir / "stderr").write_text(stderr, encoding="utf-8")
        launch = make_launch(attempt_dir, session_id="s", model_id="claude-opus-5-5",
                             identity=None, label=None, projects_dir=attempt_dir)
        outcome = adapter.classify(attempt_dir, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert outcome.detail.endswith(ORG_BLOCK_CAUSES)
    _assert_quotes(outcome.detail, corpus)


@settings(max_examples=300, **QUIET)
@given(corpus=st.text(max_size=600))
def test_no_refusal_is_invented(corpus):
    """Without the words and without the error kind there is no refusal."""
    assume(not ORG_BLOCK_RE.search(corpus))
    assert _subscription_refusal(corpus, parse_stream("")) is None


@settings(max_examples=500, **QUIET)
@given(case=classified_corpora())
def test_an_excerpt_always_holds_its_match(case):
    """Every detail quotes the words that classified it: a line up to the limit is
    quoted whole, and a longer one is cut to a bounded window of itself that holds
    the whole match, with an ellipsis at each cut end."""
    pattern, corpus = case
    match = pattern.search(corpus)
    assert match is not None
    excerpt = _first_line_containing(corpus, match)
    assert match.group(0) in excerpt
    start = corpus.rfind("\n", 0, match.start()) + 1
    end = corpus.find("\n", match.end())
    line = corpus[start: end if end != -1 else len(corpus)].strip()
    if len(line) <= LINE_EXCERPT_MAX:
        assert excerpt == line
        return
    body = excerpt.removeprefix("…").removesuffix("…")
    assert body in line
    assert len(excerpt) <= max(LINE_EXCERPT_MAX, len(match.group(0))) + 2
    if body == line:
        assert excerpt == line          # a match as long as its line is the line
    else:
        assert excerpt.startswith("…") or excerpt.endswith("…")
