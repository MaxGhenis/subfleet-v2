"""Round-two layout and presentation regressions, with recorded Codex commands."""
import json
import os
from pathlib import Path
import shutil

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift
COMMANDS = json.loads((ROOT / "tests/fixtures/visual/codex-commands.json").read_text())


@pytest.mark.parametrize("index,record", list(enumerate(COMMANDS)), ids=lambda x: str(x)[:80])
def test_labels_strip_recorded_shell_wrappers(r2_models, index, record):
    row = r2_models["commands"][index]
    assert row["script"] == record["script"]
    assert row["label"] and not row["label"].startswith(("/bin/", "cd "))
    if "<<" in record["command"]:
        assert row["label"] == "Run a Python script"


def test_cd_prelude_labels_the_actual_command(r2_models):
    assert r2_models["commands"][-1]["label"] == "python ../run.py 540"


def test_pipes_and_chains_label_the_first_script_command(r2_models):
    assert r2_models["commands"][7]["label"] == "nl -ba subfleet/daemon.py"
    assert r2_models["commands"][4]["label"] == "git show d9b378cc"


def test_all_supported_shells_and_flags_preserve_the_recorded_script(r2_models):
    assert r2_models["wrappers"] == [COMMANDS[-1]["script"]] * 9


@pytest.mark.parametrize("path", [
    ".agent_id", ".classifier_approvable", ".subtype", ".tool_use_id", ".tool_name", ".display_name", ".title",
    ".description", ".decision_reason", ".decision_reason_type", ".suppress_always_allow_rule", ".requires_user_interaction", ".method",
    "params.threadId", "params.turnId", "params.itemId", "params.startedAtMs", "params.reason", "params.approvalId", "params.commandActions", "input.description",
])
def test_named_protocol_plumbing_is_hidden_and_unknown_keys_survive(r2_models, path):
    fields = r2_models["hidden"][path]
    assert len(fields) == 1 and next(iter(fields.values())) == "/future/root"
    assert path.lstrip(".") not in fields


@pytest.mark.parametrize("key", ["tool", "title", "description", "reason"])
def test_summary_copy_is_not_repeated_as_a_grant(r2_models, key):
    assert r2_models["display_hidden"][key] == {"newGrant": "/future/root"}


@pytest.mark.parametrize("key", ["permission_suggestions", "execpolicy_amendment", "network_amendments",
                                 "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments"])
def test_empty_known_amendments_are_omitted_but_unknown_empty_fields_survive(r2_models, key):
    assert r2_models["empty_amendments"][key] == {"newGrant": "[]"}


def test_scope_precedes_command_and_bulk_content(r2_models):
    assert r2_models["order"] == ["input.file_path", "cwd", "grantRoot", "permissions.fileSystem.write[0]",
                                  "permissions.network.enabled", "input.command", "futureGrant.method", "nullable", "input.content"]
    assert r2_models["fields"]["input.command"] == "echo [MASKED]"
    assert r2_models["fields"]["nullable"] == "null"
    assert r2_models["fields"]["futureGrant.method"] == "must stay visible"


def test_questions_have_one_presentation_and_answers_survive_resolution(r2_models):
    assert r2_models["question_fields"] == ["blocked_path"]
    assert r2_models["question_summary_fields"] == []
    assert r2_models["answers"] == {"Which scope?": "Local"}
    assert not r2_models["question_pending"]


def test_submitted_answers_survive_event_reset_and_replay(r2_models):
    assert r2_models["reset_answers"] == {"Which scope?": "Local"}
    assert r2_models["reset_approval_id"] == "q"
    assert not r2_models["reset_question_pending"]


def test_question_summaries_preserve_unknown_object_input_grants(r2_models):
    expected = {"input.newGrant": "/future/root"}
    assert r2_models["question_object_input_fields"] == expected
    assert r2_models["question_string_input_fields"] == expected


def test_file_change_fixture_matches_the_request_schema():
    fixture = next(f for f in json.loads((ROOT / "tests/fixtures/visual/approvals.json").read_text()) if f["id"] == "codex-file-change")
    assert set(fixture["request"]["params"]) == {"threadId", "turnId", "itemId", "startedAtMs", "reason", "grantRoot"}
    assert fixture["request"]["params"]["grantRoot"] == "/"
    assert fixture["visible"] == ["grantRoot: /"]


@pytest.fixture(scope="session")
def r2_views(tmp_path_factory):
    ocr = shutil.which("tesseract")
    if ocr is None:
        pytest.skip("rendered-text validation requires tesseract")
    probe = compile_probe(tmp_path_factory.mktemp("r2-views") / "probe",
                          ROOT / "tests/frontend/R2ApprovalViewProbe.swift", "SUBFLEET_VIEW_TEST")
    out = Path(os.environ.get("SF_R2_SNAPSHOTS", tmp_path_factory.mktemp("r2-render")))
    result = run_probe(probe, ROOT / "tests/fixtures/visual/approval-layout.json", out,
                       # The serialized run renders 32 surfaces plus scroll tails
                       # and waits for each OCR child, including on a loaded host.
                       timeout=540, env={"R2_TESSERACT": ocr})
    if path := os.environ.get("SF_R2_RESULT"):
        Path(path).write_text(json.dumps(result, indent=2) + "\n")
    return result


@pytest.mark.parametrize("scene", ["claude-write-120-lines", "codex-long-command", "masked-write-120-lines", "claude-short-bash"])
@pytest.mark.parametrize("mode", ["dark", "light"])
def test_large_grants_keep_actions_visible_and_fit_a_400_point_offer(r2_views, scene, mode):
    entry = r2_views["approvals"][scene][mode]
    assert entry["card_height"] <= 400
    assert entry["sheet_height"] <= 400
    for label in ("Allow", "Deny"):
        assert label in entry["card_ocr"]
        if scene != "masked-write-120-lines" or label != "Allow":
            assert label in entry["sheet_ocr"]
    assert "Cancel" in entry["sheet_ocr"]
    assert r2_views["sheetfit"][scene]["fits_in_400"] <= 400
    assert r2_views["visible_windows"] == 0


def test_question_picker_is_near_the_top_without_flattened_questions(r2_views):
    text = r2_views["approvals"]["question-two"]["light"]["card_ocr"]
    assert text.count("Which release task should go first?") == 1
    assert "input.questions" not in text
    assert "Option" in text and "Choose one" in text


def test_answered_questions_show_question_and_answer_once(r2_views):
    text = r2_views["approvals"]["question-two"]["answered_card_ocr"]
    for question in ("Which release task should go first?", "When should the notes go out?"):
        assert text.count(question) == 1
    assert text.count("Answer: Option 0") == 1 and text.count("Answer: Window 0") == 1
    assert "input.questions" not in text and "null" not in text


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("surface", ["card", "sheet"])
def test_the_last_write_line_is_reachable_without_moving_actions(r2_views, mode, surface):
    key = f"{surface}-claude-write-120-lines-{mode}.png"
    text = r2_views["scrolling"][key]["tail_ocr"]
    assert "line 119" in text and "Allow" in text and "Deny" in text


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_masked_confirmation_is_reachable_and_enables_allow(r2_views, mode):
    confirmed = r2_views["approvals"]["masked-write-120-lines"]["confirmed_" + mode]
    assert confirmed["height"] <= 400
    assert "Allow" in confirmed["ocr"] and "Cancel" in confirmed["ocr"] and "Deny" in confirmed["ocr"]
    assert "have reviewed the masked values" in confirmed["ocr"]


# Round-three review regressions adapted from the reviewer's proposed patch.

def test_codex_input_to_a_running_terminal_is_shown(r2_models):
    # Codex 0.159: kind "Distinguishes a command approval from input sent to an existing terminal" (C-27.1).
    review = r2_models["review_r3"]
    assert "writeStdin" in review["write_stdin_fields"].values()
    assert "writeStdin" in review["write_stdin_summary"].values()


def test_the_command_precedes_parsed_actions_and_arrays_keep_their_order(r2_models):
    keys = r2_models["review_r3"]["chain_order"]
    actions = [i for i, k in enumerate(keys) if k.startswith("params.commandActions")]
    assert keys.index("params.command") < min(actions, default=len(keys))
    writes = [k for k in r2_models["review_r3"]["roots_order"] if "write[" in k]
    assert writes == sorted(writes, key=lambda k: int(k.rsplit("[", 1)[1].rstrip("]")))


def test_a_cd_prelude_ends_at_its_own_separator(r2_models):
    assert r2_models["review_r3"]["label_cd_semicolon"] not in ("make", "make ")


def test_file_change_fixture_validates_against_the_vendored_codex_schema():
    schema = json.loads((ROOT / "tests/fixtures/codex/app-server-0.153.3/ServerRequest.json").read_text())
    params_schema = schema["definitions"]["FileChangeRequestApprovalParams"]
    fixture = next(f for f in json.loads((ROOT / "tests/fixtures/visual/approvals.json").read_text()) if f["id"] == "codex-file-change")
    params = fixture["request"]["params"]
    assert set(params_schema["required"]) <= set(params) <= set(params_schema["properties"])
