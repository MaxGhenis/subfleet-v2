"""A mutation is killed by a failed assertion, not an unsupported test harness."""
from types import SimpleNamespace

import pytest

from tools import daemon_log_cut_mutations as mutations


@pytest.mark.parametrize("failure,expected", [(AssertionError, 0), (NotImplementedError, 1), (None, 1)])
def test_only_an_assertion_failure_kills_a_mutation(monkeypatch, capsys, failure, expected):
    state = SimpleNamespace(mutated=False)

    def mutate(patch):
        patch.setattr(state, "mutated", True)

    def probe():
        if state.mutated and failure is not None:
            raise failure("probe outcome")

    monkeypatch.setattr(mutations, "MUTATIONS", {"baseline": lambda patch: None, "mutant": mutate})
    monkeypatch.setattr(mutations, "TESTS", [("probe", probe, {})])
    assert mutations.main(["baseline", "mutant"]) == expected


def test_a_baseline_harness_error_refuses_the_sweep(monkeypatch, capsys):
    def unsupported():
        raise NotImplementedError("unsupported counter expression")

    monkeypatch.setattr(mutations, "MUTATIONS", {"baseline": lambda patch: None})
    monkeypatch.setattr(mutations, "TESTS", [("probe", unsupported, {})])
    assert mutations.main(["baseline"]) == 1
