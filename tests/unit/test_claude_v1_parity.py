"""C-12.4: the launch line, checked against v1's actual source, not a paraphrase.

`reconstruct_v1_argv` in the adapter encodes what v1 `bin/subfleet-claude` builds.
That encoding is only trustworthy if it is tied to v1's bytes, so these tests read
v1's shell source and rebuild the launch line from it — the flag list, the tool
surface, the isolated branch, and the environment it unsets — then compare.

They skip when the v1 tree is not present, so the suite stays portable; on this Mac
they run, and they are the thing that fails when either side drifts.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from subfleet.adapters.claude import (
    ENV_REMOVE, ClaudeAdapter, reconstruct_v1_argv,
)
from subfleet.contracts import Sandbox

V1_RUNNER = Path.home() / "chief-of-staff" / "subfleet" / "bin" / "subfleet-claude"

pytestmark = pytest.mark.skipif(
    not V1_RUNNER.is_file(), reason=f"the v1 runner is not present at {V1_RUNNER}"
)


@pytest.fixture(scope="module")
def v1_source() -> str:
    return V1_RUNNER.read_text(encoding="utf-8")


def _perm_args_block(source: str) -> str:
    """The `PERM_ARGS` construction, from `if [ "$SANDBOX" = "workspace-write" ]` to
    the `fi` that closes it."""
    start = source.index('if [ "$SANDBOX" = "workspace-write" ]; then')
    end = source.index("\n  fi\n", start)
    return source[start:end]


def _v1_read_only_perm_args(source: str, *, isolated: bool,
                            review_root: str = "/tmp/review") -> list[str]:
    """Rebuild v1's read-only `PERM_ARGS` array by reading its own array literal."""
    block = _perm_args_block(source)
    tools = "Read,Glob,Grep"
    if not isolated:
        # v1: `[ "$ISOLATED" = 1 ] || READ_ONLY_TOOLS+=",WebSearch,WebFetch"`
        appended = re.search(r'READ_ONLY_TOOLS\+="([^"]+)"', block)
        assert appended, "v1 no longer appends the web tools; re-read the runner"
        tools += appended.group(1)

    literal = re.search(r"PERM_ARGS=\(\n(.*?)\n\s*\)\n", block, re.DOTALL)
    assert literal, "v1's read-only PERM_ARGS array literal was not found"
    body = literal.group(1)
    body = body.replace('"$READ_ONLY_TOOLS"', shlex.quote(tools))
    args: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        args.extend(shlex.split(line))
    if isolated:
        # v1: `[ "$ISOLATED" = 0 ] || PERM_ARGS+=(--add-dir "$REVIEW_ROOT")`
        added = re.search(r'PERM_ARGS\+=\(--add-dir "\$REVIEW_ROOT"\)', block)
        assert added, "v1 no longer adds the review root when isolated"
        args += ["--add-dir", review_root]
    return args


def _v1_workspace_write_perm_args(source: str) -> list[str]:
    block = _perm_args_block(source)
    literal = re.search(r"PERM_ARGS=\((--[^)\n]+)\)", block)
    assert literal, "v1's workspace-write PERM_ARGS was not found"
    return shlex.split(literal.group(1))


def test_read_only_permission_args_come_from_v1s_own_array(v1_source):
    """C-12.4 the read-only permission flags are v1's array, in v1's order, with v1's
    tool surface — read out of `bin/subfleet-claude` at test time."""
    assert list(ClaudeAdapter.permission_args(Sandbox.READ_ONLY)) == (
        _v1_read_only_perm_args(v1_source, isolated=False)
    )


def test_isolated_read_only_permission_args_come_from_v1s_own_array(v1_source):
    """C-12.4 the isolated branch drops the web tools and gains the review root,
    exactly as v1's two conditional lines say."""
    assert list(ClaudeAdapter.permission_args(
        Sandbox.READ_ONLY, isolated=True, review_root="/tmp/review"
    )) == _v1_read_only_perm_args(v1_source, isolated=True, review_root="/tmp/review")


def test_workspace_write_permission_args_come_from_v1s_own_array(v1_source):
    """C-12.4 workspace-write is v1's single bypass flag and nothing more."""
    assert list(ClaudeAdapter.permission_args(Sandbox.WORKSPACE_WRITE)) == (
        _v1_workspace_write_perm_args(v1_source)
    )


def test_the_base_launch_line_matches_v1s_apart_from_the_documented_changes(v1_source):
    """C-12.4 v1's launch line is `-p --model M --session-id S --output-format json`
    followed by PERM_ARGS. v2 changes exactly two things: `stream-json` instead of
    `json`, so the `rate_limit_event` is readable, and `--verbose` beside it."""
    launch_line = re.search(
        r'"\$CLAUDE_BIN" -p --model "\$MODEL" \\\n\s*'
        r'--session-id "\$SID" --output-format (\w+) '
        r'\$\{PERM_ARGS\[@\]\+"\$\{PERM_ARGS\[@\]\}"\}',
        v1_source,
    )
    assert launch_line, "v1's launch line has moved; re-read bin/subfleet-claude"
    assert launch_line.group(1) == "json"

    argv = reconstruct_v1_argv("claude", "claude-opus-5", "sid", "read-only")
    assert argv[:7] == (
        "claude", "-p", "--model", "claude-opus-5", "--session-id", "sid",
        "--output-format",
    )
    assert argv[7:9] == ("stream-json", "--verbose")
    assert argv[9:] == ClaudeAdapter.permission_args(Sandbox.READ_ONLY)


def test_v1_unsets_the_same_api_key_variables_this_adapter_removes(v1_source):
    """C-12.4, C-14.4 v1 unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` inside
    the launch subshell; v2 removes exactly those two through `Launch.env_remove`."""
    unset = re.search(r"^\s*unset (ANTHROPIC_[A-Z_ ]+)$", v1_source, re.MULTILINE)
    assert unset, "v1 no longer unsets the Anthropic key variables"
    assert set(unset.group(1).split()) == set(ENV_REMOVE)


def test_v1_pins_the_session_id_it_generates(v1_source):
    """C-12.2, C-12.5 v1 mints a lowercase uuid and passes it as `--session-id`; the
    adapter chooses the same thing up front so the transcript can be found later."""
    assert re.search(r"SID=\$\(uuidgen \| tr '\[:upper:\]' '\[:lower:\]'\)", v1_source)
    adapter = ClaudeAdapter()
    minted = adapter._new_session_id()
    assert minted == minted.lower()
    assert len(minted) == 36 and minted.count("-") == 4
