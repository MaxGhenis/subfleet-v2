"""Review r2 of #248 (D-ENV2), P3-2, at the daemon: the Codex isolated-review
inspection receives no name the daemon withholds.

r2 ran the real `CodexAdapter._launch` for codex-1 with the daemon's own
credential additions and environment, and a fake `codex` that recorded names:
the main launch withheld the hook, but the inspection (`isolation.inspect_codex`,
an `app-server` read and an `mcp list`) received it, both for a lane held on the
hook and for an ignored lanes.json row's hook. The inspection is covered twice
now: the adapter passes its withheld names into the `build_environment` the
inspection selects from, and the daemon's helper scope withholds them too.

The fake `codex` records variable names, and which names hold a `SECRET7`
dummy, never a value. No provider, credential store or live daemon is used.
"""

import json
import sys

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import isolation, registry
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import JobSpec, Sandbox
from tests.fake.test_launch_env_daemon import (
    TOKEN, nine_root, nine_row, roster, store_as_round_four_left_it, stored, write_roster,
)

# Review r2's hook: no credential word, so every launch inherits it unless withheld.
EIGHT = "SUBFLEET_FAKE_LANE_EIGHT"
# A hook no lane names: the inspection still inherits it, so a recorder that saw
# no hook at all could not pass.
SEVEN = "SUBFLEET_FAKE_LANE_SEVEN"

FAKE_CODEX = '''#!{python} -I
import json, os, sys
with open({record!r}, "a") as out:
    out.write(json.dumps({{"command": sys.argv[1:2], "names": sorted(os.environ),
                          "dummies": sorted(n for n, v in os.environ.items() if v.startswith("SECRET7"))}}) + "\\n")
if sys.argv[1:2] == ["app-server"]:
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") == 2:
            print(json.dumps({{"id": 2, "result": {{"requirements": None}}}}), flush=True)
        elif message.get("id") == 3:
            print(json.dumps({{"id": 3, "result": {{"layers": [{{"name": {{"type": "user"}}, "config": {{}}}}]}}}}),
                  flush=True)
            break
elif sys.argv[1:3] == ["mcp", "list"]:
    print("[]")
'''


def fake_codex(directory):
    """A `codex` that answers the inspection and records what each run received: names only."""
    record = directory / "codex-runs.jsonl"
    binary = directory / "codex"
    binary.write_text(FAKE_CODEX.format(python=sys.executable, record=str(record)))
    binary.chmod(0o700)
    return binary, record


def hook_root(tmp_path, monkeypatch, case):
    """Review r2's two stores. `held-lane`: claude-9 stored on the hook, with no
    lanes.json row (held). `ignored-row`: claude-9 stored on its own variable, its
    lanes.json row naming the hook (ignored at startup)."""
    root, _ = nine_root(tmp_path, monkeypatch, TOKEN)
    if case == "held-lane":
        write_roster(root, [nine_row(EIGHT) if row["lane_id"] == "claude-9" else row for row in roster(root)])
        store_as_round_four_left_it(root)
        write_roster(root, [row for row in roster(root) if row["lane_id"] != "claude-9"])
        assert stored(root)["claude-9"] == (EIGHT, 1)
    else:
        store_as_round_four_left_it(root)
        write_roster(root, [nine_row(EIGHT) if row["lane_id"] == "claude-9" else row for row in roster(root)])
        assert stored(root)["claude-9"] == (TOKEN, 1)
    monkeypatch.setenv(EIGHT, "SECRET7-eight")
    monkeypatch.setenv(SEVEN, "SECRET7-seven")
    for name in isolation.MANAGED_ENV:
        monkeypatch.delenv(name, raising=False)
    return root


def isolated_review(directory):
    review = directory / "review"
    review.mkdir()
    prompt = directory / "prompt.md"
    prompt.write_text("Reply OK")
    return JobSpec(request_id="fake", kind="dispatch", workdir=str(review), prompt_path=str(prompt),
                   task="review", tier="standard", pinned_model=None, pinned_lane=None,
                   sandbox=Sandbox.READ_ONLY, out_path=None, name="env-p3", isolated_review=True,
                   review_root=str(review)), prompt


def inspection_runs(tmp_path, monkeypatch, case, scope):
    """Review r2's input: the real `CodexAdapter._launch` for an isolated review on
    codex-1, built as the daemon builds it (its adapter wiring and credential
    additions), in the daemon's worker scope or in none."""
    root = hook_root(tmp_path, monkeypatch, case)
    binary, record = fake_codex(tmp_path)
    service = daemon_module.Daemon(root)
    try:
        assert EIGHT in service._withheld_references(), "the daemon withholds the hook from its launches"
        monkeypatch.setitem(registry._factories, "codex", lambda: CodexAdapter(str(binary)))
        adapter = service._adapter("codex")
        lane = service.store.get_lane("codex-1")
        job, prompt = isolated_review(tmp_path)
        attempt = tmp_path / "attempt"

        def build():
            return adapter.build_launch(job, "fake/a1", attempt, lane, service._credential_environment(lane),
                                        "fake-model", None, prompt, None)

        launch = service._run_worker(build) if scope == "worker" else build()
        withheld = service._provider_environment.withheld()
    finally:
        service.close()
    assert "--ephemeral" in launch.argv, "the inspection ran and the launch was built"
    return [json.loads(line) for line in record.read_text().splitlines()], withheld


@pytest.mark.parametrize("scope", ["worker", "no-scope"])
@pytest.mark.parametrize("case", ["held-lane", "ignored-row"])
def test_review_r2_the_isolated_inspection_receives_no_withheld_hook(tmp_path, monkeypatch, case, scope):
    """Review r2, P3-2: both inspection runs (`app-server`, `mcp list`) lack the
    hook the daemon withholds, inside the daemon's worker scope and outside every
    scope, where only the adapter's own withheld names cover it. A hook no lane
    names still reaches both, and no other dummy does."""
    runs, withheld = inspection_runs(tmp_path, monkeypatch, case, scope)
    assert EIGHT in withheld
    assert [run["command"] for run in runs] == [["app-server"], ["mcp"]]
    for run in runs:
        assert EIGHT not in run["names"], "the inspection received the withheld hook"
        assert SEVEN in run["names"], "a hook no lane names still reaches the inspection"
        assert run["dummies"] == [SEVEN], "only the unnamed hook's dummy reaches it"
