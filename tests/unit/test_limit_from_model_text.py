"""C-9.2–9.8: provider verdicts never come from a model's report."""

import json
from pathlib import Path

import pytest

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import OutcomeClass
from tests.conftest import NOW, exit_info, make_launch


MODEL = "claude-opus-5-5"
SESSION = "limit-from-model-text"
LIVE_REPORTS = (
    "The first US-hub triage session hit its usage limit.",
    "The second US-hub triage session hit its usage limit.",
    'App inventory: "Continues elsewhere when an account hits its usage limit".',
)


def assistant(text):
    return {"type": "assistant", "session_id": SESSION,
            "message": {"model": MODEL, "content": [{"type": "text", "text": text}]}}


def success(text):
    return {"type": "result", "subtype": "success", "is_error": False,
            "session_id": SESSION, "result": text}


def classify(directory, rows, rc=0, stderr=""):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (directory / "stderr").write_text(stderr)
    projects = directory / "projects"
    adapter = ClaudeAdapter(now=lambda: NOW, projects_dir=projects)
    launch = make_launch(directory, session_id=SESSION, model_id=MODEL,
                         projects_dir=projects, identity=None, label=None)
    return adapter.classify(directory, launch, exit_info(rc))


@pytest.mark.parametrize("report", LIVE_REPORTS, ids=("triage-first", "triage-second", "inventory"))
def test_live_success_report_never_limits(tmp_path, report):
    """2026-10-11: reconstructed reports from the three incident job shapes."""
    outcome = classify(tmp_path, [assistant(report), success(report)])
    assert outcome.cls is OutcomeClass.OK, outcome.detail
    assert outcome.closure is None
