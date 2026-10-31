"""Same-input comparison for the two unchanged spelling strategy modules."""
import errno
import functools
import json
import os
from pathlib import Path

from subfleet import dependencies
from tests.unit.test_retention_location_readable import baseline_reader, r10_reader

DATA = Path(__file__).resolve().parent
COMPARISONS = []
NODE = ""


def pytest_runtest_setup(item):
    global NODE
    NODE = item.nodeid


def pytest_configure(config):
    original = dependencies.record
    baseline, r10 = baseline_reader(), r10_reader()

    @functools.wraps(original)
    def observed(*args, **kwargs):
        actual = original(*args, **kwargs)
        if not any(name in NODE for name in ("test_retention_location_inputs.py::",
                                              "test_retention_location_spelling.py::")):
            return actual
        before = baseline.record(*args, **kwargs)
        old = r10.record(*args, **kwargs)
        assert actual.get("incomplete", []) == before.get("incomplete", []) == old.get("incomplete", [])
        assert set(actual.get("git", [])) <= set(before.get("git", []))
        dropped = []
        for place in sorted(set(before.get("git", [])) - set(actual.get("git", []))):
            try:
                os.stat(place)
            except OSError as exc:
                assert exc.errno in (errno.ELOOP, errno.ENAMETOOLONG)
                dropped.append(dict(place=place, errno=exc.errno))
            else:
                raise AssertionError(("Readable place omitted", place))
        assert actual == before
        COMPARISONS.append(dict(node=NODE, args=args, kwargs=kwargs,
                                baseline=before, r10=old, actual=actual, dropped=dropped))
        return actual

    dependencies.record = observed
    config._r11_record_original = original


def pytest_unconfigure(config):
    dependencies.record = config._r11_record_original


def pytest_sessionfinish(session, exitstatus):
    destination = DATA / ("differential-" + os.environ["SF_R11_HOLD_NAME"] + ".json")
    destination.write_text(json.dumps(COMPARISONS, indent=2) + "\n")
