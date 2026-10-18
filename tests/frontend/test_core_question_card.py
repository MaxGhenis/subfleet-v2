"""Exercise inline question choices against the app's Foundation-only state."""

from tests.frontend.conftest import needs_swift, run_probe, write_json

pytestmark = needs_swift


def question(text="Which color?", *, multi=False, labels=("Blue", "Red", "Green")):
    return {
        "question": text,
        "header": "Color",
        "multiSelect": multi,
        "options": [{"label": label, "description": f"Use {label.lower()}"} for label in labels],
    }


def run(core_probe, tmp_path, questions, *steps):
    return run_probe(core_probe, "questions", write_json(tmp_path / "questions.json", {
        "questions": questions, "steps": list(steps),
    }))


def test_options_keep_descriptions_previews_and_multi_select(core_probe, tmp_path):
    item = question(multi=True)
    item["options"][0]["preview"] = "Blue preview\nSecond line"
    result = run(core_probe, tmp_path, [item])
    assert result["questions"][0] == item
    assert result["snapshots"][0]["answered_count"] == 0
    assert not result["snapshots"][0]["can_submit"]


def test_single_select_replaces_the_choice_and_other_preserves_draft(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question()],
                 {"do": "number", "number": 1}, {"do": "number", "number": 2},
                 {"do": "text", "text": "  Purple  \n"}, {"do": "number", "number": 3},
                 {"do": "other"})
    snapshots = result["snapshots"]
    assert snapshots[2]["selected"] == [1]
    assert snapshots[2]["answers"] == {"Which color?": "Red"}
    assert snapshots[3]["selected"] == [] and snapshots[3]["uses_other"]
    assert snapshots[3]["answers"] == {"Which color?": "Purple"}
    assert snapshots[4]["answers"] == {"Which color?": "Green"}
    assert not snapshots[4]["uses_other"]
    assert snapshots[5]["answers"] == {"Which color?": "Purple"}
    assert snapshots[5]["can_submit"]


def test_number_shortcuts_stop_at_nine_and_do_not_toggle_single_choices(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question(labels=tuple(f"Choice {i}" for i in range(1, 11)))],
                 {"do": "number", "number": 9}, {"do": "number", "number": 9},
                 {"do": "number", "number": 0}, {"do": "number", "number": 10},
                 {"do": "select", "index": -1}, {"do": "select", "index": 10})
    assert result["results"] == [True, True, False, False, False, False]
    assert result["snapshots"][-1]["answers"] == {"Which color?": "Choice 9"}


def test_multi_select_toggles_and_combines_in_option_order(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question(multi=True)],
                 {"do": "number", "number": 3}, {"do": "number", "number": 1},
                 {"do": "number", "number": 2}, {"do": "number", "number": 3},
                 {"do": "text", "text": "Purple"})
    assert result["snapshots"][2]["answers"] == {"Which color?": "Blue, Green"}
    assert result["snapshots"][-1]["selected"] == [0, 1]
    assert result["snapshots"][-1]["answers"] == {"Which color?": "Blue, Red, Purple"}
    assert result["snapshots"][-1]["answered_count"] == 1


def test_blank_other_must_be_completed_or_deselected(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question(multi=True)],
                 {"do": "number", "number": 1}, {"do": "other"},
                 {"do": "text", "text": " \n\t "}, {"do": "other"})
    assert not result["snapshots"][2]["can_continue"]
    assert not result["snapshots"][3]["can_submit"]
    assert result["snapshots"][4]["answers"] == {"Which color?": "Blue"}
    assert result["snapshots"][4]["can_submit"]


def test_steps_keep_answers_and_back_navigation_edits_combined_reply(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question(), question("Which extras?", multi=True)],
                 {"do": "next"}, {"do": "back"}, {"do": "select", "index": 0},
                 {"do": "next"}, {"do": "number", "number": 3}, {"do": "number", "number": 2},
                 {"do": "back"}, {"do": "number", "number": 2}, {"do": "next"}, {"do": "next"})
    assert result["results"][:2] == [False, False]
    assert result["snapshots"][4]["current_index"] == 1
    assert not result["snapshots"][4]["can_submit"]
    assert result["snapshots"][7]["selected"] == [0]
    assert result["snapshots"][9]["selected"] == [1, 2]
    assert result["snapshots"][-1]["answers"] == {
        "Which color?": "Red", "Which extras?": "Red, Green",
    }
    assert result["snapshots"][-1]["answered_count"] == 2
    assert result["snapshots"][-1]["can_submit"]
    assert result["snapshots"][-1]["is_last"]
    assert result["results"][-1] is False


def test_skip_omits_only_that_question_and_can_be_answered_after_going_back(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question(), question("Which extras?")],
                 {"do": "skip"}, {"do": "next"}, {"do": "number", "number": 2},
                 {"do": "back"}, {"do": "number", "number": 1})
    assert result["snapshots"][3]["answers"] == {"Which extras?": "Red"}
    assert result["snapshots"][3]["decision"] == "answer"
    assert result["snapshots"][3]["can_submit"]
    assert result["snapshots"][4]["skipped"]
    assert not result["snapshots"][5]["skipped"]
    assert result["snapshots"][5]["answers"] == {"Which color?": "Blue", "Which extras?": "Red"}


def test_skip_every_question_declines_without_a_cancel_turn(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [question(), question("Which extras?")],
                 {"do": "number", "number": 2}, {"do": "skip"}, {"do": "next"}, {"do": "skip"})
    final = result["snapshots"][-1]
    assert final["answers"] == {}
    assert final["decision"] == "deny"
    assert final["can_submit"] and final["can_continue"]
    assert final["answered_count"] == 0


def test_question_without_options_accepts_free_text(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [{"question": "What should we call it?"}],
                 {"do": "number", "number": 1}, {"do": "text", "text": "Subfleet"})
    assert result["results"] == [False, True]
    assert result["snapshots"][-1]["answers"] == {"What should we call it?": "Subfleet"}
    assert result["snapshots"][-1]["can_submit"]


def test_empty_questions_do_not_advance_or_submit(core_probe, tmp_path):
    result = run(core_probe, tmp_path, [], {"do": "number", "number": 1}, {"do": "skip"},
                 {"do": "next"}, {"do": "back"}, {"do": "text", "text": "unused"})
    final = result["snapshots"][-1]
    assert final["current_question"] is None
    assert not final["can_continue"] and not final["can_submit"] and not final["is_last"]
    assert final["answers"] == {}
