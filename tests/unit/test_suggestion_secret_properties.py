"""No recognised secret ever appears in a suggestion's fields or events (C-25.5, C-25.7).

Hypothesis properties. A secret is a value the C-25.5 scrubber recognizes (C-23.14's
families: provider and forge tokens, cloud keys, JWTs, bearer tokens, URL passwords,
sensitive assignments), set between separators in text a failing start says (a git
hook's stderr, an OS error, a folder check's refusal) or in what an agent sends
(the folder, its session, its job). For every one, nothing a suggestion keeps or
shows holds it: the row, `suggestion.list` (listing and by id), the watch feed,
the origin's events, the error Start returns and the daemon's log line; every
string there is one the scrubber leaves unchanged, and every error is one line
within ERROR_MAX.
"""

from __future__ import annotations

import collections
import errno
import json
import logging
import os
import shutil
import sqlite3
import string
import subprocess
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import HealthCheck, assume, example, given, settings

from subfleet.conversations import suggestions
from subfleet.conversations.peers import Verdict
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.sessions.handoff import REDACTION_MARKERS, scrub_secrets
from tests.unit.test_conversation_service import SETTINGS, FakeDaemon

PERSON, AGENT = 1, 2
ALNUM = string.ascii_letters + string.digits
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "T",
           "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}


def _tail(least: int, alphabet: str = ALNUM) -> st.SearchStrategy[str]:
    return st.text(alphabet, min_size=least, max_size=least + 24)


#: Quoted values with arbitrary Unicode and whitespace, including line breaks.
whitespace = st.sampled_from([" ", "\n", "\t", "\r", "\r\n", "\u2003", "\u2028", "\u2029"])
unicode_words = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\"'\\\x00"), max_size=24)
quoted_values = st.tuples(unicode_words, whitespace, unicode_words).map(
                             lambda p: "SECRET7-" + "".join(p) + "-右秘密-TAIL8")
quoted_secrets = st.tuples(st.sampled_from(["password", "token", "client_secret", "credentials"]),
                           st.sampled_from(['"', "'"]), quoted_values).map(
                               lambda p: p[0] + "=" + p[1] + p[2] + p[1])

#: Every recognized family, including assignments beyond the old line boundary.
secrets = st.one_of(
    st.tuples(st.sampled_from(["sk-ant-api03-", "sk-proj-", "sk-", "ghp_", "gho_", "github_pat_", "glpat-", "xoxb-",
                               "npm_", "hf_", "sk_live_", "rk_test_"]), _tail(16)).map("".join),
    _tail(16, string.ascii_uppercase + string.digits).map(lambda t: "AKIA" + t[:16]),
    _tail(20).map(lambda t: "AIza" + t),
    st.tuples(_tail(8), _tail(8), _tail(8)).map(lambda p: "eyJ" + ".".join(p)),
    _tail(8).map(lambda t: "Bearer " + t),
    _tail(6).map(lambda t: "https://agent:" + t + "@example.test/repo"),
    _tail(8).map(lambda t: "api_key=" + t),
    quoted_secrets,
)
separators = st.sampled_from([" ", "\n", "\t", " (", "] ", ", ", "\r\n", "\u2003", "\u2028"])
#: Unicode prose and whitespace around each credential.
padding = st.text(string.ascii_lowercase + "界右🧪éα .:'\"\n\r\t\u2003\u2028", max_size=1200)


def said(before: str, left: str, secret: str, right: str, after: str) -> str:
    return before + left + secret + right + after


def recognized(value) -> list[str]:
    """Every string in `value` (any JSON shape) the scrubber would change."""
    found = []
    if isinstance(value, str):
        clean, count = scrub_secrets(value, strip_reminders=True, whole=True)
        if count or clean != value:
            found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found += recognized(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found += recognized(item)
    return found


TOKEN_PREFIXES = ("sk-ant-api03-", "sk-proj-", "sk-", "ghp_", "gho_", "github_pat_", "glpat-", "xoxb-",
                  "npm_", "hf_", "sk_live_", "rk_test_", "AKIA", "AIza", "eyJ", "Bearer ",
                  "https://agent:", "api_key=")


def assert_secret_absent(value, secret: str) -> None:
    """Check full values, token fragments past the fixed prefix, and whole markers."""
    if isinstance(value, dict):
        for item in value.values():
            assert_secret_absent(item, secret)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_secret_absent(item, secret)
    elif isinstance(value, str):
        assert secret not in value and " ".join(secret.split()) not in value
        prefix = next((p for p in TOKEN_PREFIXES if secret.startswith(p)), None)
        if prefix is not None:
            # Absence of this shortest forbidden prefix excludes every longer one.
            assert secret[:len(prefix) + 1] not in value
        else:
            # These sentinels sit inside the generated assignment's value on
            # opposite sides of its arbitrary Unicode/whitespace contents.
            assert "SECRET7-" not in value and "TAIL8" not in value
        for marker in REDACTION_MARKERS:
            for end in range(1, len(marker)):
                assert not value.endswith((marker[:end], marker[:end] + "…")), value[-60:]
        assert not recognized(value)


@given(before=padding, left=separators, secret=secrets, right=separators, after=padding)
@example(before="界" * 493, left=" ", secret="ghp_" + "A" * 36, right=" ", after="end")
@example(before="", left="", secret='password="SECRET7-left\n右秘密-TAIL8"', right="", after="")
@settings(max_examples=500, deadline=None)
def test_c25_5_a_starts_error_is_scrubbed_whole_then_bounded(before, left, secret, right, after):
    text = said(before, left, secret, right, after)
    assert scrub_secrets(text, strip_reminders=True, whole=True)[1] > 0
    error = suggestions.scrub_error(text)
    assert secret not in error and not recognized(error)
    assert len(error) <= suggestions.ERROR_MAX and "\n" not in error and error
    assert_secret_absent(error, secret)


#: Each value a suggestion keeps or shows only within a bound, and the bound.
CUTS = {"title": suggestions.TITLE_MAX, "tldr": suggestions.TLDR_MAX, "error": suggestions.ERROR_MAX,
        "decided_by": suggestions.ERROR_MAX, "preview": suggestions.PREVIEW_CHARS}


def kept(where: str, text: str) -> str:
    """What a suggestion keeps or shows of `text` said as `where`."""
    if where in ("title", "tldr"):
        words, _ = suggestions.normalize(text if where == "title" else "Fixture",
                                         text if where == "tldr" else "Fixture", "Fixture brief")
        return words[where]
    if where == "error":
        return suggestions.scrub_error(text)
    if where == "decided_by":
        return suggestions.scrubbed_line(text)
    brief = suggestions.normalize("Fixture", "Fixture", text)[0]["brief"]
    row = {"suggestion_id": "sg-fixture", "state": "open", "title": "Fixture", "tldr": "Fixture", "brief": brief,
           "worktree": False, "provider": "claude"}
    shown = suggestions.view(collections.defaultdict(lambda: None, row))["brief"]
    assert brief.startswith(shown)
    return shown


@given(secret=secrets, left=separators, back=st.integers(0, 48), where=st.sampled_from(sorted(CUTS)))
@settings(max_examples=300, deadline=None)
def test_c25_5_no_cut_leaves_text_the_scrubber_recognizes(secret, left, back, where):
    """Every value a suggestion bounds is scrubbed whole, then cut; for every family,
    set anywhere near the cut, what is kept is never text the scrubber would change
    (a cut through `api_key=[REDACTED]`, leaving `api_key=[REDACTE`, would be)."""
    limit = CUTS[where]
    if where in ("title", "tldr"):              # refused, not cut, when longer than its bound
        text = ("a " * limit)[:max(0, limit - back - len(left) - len(secret))] + left + secret
        assume(len(" ".join(text.split())) <= limit)
    else:
        text = ("a " * limit)[:limit - back] + left + secret + " tail " * 4
    value = kept(where, text)
    assert secret not in value and not recognized(value), value[-40:]
    assert len(value) <= limit and (where == "preview" or "\n" not in value)
    assert_secret_absent(value, secret)


@given(secret=quoted_secrets, before=st.text("界🧪éα ", max_size=4), after=st.text("右🧪éα ", max_size=4))
@settings(max_examples=100, deadline=None)
def test_quoted_unicode_values_are_scrubbed_in_raw_text_before_folding(secret, before, after):
    raw = before + " " + secret + " " + after
    clean = scrub_secrets(raw, strip_reminders=True, whole=True)[0]
    assert_secret_absent(clean, secret)
    for where in CUTS:
        # Keep the submitted title/summary within their original input bound.
        if where in ("title", "tldr") and len(" ".join(raw.split())) > CUTS[where]:
            continue
        assert_secret_absent(kept(where, raw), secret)


@given(marker=st.sampled_from(REDACTION_MARKERS), prefix=st.text("界右🧪éα ", max_size=40),
       inside=st.integers(1, 30), mark=st.sampled_from(["", "…"]))
@settings(max_examples=100, deadline=None)
def test_character_bounds_keep_all_scrubber_markers_whole(marker, prefix, inside, mark):
    inside = 1 + (inside - 1) % (len(marker) - 1)
    limit = len(prefix) + inside + len(mark)
    shown = suggestions.bounded(prefix + marker + " tail", limit, mark)
    assert len(shown) <= limit
    assert "[" not in shown.replace(marker, "")


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextmanager
def fixture():
    """A real conversation service over a fake daemon, in its own folder; the person
    is pid PERSON. One per example, so no example's claims count against another's."""
    folder = Path(tempfile.mkdtemp(prefix="sf-sg-secret-"))
    root = folder / "state"
    root.mkdir()
    daemon = FakeDaemon(root)
    # Keep the real Git checks. Host suspension must not prevent a generated
    # example from reaching the injected failure whose channels it verifies.
    daemon.policy["caps"]["workspace_git_timeout_s"] = 120
    service = ConversationService(daemon)
    service._person = lambda peer, what: (Verdict(True, "the Subfleet app", peer) if peer == PERSON else
                                          (_ for _ in ()).throw(ConversationError("person-only", what, code=7)))
    captured, level = Captured(), daemon.log.level
    daemon.log.addHandler(captured)
    daemon.log.setLevel(logging.INFO)
    service.captured = captured
    service.folder = folder
    try:
        yield service
    finally:
        daemon.log.removeHandler(captured)
        daemon.log.setLevel(level)
        service.close()
        daemon.store.close()
        shutil.rmtree(folder, ignore_errors=True)


def origin_turn(service) -> tuple[str, str]:
    """A conversation with a running turn whose job names it: (conversation, job)."""
    work = service.folder / "work"
    work.mkdir(exist_ok=True)
    cid = service.store.create_conversation(provider="claude", workspace=str(work), workspace_kind="in-place",
                                            settings=SETTINGS, origin="new")[0]["conversation_id"]
    mid = str(uuid.uuid4())
    service.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="work",
                                 attachments=[], settings=SETTINGS)
    job = "20261011-000000-turn-" + mid[:8]
    service.daemon.store.add_job(job_id=job, request_id=f"turn:{mid}:0", payload_digest="d", kind="turn",
                                 workdir=str(work), prompt_path="p.md", sandbox="read-only", name=f"turn-{cid}",
                                 in_place=1, max_attempts=1, state="running")
    return cid, job


def checkout(service, hook: str) -> Path:
    """A git checkout whose post-checkout hook says `hook` on stderr and fails."""
    repo = service.folder / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "file.txt").write_text("base\n")
    env = {**os.environ, **GIT_ENV}
    for argv in (["init", "-q", "-b", "feature/x"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, env=env)
    (repo / "said.txt").write_text(hook)
    script = repo / ".git" / "hooks" / "post-checkout"
    script.write_text(f"#!/bin/sh\ncat '{repo / 'said.txt'}' >&2\nexit 1\n")
    script.chmod(0o755)
    return repo


def everything_shown(service, sid: str, cid: str, error: str) -> dict:
    return {"row": service.store.query("SELECT * FROM suggestions WHERE suggestion_id=?", (sid,)),
            "listing": service.handle("suggestion.list", {}, AGENT),
            "named": service.handle("suggestion.list", {"suggestion_ids": [sid]}, AGENT),
            "feed": service.handle("conversation.watch", {"after": 0}, AGENT),
            "events": service.handle("conversation.events", {"conversation_id": cid, "after": 0}, AGENT),
            "error": error, "log": service.captured.lines}


@given(before=padding, left=separators, secret=secrets, right=separators, after=padding)
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_c25_5_what_a_failing_checkout_hook_says_never_reaches_a_suggestion_field_or_event(
        before, left, secret, right, after):
    """The review's leak, for every family and placement: a hook's stderr through
    `git worktree add`, the claim's `start_error` and the returned error; then the
    daemon's own finishing of the claim (which adopts the worktree the failed add
    left, or fails again and logs why)."""
    hook = said(before, left, secret, right, after)
    with fixture() as service:
        repo = checkout(service, hook)
        cid, job = origin_turn(service)
        row = service.handle("suggestion.create", {"request_id": str(uuid.uuid4()), "title": "Fixture",
                                                   "tldr": "Fixture", "brief": "Fixture brief",
                                                   "workspace": str(repo), "worktree": True, "calling_job": job},
                             AGENT)["suggestion"]
        sid = row["suggestion_id"]
        try:
            service.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
        except ConversationError as exc:
            assert exc.reason == "worktree-failed", exc
            error = str(exc)
        else:
            raise AssertionError("the failing hook did not fail the start")
        assert service.suggestions.get(sid)["state"] == "starting"
        assert service.suggestions.get(sid)["start_error"].startswith("worktree-failed: ")
        shown = everything_shown(service, sid, cid, error)
        assert secret not in json.dumps(shown) and not recognized(shown), recognized(shown)
        assert_secret_absent(shown, secret)
        service._resume_start(sid, 1, service.clock())
        shown = everything_shown(service, sid, cid, error)
        assert service.captured.lines and secret not in json.dumps(shown) and not recognized(shown), recognized(shown)
        assert_secret_absent(shown, secret)
        assert len(error) <= suggestions.ERROR_MAX
        # Unless the daemon's retry adopted the worktree the failed add left and finished.
        assert all(len(r["start_error"] or "") <= suggestions.ERROR_MAX for r in shown["row"])


@given(before=padding, left=separators, secret=secrets, right=separators, after=padding,
       where=st.sampled_from(["folder check before the claim", "an OS error after it",
                              "a refusal after it", "a store error inside the brief's transaction"]))
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_c25_5_any_start_failure_is_scrubbed_wherever_it_happens(before, left, secret, right, after, where):
    text = said(before, left, secret, right, after)
    with fixture() as service:
        cid, job = origin_turn(service)
        work = service.folder / "work"
        row = service.handle("suggestion.create", {"request_id": str(uuid.uuid4()), "title": "Fixture",
                                                   "tldr": "Fixture", "brief": "Fixture brief",
                                                   "workspace": str(work), "calling_job": job}, AGENT)["suggestion"]
        sid = row["suggestion_id"]

        def fail(*args, **kwargs):
            if where == "folder check before the claim":
                raise ConversationError("protected-branch", text, code=7)
            if where == "an OS error after it":
                raise OSError(5, text)
            if where == "a refusal after it":
                raise ConversationError("worktree-failed", text, code=1)
            raise sqlite3.OperationalError(text)
        if where == "folder check before the claim":
            service._validate_workspace = fail
        elif where == "a store error inside the brief's transaction":
            service.suggestions.finish_in = fail
        else:
            service.store.submit_message = fail
        error = ""
        try:
            service.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
        except ConversationError as exc:
            error = str(exc)
            assert where in ("folder check before the claim", "a refusal after it")
        except (OSError, sqlite3.OperationalError):
            # Not a refusal: the daemon answers these as "operation failed" (C-16.7),
            # never with their text; the claim records them scrubbed.
            assert where in ("an OS error after it", "a store error inside the brief's transaction")
        else:
            raise AssertionError("the start did not fail")
        state = service.suggestions.get(sid)["state"]
        assert state == ("open" if where == "folder check before the claim" else "starting")
        if state == "starting":
            # The daemon's own finishing of it fails the same way, and logs why.
            service._resume_start(sid, 1, service.clock())
            assert any("finishing the start" in line for line in service.captured.lines)
        shown = everything_shown(service, sid, cid, error)
        assert secret not in json.dumps(shown) and not recognized(shown), recognized(shown)
        assert len(error) <= suggestions.ERROR_MAX
        assert_secret_absent(shown, secret)


#: What each field a caller names is refused as when the scrubber would change it.
REFUSED_AS = {"workspace": "bad-workspace", "session_id": "bad-session-id", "calling_job": "bad-calling-job"}


@given(secret=secrets.filter(lambda s: "/" not in s and "\n" not in s and "\x00" not in s),
       which=st.sampled_from(sorted(REFUSED_AS)))
@example(secret='password="SECRET7- \U0002ee5e-右秘密-TAIL8"', which="workspace")
@settings(max_examples=100, deadline=None)
def test_c25_5_a_folder_session_or_job_shaped_like_a_secret_is_refused_and_makes_nothing(secret, which):
    """The row keeps the folder, the caller's session and its job as stated: one the
    scrubber would change is refused, so no row holds it."""
    with fixture() as service:
        args = {"request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture brief"}
        if which == "workspace":
            folder = service.folder / ("work " + secret)
            try:
                folder.mkdir()
            except OSError as exc:
                # APFS rejects some Unicode scalars as filenames. Still send
                # that path: the service must refuse it without publishing it.
                unassigned = any(unicodedata.category(char) == "Cn" for char in secret)
                if exc.errno != errno.EILSEQ and not (exc.errno == errno.EPERM and unassigned):
                    raise
            args["workspace"] = str(folder)
        else:
            args[which] = which.split("_")[0] + " " + secret
        try:
            service.handle("suggestion.create", args, AGENT)
        except ConversationError as exc:
            assert exc.reason == REFUSED_AS[which], exc
            assert secret not in str(exc)
            assert_secret_absent(str(exc), secret)
        else:
            raise AssertionError("a secret-shaped " + which + " was accepted")
        assert service.store.query("SELECT * FROM suggestions") == []
        assert service.store.changes_after(0)["changes"] == []
