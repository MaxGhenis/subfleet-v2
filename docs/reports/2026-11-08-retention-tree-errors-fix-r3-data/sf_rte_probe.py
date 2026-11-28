"""Record real maintenance passes and ACL repair calls in serial pytest runs."""

from functools import wraps
import json
from pathlib import Path

import pytest


RESULT_FIELDS = (
    "pruned", "deferred", "awaiting_check2", "interrupted",
    "holder_scan_failed", "progressed", "listings",
)
_active = None
_output = None


def _record(**fields):
    with _output.open("a") as stream:
        stream.write(json.dumps({"nodeid": _active["nodeid"], **fields}, sort_keys=True) + "\n")


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    from subfleet import attempt_tmp, retention

    global _output
    _output = Path(config.getoption("basetemp")).with_suffix(".probe.jsonl")
    assert not _output.exists()
    real_maintenance = retention.maintenance
    real_clear_entry_acl = attempt_tmp._clear_entry_acl

    @wraps(real_maintenance)
    def maintenance(*args, **kwargs):
        if _active is None:
            return real_maintenance(*args, **kwargs)
        _active["passes"] += 1
        call = _active["passes"]
        result = real_maintenance(*args, **kwargs)
        _record(call=call, **{field: result.get(field) for field in RESULT_FIELDS})
        return result

    @wraps(real_clear_entry_acl)
    def clear_entry_acl(*args, **kwargs):
        if _active is not None:
            _active["clear_entry_acl_calls"] += 1
        return real_clear_entry_acl(*args, **kwargs)

    retention.maintenance = maintenance
    attempt_tmp._clear_entry_acl = clear_entry_acl


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    global _active
    _active = {"nodeid": item.nodeid, "passes": 0, "clear_entry_acl_calls": 0}
    try:
        yield
    finally:
        _record(passes=_active["passes"], clear_entry_acl_calls=_active["clear_entry_acl_calls"])
        _active = None
