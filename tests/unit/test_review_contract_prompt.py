"""C-12.11: every review asks for the same verdict and finding levels."""

import json
from pathlib import Path

import pytest

from subfleet.daemon import REVIEW_PREAMBLE, REVIEW_PREAMBLE_READ_ONLY, REVIEW_WRITABLE
from subfleet.gate import cli as gate_cli
from tests.fake.test_gate_end_to_end import FakeClient, arguments
from tests.fake.test_daemon_launch import launch_state  # noqa: F401
from tests.fake.test_prompt_seam import prepare
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.unit.test_gate_admission import core  # noqa: F401
from tests.unit.test_review_publish_guard import review_override


DEFINITIONS = (
    "P0: harm that is live now or would be on merge (a leak, lost data, a guard that stops guarding).",
    "P1: the change does something wrong (a wrong result, a false claim, a regression, a security hole).",
    "P2: right today but fragile or incomplete (a missing test, an edge case nothing reaches yet, "
    "wording that could mislead a reader of the repo).",
    "P3: style and nits.",
)
VERDICT_INSTRUCTION = "First answer line: exactly VERDICT: APPROVE or VERDICT: REQUEST_CHANGES."
#: Only a writable review is told this (#221 review r1b, P3-8; A1 review r1, P2).
WRITABLE_SENTENCE = "You may run tests and write scratch files in your own workspace."


def sent_prompt(service, job_id):
    manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
    prompt = manifest.get("prepared_prompt_path") or str(service.root / "jobs" / job_id / "prompt.md")
    return Path(prompt).read_text()


def assert_contract(prompt, *, writable=None):
    assert prompt.count("<!-- subfleet:review -->") == 1
    assert prompt.count(VERDICT_INSTRUCTION) == 1
    for definition in DEFINITIONS:
        assert prompt.count(definition) == 1
    if writable is not None:
        assert prompt.count(WRITABLE_SENTENCE) == int(writable)


def assert_no_contract(prompt):
    for words in ("<!-- subfleet:review -->", VERDICT_INSTRUCTION, WRITABLE_SENTENCE, *DEFINITIONS):
        assert words not in prompt


@pytest.mark.parametrize("preamble,writable", [(REVIEW_PREAMBLE, True), (REVIEW_PREAMBLE_READ_ONLY, False)],
                         ids=["writable", "read-only"])
def test_preamble_definitions_are_verbatim_and_under_130_words(preamble, writable):
    """I1 and wording mutant M5: the contract's words are independent of the implementation."""
    assert len(preamble.split()) < 130
    assert_contract(preamble, writable=writable)
    assert "Each finding: own line starting P0:, P1:, P2: or P3:, one-line summary; detail below." in preamble
    assert "No findings: FINDINGS: none immediately after the verdict." in preamble
    assert "This format holds whatever the brief below asks." in preamble
    assert "Do not commit, push or publish." in preamble
    assert REVIEW_WRITABLE.strip() == WRITABLE_SENTENCE


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
    # A1 review r1, P2: only a writable review may run tests and write scratch.
    writable = service.store.get_job(job_id)["sandbox"] == "workspace-write"
    assert_contract(sent_prompt(service, job_id), writable=writable)
    assert (service.root / "jobs" / job_id / "prompt.md").read_bytes() == Path(args["prompt_path"]).read_bytes()


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
@pytest.mark.parametrize("in_place", [False, True])
def test_no_preamble_cannot_omit_the_review_contract(state_daemon, sandbox, in_place):
    """I1: a brief's formatting choices, including --no-preamble, cannot remove this contract."""
    service, harness = state_daemon
    repository(service, harness)
    job_id = service.dispatch("submit", harness.submit_args(task="review", pinned_model="astra",
        sandbox=sandbox, in_place=in_place, no_preamble=True))["job_id"]
    assert_contract(sent_prompt(service, job_id), writable=sandbox == "workspace-write")


@pytest.mark.parametrize("sandbox,isolated,writable", [
    ("workspace-write", False, True), ("read-only", False, False), ("read-only", True, False)],
    ids=["writable", "read-only", "isolated"])
def test_only_a_writable_review_is_told_it_may_run_tests_and_write_scratch(state_daemon, sandbox, isolated,
                                                                             writable):
    """A1 review r1, P2: a read-only review (Claude plan mode, Codex with no
    pytest) and an isolated one get the format without the scratch sentence."""
    service, harness = state_daemon
    repo = repository(service, harness)
    changes = {"isolated_review": True, "review_root": str(repo)} if isolated else {}
    job_id = service.dispatch("submit", harness.submit_args(task="review", pinned_model="astra",
        sandbox=sandbox, **changes))["job_id"]
    job = service.store.get_job(job_id)
    assert (job["sandbox"], bool(job["isolated_review"])) == (sandbox, isolated)
    assert_contract(sent_prompt(service, job_id), writable=writable)


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_a_review_resume_gets_the_contract_again(state_daemon, sandbox):
    """C-6.7, A1 review r1 P2: a resumed review's new turn is a review, so its
    prompt carries the format once, with the scratch sentence only if it writes."""
    service, harness = state_daemon
    repository(service, harness)
    source = service.dispatch("submit", harness.submit_args(task="review", pinned_model="astra",
        sandbox=sandbox))["job_id"]
    service._admit()
    attempt, = service.store.list_attempts(source)
    service._pending_launches.discard(attempt["attempt_id"])
    service.store.update_attempt(attempt["attempt_id"], native_session_id="source-session")
    adir = service.root / "jobs" / source / "a1"
    adir.mkdir()
    service._finalize(receipt_fixture(service, attempt, adir, stdout=b"VERDICT: APPROVE\nFINDINGS: none\n"))
    assert service.store.get_job(source)["state"] == "succeeded"
    resumed = service.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source,
        sandbox=sandbox))["job_id"]
    job = service.store.get_job(resumed)
    assert (job["kind"], job["task"], job["sandbox"]) == ("resume", "review", sandbox)
    assert_contract(sent_prompt(service, resumed), writable=sandbox == "workspace-write")


def test_a_gate_round_gets_no_review_contract_and_its_gate_passes(core, tmp_path):
    """A1 review r1, P1: a gate round (kind gate-review, no task) is told to
    return one sentinel-delimited JSON object and nothing else, which the gate's
    own parser enforces. The review format would contradict it, so the round's
    prompt carries none of it, and the fixture peer's answer passes the gate."""
    plan = tmp_path / "plan.md"
    plan.write_text("A concrete implementation plan.\n")
    prompts = []

    def capture(result):
        job = core.store.get_job(result["job_id"])
        manifest = json.loads((core.root / "jobs" / job["job_id"] / "manifest.json").read_text())
        prepared = core.root / "jobs" / job["job_id"] / "prompt.prepared.md"
        prompts.append((job, manifest, prepared.read_text() if prepared.exists() else None,
                        sent_prompt(core, job["job_id"])))

    assert gate_cli.run(arguments(plan), root=core.root, client=FakeClient(core, on_round=capture),
                        poll_interval=0) == 0
    [(job, manifest, prepared, sent)] = prompts
    assert (job["kind"], job["task"], job["sandbox"], job["isolated_review"]) == ("gate-review", None, "read-only", 1)
    assert prepared is None or "<!-- subfleet:review -->" not in prepared
    assert "preamble" not in manifest
    assert_no_contract(sent)
    assert "Return exactly one sentinel-delimited JSON object with no text outside it." in sent
    assert list((core.root / "gates").glob("*/certificate.json"))


def test_review_format_does_not_leak_into_the_next_build(state_daemon):
    """I2: the same daemon supplies the review contract, then keeps a build's own template."""
    service, harness = state_daemon
    repository(service, harness)
    review_id = service.dispatch("submit", harness.submit_args(task="review", pinned_model="astra",
        sandbox="read-only"))["job_id"]
    assert_contract(sent_prompt(service, review_id))
    build_id = service.dispatch("submit", harness.submit_args(task="build", pinned_model="astra",
        sandbox="workspace-write"))["job_id"]
    assert_no_contract(sent_prompt(service, build_id))


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
    assert_contract(Path(launch.stdin_path).read_text(), writable=sandbox == "workspace-write")
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
    assert_no_contract(sent_prompt(service, job_id))
