"""C-8.4: a failed census must stop before any recovery step."""
import pytest

from subfleet import dependencies, retention
from tests.fake.test_retention_dependency_review_r2 import world, maintain


def test_failed_legacy_census_never_enters_recovery(world, monkeypatch):
    tree = world.job("owner")
    def failed(read):
        assert not world.store.connection.in_transaction
        raise OSError("legacy census unavailable")
    def forbidden(self):
        pytest.fail("recovery entered without a census")
    monkeypatch.setattr(dependencies, "legacy", failed)
    monkeypatch.setattr(retention._Pass, "_recover", forbidden)
    with pytest.raises(OSError, match="legacy census unavailable"):
        maintain(world)
    assert tree.is_dir()
    assert not (world.root / "retention").exists()


def test_recovery_and_archive_share_the_pass_census(world, monkeypatch):
    census = dependencies.Places()
    calls = []
    def discover(read):
        assert not world.store.connection.in_transaction
        calls.append(True)
        return census
    original = retention._Pass._recover
    def recover(self):
        assert self.legacy is census and self.ctx.legacy is census
        return original(self)
    monkeypatch.setattr(dependencies, "legacy", discover)
    monkeypatch.setattr(retention._Pass, "_recover", recover)
    maintain(world)
    assert calls == [True]
