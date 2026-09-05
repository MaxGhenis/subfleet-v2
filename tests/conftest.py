"""Shared fixture plumbing for the Claude adapter tests.

Every helper here is deliberately small: the adapter must be testable without the
store, the daemon, or the scheduler (they are other lanes' files), so the tests
call adapter methods on fixture directories directly.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from subfleet.contracts import (
    Credential, ExitInfo, JobSpec, Lane, LaneOwner, Launch, Sandbox,
)

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures" / "claude"
FAKE_CLAUDE = TESTS / "bin" / "claude"

#: The instant every fixture-driven assertion is pinned to. Matches `NOW_ISO` in
#: `tests/fixtures/claude/make_fixtures.py`; all experiment-0 reset epochs are later,
#: so a "reported" clock is always in the future and a "guessed" one is unambiguous.
NOW = datetime(2026, 9, 5, 11, 30, 0, tzinfo=timezone.utc)

#: The identity every fixture lane is bound to (C-10.6). Synthetic: no real
#: account's uuids belong in a test fixture, and the one real triple this lane
#: uses is the incident's own, under tests/fixtures/claude/identity/.
LANE_ACCOUNT_UUID = "9f2a1e64-1111-4000-8000-000000000001"
LANE_ORG_UUID = "9f2a1e64-2222-4000-8000-0000000000a1"
LANE_IDENTITY = f"{LANE_ACCOUNT_UUID}:{LANE_ORG_UUID}"
LANE_EMAIL = "max@axiom.org"


def case_names() -> list[str]:
    """Every provider-run fixture: a directory with an `expected.json` beside its
    stream. `identity/` holds profile-endpoint answers, not runs, and is not one."""
    return sorted(p.name for p in FIXTURES.iterdir()
                  if p.is_dir() and (p / "expected.json").is_file())


def load_expected(case: str) -> dict:
    return json.loads((FIXTURES / case / "expected.json").read_text(encoding="utf-8"))


def stage_case(case: str, attempt_dir: Path) -> tuple[Path, int]:
    """Copy one fixture's artifacts into an attempt directory and return its rc."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    source = FIXTURES / case
    shutil.copyfile(source / "stdout", attempt_dir / "stream.jsonl")
    shutil.copyfile(source / "stdout", attempt_dir / "stdout")
    shutil.copyfile(source / "stderr", attempt_dir / "stderr")
    rc = int((source / "rc").read_text(encoding="utf-8").strip())
    return attempt_dir, rc


def stage_transcript(case: str, projects_dir: Path, session_id: str,
                     workdir: str = "/Users/maxghenis/subfleet-v2") -> Path | None:
    """Place the fixture's transcript where the adapter will look for it."""
    from subfleet.adapters.claude import encode_project_dir

    source = FIXTURES / case / "transcript.jsonl"
    if not source.is_file():
        return None
    target_dir = projects_dir / encode_project_dir(workdir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{session_id}.jsonl"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def make_lane(lane_id: str = "claude-1", account: str = LANE_EMAIL, *,
              identity: str | None = LANE_IDENTITY, label: str | None = None) -> Lane:
    """A bound Claude lane (C-10.6). `identity=None` gives a lane that records
    nothing, which C-10.6 can only call unverified."""
    return Lane(
        lane_id=lane_id,
        provider="claude",
        account_key=f"claude:{account}",
        credential=Credential(provider="claude", ref=f"claude-quota-{account}",
                              kind="keychain-token"),
        home=None,
        owner=LaneOwner.V2,
        desktop=False,
        identity=identity,
        label=label if label is not None else account,
    )


def make_job(workdir: str, prompt_path: str, *,
             sandbox: Sandbox = Sandbox.READ_ONLY,
             pinned_model: str | None = None) -> JobSpec:
    return JobSpec(
        request_id="req-test",
        kind="dispatch",
        workdir=workdir,
        prompt_path=prompt_path,
        task="review",
        tier="standard",
        pinned_model=pinned_model,
        pinned_lane=None,
        sandbox=sandbox,
        out_path=None,
        name="claude-adapter-test",
    )


def make_launch(attempt_dir: Path, *, session_id: str, model_id: str,
                lane_id: str = "claude-1", attempt_id: str = "job-1/a1",
                projects_dir: Path | None = None,
                transcript_offset: int = 0,
                identity: str | None = LANE_IDENTITY,
                label: str | None = LANE_EMAIL,
                workdir: str = "/Users/maxghenis/subfleet-v2") -> Launch:
    """A Launch shaped exactly as `build_launch` produces one, without spawning."""
    notes = {
        "lane_id": lane_id,
        "account_key": "claude:max@axiom.org",
        "identity": identity,      # C-10.6
        "label": label,
        "attempt_id": attempt_id,
        "model_id": model_id,
        "session_id": session_id,
        "workdir": workdir,
        "sandbox": "read-only",
        "transcript_offset": transcript_offset,
    }
    if projects_dir is not None:
        notes["projects_dir"] = str(projects_dir)
    return Launch(
        argv=("claude", "-p", "--model", model_id, "--session-id", session_id,
              "--output-format", "stream-json", "--verbose"),
        env_add={"CLAUDE_CODE_OAUTH_TOKEN": "REDACTED"},
        env_remove=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        cwd=workdir,
        stdin_path=str(attempt_dir / "prompt.sent.md"),
        stdout_path=str(attempt_dir / "stdout"),
        stderr_path=str(attempt_dir / "stderr"),
        raw_stream_path=str(attempt_dir / "stream.jsonl"),
        native_session_id=session_id,
        notes=notes,
    )


def exit_info(rc: int, *, wall_s: float = 4.2, spawn_error: str | None = None) -> ExitInfo:
    return ExitInfo(rc=rc, signal=None, wall_s=wall_s, child_pid=4242,
                    spawn_error=spawn_error)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The profile endpoint (C-10.6) is the one network call subfleet makes, and
    no test may make it.

    The stand-in answers for the fixture lane `make_lane` builds, so an ordinary
    adapter test sees a verified identity without knowing this exists. A test
    about identity passes `profile_opener(...)` to the adapter it builds, which
    takes precedence, and asserts on what it chose.
    """
    monkeypatch.setattr("subfleet.adapters.claude._urlopen", profile_opener())


@pytest.fixture(autouse=True)
def no_desktop_login(monkeypatch):
    """`~/.claude.json` belongs to whoever runs the tests, and the desktop app's
    keychain item is their real credential. No test reads either by accident
    (C-10.3, C-10.5): a test about desktop identity says which login it means."""
    monkeypatch.setattr("subfleet.capacity.read_desktop_account", lambda path=None: None)


def profile_body(email: str = LANE_EMAIL, account_uuid: str = LANE_ACCOUNT_UUID,
                 org_uuid: str = LANE_ORG_UUID) -> bytes:
    """A profile payload shaped as Claude Code's own profile loader reads it."""
    return json.dumps({"account": {"email": email, "uuid": account_uuid},
                       "organization": {"uuid": org_uuid}}).encode("utf-8")


def profile_opener(status: int = 200, body: bytes | None = None, *,
                   error: Exception | None = None, seen: list | None = None):
    """An injectable `(status, body)` opener, recording the URLs it was asked for."""
    def opener(request, timeout):
        if seen is not None:
            seen.append((request.full_url, request.headers.get("Authorization"), timeout))
        if error is not None:
            raise error
        return status, (profile_body() if body is None else body)

    return opener


@pytest.fixture
def frozen_now() -> datetime:
    return NOW


@pytest.fixture
def adapter(tmp_path):
    """A `ClaudeAdapter` with a pinned clock, a pinned session id, and a projects
    directory inside the test's own tree so nothing touches `~/.claude`."""
    from subfleet.adapters.claude import ClaudeAdapter

    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    return ClaudeAdapter(
        claude_bin=str(FAKE_CLAUDE),
        now=lambda: NOW,
        new_session_id=lambda: "00000000-0000-4000-8000-000000000000",
        projects_dir=projects,
    )
