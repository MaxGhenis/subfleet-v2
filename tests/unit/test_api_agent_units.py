"""Unit and property tests for the API credit lanes' harness (C-31.7 to C-31.16).

Invariants stated for the harness (docs/reports/credit-lane-harness-report.md):
- I2 (no identity prompt): every system prompt the harness can send holds no identity
  line, "Claude Code" or "Agent SDK" after case folding and whitespace normalization.
- Pricing: `usd` is 0 for no tokens, never negative, monotone in every count, linear
  over a response's compaction iterations, and agrees with a reference formula.
- Redaction: for any secret and any text, the redacted text holds neither the secret
  nor any 12-character run of it, and text without a run passes unchanged.
- Hooks fail closed: any output that is not an allow decision denies.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.api_agent import cli, hooks, pricing, prompt, sandbox
from subfleet.api_agent.loop import UNKNOWN_TEXT, resume_history
from subfleet.api_agent.records import RUN, HistoryError, Records, Redactor, load_history

REPO = Path(__file__).resolve().parents[2]
COUNTS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
tokens = st.integers(min_value=0, max_value=5_000_000)
usages = st.fixed_dictionaries({name: tokens for name in COUNTS})
#: The repo's usual property settings: no deadline, no too-slow check (shared, loaded hosts).
PROPERTY = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=50)


# -- the SDK stays inside the harness ------------------------------------------------------------------
def test_nothing_outside_the_harness_imports_anthropic():
    """C-31.7: only subfleet/api_agent/ imports `anthropic`, and inside it only client.py."""
    offenders = []
    for root in ("subfleet", "tools", "tests", "app"):
        for path in (REPO / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                if any(name == "anthropic" or name.startswith("anthropic.") for name in names):
                    relative = path.relative_to(REPO).as_posix()
                    if relative != "subfleet/api_agent/client.py":
                        offenders.append(relative)
    assert offenders == []


# -- invariant 2 over every system prompt ------------------------------------------------------------
@pytest.mark.parametrize("sandbox_mode", cli.SANDBOXES)
@pytest.mark.parametrize("network", [False, True])
def test_every_system_prompt_is_identity_free(sandbox_mode, network):
    blocks = prompt.system_blocks(sandbox_mode, network)
    assert prompt.identity_free(blocks)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "claude" not in prompt.normalize(blocks[0]["text"])


@PROPERTY
@given(st.text(), st.sampled_from(prompt.FORBIDDEN), st.text())
def test_identity_check_catches_every_spelling(before, phrase, after):
    """The check itself: any case and whitespace spelling of a phrase is caught."""
    spelled = "".join(char.upper() if index % 2 else char for index, char in enumerate(phrase))
    spelled = spelled.replace(" ", " \n\t ")
    assert not prompt.identity_free([{"type": "text", "text": before + spelled + after}])
    assert not prompt.identity_free(before + spelled + after)


def test_tools_are_the_two_client_tools_with_a_cache_breakpoint():
    tools = prompt.tools()
    assert [(tool["type"], tool["name"]) for tool in tools] == [
        ("bash_20250124", "bash"), ("text_editor_20250728", "str_replace_based_edit_tool")]
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("input_schema" not in tool and "eager_input_streaming" not in tool for tool in tools)


# -- pricing ----------------------------------------------------------------------------------------
def reference(model: str, usage: dict) -> float:
    rates = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
             "claude-haiku-5-5": (0.10, 0.50, 0.01)}[model]
    prompt_tokens = usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
    if model == "claude-haiku-5-5" and prompt_tokens > 100_000:
        rates = (0.50, 2.50, 0.05)
    return (usage["input_tokens"] * rates[0] + usage["output_tokens"] * rates[1]
            + usage["cache_read_input_tokens"] * rates[2]
            + usage["cache_creation_input_tokens"] * rates[0] * 1.25) / 1_000_000


@pytest.mark.parametrize("model", sorted(pricing.PRICES))
def test_interface_prices(model):
    one_million_each = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    expected = {"claude-opus-5-5": 24.0, "claude-sonnet-5-5": 12.0, "claude-haiku-5-5": 3.0}[model]
    assert pricing.usd(model, one_million_each) == pytest.approx(expected)
    small = {"input_tokens": 1_000, "output_tokens": 1_000}
    assert pricing.usd(model, small) == pytest.approx(
        {"claude-opus-5-5": 0.024, "claude-sonnet-5-5": 0.012, "claude-haiku-5-5": 0.0006}[model])


@PROPERTY
@given(st.sampled_from(sorted(pricing.PRICES)), usages)
def test_usd_agrees_with_the_reference_and_is_not_negative(model, usage):
    value = pricing.usd(model, usage)
    assert value >= 0
    assert value == pytest.approx(reference(model, usage), rel=1e-9, abs=1e-12)


@PROPERTY
@given(st.sampled_from(sorted(pricing.PRICES)), usages, st.sampled_from(COUNTS), st.integers(1, 1_000_000))
def test_usd_is_monotone_in_each_count(model, usage, name, more):
    bigger = dict(usage, **{name: usage[name] + more})
    # Haiku's long-prompt tier is a step up, so more tokens never cost less either way.
    assert pricing.usd(model, bigger) >= pricing.usd(model, usage)


@PROPERTY
@given(st.sampled_from(sorted(pricing.PRICES)), usages, st.lists(usages, max_size=4))
def test_compaction_iterations_add(model, usage, iterations):
    with_iterations = dict(usage, iterations=[dict(item, type="compaction") for item in iterations]
                           + [dict(usage, type="message")])
    expected = pricing.usd(model, usage) + sum(pricing.usd(model, item) for item in iterations)
    assert pricing.usd(model, with_iterations) == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_zero_and_missing_counts_cost_nothing():
    for model in pricing.PRICES:
        assert pricing.usd(model, {}) == 0
        assert pricing.usd(model, {"input_tokens": None, "output_tokens": -5}) == 0
    assert pricing.four({"input_tokens": 3, "cache_read_input_tokens": None}) == {
        "input_tokens": 3, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def test_one_hour_cache_writes_cost_twice_input():
    usage = {"cache_creation_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 0}}
    assert pricing.usd("claude-opus-5-5", usage) == pytest.approx(8.0)


# -- redaction --------------------------------------------------------------------------------------
secrets_ = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_", min_size=1,
                   max_size=120)


@PROPERTY
@given(secrets_, st.text(max_size=200), st.text(max_size=200), st.integers(0, 200), st.integers(0, 200))
def test_redaction_leaves_no_run(secret, before, after, start, length):
    piece = secret[start:start + max(length, RUN)]
    redacted = Redactor(secret)(before + piece + after + secret)
    assert secret not in redacted
    if len(secret) >= RUN:
        assert not any(secret[index:index + RUN] in redacted for index in range(len(secret) - RUN + 1))


@PROPERTY
@given(secrets_, st.text(alphabet="!@#$%^&*() \n.,", max_size=200))
def test_redaction_keeps_text_without_runs(secret, other):
    assert Redactor(secret)(other) == other


def test_redactor_never_shows_its_secret():
    secret = "sk-ant-api03-" + "Q" * 60
    redactor = Redactor(secret)
    assert secret[:RUN] not in repr(redactor) and secret[:RUN] not in str(redactor)
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(redactor)


# -- records ----------------------------------------------------------------------------------------
def test_records_lines_are_versioned_utc_and_redacted(tmp_path):
    secret = "sk-ant-api03-" + "Z" * 40
    records = Records(tmp_path, Redactor(secret))
    try:
        records.event("error", status=None, error_type="x", message=f"oops {secret[5:30]}")
    finally:
        records.close()
    line = json.loads((tmp_path / "stream.jsonl").read_text())
    assert line["v"] == 1 and line["type"] == "error" and line["ts"].endswith("Z")
    assert "[redacted]" in line["message"]
    assert (tmp_path / "last.md").read_text() == ""


def test_load_history_cuts_a_torn_last_line_and_refuses_other_damage(tmp_path):
    path = tmp_path / "messages.jsonl"
    whole = json.dumps({"role": "user", "content": "hi"}) + "\n"
    path.write_text(whole + '{"role": "assis')
    assert load_history(path) == [{"role": "user", "content": "hi"}]
    assert path.read_text() == whole
    path.write_text(whole + "not json\n")
    with pytest.raises(HistoryError):
        load_history(path)
    path.write_text(json.dumps({"role": "system", "content": "x"}) + "\n")
    with pytest.raises(HistoryError):
        load_history(path)
    assert load_history(tmp_path / "missing.jsonl") == []


def test_resume_answers_unfinished_tool_calls():
    history = [{"role": "user", "content": [{"type": "text", "text": "job"}]},
               {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash",
                                                  "input": {"command": "ls"}},
                                                 {"type": "tool_use", "id": "t2", "name": "bash",
                                                  "input": {"command": "pwd"}}]}]
    tail = resume_history(history)
    assert [block["tool_use_id"] for block in tail[0]["content"]] == ["t1", "t2"]
    assert all(block["is_error"] and block["content"] == UNKNOWN_TEXT for block in tail[0]["content"])
    assert resume_history(history[:1]) == []
    text_only = history[:1] + [{"role": "assistant", "content": [{"type": "text", "text": "done?"}]}]
    assert resume_history(text_only)[0]["role"] == "user"


# -- argv -------------------------------------------------------------------------------------------
BASE_ARGV = ["--attempt-dir", "/a", "--workdir", "/w", "--prompt", "/p", "--model", "claude-opus-5-5",
             "--effort", "high", "--sandbox", "workspace-write", "--max-usd", "25", "--max-turns", "400"]


def test_argv_parses_the_interface():
    options = cli.parse([*BASE_ARGV, "--network", "--guard-hook", "/g", "--publish-hook", "/h", "--resume"])
    assert options.max_usd == 25.0 and options.max_turns == 400 and options.network and options.resume
    assert options.guard_hook == Path("/g") and options.publish_hook == Path("/h")
    plain = cli.parse(BASE_ARGV)
    assert not plain.network and plain.guard_hook is None and not plain.resume


@pytest.mark.parametrize("change", [
    ["--effort", "extreme"], ["--sandbox", "danger-full-access"], ["--max-usd", "nan"], ["--max-usd", "-1"],
    ["--max-turns", "-1"], ["--workdir", "relative"], ["--unknown"], ["--max-u", "3"]])
def test_argv_refuses_what_the_interface_does_not_allow(change):
    with pytest.raises(cli.UsageError):
        cli.parse([*BASE_ARGV, *change])


# -- the endpoint -----------------------------------------------------------------------------------
def test_endpoint_is_api_anthropic_com_whatever_the_environment_says(monkeypatch):
    from subfleet.api_agent import client
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/elsewhere")
    assert client.endpoint(None) == "https://api.anthropic.com"
    assert client.endpoint("") == "https://api.anthropic.com"
    # Outside a test session (no session markers), the override is ignored.
    assert client.endpoint("http://127.0.0.1:8123", environ={}) == "https://api.anthropic.com"
    # Inside one, only a loopback address is honored.
    assert client.endpoint("http://127.0.0.1:8123") == "http://127.0.0.1:8123"
    for remote in ("http://example.com:8123", "http://user:pw@127.0.0.1:1", "ftp://127.0.0.1:1",
                   "http://127.0.0.1:notaport"):
        assert client.endpoint(remote) == "https://api.anthropic.com"


def test_client_ignores_anthropic_base_url(monkeypatch):
    from subfleet.api_agent import client
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    api = client.Api("sk-ant-api03-test-only", client.endpoint(None))
    try:
        assert str(api._client.base_url).rstrip("/") == "https://api.anthropic.com"
    finally:
        api.close()


# -- hooks ------------------------------------------------------------------------------------------
def _hook(tmp_path: Path, body: str) -> hooks.Hook:
    path = tmp_path / "hook.sh"
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return hooks.Hook(path, "guard", {"PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize("body,allowed", [
    ("cat >/dev/null; exit 0", True),
    ("cat >/dev/null; echo '{\"hookSpecificOutput\":{\"permissionDecision\":\"allow\"}}'", True),
    ("cat >/dev/null; echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":"
     "\"deny\",\"permissionDecisionReason\":\"[rule] no\"}}'", False),
    ("cat >/dev/null; echo nope >&2; exit 2", False),
    ("cat >/dev/null; exit 1", False),
    ("cat >/dev/null; echo 'not json'", False),
    ("cat >/dev/null; echo '{\"hookSpecificOutput\":{\"permissionDecision\":\"ask\"}}'", False),
    ("cat >/dev/null; echo '{\"decision\":\"block\",\"reason\":\"old style\"}'", False),
])
def test_hooks_decide_and_fail_closed(tmp_path, body, allowed):
    verdict = _hook(tmp_path, body).check(hooks.payload("Bash", "ls", str(tmp_path), "t1", "m"), str(tmp_path))
    assert verdict.allowed is allowed
    if not allowed:
        assert verdict.reason


def test_hook_gets_codex_input_shape(tmp_path):
    seen = tmp_path / "seen.json"
    hook = _hook(tmp_path, f"cat > {seen}")
    hook.check(hooks.payload("Bash", "git status", "/somewhere", "toolu_1", "claude-opus-5-5"), str(tmp_path))
    call = json.loads(seen.read_text())
    assert call["tool_name"] == "Bash" and call["tool_input"] == {"command": "git status"}
    assert call["cwd"] == "/somewhere" and call["hook_event_name"] == "PreToolUse"


def test_hook_that_cannot_run_denies(tmp_path):
    hook = hooks.Hook(tmp_path / "missing", "guard", {})
    assert not hook.check(hooks.payload("Bash", "ls", "/", "t", "m"), "/").allowed
    assert hooks.validate(tmp_path / "missing")


def test_patch_text_is_apply_patch_shaped():
    created = hooks.patch_text("/w/a.py", "x = 1\ny = 2\n")
    assert created.splitlines() == ["*** Begin Patch", "*** Add File: /w/a.py", "+x = 1", "+y = 2",
                                    "*** End Patch"]
    updated = hooks.patch_text("/w/a.py", "new\n", "old\n")
    assert "*** Update File: /w/a.py" in updated and "-old" in updated and "+new" in updated


# -- the tool environment ---------------------------------------------------------------------------
@PROPERTY
@given(st.dictionaries(st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True), st.text(max_size=8),
                       max_size=12))
def test_tool_environment_has_no_credential_name(environment):
    launch = dict(environment, ANTHROPIC_API_KEY="k", MY_TOKEN="t", db_password="p", BASH_ENV="/x",
                  PATH="/usr/bin")
    env = sandbox.tool_environment(launch, Path("/tmp/x"))
    for name in env:
        upper = name.upper()
        assert not any(word in upper for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")), name
    assert "BASH_ENV" not in env and env["TMPDIR"] == "/tmp/x" and env["PATH"] == "/usr/bin"


def test_layout_writes(tmp_path):
    work, attempt = tmp_path / "w", tmp_path / "a"
    (work / ".git").mkdir(parents=True)
    attempt.mkdir()
    (attempt / "stdout").write_text("")
    (attempt / "old-notes").mkdir()
    layout = sandbox.layout(workdir=work, attempt=attempt, sandbox=cli.WORKSPACE_WRITE, network=False,
                            environ={"HOME": str(tmp_path)})
    real = Path(os.path.realpath(tmp_path))
    assert layout.writable(real / "w" / "src" / "x.py")
    assert not layout.writable(real / "w" / ".git" / "config")
    assert not layout.writable(real / "a" / "stream.jsonl")
    assert not layout.writable(real / "a" / "stdout")
    assert not layout.writable(real / "a" / "old-notes" / "x")
    assert not layout.writable(real / "a" / "harness" / "command-00001.sh")
    assert layout.writable(real / "a" / "tmp" / "scratch")
    assert layout.writable(real / "a" / "new-file")
    assert not layout.writable(real / "elsewhere")
    text = sandbox.profile(layout)
    assert "com.apple.SecurityServer" not in text and "network-outbound (remote ip" not in text
    readonly = sandbox.layout(workdir=work, attempt=attempt, sandbox=cli.READ_ONLY, network=True,
                              environ={"HOME": str(tmp_path)})
    assert not readonly.writable(real / "w" / "x") and readonly.writable(real / "a" / "tmp" / "x")
    assert "network-outbound (remote ip" in sandbox.profile(readonly)


# -- the exec-time environment block ------------------------------------------------------------
EXEC_BLOCK_CHILD = r"""
import ctypes, json, os, sys
from subfleet.api_agent import exec_env

def block():
    libc = ctypes.CDLL(None, use_errno=True)
    size = ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
    buffer = ctypes.create_string_buffer(size.value)
    name = (ctypes.c_int * 3)(1, 49, os.getpid())  # CTL_KERN, KERN_PROCARGS2
    assert libc.sysctl(name, 3, buffer, ctypes.byref(size), None, 0) == 0, ctypes.get_errno()
    return buffer.raw[:size.value].split(b"\0")

before = block()
count = exec_env.overwrite(lambda name: name.endswith("_SECRET_KEY"))
after = block()
print(json.dumps({"count": count, "before": [s.decode("utf-8", "replace") for s in before],
                  "after": [s.decode("utf-8", "replace") for s in after],
                  "python": os.environ["SF_ONE_SECRET_KEY"]}))
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the block with sysctl KERN_PROCARGS2")
def test_overwrite_hides_secret_values_from_the_exec_block_and_keeps_it_whole():
    """C-31.12: every named value becomes `*`s of the same length in the block the
    kernel copied at exec; every other string, and Python's own copy, is unchanged."""
    secrets_ = {"SF_ONE_SECRET_KEY": "sk-ant-api03-one-" + "A" * 40, "SF_TWO_SECRET_KEY": "two=has=equals",
                "SF_EMPTY_SECRET_KEY": ""}
    plain = {"SUBFLEET_ATTEMPT": "job-1/a1", "SF_SECRET_KEY_PREFIXED_NOT": "kept", "SF_UNICODE": "café"}
    env = {"PATH": "/usr/bin:/bin", **secrets_, **plain}
    proc = subprocess.run([sys.executable, "-I", "-c", EXEC_BLOCK_CHILD], env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    found = json.loads(proc.stdout)
    assert found["count"] == 2  # the empty value has nothing to overwrite
    assert found["python"] == secrets_["SF_ONE_SECRET_KEY"]
    for name, value in secrets_.items():
        assert f"{name}={value}" in found["before"]
        assert f"{name}={'*' * len(value)}" in found["after"]
        if value:
            assert f"{name}={value}" not in found["after"] and value not in "\n".join(found["after"])
    for name, value in plain.items():
        assert f"{name}={value}" in found["after"]
    assert len(found["after"]) == len(found["before"])


# -- the key's one way in: the --key-fd pipe (C-31.17) ---------------------------------------------------
def _pipe_holding(data: bytes) -> int:
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, data)
    finally:
        os.close(write_end)
    return read_end


def _closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


@PROPERTY
@given(st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=1, max_size=4096))
def test_read_key_returns_the_framed_key_and_closes_the_descriptor(key):
    """C-31.17: any printable-ASCII key of 1 to 4096 bytes comes back unchanged, and
    the descriptor is closed after the one read."""
    from subfleet.api_agent.harness import read_key
    fd = _pipe_holding(len(key).to_bytes(4, "big") + key.encode("ascii"))
    try:
        assert read_key(fd) == (key, None)
        assert _closed(fd)
    finally:
        if not _closed(fd):
            os.close(fd)


@pytest.mark.parametrize("data", [b"", b"\x00", bytes(4), (4097).to_bytes(4, "big") + b"k" * 80,
                                  (30).to_bytes(4, "big") + b"sk-ant-api03-cut",
                                  (4).to_bytes(4, "big") + b"\xff\xfe\xfd\xfc",
                                  (4).to_bytes(4, "big") + b"a\tbc"])
def test_read_key_refuses_a_bad_frame_closes_and_never_raises(data):
    """C-31.17: end-of-file, a short read, a zero or oversized length, or bytes that are
    not printable ASCII give no key and a reason holding none of the frame's bytes;
    the descriptor is closed either way."""
    from subfleet.api_agent.harness import read_key
    fd = _pipe_holding(data)
    key, problem = read_key(fd)
    assert key is None and problem.startswith("no key arrived: ")
    assert _closed(fd)
    if len(data) > 4:
        assert data[4:].decode("latin-1") not in problem


def test_read_key_without_a_descriptor_or_with_a_closed_one(tmp_path):
    from subfleet.api_agent.harness import read_key
    assert read_key(None) == (None, "no key arrived: the launch named no --key-fd")
    fd = _pipe_holding(b"")
    os.close(fd)
    key, problem = read_key(fd)
    assert key is None and problem.startswith("no key arrived: --key-fd could not be read")
    held = tmp_path / "file"
    held.write_bytes((3).to_bytes(4, "big") + b"abc")
    fd = os.open(held, os.O_RDONLY)
    assert read_key(fd) == (None, "no key arrived: --key-fd is not a pipe")
    assert _closed(fd)


@pytest.mark.parametrize("fd", ["0", "1", "2", "-1"])
def test_key_fd_must_be_above_the_standard_streams(fd):
    with pytest.raises(cli.UsageError):
        cli.parse(["--attempt-dir", "/a", "--workdir", "/w", "--prompt", "/p", "--model", "m", "--effort", "high",
                   "--sandbox", "read-only", "--max-usd", "1", "--max-turns", "1", "--key-fd", fd])


def test_the_harness_reads_no_key_from_its_environment():
    """C-31.17, mutant (c): no module of the harness names ANTHROPIC_API_KEY as a value
    to read (`os.environ[...]`, `.get(...)`, `.pop(...)`, `getenv(...)`); the process
    tests check the same at run time (`test_the_key_comes_only_from_the_key_fd`)."""
    offenders = []
    for path in sorted((REPO / "subfleet" / "api_agent").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Call, ast.Subscript)):
                continue
            args = node.args if isinstance(node, ast.Call) else [node.slice]
            if any(isinstance(arg, ast.Constant) and arg.value == "ANTHROPIC_API_KEY" for arg in args):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
