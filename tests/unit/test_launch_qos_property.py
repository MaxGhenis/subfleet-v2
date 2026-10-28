"""C-5.1, C-6.16, D-QOS-BOOKED1: who starts a provider unclamped, over generated jobs.

A job may request `run --qos default`. At each launch the daemon asks the guardian for
`--provider-qos inherit` only when the job requested `default` and is `priority` at that
launch (C-6.16: its caller, or an ancestor's, is in `admission.priority_callers`); the
operator's `SUBFLEET_PROVIDER_QOS=inherit` unclamps every launch. The property composes the
real pieces: the daemon's decision (`scheduler.launch_qos`), the guardian argv it adds
(`daemon.guardian_qos_args`), the guardian's own parse (`guardian.main`) and its clamp
(`guardian.provider_qos`), against an oracle written from the rule, not from the code.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import daemon as daemon_module, guardian, scheduler

pytestmark = pytest.mark.skipif(not os.access(guardian.TASKPOLICY, os.X_OK),
                                reason="C-5.1's clamp is taskpolicy(8), which ships with macOS")

CHOSEN = "0a1b2c3d-0000-4000-8000-00000000c0de"
KINDS = ("dispatch", "gate-review", "turn", "resume", "revive", "handoff")


def policy(callers):
    return {"admission": {"priority_callers": callers}}


def family(kind, requested, caller, via, cycle=False):
    """The job, and the ancestors the daemon would read for it (C-6.16)."""
    others = {"self": None, "parent": None, "grandparent": None, "none": None}
    sessions = {where: (caller if where == via else f"other-{where}") for where in others}
    job = {"job_id": "job", "kind": kind, "provider_qos": requested,
           "caller_session": sessions["self"], "parent_job_id": "parent"}
    parent = {"job_id": "parent", "caller_session": sessions["parent"], "parent_job_id": "grandparent"}
    grandparent = {"job_id": "grandparent", "caller_session": sessions["grandparent"],
                   "parent_job_id": "job" if cycle else None}
    return job, {"job": job, "parent": parent, "grandparent": grandparent}


def guardian_given(flags: list[str], env: str | None, tmp: Path) -> str | None:
    """What the guardian started the provider under for this daemon argv tail and operator env."""
    seen = {}

    def record(command, **options):
        seen.update(options)
        return 0
    with mock.patch.object(guardian, "run_guardian", record):
        assert guardian.main(["--attempt-dir", str(tmp), "--cwd", str(tmp), "--stdout-path", str(tmp / "o"),
                              "--stderr-path", str(tmp / "e"), *flags, "--", "provider"]) == 0
    environment = {key: value for key, value in os.environ.items() if key != guardian.PROVIDER_QOS_ENV}
    if env is not None:
        environment[guardian.PROVIDER_QOS_ENV] = env
    with mock.patch.dict(os.environ, environment, clear=True):
        return guardian.provider_qos(seen["qos_request"])


JOBS = st.fixed_dictionaries({
    "requested": st.sampled_from([None, "utility", "default"]),
    "kind": st.sampled_from(KINDS),
    # Where the chosen caller sits: the job's own caller, an ancestor's, or nowhere.
    "via": st.sampled_from(["self", "parent", "grandparent", "none"]),
    "caller": st.sampled_from([CHOSEN, CHOSEN.upper(), f"  {CHOSEN}  "]),
    "callers": st.sampled_from([None, [], [CHOSEN], [CHOSEN.upper()], [f" {CHOSEN} "], ["someone-else"],
                                ["someone-else", CHOSEN]]),
    "cycle": st.booleans(),
    "env": st.sampled_from([None, "inherit", "utility", "", "INHERIT"]),
})


def test_d_qos_booked1_the_provider_inherits_iff_an_honoured_request_or_the_operator_says_so(tmp_path):
    counts = {"examples": 0, "inherit": 0, "utility": 0, "honoured": 0, "operator": 0}

    @settings(max_examples=600, deadline=None, database=None)
    @given(case=JOBS)
    def check(case):
        job, rows = family(case["kind"], case["requested"], case["caller"], case["via"], case["cycle"])
        listed = case["callers"] is not None and CHOSEN in {item.strip().lower() for item in case["callers"]}
        # The oracle, from the rule: a turn is never priority (C-6.16); everything else
        # is priority when the chosen caller is listed and is the job's or an ancestor's.
        honoured = (case["requested"] == "default" and case["kind"] != "turn"
                    and listed and case["via"] != "none")
        expected_inherit = honoured or case["env"] == "inherit"
        qos, reason = scheduler.launch_qos(job, policy=policy(case["callers"]), jobs=rows)
        given_qos = guardian_given(daemon_module.guardian_qos_args(qos), case["env"], tmp_path)
        assert (given_qos is None) is expected_inherit, (case, qos, reason)
        assert (qos == "inherit") is honoured, (case, qos, reason)
        assert ("provider starts unclamped" in reason) is honoured, reason
        counts["examples"] += 1
        counts["inherit" if given_qos is None else "utility"] += 1
        counts["honoured"] += honoured
        counts["operator"] += case["env"] == "inherit" and not honoured

    check()
    print(f"\nD-QOS-BOOKED1 property: {counts}")
    assert counts["examples"] >= 100, counts
    # At least one drawn case on each side of the iff, and on each way to inherit.
    assert counts["inherit"] and counts["utility"] and counts["honoured"] and counts["operator"], counts


@pytest.mark.parametrize("job, callers, expected", [
    ({"kind": "dispatch", "provider_qos": "default", "caller_session": CHOSEN}, [CHOSEN],
     ("inherit", "qos default requested; class priority (admission.priority_callers); provider starts unclamped")),
    ({"kind": "dispatch", "provider_qos": "default", "caller_session": "someone-else"}, [CHOSEN],
     ("utility", "qos default requested; caller is not in admission.priority_callers; provider clamped to utility")),
    ({"kind": "dispatch", "provider_qos": "default", "caller_session": CHOSEN}, None,
     ("utility", "qos default requested; caller is not in admission.priority_callers; provider clamped to utility")),
    ({"kind": "turn", "provider_qos": "default", "caller_session": CHOSEN}, [CHOSEN],
     ("utility", "qos default requested; a conversation turn is never priority (C-6.16); "
                 "provider clamped to utility")),
    ({"kind": "dispatch", "provider_qos": None, "caller_session": CHOSEN}, [CHOSEN],
     ("utility", "qos utility requested; provider clamped to utility")),
    ({"kind": "dispatch", "provider_qos": "utility", "caller_session": CHOSEN}, [CHOSEN],
     ("utility", "qos utility requested; provider clamped to utility")),
    # A row from before the column: nothing to read, which is `utility`.
    ({"kind": "dispatch", "caller_session": CHOSEN}, [CHOSEN],
     ("utility", "qos utility requested; provider clamped to utility")),
], ids=["honoured", "not-listed", "no-list", "turn", "null", "utility", "no-column"])
def test_d_qos_booked1_why_says_why(job, callers, expected):
    assert scheduler.launch_qos(job, policy=policy(callers)) == expected


def test_d_qos_booked1_an_inherited_priority_is_honoured_and_a_missing_parent_ends_the_walk():
    job = {"job_id": "child", "kind": "dispatch", "provider_qos": "default", "caller_session": "child-caller",
           "parent_job_id": "root"}
    root = {"job_id": "root", "caller_session": CHOSEN, "parent_job_id": None}
    assert scheduler.launch_qos(job, policy=policy([CHOSEN]), jobs={"root": root})[0] == "inherit"
    assert scheduler.launch_qos(job, policy=policy([CHOSEN]), jobs={})[0] == "utility"


def test_d_qos_booked1_requests_and_the_guardian_flag():
    assert scheduler.QOS_REQUESTS == ("utility", "default")
    assert [scheduler.requested_qos(row) for row in (
        {}, {"provider_qos": None}, {"provider_qos": "utility"}, {"provider_qos": "default"})] == [
        "utility", "utility", "utility", "default"]
    assert daemon_module.guardian_qos_args("inherit") == ["--provider-qos", "inherit"]
    # Anything else asks nothing: the guardian's own default is the clamp.
    assert daemon_module.guardian_qos_args("utility") == []
