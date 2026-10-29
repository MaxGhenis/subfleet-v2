"""#271 fix r3 and r4: the named mutants of fix r3's decisions, each against its killer.

Run with pytest from the repository root. As in fix r2's harness, production files are never
edited: each mutant replaces one function or method, compiled from its source with one change,
or one constant, for the length of one test. A mutant counts as killed only when its named
test fails with an AssertionError, and each killer passes unmutated (the controls).

- Decision 2 (fix r3): the unknown backlog read as zero, and the deviation's short-read
  catch-up removed (`TurnRunner` methods).
- Decision 1, built in fix r4 (`subfleet/relay.py`): round 3's two (the helper without its
  peek, a peek that reaps) and round 4's six (each of the relay's three exit reads left on
  `poll()`, ECHILD read as "not exited", another OSError read as "exited", the ctypes binding
  without WNOWAIT). Their killers peek through the binding this interpreter uses, except the
  ctypes mutant's, which runs on every Python. `test_report_whether_the_property_kills_the_peek_mutant`
  prints whether the exit-peek property catches the peek mutants too (shrinking off).
"""

from contextlib import contextmanager
import errno
import inspect
import os
import textwrap

from hypothesis import Phase, settings
import pytest

from subfleet import relay as relay_module
from subfleet.conversations import runner as runner_module
from tests.fake import test_turn_relay_exit_peek as peek
from tests.fake import test_turn_relay_exit_r3 as r3
from tests.fake import test_turn_relay_exit_recovery as recovery

SHORT_READ = ("and self.offset + len(self.partial) - position < READ_CHUNK):",
              "and not progressed):")
MUTANTS = {
    # The brief's: an entry size still unknown after the retries is read as zero.
    "unknown-backlog-read-as-zero": ("_stdout_size", ("    return None", "    return 0")),
    # The deviation's: only an empty read catches up an unknown entry size (the decision as written).
    "short-read-catch-up-removed": ("_drain_stdout", SHORT_READ),
}
KILLERS = {
    "unknown-backlog-read-as-zero": lambda r, patch:
        r3.test_every_entry_stat_failing_still_reads_the_whole_backlog_and_settles(r, patch, "proof"),
    "short-read-catch-up-removed": lambda r, patch:
        r3.test_an_unknown_entry_size_still_bounds_a_writer_that_never_pauses(r, patch),
}


def apply(patch, name):
    method, (before, after) = MUTANTS[name]
    source = textwrap.dedent(inspect.getsource(getattr(runner_module.TurnRunner, method)))
    assert source.count(before) == 1, (name, before)
    scope = dict(vars(runner_module))
    exec(compile(source.replace(before, after), f"<fix r3 mutant {name}>", "exec"), scope)
    patch.setattr(runner_module.TurnRunner, method, scope[method])


@pytest.mark.parametrize("name", list(MUTANTS))
def test_the_named_killer_kills_the_mutant(tmp_path, name):
    with pytest.MonkeyPatch.context() as patch:
        apply(patch, name)
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            with pytest.raises(AssertionError):
                KILLERS[name](r, patch)


@pytest.mark.parametrize("name", list(MUTANTS))
def test_the_named_killer_passes_unmutated(tmp_path, name):
    with pytest.MonkeyPatch.context() as patch:
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            KILLERS[name](r, patch)


# Decision 1: (function or RelayServer method, (before, after)), or ("constant", value).
IN_USE = "os" if peek.HAS_OS_WAITID else "libc"
LOGGED = "_stderr_line(PEEK_FAILURE_LOG_LINE.format(errno=errno.errorcode.get(exc.errno, exc.errno)))"
PEEK_MUTANTS = {
    # Round 3's two.
    "helper-without-the-peek": ("child_exited", ("        return _exit_peek(pid) is not None",
                                                 "        return False")),
    "a-peek-that-reaps": ("_PEEK_FLAGS", os.WEXITED | os.WNOHANG),
    # Round 4's.
    "status-left-on-poll": ("RelayServer.status", (
        '"exited" if child_exited(child) else "running"', '"running" if child.poll() is None else "exited"')),
    "refusal-flag-left-on-poll": ("RelayServer._refusal", (
        "exited = child is not None and child_exited(child)",
        "exited = child is not None and child.poll() is not None")),
    "signal-delivery-left-on-poll": ("RelayServer.apply", (
        "if child is not None and not child_exited(child):", "if child is not None and child.poll() is None:")),
    "echild-read-as-not-exited": ("child_exited", ("    except ChildProcessError:\n        return True",
                                                   "    except ChildProcessError:\n        return False")),
    "other-oserror-read-as-exited": ("child_exited", (LOGGED + "\n        return False",
                                                      LOGGED + "\n        return True")),
    "ctypes-binding-without-wnowait": ("_waitid_libc", ("ctypes.byref(info), _PEEK_FLAGS)",
                                                        "ctypes.byref(info), os.WEXITED | os.WNOHANG)")),
}
SITES = lambda tmp_path, patch, capfd: peek.test_every_relay_site_reads_an_exit_the_guardian_wait_hides(
    tmp_path, patch, IN_USE)


def _wait_lock_regression(tmp_path, patch, capfd):
    with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
        r3.test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock(r, patch)


PEEK_KILLERS = {
    "helper-without-the-peek": _wait_lock_regression,
    "a-peek-that-reaps": lambda tmp_path, patch, capfd:
        peek.test_a_peek_never_changes_the_status_the_guardian_records(patch, IN_USE),
    "status-left-on-poll": SITES,
    "refusal-flag-left-on-poll": SITES,
    "signal-delivery-left-on-poll": SITES,
    "echild-read-as-not-exited": lambda tmp_path, patch, capfd:
        peek.test_a_child_the_guardian_reaped_reads_as_exited(patch, IN_USE),
    "other-oserror-read-as-exited": lambda tmp_path, patch, capfd:
        peek.test_another_peek_failure_proves_nothing_and_is_logged_once(patch, capfd, errno.EIO),
    "ctypes-binding-without-wnowait": lambda tmp_path, patch, capfd:
        peek.test_a_binding_reads_an_exit_without_reaping_it("libc", "exit 5"),
}


def apply_peek(patch, name):
    target, change = PEEK_MUTANTS[name]
    if target == "_PEEK_FLAGS":
        patch.setattr(relay_module, target, change)
        return
    before, after = change
    owner, _, attribute = target.rpartition(".")
    holder = getattr(relay_module, owner) if owner else relay_module
    source = textwrap.dedent(inspect.getsource(getattr(holder, attribute)))
    assert source.count(before) == 1, (name, before)
    scope = {}       # the mutant's globals are the module's own, so a test's patches reach it
    exec(compile(source.replace(before, after), f"<fix r4 mutant {name}>", "exec"), vars(relay_module), scope)
    patch.setattr(holder, attribute, scope[attribute])


@pytest.mark.parametrize("name", list(PEEK_MUTANTS))
def test_the_named_killer_kills_the_peek_mutant(tmp_path, capfd, name):
    with pytest.MonkeyPatch.context() as patch:
        apply_peek(patch, name)
        with pytest.raises(AssertionError):
            PEEK_KILLERS[name](tmp_path, patch, capfd)


@pytest.mark.parametrize("name", list(PEEK_MUTANTS))
def test_the_named_killer_passes_without_the_peek_mutant(tmp_path, capfd, name):
    with pytest.MonkeyPatch.context() as patch:
        PEEK_KILLERS[name](tmp_path, patch, capfd)


@pytest.mark.parametrize("name", ["helper-without-the-peek", "a-peek-that-reaps", "echild-read-as-not-exited",
                                  "ctypes-binding-without-wnowait"])
def test_report_whether_the_property_kills_the_peek_mutant(name):
    """Informational: prints whether the exit-peek property catches the mutant. The
    named killers above are the kill record."""
    settings.register_profile("fix-r4-mutants", phases=[Phase.explicit, Phase.generate])
    previous = settings.get_current_profile_name()
    settings.load_profile("fix-r4-mutants")
    binding = "libc" if name == "ctypes-binding-without-wnowait" else IN_USE
    try:
        with pytest.MonkeyPatch.context() as patch:
            apply_peek(patch, name)
            try:
                peek.test_a_peek_reads_exit_only_after_it_and_never_reaps(binding)
            except AssertionError as exc:
                where = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
                print(f"PROPERTY {name} ({binding}): killed ({where[:160]})")
            else:
                print(f"PROPERTY {name} ({binding}): not killed")
    finally:
        settings.load_profile(previous)
