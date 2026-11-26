"""Failing tests a child session runs with dummy credentials set (C-20.6, D-PRR1).

`tests/unit/test_session_credential_env.py` starts a pytest session on this file
with dummy credential-shaped variables in its environment, then searches the
whole report for any part of their values. Each failure reaches one way a report
prints values: a frame's arguments (`subprocess.run`'s `kwargs`, the path that
printed a lane's token on 2026-10-21), `-l` locals, captured output, an
assertion's operands and Hypothesis's falsifying example. The suite does not
collect this file: its name does not match `test_*.py`.
"""

from __future__ import annotations

import os
import subprocess

from hypothesis import given, settings, strategies as st

#: The dummies the parent sets, the empty one included; names only.
DUMMY_NAMES = (
    "SUBFLEET_DUMMY_TOKEN", "PRR1_DUMMY_API_KEY", "PRR1_DUMMY_SECRET", "PRR1_DUMMY_PASSWORD",
    "PRR1_DUMMY_COOKIE", "PRR1_DUMMY_CREDENTIALS", "PRR1_DUMMY_AUTH_HEADER",
    "prr1_dummy_lower_token", "PRR1_DUMMY_EMPTY_TOKEN",
)


def test_no_dummy_reaches_a_test():
    assert [name for name in DUMMY_NAMES if name in os.environ] == []


def test_arguments_locals_and_captured_output():
    values = {name: os.environ.get(name) for name in DUMMY_NAMES}
    print(dict(os.environ))
    assert values
    subprocess.run(["/bin/sh", "-c", "exit 3"], env={**os.environ}, check=True)


@settings(max_examples=1, database=None, derandomize=True, deadline=None)
@given(st.deferred(lambda: st.just(dict(os.environ))))
def test_a_falsifying_example(environment):
    assert environment is None
