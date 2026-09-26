"""What a handoff may carry, and how it is dispatched: C-23.14, C-23.36, C-23.54.

Every test names the clause it proves (C-20.5). `sessions_fixtures.FAKE_SECRET`
is a token shaped like the ones the scrub list catches and is not, and has never
been, a key; no test here reads a real credential from anywhere.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from subfleet.contracts import Sandbox
from subfleet.sessions import handoff
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
CODE = "def total(rows):\n    return sum(row.amount for row in rows)\n"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    return fx.claude_home(tmp_path, monkeypatch)


@pytest.fixture
def policy():
    return fx.policy()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real git worktree, because the brief's last section runs git."""
    path = tmp_path / "repo"
    path.mkdir()
    for argv in (["init", "-q", "-b", "work"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True,
                       capture_output=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "first"], check=True,
                   capture_output=True)
    return path


def build(home, repo, entries, policy, *, caps=None):
    path = fx.transcript(home, SESSION, entries)
    return handoff.build_brief(SESSION, path, repo, str(repo),
                               caps or policy["sessions"]["handoff_caps"])


def conversation(*extra):
    return [fx.typed_prompt("Port the ledger importer to v2.", uuid="p0",
                            at=fx.ago(3600)),
            *extra,
            fx.assistant_text("done for now", uuid="last", at=fx.ago(60))]


# --- the scrub list (C-23.14) -------------------------------------------------

def test_handoff_scrubs_credentials_and_binary_but_keeps_code(home, repo, policy):
    """C-23.14: private keys, JWTs, prefixed tokens and `Bearer` values are
    replaced and encoded binary is omitted, while ordinary code, commands and
    tool output are retained verbatim.

    Ledger row 208. A lossy rewrite destroys the continuity the brief exists to
    carry, so the rule is a scalpel, not a shredder.
    """
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLXZhbHVl"
    noisy = (f"export ANTHROPIC_API_KEY={fx.FAKE_SECRET}\n"
             f"curl -H 'Authorization: Bearer {jwt}' https://example.test/v1\n"
             f"{CODE}"
             f"payload = 'data:image/png;base64,{'A' * 200}'\n")
    brief = build(home, repo, conversation(
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": "cat notes.md"}),
        fx.user_tool_result(noisy, uuid="r1", at=fx.ago(500))), policy)

    assert fx.FAKE_SECRET not in brief.text
    assert jwt not in brief.text
    assert "BASE64" in brief.text and "A" * 200 not in brief.text
    assert "[REDACTED]" in brief.text
    assert CODE.strip() in brief.text, "ordinary code is retained verbatim"
    assert "curl -H" in brief.text, "the command survives; only its value does not"
    assert "cat notes.md" in brief.text, "an ordinary tool input is context"
    assert brief.redactions >= 3


def test_a_private_key_block_is_replaced_whole(home, repo, policy):
    """C-23.14: a PEM block is one value, not a run of lines to redact."""
    pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           + "b3BlbnNzaC1rZXktdjEAAAAA\n" * 4
           + "-----END OPENSSH PRIVATE KEY-----")
    brief = build(home, repo, conversation(
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": "cat id_ed25519"}),
        fx.user_tool_result(pem, uuid="r1", at=fx.ago(500))), policy)
    assert "PRIVATE KEY REDACTED" in brief.text
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in brief.text


def test_a_long_unbroken_encoded_run_is_omitted_before_it_is_capped(home, repo, policy):
    """C-23.14: encoded binary is omitted, and that runs before the caps.

    A 5000-character unbroken base64-alphabet run is a payload whatever the
    section cap says, so it never reaches truncation — which is why the caps
    test above uses prose.
    """
    text, count = handoff.scrub_secrets("A" * 5000)
    assert text == "[BASE64 OMITTED]" and count == 1
    kept, _count = handoff.scrub_secrets("word " * 1000)
    assert kept.startswith("word word"), "prose with spaces is not a payload"


def test_binary_tool_output_is_omitted_rather_than_pasted(home, repo, policy):
    """C-23.14: encoded binary is omitted; a brief is context, not a payload."""
    brief = build(home, repo, conversation(
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": "cat logo.png"}),
        fx.user_tool_result("\x00\x01\x02\x03" * 200, uuid="r1", at=fx.ago(500))),
        policy)
    assert handoff.OMITTED_BINARY in brief.text
    assert "\x00" not in brief.text


def test_a_system_reminder_never_reaches_the_brief(home, repo, policy):
    """The harness's own injected text is not the session's conversation."""
    brief = build(home, repo, conversation(
        fx.user_text("<system-reminder>do not mention this</system-reminder> keep me",
                     uuid="u1", at=fx.ago(600))), policy)
    assert "system-reminder" not in brief.text
    assert "keep me" in brief.text


# --- suppression by pattern (C-23.14) -----------------------------------------

@pytest.mark.parametrize("command", [
    "agent-secret get claude-quota-max@axiom.org",
    "security find-generic-password -s subfleet -w",
    "security find-generic-password -s subfleet -g",
    "security find-internet-password -s example.test -w",
    "printenv | grep TOKEN",
    "cat ~/.codex/auth.json",
    "cat .env.production",
    "/usr/bin/printenv",
    "cat .env; true",
    "cat .env&&echo done",
    "bash -lc 'printenv'",
    "sh -c \"env\"",
    "sudo -E env",
    "/usr/bin/env",
    "echo `env`",
    "xargs env < /dev/null",
    'const r = await tools.exec_command({cmd: "printenv"}); text(r.output);',
    'await tools.exec_command({cmd: "env"})',
], ids=["agent-secret", "keychain", "keychain-stderr", "keychain-internet",
        "printenv", "auth-json", "dotenv", "printenv-by-path", "dotenv-then-semicolon",
        "dotenv-then-and", "bash-lc", "sh-c", "sudo-flags-env", "env-by-path", "backquote",
        "xargs", "exec-wrapped-printenv", "exec-wrapped-env"])
def test_handoff_suppresses_credential_reading_tool_results(home, repo, policy, command):
    """C-23.14: the result of a credential-reading tool call is omitted by
    pattern rather than redacted, so a secret never reaches the excerpt even
    unredacted.

    Ledger row 209. Suppression beats redaction here because the value a
    keychain read returns has no shape a regex can rely on.
    """
    assert handoff.sensitive_tool_call("Bash", {"command": command}), command
    brief = build(home, repo, conversation(
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": command}),
        fx.user_tool_result("hunter2-the-actual-value", uuid="r1", at=fx.ago(500))),
        policy)
    assert "hunter2-the-actual-value" not in brief.text
    assert handoff.OMITTED_SENSITIVE in brief.text
    assert handoff.OMITTED_SENSITIVE_INPUT in brief.text
    assert command not in brief.text, "the input is omitted too, not just the result"


@pytest.mark.parametrize("command", [
    "cd /repo\nenv",
    "set -e\nprintenv",
    "for name in a b; do\n  env\ndone",
    "cat <<EOF\nhi\nEOF\nenv > dump.txt",
    "(env)",
    "x=1 && env",
    "env;true",
], ids=["second-line", "after-set", "indented-in-loop", "after-heredoc",
        "subshell", "after-and", "before-semicolon"])
def test_a_multi_line_environment_dump_is_suppressed_too(home, repo, policy, command):
    """C-23.14: an `env` that is not the first word of a one-line command.

    v1 matched these patterns against `json.dumps` of the tool input, which
    turns a real newline into the two characters `\\` and `n` — so `env` on the
    second line sat behind neither `^` (the rendering starts with `{`) nor a
    `;&|` separator, and the whole environment reached the brief. A multi-line
    script that dumps the environment is not an exotic input.
    """
    assert handoff.sensitive_tool_call("Bash", {"command": command}), command
    brief = build(home, repo, conversation(
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": command}),
        fx.user_tool_result(f"ANTHROPIC_API_KEY={fx.FAKE_SECRET}", uuid="r1",
                            at=fx.ago(500))), policy)
    assert fx.FAKE_SECRET not in brief.text
    assert handoff.OMITTED_SENSITIVE in brief.text


@pytest.mark.parametrize("command", [
    "git log --oneline -5", "python -m venv .venv", "grep -r inventory .",
    "echo $ENVIRONMENT", "ls /opt/envoy", "make env-check", "python envelope.py",
    "docker run --env-file f", "cd /repo\ngit status",
    "uv run --env-file .envrc x", "conda env list", "ls envs/", "cat .venv/pyvenv.cfg",
])
def test_a_word_that_merely_contains_env_is_not_a_credential_read(command):
    """C-23.14's retention half: suppressing everything is not safety."""
    assert handoff.sensitive_tool_call("Bash", {"command": command}) is False, command


def test_a_credential_read_nested_in_a_structured_input_is_still_seen(home):
    """C-23.14: the corpus walks the input, so a tool whose schema is not
    `{"command": ...}` is judged on its strings too."""
    assert handoff.sensitive_tool_call("Task", {"steps": [{"run": "set -x\nprintenv"}]})
    assert handoff.sensitive_tool_call("Read", {"file_path": "/Users/x/.codex/auth.json"})
    assert handoff.sensitive_tool_call("Edit", {"file_path": "/repo/README.md"}) is False
    assert handoff.sensitive_tool_call("Bash", None) is False


def test_an_ordinary_command_is_not_suppressed(home, repo, policy):
    """C-23.14: the retention half. Suppressing everything is not safety."""
    assert handoff.sensitive_tool_call("Bash", {"command": "git log --oneline -5"}) is False
    brief = build(home, repo, conversation(
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": "git log --oneline -5"}),
        fx.user_tool_result("abc1234 first commit", uuid="r1", at=fx.ago(500))), policy)
    assert "git log --oneline -5" in brief.text
    assert "abc1234 first commit" in brief.text


def test_a_result_whose_input_fell_outside_the_excerpt_is_omitted(home, repo, policy):
    """C-23.14: unknown sensitivity means omitted; the excerpt is bounded, and a
    result whose call it cannot see could be anything."""
    brief = build(home, repo, conversation(
        fx.user_tool_result("some output", uuid="r1", at=fx.ago(500),
                            tool_id="never-seen")), policy)
    assert handoff.OMITTED_UNMATCHED in brief.text
    assert "some output" not in brief.text


def test_a_tool_named_for_the_keychain_is_suppressed_whatever_its_input(home):
    """C-23.14: the name is evidence too, not only the command line."""
    assert handoff.sensitive_tool_call("keychain_read", {"item": "anything"}) is True
    assert handoff.sensitive_tool_call("mcp__agent-secret__get", {}) is True


# --- the caps and the source (C-23.36) ----------------------------------------

def test_handoff_sections_capped_and_source_transcript_path_recorded(home, repo, policy):
    """C-23.36: every section is bounded by an explicit per-section character
    cap, and the handoff records the absolute path of the source transcript,
    which stays the durable record.

    Ledger row 210.
    """
    caps = {**policy["sessions"]["handoff_caps"], "original_task": 200,
            "recent": 400, "tool_result": 120, "progress": 150}
    # Ordinary prose, not one unbroken alphanumeric run: that would be caught as
    # encoded binary first (see the test below) and never reach the cap.
    task = "port the ledger importer to v2 " * 200
    output = "row imported ok\n" * 400
    (repo / "PROGRESS.md").write_text("done so far: the manifest\n" * 300,
                                      encoding="utf-8")
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt(task, uuid="p0", at=fx.ago(3600)),
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600),
                              tool_input={"command": "cat big"}),
        fx.user_tool_result(output, uuid="r1", at=fx.ago(500)),
        fx.assistant_text("done", uuid="last", at=fx.ago(60))])
    brief = handoff.build_brief(SESSION, path, repo, str(repo), caps)

    assert "characters omitted" in brief.text, "truncation is marked, not silent"
    assert task not in brief.text
    assert output not in brief.text
    assert brief.text.count("row imported ok") < 400
    # The source stays authoritative and is named by absolute path.
    assert f"Source transcript: {path}" in brief.text
    assert Path(brief.transcript).is_absolute()
    assert brief.transcript == str(path)


def test_every_named_section_is_present_and_in_order(home, repo, policy):
    """C-23.36: the brief's shape is the contract a receiving agent reads."""
    brief = build(home, repo, conversation(), policy)
    order = ["# Cross-agent handoff", "## Original task",
             "## Recent main-chain excerpt", "## PROGRESS.md", "## Repository state"]
    positions = [brief.text.index(heading) for heading in order]
    assert positions == sorted(positions)
    assert "Source session: " + SESSION in brief.text
    assert f"Target cwd: {repo}" in brief.text


def test_recent_excerpt_counts_separators_inside_its_cap(home, policy):
    """C-23.36: separators and a tiny final allowance fit the section budget."""
    caps = {**policy["sessions"]["handoff_caps"], "recent": 40}
    path = fx.transcript(home, SESSION, [
        fx.assistant_text("older context " * 1000, uuid="a0", at=fx.ago(600)),
        fx.assistant_text("latest reply", uuid="a1", at=fx.ago(60))])
    excerpt, _count = handoff.recent_excerpt(path, None, caps)
    assert len(excerpt) <= caps["recent"]
    assert excerpt.endswith("latest reply"), "the newest context is kept first"
    assert "\n\n" in excerpt, "more than one segment exercises separator accounting"


def test_empty_sections_still_respect_their_caps_in_the_rendered_brief(home, repo, policy):
    """C-23.36: fallback explanations are part of the bounded section too."""
    caps = {**policy["sessions"]["handoff_caps"], "recent": 10, "progress": 5}
    brief = build(home, repo, [fx.typed_prompt("continue the work", uuid="p0",
                                              at=fx.ago(60))], policy, caps=caps)
    recent = brief.text.split("## Recent main-chain excerpt\n\n", 1)[1].split(
        "\n\n## PROGRESS.md", 1)[0]
    progress = brief.text.split("## PROGRESS.md\n\n", 1)[1].split(
        "\n\n## Repository state", 1)[0]
    assert len(recent) <= caps["recent"]
    assert len(progress) <= caps["progress"]


@pytest.mark.parametrize("cap", [0, 5])
def test_tiny_caps_allow_a_task_and_bound_non_git_repository_context(home, tmp_path, policy, cap):
    """C-23.36: zero omits task text, and non-Git fallback obeys its cap."""
    caps = {**policy["sessions"]["handoff_caps"], "original_task": cap,
            "recent": cap, "progress": cap, "repository": cap}
    brief = build(home, tmp_path, [fx.typed_prompt("continue the work", uuid="p0",
                                                  at=fx.ago(60))], policy, caps=caps)
    assert len(brief.original) <= cap
    repository = brief.text.split("## Repository state", 1)[1].strip()
    assert len(repository) <= cap


def test_the_repository_section_reports_the_real_worktree(home, repo, policy):
    """C-23.36: the brief points at state a receiving agent can verify."""
    brief = build(home, repo, conversation(), policy)
    assert "### Status" in brief.text and "### Recent commits" in brief.text
    assert "first" in brief.text, "the commit subject is real git output"


def test_a_directory_that_is_not_a_worktree_says_so(home, tmp_path, policy):
    """C-23.36: a brief never invents repository state it could not read."""
    plain = tmp_path / "plain"
    plain.mkdir()
    path = fx.transcript(home, SESSION, conversation())
    brief = handoff.build_brief(SESSION, path, plain, str(plain),
                                policy["sessions"]["handoff_caps"])
    assert "Not a Git worktree." in brief.text


def test_the_apps_stubs_and_subfleets_own_nudges_are_not_the_original_task(home, repo, policy):
    """C-23.36: the first REAL human turn is the task, not the app's bookkeeping."""
    from subfleet.sessions import transcripts
    entries = [*fx.resume_stub(at=fx.ago(4000)),
               fx.user_text(transcripts.MARKER + " continue", uuid="n", at=fx.ago(3900)),
               fx.typed_prompt("the actual instruction", uuid="p0", at=fx.ago(3600)),
               fx.assistant_text("on it", uuid="last", at=fx.ago(60))]
    brief = build(home, repo, entries, policy)
    assert brief.original == "the actual instruction"


def test_a_transcript_with_no_human_turn_is_a_user_facing_error(home, repo, policy):
    """C-17.3: exit 2 is invalid input, and the message names the transcript."""
    path = fx.transcript(home, SESSION, [
        fx.assistant_text("orphaned", uuid="a", at=fx.ago(60))])
    with pytest.raises(handoff.HandoffError) as raised:
        handoff.build_brief(SESSION, path, repo, str(repo),
                            policy["sessions"]["handoff_caps"])
    assert str(path) in str(raised.value)
    assert handoff.HandoffError.code == 2


# --- the dispatch (C-23.54) ---------------------------------------------------

def test_the_handoff_is_submitted_with_the_caller_session_recorded(home, repo, policy,
                                                                   tmp_path):
    """C-23.54: every provider launch is a `subfleet run` submission, including
    subfleet's own, so the handoff inherits routing, the guard, salvage, the
    ledger and notices — and the caller's session is recorded so the completion
    notice comes back to the session that asked.
    """
    fx.transcript(home, SESSION, conversation())
    staged = tmp_path / "prompt.md"
    daemon = fx.FakeSessions()
    result = handoff.handoff(
        daemon, policy, session_id=SESSION, last=False, model="astra",
        stage_prompt=lambda text: (staged.write_text(text, encoding="utf-8"), staged)[1],
        workdir=repo, caller_session="caller-1", caller_pid=4242)
    assert result.job_id == "job-1"
    args = daemon.submits[0]
    assert args.kind == "handoff"
    assert args.pinned_model == "astra"
    assert args.caller_session == "caller-1" and args.caller_pid == 4242
    assert args.workdir == str(repo)
    assert args.name == f"handoff-{SESSION[:8]}"
    assert Path(args.prompt_path).read_text(encoding="utf-8") == result.brief.text


def test_the_sandbox_comes_from_the_tasks_policy_permission(policy):
    """C-6.5: a writable job carries consequences a text classifier should not
    choose, so a handoff with no `--task` is read-only.

    v1 derived the class from the instruction text; v2 keeps the `permissions`
    map and drops the guess.
    """
    assert handoff.sandbox_for(policy, None) == Sandbox.READ_ONLY.value
    assert handoff.sandbox_for(policy, "build") == Sandbox.WORKSPACE_WRITE.value
    assert handoff.sandbox_for(policy, "review") == Sandbox.READ_ONLY.value
    assert handoff.sandbox_for(policy, "build", "read-only") == "read-only"


def test_a_dry_run_prints_the_brief_and_dispatches_nothing(home, repo, policy):
    """C-17.4: the brief is the thing to inspect before it is sent anywhere."""
    fx.transcript(home, SESSION, conversation())
    daemon = fx.FakeSessions()
    result = handoff.handoff(daemon, policy, session_id=SESSION, last=False,
                             model="opus", stage_prompt=lambda text: Path("/dev/null"),
                             workdir=repo, dry_run=True)
    assert daemon.submits == []
    assert result.job_id is None
    assert "# Cross-agent handoff" in result.brief.text


# --- choosing the source ------------------------------------------------------

def test_exactly_one_of_a_session_id_and_last_is_required(home):
    """v1's rule, kept: naming both is a mistake, naming neither is a mistake."""
    for session_id, last in ((SESSION, True), (None, False)):
        with pytest.raises(handoff.HandoffError, match="exactly one"):
            handoff.resolve_source(session_id, last)


def test_a_session_id_must_be_a_canonical_uuid(home):
    """v1's rule, kept: a truncated id would silently resolve to nothing.

    Case is not part of it — `uuid.UUID` and the transcript filename agree on
    lowercase, so an uppercase id resolves. The forms this rejects are the ones
    that parse as a UUID and are not the filename: braces, `urn:uuid:`, and the
    dashless 32-hex spelling.
    """
    with pytest.raises(handoff.HandoffError, match="invalid Claude session id"):
        handoff.resolve_source("3f9c1a2e", False)
    for spelling in (f"{{{SESSION}}}", f"urn:uuid:{SESSION}", SESSION.replace("-", "")):
        with pytest.raises(handoff.HandoffError, match="canonical UUID"):
            handoff.resolve_source(spelling, False)
    assert handoff.canonical_session_id(SESSION.upper()) == SESSION


def test_last_prefers_the_callers_own_session(home):
    """`--last` means "this session" when there is one to mean."""
    fx.transcript(home, SESSION, conversation())
    other = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
    fx.transcript(home, other, conversation(), cwd="/Users/fixture/other")
    found, _path = handoff.resolve_source(None, True, current=SESSION)
    assert found == SESSION


def test_last_falls_back_to_the_newest_durable_transcript(home):
    """`--last` outside a session: the newest real turn wins, then the mtime."""
    fx.transcript(home, SESSION, [
        fx.typed_prompt("older", uuid="p", at=fx.ago(9000)),
        fx.assistant_text("done", uuid="a", at=fx.ago(8000))])
    newer = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
    fx.transcript(home, newer, conversation(), cwd="/Users/fixture/other")
    found, _path = handoff.resolve_source(None, True, current=None)
    assert found == newer


def test_a_headless_lane_run_is_refused_rather_than_handed_off(home, policy, repo):
    """C-23.31: a headless lane run is never continued, and a request naming one
    is refused with the reason.

    Its transcript is one brief and one answer; there is no conversation to hand
    to anybody, and a continuation of it has no reader.
    """
    fx.transcript(home, SESSION, fx.headless(age_s=600), cwd=str(repo))
    daemon = fx.FakeSessions(lane_sessions=[SESSION])
    for lane_ids in ([SESSION], []):     # the recorded marker, then the shape
        with pytest.raises(handoff.HandoffError) as raised:
            handoff.handoff(daemon, policy, session_id=SESSION, last=False,
                            model="astra", stage_prompt=lambda text: Path("/dev/null"),
                            workdir=repo, lane_ids=lane_ids)
        assert "headless lane run" in str(raised.value)
        assert raised.value.code == 7, "refused, not invalid input (C-17.3)"
        assert raised.value.fix
    assert daemon.submits == []


def test_an_ordinary_session_is_not_refused_as_a_lane(home, policy, repo):
    """C-23.31's other half: the refusal must not catch a real session."""
    fx.transcript(home, SESSION, conversation(), cwd=str(repo))
    daemon = fx.FakeSessions()
    result = handoff.handoff(daemon, policy, session_id=SESSION, last=False,
                             model="astra",
                             stage_prompt=lambda text: Path("/dev/null"),
                             workdir=repo, dry_run=True)
    assert result.brief.session_id == SESSION


def test_a_missing_transcript_is_a_user_facing_error(home):
    """C-17.3: exit 2, and the message names the session it could not find."""
    with pytest.raises(handoff.HandoffError, match="transcript not found"):
        handoff.resolve_source(SESSION, False)


def test_a_workdir_that_is_not_a_directory_is_refused(home, tmp_path):
    """C-23.54: the job's workdir is real before it is submitted, not after."""
    path = fx.transcript(home, SESSION, conversation())
    with pytest.raises(handoff.HandoffError, match="not a directory"):
        handoff.resolve_workdir(path, tmp_path / "nope")


@pytest.mark.parametrize("tail_kind", ["sidechain", "tool-result"])
def test_workdir_uses_recorded_cwd_beyond_the_recent_tail(home, repo, tail_kind):
    """C-23.54 and v1 handoff compatibility: long tails do not lose the workdir."""
    task = fx.typed_prompt("continue the work", uuid="p0", at=fx.ago(3600))
    task["cwd"] = str(repo)
    padding = "tool output " * (handoff.LAST_SCAN_BYTES // 12 + 1000)
    if tail_kind == "sidechain":
        tail = fx.assistant_text(padding, uuid="a1", at=fx.ago(60))
        tail["isSidechain"] = True
    else:
        tail = fx.user_tool_result(padding, uuid="r1", at=fx.ago(60))
        tail.pop("cwd")
    path = fx.transcript(home, SESSION, [task, tail], cwd=str(repo))

    assert path.stat().st_size > handoff.LAST_SCAN_BYTES
    assert handoff.resolve_workdir(path, None) == (repo.resolve(), str(repo))


# --- the scrubber, exercised directly -----------------------------------------

def test_the_scrubber_counts_what_it_replaced(home):
    """C-23.36: the brief states its own redaction count, so a reader can tell."""
    text, count = handoff.scrub_secrets(f"key={fx.FAKE_SECRET} and nothing else")
    assert fx.FAKE_SECRET not in text and count >= 1


def test_the_scrubber_leaves_ordinary_prose_and_numbers_alone(home):
    """C-23.14's retention half, at the value level."""
    ordinary = "The importer processed 1284 rows in 3.2s; see docs/migration.md."
    text, count = handoff.scrub_secrets(ordinary)
    assert (text, count) == (ordinary, 0)


def test_a_url_password_is_replaced_but_the_url_survives(home):
    """C-23.14: the connection string is context; the password is not."""
    text, count = handoff.scrub_secrets("psql postgres://app:s3cr3tpw@db.test/main")
    assert "s3cr3tpw" not in text and "postgres://app:" in text and "@db.test/main" in text
    assert count == 1


def test_truncation_keeps_the_head_and_the_tail(home):
    """C-23.36: a truncated section still shows how the work started and ended."""
    body = "START" + "m" * 5000 + "END"
    cut = handoff.truncate(body, 200)
    assert cut.startswith("START") and cut.endswith("END")
    assert len(cut) <= 200 and "characters omitted" in cut


@pytest.mark.parametrize("limit", [0, 1, 12, 13, 30])
def test_tiny_truncation_budget_never_returns_the_whole_section(limit):
    """C-23.36: even a cap smaller than the omission marker is a hard bound."""
    cut = handoff.truncate("ordinary text " * 1000, limit)
    assert len(cut) <= limit
    if limit >= len(handoff.ELIDED):
        assert "omitted" in cut


def test_a_conversations_session_is_refused_rather_than_handed_off(home, policy, repo):
    """C-26.13: the kit does not hand off a conversation's session; its work
    continues in its conversation (a labelled cross-provider handoff is the
    app's, C-30.3). Checked before C-23.31, whose lane reason would mislead."""
    fx.transcript(home, SESSION, fx.conversation_turns(turns=1), cwd=str(repo))
    daemon = fx.FakeSessions(conversation_sessions=[SESSION])
    with pytest.raises(handoff.HandoffError) as raised:
        handoff.handoff(daemon, policy, session_id=SESSION, last=False, model="astra",
                        stage_prompt=lambda text: Path("/dev/null"), workdir=repo,
                        lane_ids=[SESSION], conversation_ids=[SESSION], dry_run=True)
    assert "bound to a Subfleet conversation" in str(raised.value)
    assert raised.value.code == 7
    assert "Subfleet app" in raised.value.fix
    assert daemon.submits == []


def test_scrub_secrets_counts_each_credential_once_whatever_rules_match_it():
    """Property check (seeded): lines that each carry one credential in a context
    two rules may match (a bearer token in an Authorization header, a key in a
    quoted assignment with more text, a key block in JSON) give a count equal to
    the number of credentials, and no credential survives."""
    import random
    from subfleet.sessions.handoff import scrub_secrets
    rng = random.Random(20260925)
    key = lambda n: "sk-ant-api03-" + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(40)) + str(n)
    contexts = [
        lambda s: f"Authorization: Bearer {s}",
        lambda s: f"Authorization: Basic {s}",
        lambda s: f"token = '{s}'",
        lambda s: f'api_key = "{s} and more"',
        lambda s: f"password={s}",
        lambda s: f"curl -H 'X-Key: 1' https://user:{s}@example.com/x",
        lambda s: f'"private_key": "-----BEGIN PRIVATE KEY-----\\n{s}\\n-----END PRIVATE KEY-----\\n"',
        lambda s: f"export SECRET_TOKEN={s}",
    ]
    for trial in range(300):
        secrets = [key(n) for n in range(rng.randint(0, 6))]
        lines = [rng.choice(contexts)(s) for s in secrets]
        lines += ["ordinary text"] * rng.randint(0, 3)
        rng.shuffle(lines)
        text, count = scrub_secrets("\n".join(lines))
        assert count == len(secrets), (trial, lines, text)
        assert not any(s in text for s in secrets), (trial, text)


def test_scrub_secrets_counts_every_value_a_header_held():
    """Review of 5aa2718, finding 1 (seeded property check): a `Cookie` or
    `Authorization` header holding two credentials, one of which an earlier rule
    already replaced, counts two; a header whose only credential an earlier rule
    replaced counts none again; nothing survives."""
    import random
    from subfleet.sessions.handoff import scrub_secrets
    rng = random.Random(925)
    alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    value = lambda: "".join(rng.choice(alnum) for _ in range(15)) + str(rng.randint(0, 9))
    key = lambda: "sk-ant-api03-" + "".join(rng.choice(alnum.lower()) for _ in range(40))
    for trial in range(300):
        known, plain = key(), value()
        shape = rng.choice(["cookie-mixed", "cookie-two-plain", "bearer-only", "already"])
        if shape == "cookie-mixed":
            line, secrets = f"Cookie: session={plain}; refresh={known}", [plain, known]
        elif shape == "cookie-two-plain":
            other = value()
            line, secrets = f"Cookie: a={plain}; b={other}", [plain, other]
        elif shape == "bearer-only":
            line, secrets = f"Authorization: Bearer {known}", [known]
        else:
            line, secrets = f"Cookie: [REDACTED]; session={plain}", [plain]
        text, count = scrub_secrets(line)
        assert count == len(secrets), (trial, line, text, count)
        assert not any(secret in text for secret in secrets), (trial, text)


@pytest.mark.parametrize("line,secret", [
    ("mysql --password hunter2hunter2 db", "hunter2hunter2"),
    ("tool --api-key=abcd1234efgh --token s3cr3t99", "s3cr3t99"),
    ('password = "abc\\"defghijk"', "defghijk"),
    ("deploy --client-secret\tz9y8x7w6v5", "z9y8x7w6v5"),
])
def test_a_credential_given_as_a_flag_or_with_an_escaped_quote_is_scrubbed(line, secret):
    """Review of 5aa2718 (older gaps): a secret passed as a flag's separate
    argument, or holding an escaped quote, was left in the text uncounted."""
    from subfleet.sessions.handoff import scrub_secrets
    text, count = scrub_secrets(line)
    assert secret not in text and count >= 1, (line, text, count)


@pytest.mark.parametrize("line", ["mysql --password\nnext line", "git log --token-limit 5", "cmd --secret-file path"])
def test_a_flag_without_a_value_on_its_line_is_left_alone(line):
    from subfleet.sessions.handoff import scrub_secrets
    assert scrub_secrets(line) == (line, 0)


@pytest.mark.parametrize("request_id,given,minted", [
    (None, None, True), ("operator-rid", None, False),
    ("cli-minted", True, True),            # what `handoff` without --request-id passes
    ("operator-rid", False, False)])
def test_c16_3_handoff_says_whether_it_minted_the_request_id(home, repo, policy, tmp_path,
                                                              request_id, given, minted):
    """C-16.3: a handoff without --request-id mints its id, and says so to the kit; an
    explicit `minted` from the CLI reaches the kit unchanged."""
    fx.transcript(home, SESSION, conversation())
    staged = tmp_path / "prompt.md"
    daemon = fx.FakeSessions()
    extra = {} if given is None else {"minted": given}
    handoff.handoff(daemon, policy, session_id=SESSION, last=False, model="astra",
                    stage_prompt=lambda text: (staged.write_text(text, encoding="utf-8"), staged)[1],
                    workdir=repo, request_id=request_id, **extra)
    assert daemon.minted == [minted]
