"""D-QOS-BOOKED1: `subfleet run --qos {utility,default}` and what `why` says about it.

The request travels as `provider_qos` and is recorded on the job; absent, it is `utility`
(the guardian's clamp, C-5.1). Whether a `default` request is honoured is decided by the
daemon at each launch (tests/unit/test_launch_qos_property.py); `why` says which and why.
"""

from __future__ import annotations

import pytest

from subfleet import cli, render

from test_cli import run_cli, submit_ok


@pytest.mark.parametrize("flags, sent", [((), None), (("--qos", "utility"), "utility"),
                                         (("--qos", "default"), "default")])
def test_d_qos_booked1_run_sends_the_request(daemon, flags, sent):
    server = daemon({"submit": submit_ok})
    assert run_cli(["run", "-m", "astra", "-d", *flags, "run the microsimulation"]) == 0
    assert server.args("submit")["provider_qos"] == sent


@pytest.mark.parametrize("value", ["inherit", "background", "Default", ""])
def test_d_qos_booked1_run_refuses_any_other_qos(value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["run", "-m", "astra", "--qos", value, "task"])
    assert exc.value.code == 2
    assert "--qos" in capsys.readouterr().err


def test_d_qos_booked1_run_help_names_the_rule(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--qos {utility,default}" in text
    assert "admission.priority_callers" in text


def test_d_qos_booked1_a_batch_entry_may_request_it(tmp_path):
    path = tmp_path / "jobs.toml"
    path.write_text('[defaults]\nmodel="luna"\nqos="default"\n[[jobs]]\nprompt_text="a"\n'
                    '[[jobs]]\nprompt_text="b"\nqos="utility"\n')
    _, defaults, jobs = cli.load_batch(path)
    assert defaults["qos"] == "default" and "qos" not in jobs[0] and jobs[1]["qos"] == "utility"
    path.write_text('[[jobs]]\nprompt_text="a"\nqos="inherit"\n')
    with pytest.raises(cli.BatchError, match="qos must be one of utility, default"):
        cli.load_batch(path)


QUEUED = {"job_id": "j", "state": "queued", "kind": "dispatch"}


def test_d_qos_booked1_why_says_an_unhonoured_request_and_why():
    reason = "qos default requested; caller is not in admission.priority_callers; provider clamped to utility"
    lines = render.why_queue({**QUEUED, "qos": {"requested": "default", "provider_qos": "utility",
                                                "reason": reason}})
    # Beside the job's line; every line it said before is still there, in order.
    plain = render.why_queue(QUEUED)
    assert lines == [plain[0], reason, *plain[1:]]


def test_d_qos_booked1_why_names_what_the_last_attempt_was_given():
    lines = render.why_queue({**QUEUED, "state": "running", "class": "priority", "qos": {
        "requested": "default", "provider_qos": "inherit",
        "reason": "qos default requested; class priority (admission.priority_callers); provider starts unclamped",
        "started": {"seq": 2, "provider_qos": "inherit"}}})
    assert lines == ["Job: j is running", "class priority (admission.priority_callers)",
                     "qos default requested; class priority (admission.priority_callers); "
                     "provider starts unclamped",
                     "a2's provider started at inherit (start.json)"]


def test_d_qos_booked1_why_says_nothing_new_for_a_utility_job():
    """The clamp is every job's (C-5.1); only a `default` request has anything to explain."""
    standing = {**QUEUED, "qos": {"requested": "utility", "provider_qos": "utility",
                                  "reason": "qos utility requested; provider clamped to utility",
                                  "started": {"seq": 1, "provider_qos": "utility"}}}
    assert render.why_queue(standing) == render.why_queue(QUEUED)
    assert not [line for line in render.why_queue(standing) if "qos" in line]
