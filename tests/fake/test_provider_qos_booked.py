"""D-QOS-BOOKED1 through a real daemon: an honoured job's provider runs unclamped, an
unhonoured one at `utility` (C-5.1, C-6.16).

Both jobs request `run --qos default`; only one has a caller in
`admission.priority_callers`. Each provider is the fake provider's `qos` scenario, which
writes `ps -M`'s priorities for itself, a child it starts and its guardian to its own
stdout file. A clamped process reads 20 or lower (an idle thread exactly 20); an unclamped
one reads its guardian's, 31 at the default QoS. The daemon is a subprocess of this test,
so the guardian runs at this test's scheduling: on a runner that is itself clamped (a
Subfleet lane is), unclamped and clamped read alike, and only the receipts can differ.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from subfleet import guardian
from subfleet.contracts import attempt_dir

UTILITY = 20
DEFAULT = 31
BACKGROUND = 4       # darwinbg's MAXPRI_THROTTLE
CHOSEN = "0a1b2c3d-0000-4000-8000-00000000c0de"
OTHER = "0a1b2c3d-0000-4000-8000-0000000000ff"

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.access(guardian.TASKPOLICY, os.X_OK),
                                reason="C-5.1's clamp is taskpolicy(8), which ships with macOS")


def own_priority() -> int:
    out = subprocess.run(["/bin/ps", "-o", "pri=", "-p", str(os.getpid())], capture_output=True, text=True)
    return int(out.stdout.split()[0].rstrip("TRSUIZ"))


def booked_pair(daemon, monkeypatch) -> dict[str, dict]:
    """Run one honoured and one unhonoured `--qos default` job; their reports and receipts."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    policy = json.loads((daemon.root / "policy.json").read_text())
    policy["admission"]["priority_callers"] = [CHOSEN]
    (daemon.root / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
    daemon.start()
    jobs = {"honoured": daemon.submit("qos", caller_session=CHOSEN, provider_qos="default"),
            "unhonoured": daemon.submit("qos", caller_session=OTHER, provider_qos="default")}
    found = {}
    for name, job_id in jobs.items():
        job = daemon.until(lambda: (lambda row: row if row["state"] in {"succeeded", "failed", "cancelled", "lost"}
                                    else None)(daemon.job(job_id)), timeout=90)
        assert job["state"] == "succeeded", daemon.log_text()
        adir = attempt_dir(daemon.root, job_id, 1)
        found[name] = {"job": job, "report": json.loads((adir / "stdout").read_text()),
                       "start": json.loads((adir / "start.json").read_text())}
    print("\nD-QOS-BOOKED1 readings: " + json.dumps(
        {"test_process_pri": own_priority(),
         **{name: {"start.provider_qos": value["start"]["provider_qos"],
                   **{key: value["report"][key] for key in ("provider", "child", "guardian")}}
            for name, value in found.items()}}))
    return found


def test_d_qos_booked1_only_the_honoured_launch_is_given_inherit(daemon, monkeypatch):
    """The receipts: what each guardian gave its provider, and the unhonoured one's clamp."""
    found = booked_pair(daemon, monkeypatch)
    assert found["honoured"]["job"]["provider_qos"] == found["unhonoured"]["job"]["provider_qos"] == "default"
    assert found["honoured"]["start"]["provider_qos"] == "inherit"
    assert found["unhonoured"]["start"]["provider_qos"] == "utility"
    clamped = found["unhonoured"]["report"]
    assert clamped["child"] == [UTILITY] * len(clamped["child"]), clamped
    assert BACKGROUND < min(clamped["provider"]) and max(clamped["provider"]) <= UTILITY, clamped


def test_d_qos_booked1_the_honoured_provider_reads_the_default_and_the_other_utility(daemon, monkeypatch):
    """The priorities: 31 against 20, where this test runs at the default QoS."""
    if own_priority() <= UTILITY:
        pytest.skip(f"this test process is itself clamped (PRI {own_priority()}): an unclamped provider "
                    "inherits that, so the two read alike; CI runs it unclamped")
    found = booked_pair(daemon, monkeypatch)
    honoured, clamped = found["honoured"]["report"], found["unhonoured"]["report"]
    assert max(honoured["provider"]) > UTILITY and min(honoured["child"]) > UTILITY, honoured
    if own_priority() == DEFAULT:
        assert honoured["child"] == [DEFAULT] * len(honoured["child"]), honoured   # idle: the default's base
    assert clamped["child"] == [UTILITY] * len(clamped["child"]), clamped
    assert max(clamped["provider"]) <= UTILITY, clamped
