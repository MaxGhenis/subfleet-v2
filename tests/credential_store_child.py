"""Failing tests that hold the session's stores themselves (C-20.6, D-PRR1).

`tests/unit/test_session_credential_env.py` runs this file in a child session
that inherits dummy credential-shaped variables and sets one more after
configure, then searches the whole report for any part of their values. Each
test fails while it holds the store of removed credentials, or the session's
baseline snapshot, in one way a report prints a value: a local under `-l`, a
helper's argument, an assertion's operand, a comparison with a dict, captured
output and a Hypothesis draw. The suite does not collect this file: its name
does not match `test_*.py`.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st


def test_binds_the_store(pytestconfig):
    store = pytestconfig._subfleet_removed_credentials
    print(store)
    print(repr(store))
    assert not store


def test_binds_the_baseline_snapshot(pytestconfig):
    snapshot = pytestconfig._subfleet_original_home_env
    assert not snapshot


def test_compares_the_baseline_snapshot(pytestconfig):
    # pytest diffs two dicts item by item, sorted, so a snapshot holding a
    # credential's value prints it even under `-l` alone (#244 review r2's P3-3).
    assert pytestconfig._subfleet_original_home_env == {}


def test_compares_the_store(pytestconfig):
    assert pytestconfig._subfleet_removed_credentials == {}


def test_compares_the_store_with_its_own_names(pytestconfig):
    store = pytestconfig._subfleet_removed_credentials
    assert store == dict.fromkeys(store, "")


def _check(store):
    assert len(store) == 0


def test_passes_the_store_on(pytestconfig):
    _check(pytestconfig._subfleet_removed_credentials)


@settings(max_examples=1, database=None, derandomize=True, deadline=None)
@given(data=st.data())
def test_draws_the_store(pytestconfig, data):
    store = data.draw(st.just(pytestconfig._subfleet_removed_credentials), label="store")
    assert not store
