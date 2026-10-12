"""Round-2 regression: durable claim latency must not invent a spend."""
from __future__ import annotations

from contextlib import contextmanager
import time
from types import SimpleNamespace

from subfleet import actions as actions_module
from subfleet.store import Store
from tests.unit import test_reset_credit_properties as properties
from tests.unit.test_reset_credit_properties import Step, World, fake_transport_clock, play  # noqa: F401 (fixture)


def test_full_delayed_claim_has_no_submission_and_no_interval_violation(monkeypatch):
    """The reviewer's exact two-step world and 61-second FULL claim delay, without sleeping."""
    world = World(lanes=(("limited", True, 6, False), ("limited", True, 2, False)),
                  jobs=(frozenset(),), steps=(Step(0, "none", None), Step(10, "used-up", None)),
                  enabled=True, interval=30, adversarial=None)
    monotonic, providers = [0.0], []
    monkeypatch.setattr(actions_module, "time", SimpleNamespace(monotonic=lambda: monotonic[0]))
    transaction, build = Store.transaction, properties._build

    @contextmanager
    def delayed_commit(store, kind, **kwargs):
        with transaction(store, kind, **kwargs) as connection:
            yield connection
        if kind == "action.executing":
            assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
            monotonic[0] += 61

    def capture_provider(*args, **kwargs):
        built = build(*args, **kwargs)
        providers.append(built[1])
        return built

    monkeypatch.setattr(Store, "transaction", delayed_commit)
    monkeypatch.setattr(properties, "_build", capture_provider)
    violations, decisions = play(world)
    assert [status for status, _, _ in decisions] == ["failed", "failed"]
    assert not [call for provider in providers for call in provider.calls if call[0] == "POST"]
    assert not violations, violations


def test_restart_equivalence_survives_full_commit_delays(monkeypatch):
    """The fixture controls transport time even if the host's monotonic clock jumps at commit."""
    world = World(lanes=(("limited", True, 6, False), ("limited", True, 2, False)),
                  jobs=(frozenset(),), steps=(Step(0, "none", None), Step(10, "used-up", None)),
                  enabled=True, interval=30, adversarial=None)
    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    transaction = Store.transaction

    @contextmanager
    def delayed_commit(store, kind, **kwargs):
        with transaction(store, kind, **kwargs) as connection:
            yield connection
        if kind == "action.executing":
            assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
            elapsed[0] += 61

    monkeypatch.setattr(Store, "transaction", delayed_commit)
    violations, steady = play(world)
    restarted_violations, restarted = play(world, restart_every_step=True)
    assert elapsed[0] == 122
    assert not violations and not restarted_violations
    assert steady == restarted
    assert [status for status, _, _ in steady] == ["confirmed", "interval-blocked"]
