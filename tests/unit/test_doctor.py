"""`subfleet doctor`: the checks that gate the cutover (C-17.1, C-17.3).

Every test names the clause it proves (C-20.5). The provider probes are stubbed
throughout: this suite measures the table's logic, not whether the machine
running it happens to have `claude` and `codex` on PATH.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from subfleet import cli, doctor
from subfleet.contracts import Exit

STATUSES = {doctor.PASS, doctor.FAIL, doctor.UNKNOWN}


@pytest.fixture
def stub_probes(monkeypatch):
    """Every subprocess the table would run answers 0 with a stub version line."""
    def run(argv, timeout=20.0):
        return 0, f"stub {Path(argv[0]).name}"
    monkeypatch.setattr(doctor, "_run", run)
    monkeypatch.setattr(doctor, "_run_full", run)
    return run


# --- the shape of a row -------------------------------------------------------

def test_every_row_is_pass_fail_or_unknown_with_a_fix(root, stub_probes):
    """C-17.3 the exit code is decided by these statuses, so each row reports
    pass, fail, or unknown, and carries a fix line whatever it reports."""
    rows = doctor.checks(root)
    assert rows, "the table is not empty"
    for item in rows:
        assert set(item) == {"check", "status", "detail", "fix"}
        assert item["status"] in STATUSES
        assert item["detail"].strip() and item["fix"].strip()


def test_the_table_covers_every_check_the_lane_brief_names(root, stub_probes):
    """C-17.1 `doctor` is the cutover gate: the compat table (C-17.1), the hook
    entries (C-15.2), the symlink, the PATH shadows, the state root (C-2.2), and
    whether daemon.lock names a live process (C-5.8)."""
    names = {item["check"] for item in doctor.checks(root)}
    assert {"compat table loads",
            "hook entries in ~/.claude/settings.json",
            "subfleet symlink target",
            "PATH shadows for subfleet",
            "PATH shadows for claude",
            "PATH shadows for codex",
            "state root layout",
            "daemon.lock names a live process"} <= names


def test_an_unknown_never_decides_the_exit_code(root):
    """C-17.3 exit 0 is ok and 1 is an operational error; `unknown` is neither."""
    unknown = [doctor.row("a", doctor.UNKNOWN, "could not look", "look by hand")]
    assert doctor.exit_code(unknown) == int(Exit.OK)
    assert doctor.exit_code([*unknown, doctor.row("b", doctor.PASS, "d", "f")]) == 0
    assert doctor.exit_code([*unknown, doctor.row("b", doctor.FAIL, "d", "f")]) == \
        int(Exit.OPERATIONAL)


def test_render_prints_a_fix_line_for_everything_that_is_not_a_pass():
    """C-17.3 a non-zero exit sends someone to this table, so the fix sits
    beside the finding rather than in the documentation."""
    text = doctor.render([doctor.row("ok thing", doctor.PASS, "fine", "nothing"),
                          doctor.row("bad thing", doctor.FAIL, "broken", "do this"),
                          doctor.row("dark", doctor.UNKNOWN, "no idea", "look here")])
    assert "fix: do this" in text and "fix: look here" in text
    assert "fix: nothing" not in text


# --- the compat table ---------------------------------------------------------

def test_compat_row_passes_when_every_rule_reaches_a_verb():
    """C-17.1 the whole point of the row: a rule whose target verb was renamed
    is a command that used to work and now prints a usage error."""
    item = doctor.check_compat_table()
    assert item["status"] == doctor.PASS
    assert "rules" in item["detail"] and "reachable" in item["detail"]


def test_compat_row_fails_and_names_the_broken_rules(monkeypatch):
    """C-17.3 a fail names what is wrong, not just that something is."""
    from subfleet import compat
    monkeypatch.setattr(compat, "self_check", lambda: {
        "rules": 3, "verbs": 2, "env": 1, "unreachable": ["jobs -> runs"]})
    item = doctor.check_compat_table()
    assert item["status"] == doctor.FAIL and "jobs -> runs" in item["detail"]


def test_compat_row_fails_when_the_module_will_not_import(monkeypatch):
    """C-17.1 every v1 invocation goes through this table, so a table that
    cannot load is a blocked cutover — a fail, never an unknown."""
    import builtins
    real = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "subfleet.compat" or (args and args[2] == ("compat",)):
            raise ImportError("stubbed")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    item = doctor.check_compat_table()
    assert item["status"] == doctor.FAIL and "did not import" in item["detail"]


# --- hook entries -------------------------------------------------------------

def test_hook_row_fails_until_daemon_install_hooks_has_run(tmp_path):
    """C-15.2 the three entries must be in ~/.claude/settings.json to deliver."""
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    item = doctor.check_hook_entries(settings)
    assert item["status"] == doctor.FAIL
    assert "PostToolUse" in item["detail"] and "--hooks" in item["fix"]


def test_hook_row_passes_once_they_match_and_reports_v1s_own(tmp_path, monkeypatch):
    """v1's entries are reported, never rewritten: its PreToolUse guard is the
    only thing enforcing the front-door rule inside a session (C-14.3)."""
    from subfleet import hooks
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "~/cos/subfleet/bin/subfleet-hook pre-bash"}]}]}}))
    monkeypatch.setenv("SUBFLEET_HOOK_COMMAND", "/bin/sf hook")
    hooks.apply(settings, command="/bin/sf hook")
    item = doctor.check_hook_entries(settings)
    assert item["status"] == doctor.PASS
    assert "v1 bin/subfleet-hook still installed for PreToolUse" in item["detail"]


def test_hook_row_is_unknown_when_the_settings_file_will_not_parse(tmp_path):
    """C-17.3 an unknown never decides the exit code: a file that cannot be read
    is an unreadable check, not a failing one."""
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    item = doctor.check_hook_entries(settings)
    assert item["status"] == doctor.UNKNOWN and str(settings) in item["detail"]


# --- the never-rules guard ----------------------------------------------------

def test_never_rules_row_reads_the_settings_file(tmp_path):
    """C-14.3 doctor reports whether ~/.claude/settings.json carries the hook."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    item = doctor.check_never_rules(settings)
    assert item["status"] == doctor.FAIL and "C-14.3" in item["detail"]

    settings.write_text(json.dumps(
        {"hooks": {"PreToolUse": [{"command": "guard-never-rules.sh"}]}}))
    assert doctor.check_never_rules(settings)["status"] == doctor.PASS

    settings.unlink()
    absent = doctor.check_never_rules(settings)
    assert absent["status"] == doctor.UNKNOWN and "C-14.3" in absent["detail"]


# --- the symlink and PATH -----------------------------------------------------

def test_symlink_row_fails_while_subfleet_still_resolves_into_v1(tmp_path,
                                                                monkeypatch):
    """C-17.1 and plan amendment 8: the shadow period runs both installs and the
    symlink is the visible half of the flip."""
    v1 = tmp_path / "chief-of-staff" / "subfleet" / "bin"
    v1.mkdir(parents=True)
    binary = v1 / "subfleet"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(doctor, "_run_full", lambda argv, timeout=20.0: (0, "v1"))
    item = doctor.check_symlink()
    assert item["status"] == doctor.FAIL and "still v1" in item["detail"]


def test_symlink_row_catches_a_v2_path_that_imports_v1(tmp_path, monkeypatch):
    """C-17.1 found by running this table for real: PYTHONPATH outranks the
    install, so a v2 console script in a v2 virtualenv can answer with v1's verb
    table and every command an agent types reaches v1."""
    binary = tmp_path / "subfleet"
    binary.write_text("#!/bin/sh\nexit 2\n")
    binary.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(doctor, "_run_full", lambda argv, timeout=20.0: (
        2, "usage: subfleet [-h]\n{status,...,_session-hook,...}\n"
           "subfleet: error: unrecognized arguments: -V"))
    item = doctor.check_symlink()
    assert item["status"] == doctor.FAIL
    assert "imports v1" in item["detail"] and "PYTHONPATH" in item["fix"]


def test_symlink_row_is_unknown_when_the_binary_will_not_answer(tmp_path,
                                                               monkeypatch):
    """C-17.3 a binary that will not answer is an unknown: looked, could not
    tell. It is never reported as a pass and never decides the exit code."""
    binary = tmp_path / "subfleet"
    binary.write_text("#!/bin/sh\nexit 3\n")
    binary.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(doctor, "_run_full", lambda argv, timeout=20.0: (3, "boom"))
    assert doctor.check_symlink()["status"] == doctor.UNKNOWN


def test_pythonpath_row_names_the_entry_that_shadows_the_install(tmp_path,
                                                                 monkeypatch):
    """C-17.1 PYTHONPATH precedes site-packages and an editable install's .pth,
    so a `subfleet` package on it wins for every entry point, invisibly."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert doctor.check_pythonpath()["status"] == doctor.PASS

    (tmp_path / "subfleet").mkdir()
    (tmp_path / "subfleet" / "__init__.py").write_text("")
    item = doctor.check_pythonpath()
    assert item["status"] == doctor.FAIL and str(tmp_path) in item["detail"]

    monkeypatch.delenv("PYTHONPATH")
    assert doctor.check_pythonpath()["status"] == doctor.PASS


@pytest.mark.parametrize("name", ["claude", "codex", "subfleet"])
def test_path_shadows_reports_provider_ambiguity_but_fails_duplicate_front_doors(tmp_path, monkeypatch, name):
    """C-17.1: extra provider installs are not proven faults; extra front doors are."""
    first, second = tmp_path / "a", tmp_path / "b"
    for directory in (first, second):
        directory.mkdir()
        binary = directory / name
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    item = doctor.check_path_shadows(name)
    if name == "subfleet":
        assert item["status"] == doctor.FAIL and "first wins" in item["detail"]
        assert doctor.exit_code([item]) != 0
    else:
        assert item["status"] == doctor.UNKNOWN
        assert f"effective: {first / name}" in item["detail"]
        assert f"alternatives: {second / name}" in item["detail"]
        assert doctor.exit_code([item]) == 0
        assert "remove" not in item["fix"]

    monkeypatch.setenv("PATH", str(first))
    assert doctor.check_path_shadows(name)["status"] == doctor.PASS


def test_codex_wrapper_and_standalone_and_app_installations_report_actual_order(tmp_path, monkeypatch):
    """No name-based allowlist blesses a wrapper or asks the user to delete an app."""
    binaries = []
    for directory in (tmp_path / "bin", tmp_path / ".bun/bin", tmp_path / "ChatGPT.app/Contents/Resources"):
        directory.mkdir(parents=True)
        binary = directory / "codex"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        binaries.append(binary)
    for ordered in (binaries, list(reversed(binaries))):
        monkeypatch.setenv("PATH", os.pathsep.join(str(path.parent) for path in ordered))
        item = doctor.check_path_shadows("codex")
        assert item["status"] == doctor.UNKNOWN
        assert f"effective: {ordered[0]}" in item["detail"]
        assert all(str(path) in item["detail"] for path in ordered)
        assert "unverified" in item["detail"] and "remove" not in item["fix"]


def test_a_missing_subfleet_on_path_fails_but_a_missing_provider_is_unknown(
        tmp_path, monkeypatch):
    """C-17.3 the distinction the statuses exist for: nothing can be typed
    without `subfleet`, so its absence is a fail, while a provider may simply
    not be installed on the machine running the check."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert doctor.check_path_shadows("subfleet")["status"] == doctor.FAIL
    assert doctor.check_path_shadows("claude")["status"] == doctor.UNKNOWN


# --- the state root and the lock ----------------------------------------------

def test_state_root_row_is_unknown_before_the_daemon_has_ever_run(tmp_path):
    """C-2.2 the daemon creates the root; its absence is not a broken install."""
    item = doctor.check_state_root(tmp_path / "never-created")
    assert item["status"] == doctor.UNKNOWN and "daemon start" in item["fix"]


def test_state_root_row_passes_once_the_store_exists(root):
    """C-2.2 lists what belongs in the state root; the store is the anchor."""
    (root / "state.sqlite3").write_text("")
    item = doctor.check_state_root(root)
    assert item["status"] == doctor.PASS and "state.sqlite3" in item["detail"]


def test_socket_path_row_fails_when_sun_path_cannot_hold_it(root, tmp_path):
    """C-2.1 a SUBFLEET_HOME too deep for AF_UNIX can never hold a daemon.

    `root` is the short `/tmp` state root the suite uses precisely because
    pytest's own `tmp_path` is already close to the cap — which is itself the
    reason this row exists.
    """
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    item = doctor.check_socket_path(deep)
    assert item["status"] == doctor.FAIL and "shorter path" in item["detail"]
    assert doctor.check_socket_path(root)["status"] == doctor.PASS


def test_daemon_lock_row_passes_with_neither_lock_nor_socket(root):
    """C-5.8 no lock and no socket is a consistent state: no daemon."""
    item = doctor.check_daemon_lock(root)
    assert item["status"] == doctor.PASS and "no daemon" in item["detail"]


def test_daemon_lock_row_fails_on_a_lock_whose_process_is_gone(root):
    """C-5.8 a lock naming a dead pid beside a live socket is the stale case."""
    (root / "daemon.sock").touch()
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": "1", "proc_start": "Mon Jan  1 00:00:00 2001"}))
    item = doctor.check_daemon_lock(root)
    assert item["status"] == doctor.FAIL and "stale" in item["detail"]


def test_daemon_lock_row_fails_on_a_socket_with_no_lock(root):
    """C-5.8 a socket nothing holds is an inconsistency, not an unknown."""
    (root / "daemon.sock").touch()
    item = doctor.check_daemon_lock(root)
    assert item["status"] == doctor.FAIL and "no daemon.lock" in item["detail"]


def test_daemon_lock_row_passes_against_a_live_daemon(daemon, root):
    """C-5.3 the lock is verified by process identity, not by a bare pid; here
    the fake daemon holds the socket and the lock names this very process."""
    from subfleet.client import boot_id, proc_start
    daemon({"ping": lambda request: {"pong": True, "version": "test"}})
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": os.getpid(), "boot_id": boot_id(), "proc_start": proc_start(os.getpid())}))
    assert doctor.check_daemon_lock(root)["status"] == doctor.PASS


# --- --live -------------------------------------------------------------------

def test_live_pings_the_daemon(daemon, root):
    """C-16.2 `--live` is one `ping`; the version in the reply is the daemon's."""
    daemon({"ping": lambda request: {"pong": True, "version": "2.0.0a0"}})
    item = doctor.check_live(root)
    assert item["status"] == doctor.PASS and "2.0.0a0" in item["detail"]


def test_live_fails_when_nothing_is_listening(root):
    """C-17.5 everything that needs the daemon exits 69 when it is not there,
    and `--live` is exactly the check that needs it."""
    item = doctor.check_live(root)
    assert item["status"] == doctor.FAIL and "daemon start" in item["fix"]


def test_live_adds_a_row_and_the_offline_table_does_not_have_it(daemon, root,
                                                               stub_probes):
    """C-17.5 the offline table reads files only; the probe is opt-in."""
    daemon({"ping": lambda request: {"pong": True, "version": "t"}})
    names = {item["check"] for item in doctor.checks(root)}
    live = {item["check"] for item in doctor.checks(root, live=True)}
    assert "ping the daemon" in live and "ping the daemon" not in names


# --- through the CLI ----------------------------------------------------------

def test_cli_doctor_exits_one_when_anything_failed(root, capsys, monkeypatch,
                                                   stub_probes):
    """C-17.3 exit 1 is an operational error; the fix line rides along."""
    (root / "daemon.sock").touch()
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": "1", "proc_start": "Mon Jan  1 00:00:00 2001"}))
    assert cli.main(["doctor"]) == int(Exit.OPERATIONAL)
    text = capsys.readouterr().out
    assert "FAIL" in text and "fix:" in text


def test_cli_doctor_json_emits_one_object_per_check(root, capsys, stub_probes):
    """C-17.4 `--json` emits JSON objects only, no prose."""
    cli.main(["doctor", "--json"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]
    assert rows and all(set(item) == {"check", "status", "detail", "fix"}
                        for item in rows)
    assert all(item["status"] in STATUSES for item in rows)


def test_cli_doctor_live_reaches_the_ping(daemon, root, capsys, stub_probes):
    """C-17.1 `doctor --live` is no longer reserved: it pings the daemon."""
    daemon({"ping": lambda request: {"pong": True, "version": "t"}})
    cli.main(["doctor", "--live", "--json"])
    names = {json.loads(line)["check"]
             for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert "ping the daemon" in names


def test_doctor_checks_is_the_same_table(root, stub_probes):
    """C-17.1 there is one `doctor`, so there is one table: `cli.doctor_checks`
    is a call into `doctor.checks` and not a second list of checks."""
    assert [item["check"] for item in cli.doctor_checks(root)] == \
           [item["check"] for item in doctor.checks(root)]


def test_pythonpath_row_passes_when_the_interpreter_ignores_its_environment(monkeypatch, tmp_path):
    """Decision 2026-09-05 §3: a shadowing PYTHONPATH is harmless under python -E, which bin/sf2 uses."""
    (tmp_path / "subfleet").mkdir()
    (tmp_path / "subfleet" / "__init__.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(doctor, "_ignores_environment", lambda: False)
    assert doctor.check_pythonpath()["status"] == doctor.FAIL
    monkeypatch.setattr(doctor, "_ignores_environment", lambda: True)
    item = doctor.check_pythonpath()
    assert item["status"] == doctor.PASS and "-E" in item["detail"]


# --- C-14.2, C-23.5: the Codex guard preflight rows -------------------------

def test_guard_preflight_settings_row_reports_deadline_and_markers(root, stub_probes, monkeypatch):
    """Offline, doctor shows the effective hooks/list deadline and how many verdicts are current."""
    from subfleet.guard import preflight as guard
    monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    monkeypatch.setenv(guard.CACHE_ENV, str(root / "guard-cache"))
    rows = {item["check"]: item for item in doctor.checks(root)}
    item = rows["codex guard preflight settings"]
    assert item["status"] == doctor.PASS
    assert "deadline 60s (default)" in item["detail"] and "0 current verified marker" in item["detail"]
    guard.write_cached_verdict(root / "guard-cache", "a" * 64, {"verified_at": "2099-01-01T00:00:00+00:00"})
    guard.write_cached_verdict(root / "guard-cache", "b" * 64, {"verified_at": "2000-01-01T00:00:00+00:00"})
    monkeypatch.setenv(guard.TIMEOUT_ENV, "120")
    item = {i["check"]: i for i in doctor.checks(root)}["codex guard preflight settings"]
    assert "deadline 120s (env)" in item["detail"] and "1 current verified marker" in item["detail"]


def test_guard_preflight_settings_row_fails_on_an_unusable_deadline(root, stub_probes, monkeypatch):
    from subfleet.guard import preflight as guard
    monkeypatch.setenv(guard.TIMEOUT_ENV, "soon")
    item = {i["check"]: i for i in doctor.checks(root)}["codex guard preflight settings"]
    assert item["status"] == doctor.FAIL
    assert guard.TIMEOUT_ENV in item["detail"] and guard.TIMEOUT_ENV in item["fix"]
    assert doctor.exit_code(doctor.checks(root)) == int(Exit.OPERATIONAL)


def test_live_guard_preflight_rows_cover_each_enabled_codex_lane(root, stub_probes, monkeypatch):
    """`doctor --live` runs the trust preflight per Codex lane home (C-14.2)."""
    from subfleet.guard import preflight as guard
    lanes = [
        {"lane_id": "codex-1", "provider": "codex", "enabled": 1, "home": "/fixture/codex-1"},
        {"lane_id": "codex-2", "provider": "codex", "enabled": 0, "home": "/fixture/codex-2"},
        {"lane_id": "claude-1", "provider": "claude", "enabled": 1, "home": None},
        {"lane_id": "codex-3", "provider": "codex", "enabled": 1, "home": None, "credential_ref": "/fixture/codex-3"},
    ]

    class Roster:
        def __init__(self, _root):
            pass

        def lanes(self):
            return lanes

    monkeypatch.setattr("subfleet.offline.Offline", Roster)
    calls = []

    def fake_preflight(binary, *, home, workdir, **kwargs):
        calls.append(home)
        ok = home != "/fixture/codex-3"
        return guard.PreflightResult(ok, 0 if ok else 7,
                                     "verified" if ok else "Guard preflight timed out: no answer",
                                     None if ok else guard._TIMEOUT_FIX, kind=guard.CACHED if ok else guard.TIMEOUT,
                                     cached=ok, timeout_s=60.0, elapsed_s=0.2, probe_pid=None if ok else 77)

    monkeypatch.setattr(guard, "preflight", fake_preflight)
    monkeypatch.setattr(doctor, "check_live", lambda _root: doctor.row("daemon ping", doctor.PASS, "stub", "n/a"))
    rows = {item["check"]: item for item in doctor.checks(root, live=True)}
    assert calls == ["/fixture/codex-1", "/fixture/codex-3"], "enabled Codex lanes only"
    good = rows["codex guard preflight codex-1"]
    assert good["status"] == doctor.PASS and "cached" in good["detail"] and "deadline 60s" in good["detail"]
    bad = rows["codex guard preflight codex-3"]
    assert bad["status"] == doctor.FAIL and "probe pid 77" in bad["detail"] and "timed out" in bad["detail"]
    assert "daemon scheduling" in bad["fix"]
    assert "codex guard preflight codex-2" not in rows
    assert "codex guard preflight codex-1" not in {i["check"] for i in doctor.checks(root)}, "offline runs no probe"
