"""C-8.5, review r4 P3-2: abandoned intake trees disappear only at startup.

All state and sentinels belong to a temporary test root. A second daemon must
take daemon.lock before deleting anything a live intake could still be using.
"""
from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, host_push
from subfleet.daemon import Daemon, DaemonUnavailable


@pytest.fixture
def startup(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "test-start")

    def make(root):
        return Daemon(root, desktop_prober=lambda: None)
    return make


def abandoned_intake(root, name="intake-killed", *, kind="directory"):
    quarantine = root / "push-quarantine"
    quarantine.mkdir(exist_ok=True)
    intake = quarantine / name
    if kind == "file":
        intake.write_bytes(b"partial allocation")
    else:
        (intake / "repo.git/objects/pack").mkdir(parents=True)
        (intake / "repo.git/objects/pack/partial.pack").write_bytes(b"copied objects")
        (intake / "checkout").mkdir()
        (intake / "checkout/uncommitted").write_bytes(b"half a checkout")
    return intake


def assert_restart_clears_intake(startup, root):
    service = startup(root)
    service.close()
    intake = abandoned_intake(root)
    restarted = startup(root)
    try:
        assert not intake.exists(), "abandoned intake survived daemon restart"
        assert not (root / "push-quarantine").exists()
    finally:
        restarted.close()


def test_restart_removes_objects_and_partial_checkout_left_by_a_killed_intake(startup, tmp_path):
    assert_restart_clears_intake(startup, tmp_path / "state")


def test_startup_cleans_before_any_worker_or_push_thread_can_start(startup, monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    abandoned_intake(root)
    actual = daemon_module.ThreadPoolExecutor
    pools = []

    def checked_executor(*args, **kwargs):
        assert not (root / "push-quarantine").exists(), "workers started before intake cleanup"
        pools.append(kwargs.get("thread_name_prefix"))
        return actual(*args, **kwargs)
    monkeypatch.setattr(daemon_module, "ThreadPoolExecutor", checked_executor)
    service = startup(root)
    service.close()
    assert "subfleet-push" in pools


def test_a_live_daemons_intake_is_untouched_when_a_second_start_cannot_take_the_lock(startup, tmp_path):
    root = tmp_path / "state"
    service = startup(root)
    try:
        intake = abandoned_intake(root, "intake-live")
        sentinel = intake / "checkout/uncommitted"
        with pytest.raises(DaemonUnavailable, match="another daemon holds daemon.lock"):
            startup(root)
        assert sentinel.read_bytes() == b"half a checkout"
    finally:
        service.close()
    restarted = startup(root)
    try:
        assert not intake.exists()
    finally:
        restarted.close()


@pytest.mark.parametrize("parent_link", [False, True])
def test_startup_unlinks_quarantine_links_without_deleting_their_targets(startup, tmp_path, parent_link):
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_bytes(b"outside quarantine")
    quarantine = root / "push-quarantine"
    if parent_link:
        quarantine.symlink_to(outside, target_is_directory=True)
    else:
        quarantine.mkdir()
        (quarantine / "intake-link").symlink_to(outside, target_is_directory=True)
    service = startup(root)
    try:
        assert not quarantine.exists()
        assert not quarantine.is_symlink()
        assert sentinel.read_bytes() == b"outside quarantine"
    finally:
        service.close()


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12),
                          st.sampled_from(["directory", "file"])), min_size=1, max_size=8, unique_by=lambda x: x[0]))
def test_property_no_intake_name_survives_a_restart(startup, tmp_path, cases):
    root = tmp_path / "state"
    service = startup(root)
    service.close()
    intakes = [abandoned_intake(root, "intake-" + name, kind=kind) for name, kind in cases]
    restarted = startup(root)
    try:
        assert all(not intake.exists() for intake in intakes)
        assert not list(root.glob("push-quarantine/intake-*"))
    finally:
        restarted.close()


def test_mutation_skipping_startup_cleanup_is_caught(startup, monkeypatch, tmp_path):
    monkeypatch.setattr(host_push, "clear_quarantine", lambda root: None)
    with pytest.raises(AssertionError, match="abandoned intake survived daemon restart"):
        assert_restart_clears_intake(startup, tmp_path / "state")


def test_a_cleanup_failure_aborts_startup_and_releases_the_daemon_lock(startup, monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    intake = abandoned_intake(root)
    with monkeypatch.context() as patch:
        def failed_cleanup(root):
            raise OSError("quarantine cleanup failed")
        patch.setattr(host_push, "clear_quarantine", failed_cleanup)
        with pytest.raises(OSError, match="quarantine cleanup failed"):
            startup(root)
        assert intake.exists()
    # A later construction can take the same lock and finish the cleanup.
    service = startup(root)
    try:
        assert not intake.exists()
    finally:
        service.close()
