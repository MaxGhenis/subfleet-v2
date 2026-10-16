"""C-5.1: both guardian spawn mechanisms create their session before exec."""

from types import SimpleNamespace

from subfleet import procs


def test_fallback_popen_establishes_the_session_before_exec(monkeypatch):
    calls = []
    monkeypatch.setattr(procs._spawn, "available", False)
    monkeypatch.setattr(procs, "_abandoned", [])
    monkeypatch.setattr(procs.subprocess, "Popen",
                        lambda argv, **options: calls.append(options) or SimpleNamespace(pid=42))
    procs.spawn_disclaimed(["python"], env={}, cwd="/fixture", pass_fds=(9,))
    assert calls[0]["start_new_session"] is True
    assert calls[0]["pass_fds"] == (9,)


def test_disclaimed_spawn_establishes_the_session_in_its_spawn_attributes(monkeypatch):
    flags = []

    def spawned(pid, *args):
        pid._obj.value = 42
        return 0

    names = ("posix_spawnattr_init", "posix_spawnattr_destroy", "posix_spawnattr_setsigdefault",
             "responsibility_spawnattrs_setdisclaim", "posix_spawn_file_actions_init",
             "posix_spawn_file_actions_destroy", "posix_spawn_file_actions_addopen",
             "posix_spawn_file_actions_addinherit_np", "posix_spawn_file_actions_addchdir_np")
    spi = SimpleNamespace(available=True, posix_spawn=spawned,
        posix_spawnattr_setflags=lambda attr, value: flags.append(value) or 0,
        **{name: (lambda *args: 0) for name in names})
    monkeypatch.setattr(procs, "_spawn", spi)
    monkeypatch.setattr(procs, "_abandoned", [])
    child = procs.spawn_disclaimed(["python"], env={}, cwd="/fixture", pass_fds=(9,))
    child.returncode = 0                 # Synthetic handle: no host waitpid or signal.
    assert flags == [procs._POSIX_SPAWN_CLOEXEC_DEFAULT | procs._POSIX_SPAWN_SETSIGDEF
                     | procs._POSIX_SPAWN_SETSID]
