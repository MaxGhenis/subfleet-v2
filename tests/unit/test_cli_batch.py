"""`run --batch FILE` against a fake daemon (C-17.7): one call hands off K briefs.

Incident, 2026-09-20: a session handing five stalled threads to lanes had to make
five `run` calls, and nothing recorded that the five belonged together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet import cli, protocol
from subfleet.contracts import Exit


def numbered(request: protocol.Request) -> dict:
    index = request.args["batch"]["index"]
    return {"job_id": f"20260920-15000{index}-{request.args['name']}", "request_id": request.args["request_id"],
            "created": True, "state": "queued"}


@pytest.fixture
def briefs(tmp_path, workdir):
    """A handoff folder: three briefs, three workdirs, one manifest."""
    folder = tmp_path / "handoff"
    folder.mkdir()
    for name in ("spm", "tariff", "thesis"):
        (folder / f"{name}.md").write_text(f"Continue the {name} thread.\n")
        (workdir / name).mkdir()
    return folder, workdir


def manifest(folder: Path, workdir: Path, *, suffix=".toml", extra="") -> Path:
    path = folder / f"handoff-20260920{suffix}"
    if suffix == ".toml":
        path.write_text(f'''label = "codex handoff"
[defaults]
model = "opus"
sandbox = "workspace-write"
in_place = true
{extra}
[[jobs]]
prompt = "spm.md"
workdir = "{workdir / 'spm'}"
out = "{folder / 'spm-out.md'}"

[[jobs]]
name = "tariff-p5"
prompt = "tariff.md"
workdir = "{workdir / 'tariff'}"

[[jobs]]
prompt_text = "Read the failed Actions run and report."
workdir = "{workdir / 'thesis'}"
sandbox = "read-only"
in_place = false
''')
    else:
        path.write_text(json.dumps([
            {"prompt": "spm.md", "workdir": str(workdir / "spm"), "model": "sonnet"},
            {"prompt": "tariff.md", "workdir": str(workdir / "tariff"), "model": "opus"}]))
    return path


def test_c17_7_one_call_submits_every_brief_and_prints_the_job_ids(daemon, capsys, briefs):
    """C-17.7 K briefs, K submits, K job ids on stdout in manifest order, one batch label."""
    folder, workdir = briefs
    server = daemon({"submit": numbered})
    assert cli.main(["run", "--batch", str(manifest(folder, workdir)), "-d"]) == Exit.OK
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["20260920-150001-spm", "20260920-150002-tariff-p5",
                                        "20260920-150003-codex-handoff-3"]
    sent = [request.args for request in server.requests if request.op == "submit"]
    assert [args["batch"]["index"] for args in sent] == [1, 2, 3]
    assert {args["batch"]["id"] for args in sent} == {sent[0]["batch"]["id"]}
    assert all(args["batch"]["label"] == "codex handoff" and args["batch"]["size"] == 3 for args in sent)
    # an entry overrides the manifest's defaults, which override nothing the entry said
    assert [(a["pinned_model"], a["sandbox"], a["in_place"]) for a in sent] == [
        ("opus", "workspace-write", True), ("opus", "workspace-write", True), ("opus", "read-only", False)]
    # paths are relative to the manifest, so the folder can be moved whole
    assert sent[0]["prompt_path"] == str((folder / "spm.md").resolve())
    assert sent[0]["out_path"] == str(folder / "spm-out.md") and sent[1]["out_path"] is None
    assert Path(sent[2]["prompt_path"]).read_text() == "Read the failed Actions run and report.\n"
    assert len({args["request_id"] for args in sent}) == 3
    assert "3 of 3 submitted" in captured.err and "subfleet wait 20260920-150001-spm" in captured.err


def test_c17_7_command_line_flags_are_the_lowest_defaults(daemon, capsys, briefs):
    """C-17.7 entry, then manifest defaults, then the command line's own flags."""
    folder, workdir = briefs
    server = daemon({"submit": numbered})
    path = manifest(folder, workdir, suffix=".json")
    assert cli.main(["run", "--batch", str(path), "-m", "haiku", "-s", "workspace-write", "--in-place", "-d"]) == Exit.OK
    sent = [request.args for request in server.requests if request.op == "submit"]
    assert [(a["pinned_model"], a["sandbox"], a["in_place"]) for a in sent] == [
        ("sonnet", "workspace-write", True), ("opus", "workspace-write", True)]
    assert {a["batch"]["label"] for a in sent} == {"handoff-20260920"}     # a bare list is labelled by its file


def test_c17_2_a_retired_model_in_a_manifest_submits_its_successor(daemon, capsys, briefs):
    """C-17.2, C-17.7: a manifest names models like `-m` does, so `fable` (retired
    2026-09-27) and `sol` submit their successors, with one note per entry."""
    folder, workdir = briefs
    server = daemon({"submit": numbered})
    path = folder / "retired.json"
    path.write_text(json.dumps([
        {"prompt": "spm.md", "workdir": str(workdir / "spm"), "model": "fable"},
        {"prompt": "tariff.md", "workdir": str(workdir / "tariff"), "model": "sol"}]))
    assert cli.main(["run", "--batch", str(path), "-d"]) == Exit.OK
    sent = [request.args for request in server.requests if request.op == "submit"]
    assert [a["pinned_model"] for a in sent] == ["opus", "astra"]
    err = capsys.readouterr().err
    assert "-m fable is retired; using opus" in err and "-m sol is retired; using astra" in err
    assert err.count("is retired") == 2


def test_c17_7_a_refused_entry_does_not_stop_the_rest(daemon, capsys, briefs):
    """C-17.7 entries are independent once validated; the exit code is the first failure's."""
    folder, workdir = briefs

    def submit(request):
        if request.args["batch"]["index"] == 2:
            return protocol.fail(request.id, Exit.REFUSED, "writable job refused on main", "Check out a task branch")
        return numbered(request)
    daemon({"submit": submit})
    assert cli.main(["run", "--batch", str(manifest(folder, workdir)), "-d", "--json"]) == Exit.REFUSED
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["index"] for row in rows] == [1, 2, 3]
    assert [bool(row["job_id"]) for row in rows] == [True, False, True]
    assert rows[1]["rc"] == 7 and rows[1]["error"] == "writable job refused on main" and rows[1]["fix"]
    assert {row["batch"] for row in rows} == {rows[0]["batch"]} and rows[0]["label"] == "codex handoff"


def test_c17_7_request_id_makes_the_batch_repeatable(daemon, capsys, briefs):
    """C-6.2 with --request-id, entry n is always <id>-<n>, so a retry adds nothing twice."""
    folder, workdir = briefs
    server = daemon({"submit": lambda request: {**numbered(request), "created": False}})
    assert cli.main(["run", "--batch", str(manifest(folder, workdir)), "--request-id", "handoff-0920", "-d"]) == Exit.OK
    sent = [request.args for request in server.requests if request.op == "submit"]
    assert [a["request_id"] for a in sent] == ["handoff-0920-1", "handoff-0920-2", "handoff-0920-3"]
    assert {a["batch"]["id"] for a in sent} == {"handoff-0920"}
    assert capsys.readouterr().err.count("existing job for this request id") == 3


@pytest.mark.parametrize("extra,entry_edit,expected", [
    ("", ('prompt = "spm.md"', 'prompt = "missing.md"'), "cannot read prompt file"),
    ("", ('name = "tariff-p5"', 'nmae = "tariff-p5"'), "unknown key 'nmae'"),
    ("", ('in_place = false', 'in_place = "no"'), "in_place must be true or false"),
    ("", ('sandbox = "read-only"', 'sandbox = "danger"'), "sandbox must be one of"),
    ("", ('prompt_text = "Read the failed Actions run and report."', 'task = "review"'), "needs exactly one of prompt or prompt_text"),
    ('prompt = "spm.md"\nprompt_text = "x"', None, "defaults may name prompt or prompt_text, not both"),
])
def test_c17_7_an_invalid_manifest_submits_nothing(daemon, capsys, briefs, extra, entry_edit, expected):
    """C-17.7 every entry is checked before the first submit, and the message names the entry."""
    folder, workdir = briefs
    server = daemon({"submit": numbered})
    path = manifest(folder, workdir, extra=extra)
    if entry_edit:
        path.write_text(path.read_text().replace(*entry_edit))
    assert cli.main(["run", "--batch", str(path), "-d"]) == Exit.INVALID_INPUT
    assert expected in capsys.readouterr().err
    assert "submit" not in server.ops()


@pytest.mark.parametrize("argv,expected", [
    (["-p", "x.md"], "prompts come from the manifest"),
    (["--dry-run"], "explain one job"),
])
def test_c17_7_flags_that_mean_one_job_are_refused(daemon, capsys, briefs, argv, expected):
    """C-17.7 a batch has no single prompt and no single decision."""
    folder, workdir = briefs
    server = daemon({"submit": numbered})
    assert cli.main(["run", "--batch", str(manifest(folder, workdir)), *argv]) == Exit.INVALID_INPUT
    assert expected in capsys.readouterr().err and "submit" not in server.ops()


@pytest.mark.parametrize("text,expected", [
    ("not [ toml", "is not valid TOML"), ("jobs = []", "non-empty list"), ('jobs = "x"', "non-empty list"),
    ('colour = "red"\n[[jobs]]\nprompt_text = "x"', "unknown top-level key 'colour'"),
    ('label = ""\n[[jobs]]\nprompt_text = "x"', "label must contain"),
])
def test_c17_7_load_batch_names_what_is_wrong(tmp_path, text, expected):
    """C-17.7 a manifest that is not a list of runs says why."""
    path = tmp_path / "b.toml"
    path.write_text(text)
    with pytest.raises(cli.BatchError, match=expected):
        cli.load_batch(path)
    with pytest.raises(cli.BatchError, match="cannot read"):
        cli.load_batch(tmp_path / "absent.toml")


def test_c17_7_waits_for_every_job_when_attached(daemon, capsys, briefs):
    """C-17.7 --wait blocks on the whole batch and returns the jobs' result."""
    folder, workdir = briefs
    ids = ["20260920-150001-spm", "20260920-150002-tariff-p5", "20260920-150003-codex-handoff-3"]
    server = daemon({"submit": numbered,
                     "wait": lambda request: {"jobs": {job: {"job_id": job, "state": "succeeded", "rc": 0} for job in ids}}})
    assert cli.main(["run", "--batch", str(manifest(folder, workdir)), "--wait"]) == Exit.OK
    assert sorted(server.args("wait")["job_ids"]) == sorted(ids)
