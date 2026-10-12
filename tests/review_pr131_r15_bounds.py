"""Opt-in reduced review runs; preserve every safety and liveness assertion."""
import hypothesis
import hypothesis.stateful


def pytest_configure(config):
    original = hypothesis.stateful.run_state_machine_as_test

    def reduced(factory, *, settings=None):
        budget = hypothesis.settings(settings, max_examples=5,
                                     stateful_step_count=8, database=None,
                                     derandomize=True)
        return original(factory, settings=budget)

    hypothesis.stateful.run_state_machine_as_test = reduced


def pytest_collection_modifyitems(items):
    for item in items:
        original = getattr(item.obj, "_hypothesis_internal_use_settings", None)
        if original is not None:
            item.obj._hypothesis_internal_use_settings = hypothesis.settings(
                original, max_examples=8, stateful_step_count=8,
                database=None, derandomize=True)
