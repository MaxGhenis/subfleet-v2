"""D-CEN1, C-5.5: the raw argument buffer parser and the census's in-process sources.

The marker source reads each process's `KERN_PROCARGS2` buffer: argc, the executable
path, NUL padding, the argv strings, the environment strings, and strings the kernel
adds. `ps -E` stops a process's environment at the first empty string, so a title
written over the argv area (node's `process.title`) hid vitest's markers on
2026-10-13; the census looks at every whole string after the executable path.

The fourth source counts every live process whose responsible process is the
attempt's recorded responsibility root. These cases replace the kernel with
`tests.fake_kernel.FakeKernel`; tests/process reads the real one.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.fake_kernel import FakeKernel, procargs
from tests.unit.test_procs import BOOT_A, BOOT_B, START, census

ATTEMPT, ROOT = "job/a1", "/fixture/state"
MARK, ROOT_MARK = b"SUBFLEET_ATTEMPT=" + ATTEMPT.encode(), b"SUBFLEET_ROOT=" + ROOT.encode()
PROPERTY = settings(max_examples=300, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


def ps_e_environment(buffer: bytes) -> list[bytes]:
    """What `ps -E` shows of a buffer's environment: after the executable path and its
    padding, argc strings of argv, then strings up to the first empty one."""
    argc = int.from_bytes(buffer[:4], "little")
    body = buffer[4:]
    at = body.index(b"\0")
    while at < len(body) and body[at] == 0:
        at += 1
    strings = body[at:].split(b"\0")
    environment = []
    for string in strings[argc:]:
        if not string:
            break
        environment.append(string)
    return environment


def titled(argv: list[bytes], env: list[bytes], title: bytes, *, padding: int = 3) -> bytes:
    """`procargs(argv, env)` after the process wrote `title` and NULs over its argv area,
    as node's `process.title` and the incident's vitest did."""
    buffer = bytearray(procargs(argv, env, padding=padding))
    first = 4 + len(b"/fixture/bin/" + argv[0]) + 1 + padding     # argc, the path, its NUL, padding
    end = first + sum(len(part) + 1 for part in argv)
    kept = title[:end - first - 1]
    buffer[first:end] = kept + b"\0" * (end - first - len(kept))
    return bytes(buffer)


# The parser -------------------------------------------------------------------

def test_d_cen1_a_title_over_argv_hides_the_environment_from_ps_but_not_from_the_census():
    """The incident's layout: `ps -E` sees no environment; every whole string is still read."""
    buffer = titled([b"node", b"/opt/vitest/vitest.mjs", b"run", b"--reporter=dot"],
                    [b"PATH=/usr/bin", MARK, ROOT_MARK, b"HOME=/Users/x"], b"node (vitest)")
    assert MARK not in ps_e_environment(buffer) and ps_e_environment(buffer) == []
    assert MARK in procs.procargs_strings(buffer)
    assert procs.procargs_marked(buffer, MARK, ROOT_MARK)
    assert procs.procargs_marked(buffer, MARK, None)
    untouched = procargs([b"node", b"vitest.mjs"], [MARK, ROOT_MARK])
    assert MARK in ps_e_environment(untouched)               # the reader agreed before the title


def test_d_cen1_the_root_half_of_the_marker_must_match_when_given():
    """C-5.5: another state root's process with this attempt id is not this attempt's."""
    other = procargs([b"python"], [MARK, b"SUBFLEET_ROOT=/other/state"])
    assert not procs.procargs_marked(other, MARK, ROOT_MARK)
    assert procs.procargs_marked(other, MARK, None)
    neither = procargs([b"python"], [b"SUBFLEET_ROOT=" + ROOT.encode()])
    assert not procs.procargs_marked(neither, MARK, ROOT_MARK)


@pytest.mark.parametrize("string", [
    b"SUBFLEET_ATTEMPT=job/a10", b"XSUBFLEET_ATTEMPT=job/a1", b"SUBFLEET_ATTEMPT=job/a1 ",
    b" SUBFLEET_ATTEMPT=job/a1", b"SUBFLEET_ATTEMPT=job/a", b"SUBFLEET_ATTEMPT=JOB/A1"])
def test_d_cen1_only_the_whole_string_matches(string):
    assert not procs.procargs_marked(procargs([b"python"], [string, ROOT_MARK]), MARK, ROOT_MARK)


def test_d_cen1_the_executable_path_is_not_a_string_after_it():
    """`anywhere after the executable path`: a path spelled like the marker is not one."""
    assert not procs.procargs_marked(procargs([b"x"], [], executable=MARK), MARK, None)
    assert procs.procargs_marked(procargs([MARK], [], executable=b"/bin/x"), MARK, None)  # argv counts


@pytest.mark.parametrize("cut", ["inside the marker", "after a prefix of a longer id", "inside the path",
                                 "argc only", "nothing"])
def test_d_cen1_a_truncated_buffer_never_matches_a_partial_string(cut):
    """A string the buffer ends inside of is not whole: a cut buffer can lose a string,
    never turn one into another (`SUBFLEET_ATTEMPT=job/a10` cut after `a1`)."""
    longer = procargs([b"python"], [b"SUBFLEET_ATTEMPT=job/a10"])
    exact = procargs([b"python"], [MARK])
    buffer = {
        "inside the marker": exact[:exact.index(MARK) + 5],
        "after a prefix of a longer id": longer[:longer.index(b"job/a10") + len(b"job/a1")],
        "inside the path": exact[:10],
        "argc only": exact[:4],
        "nothing": b"",
    }[cut]
    assert not procs.procargs_marked(buffer, MARK, None)
    assert MARK not in procs.procargs_strings(buffer)


@pytest.mark.parametrize("attempt", ["", "job/a 1", "job/a1\n", "job\0/a1"])
def test_d_cen1_an_attempt_id_no_environment_string_could_carry_is_refused(attempt):
    with pytest.raises(ValueError):
        procs.marker_strings(attempt, ROOT)


def reference_marked(buffer: bytes, attempt: bytes, root: bytes | None) -> bool:
    """An independent statement of the rule: after the executable path's NUL, the marker
    is a whole string exactly when NUL, the marker, NUL occurs."""
    body = buffer[4:] if len(buffer) > 4 else b""
    end = body.find(b"\0")
    if end < 0:
        return False
    after = body[end:]
    return (b"\0" + attempt + b"\0") in after and (root is None or (b"\0" + root + b"\0") in after)


strings = st.binary(max_size=24).map(lambda raw: raw.replace(b"\0", b""))
marks = st.sampled_from([MARK, b"SUBFLEET_ATTEMPT=job/a10", b"SUBFLEET_ATTEMPT=job/a", ROOT_MARK,
                         b"SUBFLEET_ROOT=/other", b""])


@PROPERTY
@given(argv=st.lists(st.one_of(strings, marks), min_size=1, max_size=6),
       env=st.lists(st.one_of(strings, marks), max_size=8),
       padding=st.integers(min_value=0, max_value=9),
       title=st.one_of(st.none(), strings),
       cut=st.one_of(st.none(), st.integers(min_value=0, max_value=400)),
       scoped=st.booleans())
def test_d_cen1_the_parser_finds_the_marker_exactly_when_it_is_a_whole_string(argv, env, padding, title, cut,
                                                                              scoped):
    """For any argv and environment, any NUL padding, a title written over the argv area
    or not, and a buffer cut anywhere: the marker (with the root, when one is given) is
    found exactly when it is a whole NUL-terminated string after the executable path,
    and a cut buffer is never found to hold one its full self does not."""
    buffer = procargs(argv, env, padding=padding)
    if title is not None:
        buffer = titled(argv, env, title or b"t", padding=padding)
    full = buffer
    if cut is not None:
        buffer = buffer[:cut]
    root = ROOT_MARK if scoped else None
    found = procs.procargs_marked(buffer, MARK, root)
    assert found == reference_marked(buffer, MARK, root)
    if found:
        assert procs.procargs_marked(full, MARK, root)
    if cut is None and title is None:
        whole = argv + env
        assert found == (MARK in whole and (root is None or root in whole))


@PROPERTY
@given(st.binary(max_size=200), st.booleans())
def test_d_cen1_the_parser_agrees_with_the_reference_on_any_bytes(raw, scoped):
    """Differential: any byte string at all, NULs anywhere."""
    for buffer in (raw, b"\x01\0\0\0/x\0" + raw, b"\x01\0\0\0/x\0" + MARK + b"\0" + raw):
        root = ROOT_MARK if scoped else None
        assert procs.procargs_marked(buffer, MARK, root) == reference_marked(buffer, MARK, root)


# The sources ------------------------------------------------------------------

ROWS = f"42 1 42 Ss {START}\n43 42 42 S {START}\n"


def kernel_with(monkeypatch, **kwargs) -> FakeKernel:
    fake = FakeKernel(**kwargs)
    monkeypatch.setattr(procs, "kernel", fake)
    return fake


def marked(*extra: bytes) -> bytes:
    return procargs([b"node (vitest)"], [MARK, ROOT_MARK, *extra])


def test_d_cen1_another_users_process_is_skipped_as_ps_skipped_it(monkeypatch):
    """EPERM: listed, never read, never an error."""
    census(monkeypatch, parents=ROWS + f"90 1 90 S {START}\n", session=BOOT_A)
    fake = kernel_with(monkeypatch, processes={90: marked()}, others={90})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.verified_empty and result.errors == ()
    assert ("arguments", 90) not in fake.calls


def test_d_cen1_a_process_gone_before_its_read_is_skipped(monkeypatch):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={91: None}, gone_after_process={91})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.verified_empty and result.errors == ()


def test_d_cen1_a_same_uid_process_whose_read_fails_prevents_release(monkeypatch):
    """A failed read for a live process of this user is an error, as `ps` failing was."""
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={92: None})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.unverifiable and not result.verified_empty
    assert result.errors == ("marker read unavailable for pid 92",)


def test_d_cen1_a_process_that_began_to_exit_is_no_writer(monkeypatch):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={93: None}, exiting={93})
    assert procs.containment(None, None, None, ATTEMPT, root=ROOT).verified_empty


def test_d_cen1_a_read_that_fills_the_buffer_prevents_release(monkeypatch):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    fake = kernel_with(monkeypatch, processes={94: marked()})

    def full(pid, buffer=None):
        raise procs.InspectionError(f"argument buffer of pid {pid} fills the read")
    monkeypatch.setattr(fake, "arguments", full)
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.errors == ("marker read unavailable for pid 94",)


@pytest.mark.parametrize("failing", [{"pids"}, {("process", 95)}])
def test_d_cen1_a_listing_or_inspection_that_fails_prevents_release(monkeypatch, failing):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={95: marked()}, fail=failing)
    root = procs.ResponsibleRoot(42, BOOT_A, 1)
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT, responsible_root=root)
    assert result.unverifiable and not result.verified_empty
    if failing == {"pids"}:
        assert result.errors == ("marker enumeration unavailable", "responsibility enumeration unavailable")
    else:
        assert result.errors == ("process inspection unavailable for pid 95",)


def test_d_cen1_a_marker_born_after_the_snapshot_is_asked_about_singly(monkeypatch):
    """The listing follows the snapshot; a pid it shows and the snapshot does not is read,
    and its state comes from `ps -p`, which drops a zombie."""
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={96: marked()})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.marker_pids == {96} and result.identities[96].pid == 96
    monkeypatch.setattr(procs, "_stat", lambda pid: "Z")
    assert procs.containment(None, None, None, ATTEMPT, root=ROOT).verified_empty


def test_d_cen1_responsibility_members_are_counted_and_recorded_without_commands(monkeypatch):
    """The fourth source: live processes naming the root, the root among them while it lives.
    Shapes and identities are recorded as for the other sources; no command, title or
    environment."""
    rows = ROWS + f"60 1 60 Ss {START}\n61 60 60 S {START}\n70 1 70 S {START}\n"
    census(monkeypatch, parents=rows, session=BOOT_A)
    title = procargs([b"renamed-escaper"], [b"SECRET_TOKEN=secret-sentinel"])
    kernel_with(monkeypatch, processes={42: title, 43: title, 60: title, 61: title, 70: title},
                responsible={43: 42, 60: 42, 61: 42}, unique={42: 500, 43: 501, 60: 520, 61: 530, 70: 540})
    result = procs.containment(42, 42, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_A, 500),
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, START)})
    assert result.responsible_pids == {42, 43, 60, 61}
    assert 70 not in result.live_pids                         # its own responsible process
    # Fresh responsibility captures share the marker identity/group bracket,
    # which records generic live state rather than the snapshot's old flags.
    assert result.shapes[60] == {"ppid": 1, "pgid": 60, "stat": "S"}
    assert result.identities[61] == procs.ProcessIdentity(61, BOOT_A, START)
    shown = json.dumps(result.to_dict())
    assert shown.count('"responsible_pids": [42, 43, 60, 61]') == 1
    assert "secret-sentinel" not in shown and "renamed" not in shown and "SECRET" not in shown


def test_d_cen1_a_process_created_before_the_root_is_not_its(monkeypatch):
    """Unique ids order a boot's processes: one older than the root names its pid only
    because the pid had another holder, and is excluded; the root's own processes are not."""
    rows = ROWS + f"60 1 60 S {START}\n61 1 61 S {START}\n"
    census(monkeypatch, parents=rows, session=BOOT_A)
    kernel_with(monkeypatch, processes={60: b"", 61: b""}, responsible={60: 42, 61: 42},
                unique={60: 499, 61: 500})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_A, 500))
    assert result.responsible_pids == {61}


@pytest.mark.parametrize("recorded,expected", [
    (BOOT_B, set()),              # the root's boot is over: none of its processes survives
    ("99", {60, 61}),             # a shifted legacy timestamp: order unknown, keep all (C-5.3)
    (None, {60, 61}),             # no recorded boot: keep all
])
def test_d_cen1_the_root_order_needs_its_boot(monkeypatch, recorded, expected):
    rows = ROWS + f"60 1 60 S {START}\n61 1 61 S {START}\n"
    census(monkeypatch, parents=rows, session=BOOT_A)
    kernel_with(monkeypatch, processes={60: b"", 61: b""}, responsible={60: 42, 61: 42},
                unique={60: 1, 61: 900})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, recorded, 500))
    assert result.responsible_pids == expected


def test_d_cen1_an_unknown_root_unique_id_keeps_every_member(monkeypatch):
    census(monkeypatch, parents=ROWS + f"60 1 60 S {START}\n", session=BOOT_A)
    kernel_with(monkeypatch, processes={60: b""}, responsible={60: 42}, unique={60: 1})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_A, None))
    assert result.responsible_pids == {60}


def test_d_cen1_a_failed_responsibility_read_prevents_release(monkeypatch):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={60: b""}, fail={("responsible", 60)})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_A, 1))
    assert result.errors == ("responsibility read unavailable for pid 60",)
    assert not result.verified_empty


def test_d_cen1_an_attempt_without_a_root_keeps_three_sources(monkeypatch):
    """A legacy or in-flight attempt: no responsibility read at all, even where the SPI
    is missing."""
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    fake = kernel_with(monkeypatch, processes={60: b""}, responsible={60: 42}, responsibility=False)
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    assert result.verified_empty and not result.responsible_pids
    assert not [call for call in fake.calls if call[0] == "responsible"]


def test_d_cen1_a_census_reads_each_candidate_once_per_source(monkeypatch):
    """C-5.12: one listing and, per live candidate of this user, one state read, one
    buffer and one responsibility read; another user's pid costs the state read alone."""
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    fake = kernel_with(monkeypatch, processes={42: b"", 43: b"", 60: b""}, others={61})
    procs.containment(42, 42, None, ATTEMPT, root=ROOT, responsible_root=procs.ResponsibleRoot(42, BOOT_A, 1))
    assert fake.calls.count(("pids",)) == 1
    for pid in (42, 43, 60):
        assert [call for call in fake.calls if call[1:] == (pid,)] == [
            ("process", pid), ("arguments", pid), ("responsible", pid)]
    assert [call for call in fake.calls if call[1:] == (61,)] == [("process", 61)]



@pytest.mark.parametrize("unique_id", [1, None])
def test_d_cen1_a_recycled_self_responsible_root_does_not_count_strangers(monkeypatch, unique_id):
    """Review P2-1: S is self-responsible and has child C, but the recorded S died."""
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    fake = kernel_with(monkeypatch, processes={42: b"", 43: b""},
                       responsible={42: 42, 43: 42}, unique={42: 100, 43: 101})
    recorded = {42: procs.ProcessIdentity(42, BOOT_A, "another start")}
    result = procs.containment(42, 42, None, ATTEMPT, root=ROOT, recorded_identities=recorded,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_A, unique_id))
    assert set(result.recycled) == {42}
    assert result.verified_empty
    assert not result.responsible_pids and not result.identities
    assert not [call for call in fake.calls if call[0] == "responsible"]


def test_d_cen1_a_new_root_holder_bounds_members_even_with_the_same_lstart(monkeypatch):
    """A pid can recycle within lstart's second; the kernel sequence still proves reuse.
    Only older members of the recorded root survive the new holder's upper bound."""
    census(monkeypatch, parents=ROWS + f"60 1 60 S {START}\n61 1 61 S {START}\n", session=BOOT_A)
    kernel_with(monkeypatch, processes={42: b"", 43: b"", 60: b"", 61: b""},
                responsible={42: 42, 43: 42, 60: 42, 61: 42},
                unique={42: 100, 43: 101, 60: 75, 61: 49})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_A, 50))
    assert result.responsible_pids == {60}
    assert set(result.identities) == {60}

# The daemon's record ----------------------------------------------------------

class Store:
    """Enough of the store for `Daemon._contain`: one attempt row, its evidence writes."""

    def __init__(self, row):
        self.row, self.events = row, []

    @contextmanager
    def transaction(self, kind, **kwargs):
        self.events.append((kind, kwargs.get("data")))
        yield self

    def execute(self, sql, args):
        assert sql.startswith("UPDATE attempts SET evidence_json=?")
        self.row["evidence_json"] = args[0]

    def get_attempt(self, attempt_id):
        return dict(self.row)


def core_for(row) -> SimpleNamespace:
    return SimpleNamespace(root=Path("/fixture/state"), store=Store(row), _read_json=lambda path: None,
        _job=lambda job: {"workdir": "/fixture/work"},
        _foreign_ownership=lambda *args: procs.ForeignOwnership(),
        _prune_foreign_lineage=Daemon._prune_foreign_lineage, _save_probe=lambda record: None)


def attempt_row(**evidence) -> dict:
    return {"attempt_id": ATTEMPT, "job_id": "job", "seq": 1, "guardian_pid": 42, "pgid": 42, "child_pid": 43,
            "boot_id": BOOT_A, "proc_start": START, "started_at": "2026-10-13T01:00:00Z",
            "evidence_json": json.dumps(evidence), "state": "finalizing"}


def captured(monkeypatch, result: procs.Containment | None = None) -> list[dict]:
    calls = []

    def containment(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        return result or procs.Containment()
    monkeypatch.setattr(procs, "containment", containment)
    return calls


def test_d_cen1_contain_passes_the_root_only_for_the_attempts_own_guardian(monkeypatch):
    calls = captured(monkeypatch)
    row = attempt_row(responsible_root={"pid": 42, "unique_id": 77})
    Daemon._contain(core_for(row), dict(row))
    assert calls[-1]["responsible_root"] == procs.ResponsibleRoot(42, BOOT_A, 77)
    row = attempt_row(responsible_root={"pid": 41, "unique_id": 77})
    Daemon._contain(core_for(row), dict(row))
    assert calls[-1]["responsible_root"] is None              # not this guardian's: three sources
    Daemon._contain(core_for(attempt_row()), attempt_row())
    assert calls[-1]["responsible_root"] is None              # legacy: three sources


def test_d_cen1_every_member_a_census_sees_is_kept_and_seeds_the_next(monkeypatch):
    """A process seen naming the root is recorded in the attempt's evidence (identity only,
    never signal authority) and passed to every later census as a recorded survivor, so
    it stays counted after the root is gone."""
    escaper = procs.ProcessIdentity(60, BOOT_A, START)
    guardian = procs.ProcessIdentity(42, BOOT_A, START)
    seen = procs.Containment(responsible_pids=frozenset({42, 60}), identities={42: guardian, 60: escaper})
    row = attempt_row(responsible_root={"pid": 42, "unique_id": 77})
    core = core_for(row)
    calls = captured(monkeypatch, seen)
    a = dict(row)
    Daemon._contain(core, a)
    evidence = json.loads(row["evidence_json"])
    assert evidence["responsible_identities"] == {"60": asdict(escaper)}     # not the guardian itself
    assert "owned_identities" not in evidence
    assert json.loads(a["evidence_json"]) == evidence          # the caller's row too, as `recycled` is kept
    assert core.store.events == [("attempt.lineage_recorded", None)]
    Daemon._contain(core, dict(row))
    assert calls[-1]["recorded"][60] == [escaper]
    assert len(core.store.events) == 2                         # one quarantine lineage transaction per census


def test_d_cen1_a_probe_record_keeps_its_members_in_the_record(monkeypatch):
    escaper = procs.ProcessIdentity(60, BOOT_A, START)
    captured(monkeypatch, procs.Containment(responsible_pids=frozenset({60}), identities={60: escaper}))
    record = {"holder": "probe:x", "job_id": "job", "lane_id": "codex-1", "directory": "/fixture/probe",
              "guardian_pid": 42, "pgid": 42, "boot_id": BOOT_A, "proc_start": START,
              "responsible_root": {"pid": 42, "unique_id": 77}, "owned_identities": {}}
    core = core_for(attempt_row())
    Daemon._contain(core, record)
    assert record["responsible_identities"] == {"60": asdict(escaper)}
    assert core.store.events == []


def test_d_cen1_a_guardian_without_a_root_is_said_once(monkeypatch):
    """The SPI missing: the guardian starts as before, without a root, and the daemon says
    so once, not at every launch."""
    lines = []
    spawned = iter(range(5000, 5010))
    monkeypatch.setattr(procs, "spawn_disclaimed",
                        lambda argv, **kwargs: SimpleNamespace(pid=next(spawned)))
    monkeypatch.setattr(procs, "responsibility_root", lambda pid: None)
    monkeypatch.setattr(procs, "disclaim_available", lambda: False)
    core = SimpleNamespace(_unrooted_logged=False,
                           log=SimpleNamespace(warning=lambda *args: lines.append(args)))
    for _ in range(3):
        child, root = Daemon._spawn_guardian(core, ["python"], {}, 9, "/fixture")
        assert root is None
    assert len(lines) == 1 and "without a responsibility root" in lines[0][0]


@pytest.mark.parametrize("unique_id", [1, None])
def test_d_cen1_a_recycled_guardian_never_persists_a_stranger(monkeypatch, unique_id):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    cwd_reads = []

    def cwd_pids(workdir):
        assert workdir == "/fixture/work"
        cwd_reads.append(workdir)
        return frozenset()

    monkeypatch.setattr(procs, "cwd_pids", cwd_pids)
    kernel_with(monkeypatch, processes={42: b"", 43: b""},
                responsible={42: 42, 43: 42}, unique={42: 100, 43: 101})
    row = attempt_row(responsible_root={"pid": 42, "unique_id": unique_id})
    row.update(proc_start="another start", child_pid=None)
    core = core_for(row)
    result = Daemon._contain(core, dict(row))
    assert result.verified_empty and not result.responsible_pids
    assert cwd_reads == ["/fixture/work"]
    assert "responsible_identities" not in json.loads(row["evidence_json"])
    assert core.store.events == [("attempt.lineage_recorded", None),
        ("attempt.processes_recycled", {"recycled": result.to_dict()["recycled"]})]


@pytest.mark.parametrize("first_error", [False, True])
def test_d_cen1_a_full_argument_read_retries_to_the_kernel_maximum(first_error):
    import ctypes
    import errno
    payload = procargs([b"node"], [MARK, ROOT_MARK, b"LARGE=" + b"x" * 4096])
    reads = []
    reader = procs.Darwin()
    reader.readable = True

    def sysctl(name, count, buffer, size, *args):
        if count == 2:
            assert list(name) == [procs._CTL_KERN, procs._KERN_ARGMAX]
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))[0] = 8192
            return 0
        reads.append(len(buffer))
        if len(reads) == 1:
            if first_error:
                ctypes.set_errno(errno.ENOMEM)
                return -1
            size._obj.value = len(buffer)
            return 0
        ctypes.memmove(buffer, payload, len(payload))
        size._obj.value = len(payload)
        return 0

    reader._sysctl = sysctl
    raw = reader.arguments(42, ctypes.create_string_buffer(1024))
    assert raw == payload and procs.procargs_marked(raw, MARK, ROOT_MARK)
    assert reads == [1024, 8192]
    assert reader.argument_max() == 8192


@pytest.mark.parametrize("maximum", [1024, 8192, None])
def test_d_cen1_a_read_filling_the_kernel_maximum_stays_unverifiable(maximum):
    import ctypes
    reader = procs.Darwin()
    reader.readable = True
    reads = []

    def sysctl(name, count, buffer, size, *args):
        if count == 2:
            if maximum is None:
                return -1
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))[0] = maximum
            return 0
        reads.append(len(buffer))
        size._obj.value = len(buffer)
        return 0

    reader._sysctl = sysctl
    with pytest.raises(procs.InspectionError, match="fills the read|maximum unavailable"):
        reader.arguments(42, ctypes.create_string_buffer(1024))
    assert reads == ([1024, 8192] if maximum == 8192 else [1024])


def test_d_cen1_a_proven_other_boot_excludes_an_unknown_sequence_root(monkeypatch):
    census(monkeypatch, parents=ROWS, session=BOOT_A)
    kernel_with(monkeypatch, processes={60: b""}, responsible={60: 42})
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               responsible_root=procs.ResponsibleRoot(42, BOOT_B, None))
    assert not result.responsible_pids and result.verified_empty
