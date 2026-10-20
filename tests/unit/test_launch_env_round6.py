"""Review r5 of #222's follow-ups (D-ENV2): a refused lanes.json row for a stored
lane, held lanes in doctor and `lanes transfer`, the session probe's names as
lane variables, and the install check `daemon start --check`.

Every value here is a dummy, and failures name variables, never values. No test
reads a credential store, the live store or a live daemon. The daemon-side
regressions are in tests/fake/test_launch_env_daemon.py, and the named
mutations in tests/unit/test_launch_env_mutations.py.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import cli, credentials, doctor, lanes_transfer, launch_env, policy, protocol, refusals
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Exit, Lane, LaneOwner
from subfleet.credentials import startup_lanes
from subfleet.launch_env import ROSTER, SESSION_PROBE_NAMES, STORE
from subfleet.policy import PolicyError, check_lane_credentials
from subfleet.store import Store
from tests.unit.test_launch_env_round3 import env_lane, write_lanes
from tests.unit.test_launch_env_round5 import daemon_start_environ, no_spawn, spawned

# Off the launch allowlist: "AUTH" is a credential word, so no launch inherits it.
TOKEN = "SUBFLEET_TEST_OAUTH"
# The source tag of an ignored row. New here, so the regressions below also run,
# and fail, on the release before it (as round 5's tests read TEST_SESSION_NAMES).
IGNORED = getattr(launch_env, "IGNORED", "lanes.json, ignored")
DUMMIES = {"E2E_LANE_ONE_TOKEN": "dummy-one", TOKEN: "dummy-oauth", "SUBFLEET_FAKE_LANE_NINE": "dummy-nine",
           "SUBFLEET_HOST_HOME": "dummy-host", "LANG": "dummy-lang"}


def home_lane(root: Path, lane_id: str) -> Lane:
    home = str(root / "homes" / lane_id)
    return Lane(lane_id, "claude", "claude:" + lane_id, Credential("claude", home, "home"), home,
                LaneOwner.V2, False)


def store_lanes(root: Path, *lanes: Lane) -> None:
    with Store(root / "state.sqlite3") as store:
        for lane in lanes:
            store.put_lane(lane)


# --- P3-1: a refused lanes.json row for a lane the store holds is ignored ---------------

# Review r5's P3-1 inputs and their neighbours: (claude-9 in the store, its lanes.json
# row's variable). None stores a home-kind claude-9.
IGNORED_CASES = {
    # Input 1: the store round 4 left, with lanes.json unchanged.
    "round-4-store-home": ("HOME", "HOME"),
    "round-4-store-hook": ("SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_FAKE_LANE_NINE"),
    "held-row-names-another-refused-variable": ("SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_HOST_HOME"),
    # Input 2: claude-9 healthy on its own variable, its row changed to HOME.
    "healthy-store-row-changed-to-home": (TOKEN, "HOME"),
    "home-kind-store-row-names-lang": (None, "LANG"),
}


@pytest.mark.parametrize("case", sorted(IGNORED_CASES))
def test_a_refused_row_for_a_stored_lane_is_ignored_and_daemon_start_goes_on(case, tmp_path, monkeypatch, capsys):
    """Review r5, P3-1: such a row stopped `daemon start` (exit 2) though seeding
    skips it. Now it is ignored with one warning naming the lane and the row's
    variable, never a value, and the stored lane's own rule decides: held when
    its own variable is refused, launching on its own credential otherwise."""
    stored, row = IGNORED_CASES[case]
    store_lanes(tmp_path, home_lane(tmp_path, "claude-9") if stored is None else env_lane("claude-9", stored))
    write_lanes(tmp_path, env_lane("claude-1", "E2E_LANE_ONE_TOKEN"), env_lane("claude-9", row))
    daemon_start_environ(monkeypatch, tmp_path, **DUMMIES)
    notes: list[str] = []
    environment = cli.daemon_env(tmp_path, notes=notes)
    held = stored is not None and launch_env.refused_reference(stored)
    [note] = notes
    assert all(text in note for text in ("claude-9", row, "lanes.json", "the row is ignored")), note
    assert ("the lane is held" in note) == held, note
    assert "claude-1" not in note and "dummy" not in note
    assert environment["E2E_LANE_ONE_TOKEN"] == "dummy-one", "the other lane's credential is still held"
    if stored == TOKEN:
        assert environment[TOKEN] == "dummy-oauth", "the healthy stored lane keeps its variable"
    assert "SUBFLEET_HOST_HOME" not in environment and "dummy-host" not in environment.values()
    seen = spawned(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 0
    err = capsys.readouterr().err
    assert len(seen) == 1
    assert [line for line in err.splitlines() if "claude-9" in line] == [f"subfleet daemon start: {note}"]
    assert "dummy" not in err


def test_a_refused_row_for_a_new_lane_still_refuses_beside_an_ignored_one(tmp_path, monkeypatch, capsys):
    """Only a refused row whose lane the store lacks refuses startup, and the
    refusal names that lane alone (review r5, P3-1; C-10.5)."""
    store_lanes(tmp_path, env_lane("claude-9", "HOME"))
    write_lanes(tmp_path, env_lane("claude-9", "HOME"), env_lane("claude-8", "LANG"))
    daemon_start_environ(monkeypatch, tmp_path, **DUMMIES)
    with pytest.raises(PolicyError) as error:
        cli.daemon_env(tmp_path)
    assert error.value.key == "credential_ref"
    assert all(text in str(error.value) for text in ("claude-8", "LANG", "lanes.json"))
    assert "claude-9" not in str(error.value), "the stored lane's row is ignored, not refused"
    no_spawn(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 2
    err = capsys.readouterr().err
    assert "claude-8" in err and "dummy" not in err


def test_each_held_lane_and_ignored_row_gets_one_warning():
    """A held lane and its own ignored row are one lane, so one warning; a lane
    with only an ignored row gets the row's warning; order is kept."""
    found = check_lane_credentials({}, "policy.json", startup_lanes(
        [{"lane_id": "claude-9", "provider": "claude", "account_key": "a", "credential_kind": "env",
          "credential_ref": "HOME"},
         {"lane_id": "claude-7", "provider": "claude", "account_key": "b", "credential_kind": "env",
          "credential_ref": "LANG"}],
        [env_lane("claude-9", "HOME"), env_lane("claude-7", TOKEN), env_lane("claude-6", "SUBFLEET_HOST_HOME")]))
    assert found == [("claude-9", "HOME", IGNORED), ("claude-7", "LANG", IGNORED),
                     ("claude-9", "HOME", STORE), ("claude-6", "SUBFLEET_HOST_HOME", STORE)]
    notes = policy.startup_notes(found)
    assert [next(lane for lane in ("claude-9", "claude-7", "claude-6") if f"lane {lane}" in note)
            for note in notes] == ["claude-9", "claude-7", "claude-6"]
    nine, seven, six = notes
    assert "the lane is held" in nine and "names HOME too, so the row is ignored" in nine
    assert "the lane is held" not in seven and "the row is ignored" in seven and "LANG" in seven
    assert "the lane is held" in six and "ignored" not in six
    assert "a test session's variable" in six, "the reason fits the variable"


# --- the rule, as a bounded property ------------------------------------------------------

REFUSED = ("HOME", "LANG", "SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_HOST_HOME", "PYTEST_CURRENT_TEST")
GOOD = (TOKEN, "E2E_LANE_TWO_TOKEN")
LANE_IDS = ("claude-7", "claude-8", "claude-9")
ACCOUNTS = ("claude:lane-a", "claude:lane-b")


def expected(roster, stored, passed):
    """The model, from review r5's ruling, not from the code.

    The policy is refused when env.pass names a variable some lane holds as its
    credential. Otherwise a refused lanes.json row refuses startup only when the
    store has no lane by its id; a refused row for a stored lane is ignored. A
    stored env-kind lane with a refused variable is held unless a row for the
    same lane and account names a variable that is not refused.
    """
    refused = set(REFUSED).__contains__
    ids = {lane_id for lane_id, _, _ in stored}
    repaired = {lane_id for lane_id, ref, account in stored if ref is not None and refused(ref)
                and any(row_id == lane_id and row_account == account and not refused(row_ref)
                        for row_id, row_ref, row_account in roster)}
    credentials = ({ref for _, ref, _ in roster if not refused(ref)}
                   | {ref for lane_id, ref, _ in stored if ref is not None and not refused(ref)})
    if set(passed) & credentials:
        return "env.pass", None, None
    if any(refused(ref) and lane_id not in ids for lane_id, ref, _ in roster):
        return "credential_ref", None, None
    held = {lane_id for lane_id, ref, _ in stored if ref is not None and refused(ref) and lane_id not in repaired}
    ignored = {lane_id for lane_id, ref, _ in roster if refused(ref) and lane_id in ids}
    return None, held, ignored


@settings(max_examples=300, deadline=None, derandomize=True)
@given(roster=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(REFUSED + GOOD),
                                 st.sampled_from(ACCOUNTS)), max_size=4),
       stored=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from((None, *REFUSED, *GOOD)),
                                 st.sampled_from(ACCOUNTS)), max_size=3, unique_by=lambda lane: lane[0]),
       passed=st.sets(st.sampled_from(["GIT_CONFIG_GLOBAL", TOKEN, "HOME"]), max_size=2))
def test_bounded_property_only_a_refused_row_for_a_new_lane_refuses_startup(roster, stored, passed):
    """Invariants (review r5, P3-1), for every lanes.json and store pair: startup
    is refused exactly when the model refuses it; otherwise every refused row is
    ignored, never fatal, each held lane and each lane with an ignored row gets
    exactly one warning naming it, held only when held, and no warning names a
    lane outside those or carries anything but names."""
    rows = [{"lane_id": lane_id, "provider": "claude", "account_key": account, "credential_kind": "env",
             "credential_ref": ref} for lane_id, ref, account in roster]
    lanes = [Lane(lane_id, "claude", account,
                  Credential("claude", ref, "env") if ref else Credential("claude", "/fake/" + lane_id, "home"),
                  None, LaneOwner.V2, False) for lane_id, ref, account in stored]
    refusal, held, ignored = expected(roster, stored, passed)
    try:
        found = check_lane_credentials({"env": {"pass": sorted(passed)}}, "policy.json", startup_lanes(rows, lanes))
    except PolicyError as error:
        assert error.key.startswith("env.pass") if refusal == "env.pass" else error.key == refusal
        return
    assert refusal is None, "startup went on past a refusal the rule makes"
    assert {lane_id for lane_id, _, source in found if source == STORE} == held
    assert {lane_id for lane_id, _, source in found if source == IGNORED} == ignored
    notes = policy.startup_notes(found)
    assert len(notes) == len(held | ignored)
    for lane_id in held | ignored:
        [note] = [note for note in notes if f"lane {lane_id} " in note]
        assert ("the lane is held" in note) == (lane_id in held)
    assert not any(f"lane {lane_id} " in note for note in notes for lane_id in set(LANE_IDS) - held - ignored)


# --- the install check agrees with startup ------------------------------------------------


@settings(max_examples=200, deadline=None, derandomize=True)
@given(roster=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(REFUSED + GOOD + ("not a name",)),
                                 st.sampled_from(["env", "home", "keychain-token"])), max_size=4))
def test_bounded_property_the_install_check_lists_what_startup_refuses_or_ignores(roster):
    """Differential (D-ENV2): with an empty store, startup refuses exactly the
    lanes the install check lists; with every lane stored, it refuses none of
    them and warns once for each. The check never needs the store to list them."""
    rows = [{"lane_id": lane_id, "provider": "claude", "account_key": "claude:" + lane_id,
             "credential_kind": kind, "credential_ref": ref} for lane_id, ref, kind in roster]
    listed = credentials.refused_rows(rows)
    assert listed == [(lane_id, ref) for lane_id, ref, kind in roster
                      if kind == "env" and launch_env.refused_reference(ref)]
    try:
        check_lane_credentials({}, "policy.json", startup_lanes(rows, []))
    except PolicyError as error:
        assert listed and all(f"lane {lane_id} " in str(error) for lane_id, _ in listed)
    else:
        assert not listed
    stored = [home_lane(Path("/fake"), lane_id) for lane_id in LANE_IDS]
    found = check_lane_credentials({}, "policy.json", startup_lanes(rows, stored))
    assert {(lane_id, name) for lane_id, name, source in found if source == IGNORED} == set(listed)
    assert len(policy.startup_notes(found)) == len({lane_id for lane_id, _ in listed})


# --- P3-1's third input: `lanes transfer` of a held lane ----------------------------------


@pytest.fixture
def transfer_world(tmp_path):
    """A v2 root with two held lanes and a healthy env-kind lane, synthetic v1 rosters."""
    world = SimpleNamespace(root=tmp_path / "v2", roster=tmp_path / "v1-roster", home=tmp_path / "home",
                            agents=tmp_path / "LaunchAgents", v1_state=tmp_path / "v1-state")
    for path in (world.root, world.roster, world.home, world.agents, world.v1_state / "runs"):
        path.mkdir(parents=True)
    (world.roster / "claude-accounts.json").write_text(json.dumps({"enrolled": {}, "accounts": []}, indent=1))
    (world.roster / "codex-accounts.json").write_text(json.dumps({"auto_reset": {"enabled": True}}, indent=1))
    world.store = Store(world.root / "state.sqlite3")
    world.store.put_lane(env_lane("claude-9", "HOME"))
    world.store.put_lane(Lane("claude-6", "claude", "claude:claude-6",
                              Credential("claude", "SUBFLEET_FAKE_LANE_SIX", "env"), None, LaneOwner.V1, False))
    world.store.put_lane(Lane("claude-7", "claude", "claude:claude-7", Credential("claude", TOKEN, "env"),
                              None, LaneOwner.V1, False))
    yield world
    world.store.close()


def transfer(world, lane_id, to, **kwargs):
    return lanes_transfer.transfer(world.store, world.root, lane_id, to, roster_dir=world.roster,
                                   home=world.home, agents_dir=world.agents, v1_state=world.v1_state, **kwargs)


def disk(world) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for base in (world.root, world.roster)
            for path in sorted(base.rglob("*")) if path.is_file() and not path.name.startswith("state.sqlite3")}


@pytest.mark.parametrize("lane_id,variable", [("claude-9", "HOME"), ("claude-6", "SUBFLEET_FAKE_LANE_SIX")])
@pytest.mark.parametrize("to", ["v1", "v2"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_lanes_transfer_refuses_a_held_lane(transfer_world, lane_id, variable, to, dry_run):
    """Review r5, P3-1, input 3: transferring a held lane wrote the store's row into
    lanes.json with its refused variable, and the next start exited 2. It is now
    refused, dry run or not, before anything is planned or written: no lanes.json
    row, no v1 roster edit, no ownership change and no event."""
    world = transfer_world
    before, owner = disk(world), world.store.get_lane(lane_id).owner
    with pytest.raises(lanes_transfer.TransferError) as refused:
        transfer(world, lane_id, to, dry_run=dry_run, confirm_v1_edit=True)
    assert refused.value.reason == "transfer-held" and refused.value.code == Exit.REFUSED
    assert disk(world) == before and not (world.root / "lanes.json").exists()
    assert world.store.get_lane(lane_id).owner == owner
    assert world.store.query("SELECT * FROM events WHERE kind='lane.transferred'") == []
    # The daemon's `lanes` op passes the refusal on as a ProtocolError; the client sees this.
    wire = protocol.ProtocolError(str(refused.value), refused.value.code, refused.value.fix,
                                  reason=refused.value.reason, details=refused.value.details)
    message, fix = refusals.handler_refusal(wire)
    assert lane_id in message and "held" in message
    assert (f"credential-refused={variable}" in message) == (variable in refusals._choices()["lane_variable"])
    assert fix == lanes_transfer.HELD_LANE_FIX == refused.value.fix
    assert "dummy" not in message + fix


def test_lanes_transfer_still_moves_a_healthy_env_lane(transfer_world):
    """The control: an env-kind lane whose variable no launch inherits transfers,
    and its lanes.json row names that variable."""
    result = transfer(transfer_world, "claude-7", "v2", confirm_v1_edit=True)
    assert result["applied"] is True and transfer_world.store.get_lane("claude-7").owner == LaneOwner.V2
    [row] = json.loads((transfer_world.root / "lanes.json").read_text())
    assert (row["lane_id"], row["credential_kind"], row["credential_ref"]) == ("claude-7", "env", TOKEN)


def test_no_transfer_writes_a_refused_row_into_lanes_json(transfer_world):
    """Invariant: after any transfer of any stored lane, to either owner, every
    env-kind lanes.json row names a variable no launch inherits."""
    for lane_id in ("claude-9", "claude-6", "claude-7"):
        for to in ("v2", "v1"):
            try:
                transfer(transfer_world, lane_id, to, confirm_v1_edit=True)
            except lanes_transfer.TransferError as error:
                assert error.reason == "transfer-held", (lane_id, to)
    rows = json.loads((transfer_world.root / "lanes.json").read_text())
    assert [row["lane_id"] for row in rows if row["credential_kind"] == "env"
            and launch_env.refused_reference(row["credential_ref"])] == []


# --- P3-2: doctor names each held lane ----------------------------------------------------


def lane_row(lane_id, kind, ref, provider="claude"):
    return {"lane_id": lane_id, "provider": provider, "account_key": f"{provider}:{lane_id}",
            "credential_kind": kind, "credential_ref": ref}


HELD = [("claude-9", "HOME"), ("claude-6", "SUBFLEET_FAKE_LANE_SIX"), ("claude-5", "PYTEST_CURRENT_TEST")]


def test_doctor_names_each_held_lane_with_its_fix(tmp_path, monkeypatch):
    """Review r5, P3-2: with lanes held, the lane-variable row passed with "the
    daemon holds no env-kind lane variable". It warns now, naming each held lane
    and its variable, and its fix is each lane's lanes.json edit. Names only."""
    lanes = [lane_row("claude-7", "env", TOKEN), *(lane_row(lane_id, "env", ref) for lane_id, ref in HELD),
             lane_row("codex-1", "home", "/fake/codex-1", "codex")]
    status = {"environment": {"extra_names": [], "lane_credentials": [TOKEN]}, "lanes": lanes}
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    found = doctor.check_daemon_lane_credentials(tmp_path)
    assert found["status"] == doctor.WARN
    for lane_id, ref in HELD:
        assert f"{lane_id} (credential-refused={ref})" in found["detail"]
        edit = {"lane_id": lane_id, "provider": "claude", "account_key": f"claude:{lane_id}",
                "credential_kind": "env", "credential_ref": "<VARIABLE>"}
        assert json.dumps(edit) in found["fix"], lane_id
    assert "claude-7 (" not in found["detail"] and "codex-1" not in found["detail"] + found["fix"]
    assert TOKEN in found["detail"], "the daemon's own lane variables are still listed"
    rendered = doctor.render([found])
    assert "fix: in lanes.json" in rendered and "dummy" not in rendered
    assert doctor.exit_code([found]) == 0, "a warning never decides the exit status"


def test_doctor_lane_row_passes_when_no_lane_is_held(tmp_path, monkeypatch):
    status = {"environment": {"extra_names": [], "lane_credentials": [TOKEN]},
              "lanes": [lane_row("claude-7", "env", TOKEN), lane_row("claude-8", "keychain-token", "HOME")]}
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    assert doctor.check_daemon_lane_credentials(tmp_path)["status"] == doctor.PASS


# --- P3-3: every session probe name is a refused lane variable ----------------------------


class Recording(dict):
    """An environment that records each name whose value is read."""

    def __init__(self, values):
        super().__init__(values)
        self.reads: list[str] = []

    def __getitem__(self, name):
        self.reads.append(name)
        return super().__getitem__(name)

    def get(self, name, default=None):
        self.reads.append(name)
        return super().get(name, default)


@pytest.mark.parametrize("name", sorted(SESSION_PROBE_NAMES))
def test_every_session_probe_name_is_a_refused_lane_variable(name, tmp_path, monkeypatch):
    """Review r5, P3-3: `PYTEST_CURRENT_TEST` was accepted as a lane variable,
    though selection reads it to find a test session before withholding any
    name. Every probe name is refused now: a lanes.json row naming one stops
    startup, a stored lane naming one is held and not handed to the daemon by
    name, and resolution refuses it before reading its value."""
    assert launch_env.refused_reference(name)
    write_lanes(tmp_path, env_lane("claude-9", name))
    daemon_start_environ(monkeypatch, tmp_path)
    with pytest.raises(PolicyError) as error:
        cli.daemon_env(tmp_path)
    assert error.value.key == "credential_ref" and "claude-9" in str(error.value) and name in str(error.value)
    (tmp_path / "lanes.json").unlink()
    store_lanes(tmp_path, env_lane("claude-9", name))
    notes: list[str] = []
    environment = cli.daemon_env(tmp_path, notes=notes)
    [note] = notes
    assert "claude-9" in note and name in note and "the lane is held" in note
    if not launch_env.inherited_name(name):
        assert name not in environment, "a held lane's variable is passed to the daemon by no one"
    assert credentials.env_references([env_lane("claude-9", name)]) == frozenset()
    parent = Recording({"PATH": "/usr/bin:/bin", name: "dummy-probe"})
    monkeypatch.setattr(os, "environ", parent)
    with pytest.raises(AdapterError) as refused:
        credentials.resolve_credential(Credential("claude", name, "env"))
    assert refused.value.reason == "credential-env-inherited"
    assert name not in parent.reads, "resolution read a refused variable's value"


def test_a_probe_name_says_why_it_is_refused():
    """The refusal's reason fits the name: it is read, not inherited."""
    assert launch_env.refused_phrase("PYTEST_CURRENT_TEST") == "a variable launch selection reads to find a test session"
    assert launch_env.refused_phrase("HOME") == "a variable every launch inherits"
    assert "test session" in launch_env.refused_phrase("SUBFLEET_HOST_HOME")


# --- the install check: `daemon start --check` --------------------------------------------


def install_root(tmp_path, variable="SUBFLEET_FAKE_LANE_NINE"):
    """One inheriting env-kind row, one clean env-kind row and a home row; beside them
    a policy and a store that would break anything reading them."""
    root = tmp_path / "state"
    root.mkdir()
    (root / "lanes.json").write_text(json.dumps([
        lane_row("claude-9", "env", variable), lane_row("claude-1", "env", TOKEN),
        lane_row("codex-1", "home", str(root / "lanes" / "codex-1"), "codex")]))
    (root / "policy.json").write_text("{not json")
    (root / "state.sqlite3").write_bytes(b"not a database")
    return root


@contextmanager
def watched(root):
    """Every path under `root` opened, and every database or socket connection
    made, while the block runs. The hook stays installed afterwards, inert."""
    seen, active = [], [True]

    def hook(event, args):
        if not active[0]:
            return
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.fsdecode(args[0])
            if path.startswith(str(root)):
                seen.append(path)
        elif event in ("sqlite3.connect", "socket.connect"):
            seen.append(event)

    sys.addaudithook(hook)
    try:
        yield seen
    finally:
        active[0] = False


def isolated_check(monkeypatch, root, argv=None):
    """Run the install check with no daemon, store, policy or spawn reachable:
    `subfleet daemon start --check <argv>`, or with no `argv` its handler alone,
    which parses nothing (argparse and gettext read locale and terminal names)."""
    parent = Recording({"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(root), **DUMMIES})
    monkeypatch.setattr(os, "environ", parent)
    assert cli._root(SimpleNamespace()).resolve() == root.resolve(), "the check would leave the test's root"
    for name in ("Client", "Offline", "load_policy", "_lane_rows", "_daemon_alive"):
        monkeypatch.setattr(cli, name, lambda *args, _name=name, **kwargs: pytest.fail(f"the check reached {_name}"))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("the check spawned"))
    parent.reads.clear()
    with watched(root) as seen:
        code = (cli.cmd_daemon_start(SimpleNamespace(check=True, json=False)) if argv is None
                else cli.main(["daemon", "start", "--check", *argv]))
    return code, seen, parent


@pytest.mark.parametrize("variable", ["SUBFLEET_FAKE_LANE_NINE", "HOME", "LANG", "SUBFLEET_HOST_HOME",
                                      "PYTEST_CURRENT_TEST"])
def test_daemon_start_check_lists_each_inheriting_env_row_and_reads_only_lanes_json(tmp_path, monkeypatch,
                                                                                    capsys, variable):
    """D-ENV2: the install check lists the inheriting env-kind row by lane and
    variable, not the clean one; exits 2, as startup would for a new lane; and
    reads lanes.json alone: no store, policy, socket, spawn or variable value."""
    root = install_root(tmp_path, variable)
    code, seen, parent = isolated_check(monkeypatch, root)
    out, err = capsys.readouterr()
    assert code == int(Exit.INVALID_INPUT), err
    assert out.splitlines() == [f"claude-9 {variable}"]
    assert "claude-1" not in out + err and "codex-1" not in out + err and "dummy" not in out + err
    assert "fix:" in err
    assert set(seen) == {str(root / "lanes.json")}, "the check read something besides lanes.json"
    assert not {variable, TOKEN} & set(parent.reads), "the check read a lane variable's value"


def test_the_parser_gives_daemon_start_its_check():
    args = cli.build_parser().parse_args(["daemon", "start", "--check"])
    assert (args.handler, args.daemon_command, args.check) == (cli.cmd_daemon, "start", True)
    assert cli.build_parser().parse_args(["daemon", "start"]).check is False


def test_daemon_start_check_through_the_cli_reads_only_lanes_json(tmp_path, monkeypatch, capsys):
    root = install_root(tmp_path)
    code, seen, _ = isolated_check(monkeypatch, root, [])
    out, _ = capsys.readouterr()
    assert (code, out) == (int(Exit.INVALID_INPUT), "claude-9 SUBFLEET_FAKE_LANE_NINE\n")
    assert set(seen) == {str(root / "lanes.json")}


def test_daemon_start_check_json_names_lane_and_variable_only(tmp_path, monkeypatch, capsys):
    root = install_root(tmp_path)
    code, seen, _ = isolated_check(monkeypatch, root, ["--json"])
    out, _ = capsys.readouterr()
    assert code == int(Exit.INVALID_INPUT)
    assert json.loads(out) == {"lanes_json": str(root / "lanes.json"),
                               "refused": [{"lane_id": "claude-9", "variable": "SUBFLEET_FAKE_LANE_NINE"}]}


@pytest.mark.parametrize("contents", [None, [], {"lanes": [lane_row("claude-1", "env", TOKEN)]}])
def test_daemon_start_check_passes_a_clean_or_missing_roster(tmp_path, monkeypatch, capsys, contents):
    root = install_root(tmp_path)
    if contents is None:
        (root / "lanes.json").unlink()
    else:
        (root / "lanes.json").write_text(json.dumps(contents))
    code, seen, _ = isolated_check(monkeypatch, root, [])
    out, err = capsys.readouterr()
    assert (code, out) == (0, ""), err
    assert set(seen) <= {str(root / "lanes.json")}


@pytest.mark.parametrize("contents", [b"{not json", b"\xff\xfe", b'"a string"'])
def test_daemon_start_check_refuses_an_unreadable_roster_without_echoing_it(tmp_path, monkeypatch, capsys,
                                                                            contents):
    root = install_root(tmp_path)
    (root / "lanes.json").write_bytes(contents)
    code, _, _ = isolated_check(monkeypatch, root, [])
    out, err = capsys.readouterr()
    assert code == int(Exit.INVALID_INPUT) and out == ""
    assert "lanes.json" in err and "not json" not in err and "a string" not in err


def test_daemon_start_check_never_waits_on_a_fifo(tmp_path, monkeypatch, capsys):
    """A FIFO at lanes.json is refused, never opened to wait for a writer."""
    root = install_root(tmp_path)
    (root / "lanes.json").unlink()
    os.mkfifo(root / "lanes.json")
    code, _, _ = isolated_check(monkeypatch, root, [])
    assert code == int(Exit.INVALID_INPUT)
    assert "NotRegularFile" in capsys.readouterr().err


def test_daemon_start_without_check_still_starts(tmp_path, monkeypatch, capsys):
    """`--check` is the only path that starts nothing; a plain start is unchanged."""
    write_lanes(tmp_path, env_lane("claude-1", TOKEN))
    daemon_start_environ(monkeypatch, tmp_path, **DUMMIES)
    seen = spawned(monkeypatch)
    assert cli.main(["daemon", "start"]) == 0
    assert len(seen) == 1 and seen[0][TOKEN] == "dummy-oauth"


def test_the_roster_check_source_tags_are_distinct():
    assert len({ROSTER, STORE, IGNORED}) == 3
