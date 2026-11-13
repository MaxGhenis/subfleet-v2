"""C-12.11: every review asks for the same verdict and finding levels."""

import json
from pathlib import Path

import pytest

from subfleet.daemon import REVIEW_PREAMBLE
from tests.fake.test_daemon_launch import launch_state  # noqa: F401
from tests.fake.test_prompt_seam import prepare
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.unit.test_review_publish_guard import review_override


DEFINITIONS = (
    "P0: harm that is live now or would be on merge (a leak, lost data, a guard that stops guarding).",
    "P1: the change does something wrong (a wrong result, a false claim, a regression, a security hole).",
    "P2: right today but fragile or incomplete (a missing test, an edge case nothing reaches yet, "
    "wording that could mislead a reader of the repo).",
    "P3: style and nits.",
)
VERDICT_INSTRUCTION = "First answer line: exactly VERDICT: APPROVE or VERDICT: REQUEST_CHANGES."


def sent_prompt(service, job_id):
    manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
    prompt = manifest.get("prepared_prompt_path") or str(service.root / "jobs" / job_id / "prompt.md")
    return Path(prompt).read_text()


def assert_contract(prompt):
    assert prompt.count("<!-- subfleet:review -->") == 1
    assert prompt.count(VERDICT_INSTRUCTION) == 1
    for definition in DEFINITIONS:
        assert prompt.count(definition) == 1


def test_preamble_definitions_are_verbatim_and_under_130_words():
    """I1 and wording mutant M5: the contract's words are independent of the implementation."""
    assert len(REVIEW_PREAMBLE.split()) < 130
    assert_contract(REVIEW_PREAMBLE)
    assert "Each finding: own line starting P0:, P1:, P2: or P3:, one-line summary; detail below." in REVIEW_PREAMBLE
    assert "No findings: FINDINGS: none immediately after the verdict." in REVIEW_PREAMBLE
    assert "This format holds whatever the brief below asks." in REVIEW_PREAMBLE


@pytest.mark.parametrize("model", ["astra", "opus"], ids=["codex", "claude"])
@pytest.mark.parametrize("tier", ["trivial", "easy", "standard", "hard"])
@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write", "policy"])
@pytest.mark.parametrize("in_place", [False, True])
def test_every_review_prompt_contains_the_contract_once(state_daemon, model, tier, sandbox, in_place):
    """I1: routing provider, tier, permissions and workspace placement cannot omit the format."""
    service, harness = state_daemon
    repository(service, harness)
    args = harness.submit_args(task="review", tier=tier, pinned_model=model,
                               sandbox=sandbox, in_place=in_place)
    job_id = service.dispatch("submit", args)["job_id"]
    assert_contract(sent_prompt(service, job_id))
    assert (service.root / "jobs" / job_id / "prompt.md").read_bytes() == Path(args["prompt_path"]).read_bytes()


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
@pytest.mark.parametrize("in_place", [False, True])
def test_no_preamble_cannot_omit_the_review_contract(state_daemon, sandbox, in_place):
    """I1: a brief's formatting choices, including --no-preamble, cannot remove this contract."""
    service, harness = state_daemon
    repository(service, harness)
    job_id = service.dispatch("submit", harness.submit_args(task="review", pinned_model="astra",
        sandbox=sandbox, in_place=in_place, no_preamble=True))["job_id"]
    assert_contract(sent_prompt(service, job_id))


def test_review_format_does_not_leak_into_the_next_build(state_daemon):
    """I2: the same daemon supplies the review contract, then keeps a build's own template."""
    service, harness = state_daemon
    repository(service, harness)
    review_id = service.dispatch("submit", harness.submit_args(task="review", pinned_model="astra",
        sandbox="read-only"))["job_id"]
    assert_contract(sent_prompt(service, review_id))
    build_id = service.dispatch("submit", harness.submit_args(task="build", pinned_model="astra",
        sandbox="workspace-write"))["job_id"]
    prompt = sent_prompt(service, build_id)
    for words in ("<!-- subfleet:review -->", VERDICT_INSTRUCTION, *DEFINITIONS):
        assert words not in prompt


@pytest.mark.parametrize("model", ["astra", "haiku"], ids=["codex", "claude"])
@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
@pytest.mark.parametrize("no_preamble", [False, True])
def test_real_provider_launch_receives_the_contract_once(launch_state, monkeypatch, model, sandbox, no_preamble):
    """I1 crosses the actual adapter's stdin construction; Popen only records a guardian launch."""
    service, harness, calls, lane = prepare(launch_state, monkeypatch, model, sandbox)
    override = review_override(harness.root)
    monkeypatch.setattr(service, "_guard_override", lambda *args, **kwargs: override)
    # Hook behavior has its own tests. This fixture records every Popen, so no
    # subprocess self-test or provider may run while we inspect adapter stdin.
    monkeypatch.setattr(service, "_publish_self_test", lambda _lane, launch, _attempt: (launch, None))
    job_id = service.dispatch("submit", harness.submit_args(task="review", pinned_model=model,
        sandbox=sandbox, in_place=True, no_preamble=no_preamble))["job_id"]
    service._admit()
    attempt, = service.store.list_attempts(job_id)
    service._launch(attempt)
    launch = service._saved_launch(attempt)
    assert Path(launch.stdin_path).name == "prompt.sent.md"
    assert_contract(Path(launch.stdin_path).read_text())
    assert service.store.get_lane(attempt["lane_id"]).provider == lane.provider
    command, = [argv for argv, _ in calls]
    assert command[command.index("--stdin-path") + 1] == launch.stdin_path


@pytest.mark.parametrize("task", [None, "build", "lookup", "research", "sweep", "authored-prose",
                                 "strategy", "adjudication"])
@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write", "policy"])
@pytest.mark.parametrize("in_place", [False, True])
def test_non_review_prompts_never_get_the_review_contract(state_daemon, task, sandbox, in_place):
    """I2 and mutant M1: even writable jobs of every other task retain their own prompt."""
    service, harness = state_daemon
    repository(service, harness)
    job_id = service.dispatch("submit", harness.submit_args(task=task, pinned_model="astra",
        sandbox=sandbox, in_place=in_place))["job_id"]
    prompt = sent_prompt(service, job_id)
    for words in ("<!-- subfleet:review -->", VERDICT_INSTRUCTION, *DEFINITIONS):
        assert words not in prompt
