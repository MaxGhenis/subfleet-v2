"""Dispose only completed task-owned Hypothesis example fleets between draws."""
import shutil
from pathlib import Path
import pytest

@pytest.fixture
def tmp_path_factory(tmp_path_factory):
    original = tmp_path_factory
    class Factory:
        previous = None
        def mktemp(self, basename, numbered=True):
            if basename == 'liveness' and self.previous is not None:
                assert str(self.previous.resolve()).startswith(str(Path(__file__).resolve().parents[1] / 'build/review/pytest') + '/')
                shutil.rmtree(self.previous)
                self.previous = None
            path = original.mktemp(basename, numbered=numbered)
            if basename == 'liveness':
                self.previous = path
            return path
        def __getattr__(self, name):
            return getattr(original, name)
    factory = Factory()
    yield factory
    if factory.previous is not None:
        shutil.rmtree(factory.previous)
