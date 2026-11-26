"""Deadline regressions from review r2, including expiry between clock reads."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from subfleet import retention_archive as rarch, retention_git as rgit


@pytest.mark.parametrize("last", [1.0, 2.0])
def test_expiry_between_listing_checkpoint_and_budget_starts_no_git(tmp_path, monkeypatch, last):
    """A scheduler pause between the two clock reads must not start a listing."""
    repository = tmp_path / "repo"
    rgit.run(["init", "--quiet", str(repository)], cwd=tmp_path)
    readings = iter([0.0, last])
    clock = lambda: next(readings, last)
    ctx = rarch.Context(tmp_path, SimpleNamespace(), clock=clock, deadline=1.0)
    original = rgit.run
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs["timeout"])
        return original(args, **kwargs)

    monkeypatch.setattr(rgit, "run", run)
    try:
        ctx.salvage_refs(repository / ".git")
    except rarch.Defer:
        pass
    print(f"listing timeout={seen}; cached={str(repository / '.git') in ctx.salvage_listings}")
    assert not seen, f"started real Git with non-positive budget {seen}"
    assert ctx.salvage_listings == {}


def test_ambient_probe_deadline_does_not_publish_partial_census(tmp_path, monkeypatch):
    now = [0.0]
    calls = []

    def run(args, **kwargs):
        calls.append(args[0])
        now[0] = 2.0
        raise rgit.GitError("ambient probe timed out")

    monkeypatch.setattr(rgit, "run", run)
    ctx = rarch.Context(tmp_path, SimpleNamespace(list_jobs=lambda: []), clock=lambda: now[0], deadline=1.0)
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        ctx.known_repositories()
    assert ctx.repositories is None
    assert calls == ["config"]


def test_ambient_probe_cancel_does_not_publish_partial_census(tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []

    def run(args, **kwargs):
        calls.append(args[0])
        cancel.set()
        return SimpleNamespace(stdout=b"global\tuser.name=review\n")

    monkeypatch.setattr(rgit, "run", run)
    ctx = rarch.Context(tmp_path, SimpleNamespace(list_jobs=lambda: []), cancel=cancel)
    with pytest.raises(rgit.Cancelled):
        ctx.known_repositories()
    assert ctx.repositories is None
    assert calls == ["config"]


def test_expiry_between_fast_fallback_checkpoint_and_budget_starts_no_git(tmp_path, monkeypatch):
    readings = iter([0.0, 0.0, 1.0])
    clock = lambda: next(readings, 1.0)
    seen = []
    monkeypatch.setattr(rgit, "_plain_common_dir", lambda path, checkpoint: None)
    original = rgit.run

    def run(args, **kwargs):
        seen.append(kwargs["timeout"])
        return original(args, **kwargs)

    monkeypatch.setattr(rgit, "run", run)
    try:
        rgit.fast_common_dir(tmp_path, deadline=1.0, clock=clock)
    except rgit.GitError:
        pass
    print(f"fast fallback timeout={seen}")
    assert not seen, f"started real Git with non-positive budget {seen}"


@pytest.mark.parametrize("spent", [1.0, 2.0])
def test_expiry_between_ambient_checkpoint_and_budget_starts_no_probe(tmp_path, monkeypatch, spent):
    readings = iter([0.0, 0.0, spent])
    clock = lambda: next(readings, spent)
    seen = []
    monkeypatch.setattr(rgit, "ambient_plain", lambda **kwargs: seen.append(kwargs))
    ctx = rarch.Context(tmp_path, SimpleNamespace(list_jobs=lambda: []),
                        clock=clock, deadline=1.0)
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        ctx.known_repositories()
    assert seen == []
    assert ctx.repositories is None


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_fast_fallback_rejects_nonpositive_timeout_without_deadline(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(rgit, "_plain_common_dir", lambda path, checkpoint: None)
    def forbidden(*args, **kwargs):
        pytest.fail("a spent fallback budget started Git")
    monkeypatch.setattr(rgit, "common_dir", forbidden)
    with pytest.raises(rgit.GitError, match="repository discovery deadline"):
        rgit.fast_common_dir(tmp_path, timeout=timeout)


@pytest.mark.parametrize("spent", [1.0, 2.0])
def test_expiry_between_census_fallback_checkpoint_and_budget_starts_no_git(tmp_path, monkeypatch, spent):
    readings = iter([0.0] * 7 + [spent])
    clock = lambda: next(readings, spent)
    seen = []
    monkeypatch.setattr(rgit, "ambient_plain", lambda **kwargs: False)
    original = rgit.run

    def run(args, **kwargs):
        seen.append(kwargs["timeout"])
        return original(args, **kwargs)

    monkeypatch.setattr(rgit, "run", run)
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        rarch.known_repositories([dict(job_id="row", sandbox="read-only", workdir=str(tmp_path))],
                                 tmp_path, deadline=1.0, clock=clock)
    print(f"census fallback timeout={seen}")
    assert not seen, f"started real Git with non-positive budget {seen}"
