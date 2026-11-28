"""C-3.8: the contention rig counts the outer hold containing a reservation."""

import contextlib
import importlib.util
import threading
import time
from pathlib import Path

import pytest

from subfleet.daemon import ADMISSION_EVENT
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


def install_meter(service):
    """Execute the rig's wrappers, without its process setup or server loop."""
    path = Path(__file__).resolve().parents[2] / "tools" / "store_contention_repro.py"
    spec = importlib.util.spec_from_file_location("store_contention_meter_repro", path)
    repro = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repro)
    start = repro.DAEMON.index("import contextlib\n")
    meter = repro.DAEMON[start:repro.DAEMON.index("real_boundary =")]
    namespace = {"daemon": service, "ADMISSION_EVENT": ADMISSION_EVENT,
                 "threading": threading, "time": time}
    exec(compile(meter, str(path), "exec"), namespace)
    namespace["measuring"].set()
    return namespace["admission"]


@pytest.mark.parametrize("enclosing_lock", [True, False], ids=["enclosing-route-lock", "transaction-outermost"])
def test_c3_8_reserving_hold_is_counted_with_and_without_an_enclosing_lock(
        routing_state, monkeypatch, enclosing_lock):  # noqa: F811
    """One real admission reserves once; the pre-round-1 lock shape remains supported."""
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    if not enclosing_lock:
        # Before fix round 1 the route context added no store-lock hold around
        # the transaction. Keep the real admission path and change only that shape.
        @contextlib.contextmanager
        def unlocked_route(job, holds):
            yield {"failed": False}

        monkeypatch.setattr(service, "_isolated_route", unlocked_route)
    meter = install_meter(service)
    service._admit_pass({}, {"placed": 0})
    assert [row["state"] for row in service.store.list_attempts(job_id)] == ["reserved"]
    assert len(meter["holds"]) == len(meter["hold_cpu"]) == 2
    assert len(meter["reserve_holds"]) == len(meter["reserve_hold_cpu"]) == 1
    assert meter["reserve_holds"][0] in meter["holds"]
    assert meter["reserve_hold_cpu"][0] in meter["hold_cpu"]
