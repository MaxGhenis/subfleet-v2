"""D-KILL2: the stand-in for a kernel that shows a process's argv and no environment.

`tests/hidden_environment.py` lets the hidden-marker incident run on any macOS: GitHub's
macos-15-arm64 runner (macOS 15.7.9) shows `/usr/bin/lockf`'s environment, where this
host (macOS 26.6.2) shows none. These cases hold it to what the kernel returns here
and to the census's own reads: it removes the environment and whatever follows it,
never alters or invents a string, keeps argv (where a marker still counts), hides only
the pids it is given, and is what `procs.containment` reads through.
"""

from __future__ import annotations

from hypothesis import given, strategies as st
import pytest

from subfleet import procs
from tests.fake_kernel import FakeKernel, procargs
from tests.hidden_environment import HidingKernel, argv_of, hiding_ps, published, without_environment
from tests.unit.test_census_procargs import ATTEMPT, MARK, PROPERTY, ROOT, ROOT_MARK
from tests.unit.test_procs import BOOT_A, START, census

LOCK = b"/var/folders/9l/x/T/sf-kill2f2/probe.lock"
#: What this host's kernel (macOS 26.6.2) returned on 2026-10-19 for a waiting lockf
#: started with `SUBFLEET_ATTEMPT`, `SUBFLEET_ROOT` and `PATH` (its lock's path shortened):
#: argc 7, the executable path, one byte of padding and the seven argv strings, and
#: nothing after them.
HOST_LOCKF = (b"\x07\0\0\0/usr/bin/lockf\0\0/usr/bin/lockf\0-k\0-t\0" + b"20\0" + LOCK
              + b"\0/bin/sleep\0" + b"1\0")
LOCKF_ARGV = [b"/usr/bin/lockf", b"-k", b"-t", b"20", LOCK, b"/bin/sleep", b"1"]
#: The shape the runner's kernel returned (CI job 118038297585, its middle elided in the
#: log): the same argv, then the environment and the strings the kernel appends.
RUNNER_LOCKF = procargs(LOCKF_ARGV, [MARK, ROOT_MARK, b"PATH=/usr/bin:/bin", b"", b"arm64e_abi=os",
                                     b"th_port=", b"security_config=0x0"], executable=b"/usr/bin/lockf",
                        padding=1)

strings = st.binary(max_size=24).filter(lambda part: b"\0" not in part)
named = st.one_of(strings, st.sampled_from([MARK, ROOT_MARK]))


def expected(argv: list[bytes], padding: int) -> bytes:
    """argc, the fixture's executable path, its NUL and padding, and argv: nothing else."""
    path = b"/fixture/bin/" + argv[0]
    return len(argv).to_bytes(4, "little") + path + b"\0" * (1 + padding) + b"".join(part + b"\0" for part in argv)


@PROPERTY
@given(first=strings.filter(bool), rest=st.lists(named, max_size=5), env=st.lists(named, max_size=8),
       padding=st.integers(min_value=0, max_value=9), cut=st.one_of(st.none(), st.integers(min_value=0, max_value=300)))
def test_d_kill2_the_stand_in_keeps_argv_exactly_and_nothing_after_it(first, rest, env, padding, cut):
    """For any argv (its first string not empty), any environment and padding: the buffer
    is cut right after its argv, byte for byte a prefix of what the kernel gave. A marker
    in the environment is never found, one in argv still is (C-5.5 over-includes), and
    cutting twice cuts nothing more. A buffer that ends early keeps whole strings only,
    each one its full buffer also keeps."""
    argv = [first, *rest]
    full = procargs(argv, env, padding=padding)
    hidden = without_environment(full)
    assert hidden == expected(argv, padding) and full.startswith(hidden)
    assert argv_of(full) == argv_of(hidden) == argv
    assert procs.procargs_marked(hidden, MARK, ROOT_MARK) == (MARK in argv and ROOT_MARK in argv)
    assert without_environment(hidden) == hidden
    if cut is not None:
        short = without_environment(full[:cut])
        assert hidden.startswith(short)
        kept = procs.procargs_strings(short)
        assert kept == procs.procargs_strings(hidden)[:len(kept)]


def test_d_kill2_the_stand_in_leaves_what_this_host_returned_for_lockf_as_it_was():
    """Differential against the kernel: macOS 26.6.2's lockf buffer is already argv alone,
    so the stand-in changes nothing; the runner's, with its environment, becomes it."""
    assert without_environment(HOST_LOCKF) == HOST_LOCKF
    assert argv_of(HOST_LOCKF) == LOCKF_ARGV
    assert procs.procargs_marked(RUNNER_LOCKF, MARK, ROOT_MARK)
    hidden = without_environment(RUNNER_LOCKF)
    assert not procs.procargs_marked(hidden, MARK, ROOT_MARK)
    assert procs.procargs_strings(hidden) == [b"", *LOCKF_ARGV]
    assert not [string for string in procs.procargs_strings(hidden) if b"=" in string]


def test_d_kill2_the_hiding_kernel_hides_only_the_pids_it_is_given():
    """Pid 70's buffer loses its environment; 71's, a refused read and every other read
    are the real reader's, failures included."""
    real = FakeKernel({70: RUNNER_LOCKF, 71: procargs([b"python"], [MARK, ROOT_MARK]), 72: None},
                      fail={("process", 73)}, responsible={70: 42})
    kernel = HidingKernel(real, lambda: {70, 72})
    assert kernel.arguments(70, None) == without_environment(RUNNER_LOCKF)
    assert kernel.arguments(71, None) == real.processes[71]
    assert kernel.arguments(72, None) is None
    assert kernel.hid == {70}
    assert kernel.pids() == frozenset({70, 71, 72}) and kernel.responsible(70) == 42
    assert kernel.process(71) == real.process(71) and kernel.responsibility is real.responsibility
    with pytest.raises(procs.InspectionError):
        kernel.process(73)


@pytest.mark.parametrize("hiding", [False, True], ids=["shown", "hidden"])
def test_d_kill2_the_census_reads_markers_through_the_hiding_kernel(monkeypatch, hiding):
    """`procs.containment` reads every buffer through `procs.kernel`: with the stand-in in
    place a lockf whose markers the host shows is not counted by them, and a python
    process beside it still is."""
    census(monkeypatch, parents=f"42 1 42 Ss {START}\n70 1 70 Ss {START}\n71 1 71 Ss {START}\n", session=BOOT_A)
    real = FakeKernel({70: RUNNER_LOCKF, 71: procargs([b"python"], [MARK, ROOT_MARK])})
    monkeypatch.setattr(procs, "kernel", HidingKernel(real, lambda: {70}) if hiding else real)
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.marker_pids == ({71} if hiding else {70, 71})
    assert result.errors == ()


def test_d_kill2_published_pids_are_read_once_they_land(tmp_path):
    """A pid file is read until it holds a pid; that pid stays hidden afterwards, and a
    missing or unreadable file hides nothing."""
    escaper, other = tmp_path / "escaper.pid", tmp_path / "other.pid"
    hidden = published([escaper, other])
    assert hidden() == frozenset()
    other.write_text("not a pid")
    escaper.write_text("4242\n")
    assert hidden() == {4242}
    escaper.unlink()
    assert hidden() == {4242}


def test_d_kill2_the_installed_census_s_ps_rows_show_a_hidden_pid_s_argv_alone():
    """`ps -axEww -o pid=,command=`: a hidden pid's row is its argv, as `ps -E` prints a
    process whose buffer holds no environment; a hidden pid gone since has no row; other
    rows and other reads are the real ones."""
    rows = (f"70 /usr/bin/lockf -k -t 20 {LOCK.decode()} /bin/sleep 1 {MARK.decode()} {ROOT_MARK.decode()}\n"
            f"71 python {MARK.decode()} {ROOT_MARK.decode()}\n"
            f"72 gone {MARK.decode()}\n")
    calls = []

    def real_read(argv, **options):
        calls.append(argv)
        return rows if "-axEww" in argv else "other\n"
    read = hiding_ps(real_read, lambda: {70, 72}, kernel=FakeKernel({70: RUNNER_LOCKF, 72: None}))
    assert read(["/bin/ps", "-axEww", "-o", "pid=,command="]).splitlines() == [
        f"70 /usr/bin/lockf -k -t 20 {LOCK.decode()} /bin/sleep 1",
        f"71 python {MARK.decode()} {ROOT_MARK.decode()}"]
    assert read(["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="]) == "other\n"
    assert hiding_ps(real_read, lambda: ())(["/bin/ps", "-axEww", "-o", "pid=,command="]) == rows
