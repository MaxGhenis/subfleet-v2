"""Review r1 of #248 (D-ENV2, fix round 1): three P3s, each from a refused
lanes.json row that D-ENV2 lets the daemon start with, where the release before
it exited 2.

- P3-1: a harness hook that only an ignored row names was withheld from no
  launch. It is withheld from every launch now, exactly as a held lane's hook
  is (`credentials.ignored_hooks`). The daemon-level regression, with real
  launches, is tests/fake/test_launch_env_ignored_hook.py.
- P3-2: `daemon start --check` printed lanes.json's lane ids unscrubbed, in text
  and in `--json`, where `daemon start` scrubs them (C-25.7). It scrubs them now,
  and so does doctor's held-lane row.
- P3-3: a transfer set only `owner` on the lane's lanes.json row, so a refused
  row stayed, and a store rebuilt from the file refused startup. A transfer now
  repairs each of the lane's refused rows to the store's own credential.

Every value is a dummy, and failures name variables and lanes, never values. No
test reads a credential store, the live store or a live daemon. The named
mutations are in tests/unit/test_launch_env_mutations.py.
"""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import cli, credentials, daemon_log, doctor, lanes_transfer, protocol, refusals
from subfleet.contracts import Credential, Exit, Lane, LaneOwner
from subfleet.credentials import startup_lanes
from subfleet.launch_env import IGNORED, build_environment, lane_reference, refused_reference
from subfleet.policy import PolicyError, check_lane_credentials
from subfleet.store import Store
from tests.unit.test_launch_env_round3 import env_lane
from tests.unit.test_launch_env_round5 import daemon_start_environ, no_spawn
from tests.unit.test_launch_env_round6 import DUMMIES, TOKEN, isolated_check, lane_row

# A dummy shaped like a GitHub token, built here; never a real one.
TOKEN_SHAPED = "ghp_" + "A" * 36
# Review r1's harness hook: no credential word, so every launch would inherit it.
EIGHT = "SUBFLEET_FAKE_LANE_EIGHT"
# Before this round `credentials.ignored_hooks` does not exist; the regressions
# read it with a fallback, so each fails on its own assertion there.
IGNORED_HOOKS = getattr(credentials, "ignored_hooks", lambda found: frozenset())

HOOKS = (EIGHT, "SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_E2E_LANE_SIX")
REFUSED = HOOKS + ("HOME", "LANG", "SUBFLEET_HOST_HOME", "PYTEST_CURRENT_TEST")
GOOD = (TOKEN, "E2E_LANE_TWO_TOKEN")
LANE_IDS = ("claude-7", "claude-8", "claude-9")


def leaked(text: str) -> list[str]:
    """What `text` shows that it must not: the token-shaped id, or a dummy value
    (named by its variable, never quoted)."""
    shown = ["the token-shaped id"] if TOKEN_SHAPED in text else []
    return shown + sorted(name for name, value in {**DUMMIES, EIGHT: "dummy-eight"}.items() if value in text)


def home_kind(lane_id: str, owner: LaneOwner = LaneOwner.V2) -> Lane:
    return Lane(lane_id, "claude", "claude:" + lane_id, Credential("claude", "/fake/" + lane_id, "home"),
                "/fake/" + lane_id, owner, False)


def stored_lane(lane_id: str, ref: str | None) -> Lane:
    """An env-kind lane on `ref`, or a home-kind one for None; one account per lane id."""
    return home_kind(lane_id) if ref is None else env_lane(lane_id, ref)


# --- P3-1: an ignored row's harness hook is withheld like a held lane's --------------------


@pytest.mark.parametrize("stored,row,withheld", [
    # Review r1's input: claude-9 healthy on its own variable, its row naming a hook.
    pytest.param(TOKEN, EIGHT, True, id="ignored-row-names-a-hook"),
    pytest.param(None, EIGHT, True, id="home-kind-lane-row-names-a-hook"),
    # The control: claude-9 held on the hook itself, withheld before this round too.
    pytest.param(EIGHT, EIGHT, True, id="held-on-the-hook"),
    # A listed name is left to the selection, for an ignored row as for a held lane.
    pytest.param(TOKEN, "HOME", False, id="ignored-row-names-home"),
])
def test_an_ignored_rows_harness_hook_is_withheld_as_a_held_lanes_is(stored, row, withheld):
    """Review r1, P3-1, without a daemon: the names the daemon withholds are the
    stored lanes' (`withheld_references`) and the ignored rows' hooks. A launch
    selected from a parent holding every name with a dummy value carries none of
    them, and still carries HOME."""
    rows = [lane_row("claude-9", "env", row), lane_row("claude-1", "env", "E2E_LANE_ONE_TOKEN")]
    lanes = [stored_lane("claude-9", stored), env_lane("claude-1", "E2E_LANE_ONE_TOKEN")]
    found = check_lane_credentials({}, "policy.json", startup_lanes(rows, lanes))
    assert ("claude-9", row, IGNORED) in found
    names = credentials.withheld_references(lanes) | IGNORED_HOOKS(found)
    assert (row in names) == withheld, "an ignored row's hook is withheld exactly when a held lane's would be"
    parent = {"PATH": "/usr/bin:/bin", "HOME": "/fake/home", EIGHT: "dummy-eight", TOKEN: "dummy-oauth",
              "E2E_LANE_ONE_TOKEN": "dummy-one"}
    selected = build_environment(environ=parent, withheld=names)
    assert (EIGHT in selected) == (not withheld and stored != EIGHT)
    assert "HOME" in selected and leaked("\n".join(selected.values())) == leaked("dummy-eight" * (not withheld))


def withheld_model(stored, rows):
    """The model, from the rule as review r1's fix states it, not from the code.

    Returns None when startup is refused: a refused row for a lane the store
    lacks. Otherwise the lanes after seeding and the names every launch
    withholds. Seeding stores each new lane from its first row, and repairs a
    held lane from the first row for it that names a good variable. Withheld
    are each lane's variable when it is good or a harness hook, and each hook a
    refused row for a stored lane names, that row being ignored.
    """
    refused, hooks = set(REFUSED).__contains__, set(HOOKS).__contains__
    ids = {lane_id for lane_id, _ in stored}
    if any(refused(ref) and lane_id not in ids for lane_id, ref in rows):
        return None
    effective = {}
    for lane_id, ref in stored:
        if ref is not None and refused(ref):
            ref = next((row_ref for row_id, row_ref in rows if row_id == lane_id and not refused(row_ref)), ref)
        effective[lane_id] = ref
    for lane_id, ref in rows:
        effective.setdefault(lane_id, ref)
    names = {ref for ref in effective.values() if ref is not None and (not refused(ref) or hooks(ref))}
    return effective, names | {ref for lane_id, ref in rows if hooks(ref) and lane_id in ids}


@settings(max_examples=300, deadline=None, derandomize=True)
@given(stored=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from((None, *REFUSED, *GOOD))),
                       max_size=3, unique_by=lambda lane: lane[0]),
       rows=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(REFUSED + GOOD)), max_size=4))
def test_bounded_property_every_lanes_hook_is_withheld_from_every_launch(stored, rows):
    """Invariant 1 (review r1, P3-1), for every lanes.json and store pair the
    daemon starts with: the names withheld are exactly the model's, so a harness
    hook named by any lane, stored or by an ignored row, reaches no launch, and
    a hook no lane names still does; a listed name is never withheld."""
    model = withheld_model(stored, rows)
    roster = [lane_row(lane_id, "env", ref) for lane_id, ref in rows]
    try:
        found = check_lane_credentials({}, "policy.json", startup_lanes(roster, [stored_lane(*lane) for lane in stored]))
    except PolicyError:
        assert model is None, "startup refused what the rule allows"
        return
    assert model is not None, "startup went on past a refused row for a new lane"
    effective, expected = model
    names = credentials.withheld_references([stored_lane(*lane) for lane in effective.items()]) | IGNORED_HOOKS(found)
    assert names == expected
    parent = {"PATH": "/usr/bin:/bin", "HOME": "/fake/home", "LANG": "C",
              **{name: "dummy-" + name.lower() for name in HOOKS + GOOD}}
    selected = build_environment(environ=parent, withheld=names)
    assert {name for name in HOOKS if name in selected} == set(HOOKS) - expected
    assert {"HOME", "LANG", "PATH"} <= set(selected) and not set(GOOD) & set(selected)


# --- P3-2: `daemon start --check` prints lane ids scrubbed, as `daemon start` does ---------


def check_root(tmp_path: Path, rows: list) -> Path:
    """A state root holding lanes.json alone: no policy, store or daemon."""
    root = tmp_path / "state"
    root.mkdir()
    (root / "lanes.json").write_text(json.dumps(rows))
    return root


@pytest.mark.parametrize("variable", ["HOME", EIGHT, "PYTEST_CURRENT_TEST"])
@pytest.mark.parametrize("mode", ["text", "json"])
def test_daemon_start_check_scrubs_a_token_shaped_lane_id(tmp_path, monkeypatch, capsys, mode, variable):
    """Review r1, P3-2: a new lane's row whose lane id is a dummy GitHub-token
    shape, naming a refused variable. `daemon start` refuses it with the id
    scrubbed (C-25.7, #233), the control; `--check` printed the raw id, in both
    modes. It prints the id as `daemon start` does now, and reads lanes.json alone."""
    root = check_root(tmp_path, [lane_row(TOKEN_SHAPED, "env", variable), lane_row("claude-1", "env", TOKEN)])
    shown = daemon_log.scrubbed(TOKEN_SHAPED)
    assert TOKEN_SHAPED not in shown, "the scrubber masks the dummy id"
    daemon_start_environ(monkeypatch, root, **DUMMIES)
    no_spawn(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(root))) == int(Exit.INVALID_INPUT)
    refusal = capsys.readouterr().err
    assert f"lane {shown} names {variable}" in refusal and leaked(refusal) == []
    code, seen, _ = isolated_check(monkeypatch, root, ["--json"] if mode == "json" else [])
    out, err = capsys.readouterr()
    assert code == int(Exit.INVALID_INPUT)
    assert leaked(out + err) == [], "the check printed the lane id unscrubbed"
    listed = ([[row["lane_id"], row["variable"]] for row in json.loads(out)["refused"]] if mode == "json"
              else [line.rsplit(" ", 1) for line in out.splitlines()])
    assert listed == [[shown, variable]]
    assert set(seen) == {str(root / "lanes.json")}


def run_check(root: Path, mode: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli._daemon_start_check(SimpleNamespace(json=mode == "json"), root)
    return code, out.getvalue(), err.getvalue()


# The token standing alone, as the scrubber reads one: nothing but a space or a
# bracket beside it.
STANDALONE = st.tuples(st.sampled_from(["", "claude-9 ", "("]), st.just(TOKEN_SHAPED),
                       st.sampled_from(["", " x", ")"])).map("".join)
# The token joined to a word by a hyphen. The scrubber's token patterns end at
# `(?<![A-Za-z0-9_-])` and `(?![A-Za-z0-9_-])` (`sessions/handoff.py`), so it
# leaves this one as it is, in `daemon start`'s refusal and in daemon.log alike.
JOINED = st.sampled_from(["lane-" + TOKEN_SHAPED, TOKEN_SHAPED + "-9"])


def joined(lane_id) -> bool:
    return isinstance(lane_id, str) and (f"-{TOKEN_SHAPED}" in lane_id or f"{TOKEN_SHAPED}-" in lane_id)


@settings(max_examples=150, deadline=None, derandomize=True)
@given(rows=st.lists(st.tuples(st.one_of(STANDALONE, JOINED, st.sampled_from(["claude-7", "codex-1", 7, None])),
                               st.sampled_from(REFUSED + GOOD + ("not a name",)), st.sampled_from(["env", "home"])),
                     max_size=4))
def test_bounded_property_the_check_prints_each_row_as_daemon_start_would(rows):
    """Invariant 2 for the install check, differential: text and `--json` list
    the same rows, in lanes.json's order, each lane id and variable exactly as
    `daemon start` prints them (`daemon_log.scrubbed`), so neither shows a
    token-shaped id the scrubber reads as standing alone. One joined to a word
    by a hyphen is printed as `daemon start` prints it, which is unchanged."""
    roster = [{"lane_id": lane_id, "provider": "claude", "account_key": "claude:x", "credential_kind": kind,
               "credential_ref": ref} for lane_id, ref, kind in rows]
    expected = [[daemon_log.scrubbed(str(lane_id)), ref] for lane_id, ref, kind in rows
                if kind == "env" and refused_reference(ref)]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "lanes.json").write_text(json.dumps(roster))
        text_code, text, text_err = run_check(root, "text")
        json_code, emitted, json_err = run_check(root, "json")
    assert text_code == json_code == (int(Exit.INVALID_INPUT) if expected else 0)
    assert [row.rsplit(" ", 1) for row in text.splitlines()] == expected
    assert [[row["lane_id"], row["variable"]] for row in json.loads(emitted)["refused"]] == expected
    if not any(joined(lane_id) for lane_id, _, _ in rows):
        assert leaked(text + text_err + emitted + json_err) == []


def test_doctor_names_a_held_lane_by_its_id_scrubbed(tmp_path, monkeypatch):
    """Invariant 2 for doctor: its held-lane row names each held lane, whose id
    the store took from lanes.json, scrubbed as `daemon start` names it; each
    lane is scrubbed apart, so none is cut for the others' length."""
    held = [lane_row(TOKEN_SHAPED, "env", "HOME"), lane_row("claude-9", "env", EIGHT),
            *(lane_row(f"claude-{n}", "env", "LANG") for n in range(10, 22))]
    status = {"environment": {"extra_names": [], "lane_credentials": [TOKEN]},
              "lanes": [*held, lane_row("claude-7", "env", TOKEN)]}
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    found = doctor.check_daemon_lane_credentials(tmp_path)
    assert found["status"] == doctor.WARN
    rendered = doctor.render([found]) + json.dumps(found)
    assert leaked(rendered) == []
    shown = daemon_log.scrubbed(TOKEN_SHAPED)
    assert f"{shown} (credential-refused=HOME)" in found["detail"]
    assert f"claude-9 (credential-refused={EIGHT})" in found["detail"]
    for lane in held[1:]:
        edit = {"lane_id": lane["lane_id"], "provider": "claude", "account_key": lane["account_key"],
                "credential_kind": "env", "credential_ref": "<VARIABLE>"}
        assert json.dumps(edit) in found["fix"], lane["lane_id"]
    assert "claude-7 (" not in found["detail"]


def test_a_held_lanes_transfer_refusal_names_no_token_shaped_id(tmp_path):
    """Invariant 2 for `lanes transfer`: the refusal a client reads renders its
    lane id from a closed list, so a token-shaped id is never shown."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(env_lane(TOKEN_SHAPED, "HOME"))
        with pytest.raises(lanes_transfer.TransferError) as refused:
            lanes_transfer.transfer(store, tmp_path, TOKEN_SHAPED, "v1", dry_run=True, roster_dir=tmp_path,
                                    home=tmp_path, agents_dir=tmp_path, v1_state=tmp_path)
    assert refused.value.reason == "transfer-held"
    wire = protocol.ProtocolError(str(refused.value), refused.value.code, refused.value.fix,
                                  reason=refused.value.reason, details=refused.value.details)
    message, fix = refusals.handler_refusal(wire)
    assert "held" in message and leaked(message + fix) == []
    assert not (tmp_path / "lanes.json").exists()


# --- P3-3: a transfer never leaves a refused row behind ------------------------------------


@contextlib.contextmanager
def repair_root(base: Path, lanes=None):
    """A v2 root holding `lanes`, by default claude-9 stored and healthy on its own
    variable, and synthetic v1 rosters. Each test writes its own lanes.json."""
    world = SimpleNamespace(root=base / "v2", roster=base / "v1-roster", home=base / "home",
                            agents=base / "LaunchAgents", v1_state=base / "v1-state")
    for path in (world.root, world.roster, world.home, world.agents, world.v1_state / "runs"):
        path.mkdir(parents=True)
    (world.roster / "claude-accounts.json").write_text(json.dumps({"enrolled": {}, "accounts": []}, indent=1))
    (world.roster / "codex-accounts.json").write_text(json.dumps({"auto_reset": {"enabled": True}}, indent=1))
    with Store(world.root / "state.sqlite3") as world.store:
        for lane in [env_lane("claude-9", TOKEN)] if lanes is None else lanes:
            world.store.put_lane(lane)
        yield world


@pytest.fixture
def repair_world(tmp_path):
    with repair_root(tmp_path) as world:
        yield world


def move(world, lane_id, to, **kwargs):
    return lanes_transfer.transfer(world.store, world.root, lane_id, to, roster_dir=world.roster,
                                   home=world.home, agents_dir=world.agents, v1_state=world.v1_state, **kwargs)


def planned(world, lane_id, to) -> list[dict]:
    """The lanes.json rows a transfer would write, from its plan."""
    plan = lanes_transfer.plan_transfer(world.store, world.root, lane_id, to, roster_dir=world.roster,
                                        home=world.home, agents_dir=world.agents, v1_state=world.v1_state)
    [edit] = [edit for edit in plan.edits if edit.owner == "v2"]
    return json.loads(edit.after)


def refused_rows(rows: list) -> list:
    """Each row's lane id whose credential, as startup reads it, names a refused variable."""
    return [found[0] for found in map(lane_reference, rows) if found and refused_reference(found[1])]


def rebuilt_store_starts(rows: list) -> bool:
    """Whether a store rebuilt from these rows, the store empty, passes startup's check."""
    try:
        check_lane_credentials({}, "policy.json", startup_lanes(rows, []))
    except PolicyError:
        return False
    return True


def nested(row: dict) -> dict:
    """The same row with its credential as an object, as `_seed_lanes` also reads it."""
    return {key: value for key, value in row.items() if not key.startswith("credential_")} | {
        "credential": {"provider": row["provider"], "kind": row["credential_kind"], "ref": row["credential_ref"],
                       "epoch": 1}}


@pytest.mark.parametrize("to", ["v1", "v2"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_review_r1_a_transfer_repairs_the_lanes_refused_row(repair_world, to, dry_run):
    """Review r1, P3-3: claude-9 stored on SUBFLEET_TEST_OAUTH while its lanes.json
    row names HOME. The transfer planned `{"credential_ref": "HOME", "owner": "v1"}`,
    and a store rebuilt from that file refused startup. The row is repaired now,
    to the store's own variable, dry run or not; no other row changes."""
    world = repair_world
    other = lane_row("claude-1", "env", "E2E_LANE_ONE_TOKEN")
    before = [{**lane_row("claude-9", "env", "HOME"), "owner": "v2"}, other]
    (world.root / "lanes.json").write_text(json.dumps(before, indent=2))
    assert not rebuilt_store_starts(before), "the input refuses a rebuilt store"
    rows = planned(world, "claude-9", to)
    nine = next(row for row in rows if row["lane_id"] == "claude-9")
    assert (nine["credential_kind"], nine["credential_ref"], nine["owner"]) == ("env", TOKEN, to)
    assert rows[1] == other and refused_rows(rows) == [] and rebuilt_store_starts(rows)
    disk = (world.root / "lanes.json").read_bytes()
    result = move(world, "claude-9", to, dry_run=dry_run, confirm_v1_edit=True)
    assert leaked(json.dumps(result)) == []
    assert '-    "credential_ref": "HOME"' in result["diff"] and f'+    "credential_ref": "{TOKEN}"' in result["diff"]
    if dry_run:
        assert (world.root / "lanes.json").read_bytes() == disk, "a dry run writes nothing"
        return
    assert json.loads((world.root / "lanes.json").read_text()) == rows
    assert world.store.get_lane("claude-9").owner == LaneOwner(to)


@pytest.mark.parametrize("shape", ["nested-credential", "duplicate-rows", "home-kind-store"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_a_transfer_repairs_every_refused_row_of_its_lane_in_its_own_shape(repair_world, shape, dry_run):
    """The repair writes the store's credential (kind, variable or home, epoch)
    in the row's own shape, to every row the lane has; a home-kind stored lane
    gives its kind and home."""
    world = repair_world
    lane_id = "claude-5" if shape == "home-kind-store" else "claude-9"
    if shape == "home-kind-store":
        world.store.put_lane(home_kind(lane_id))
    row = {**lane_row(lane_id, "env", EIGHT), "owner": "v2"}
    before = {"nested-credential": [nested(row)], "duplicate-rows": [row, {**row, "credential_ref": "HOME"}],
              "home-kind-store": [row]}[shape]
    (world.root / "lanes.json").write_text(json.dumps(before))
    result = move(world, lane_id, "v1", dry_run=dry_run, confirm_v1_edit=True)
    rows = planned(world, lane_id, "v1") if dry_run else json.loads((world.root / "lanes.json").read_text())
    assert result["changed"] and leaked(json.dumps(result)) == []
    assert refused_rows(rows) == [] and rebuilt_store_starts(rows)
    assert len(rows) == len(before)
    lane = world.store.get_lane(lane_id)
    for found in map(lane_reference, rows):
        assert found is None if lane.credential.kind != "env" else found == (lane_id, lane.credential.ref)
    if shape == "nested-credential":
        assert rows[0]["credential"] == {"provider": "claude", "kind": "env", "ref": TOKEN, "epoch": 1}
    if shape == "home-kind-store":
        assert (rows[0]["credential_kind"], rows[0]["credential_ref"]) == ("home", lane.credential.ref)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_a_codex_transfer_repairs_its_refused_row_to_the_relocated_home(repair_world, dry_run):
    """A Codex lane moves its home on `--to v2` (C-10.4); a refused env-kind row
    for it takes the store's kind and the home the transfer leaves it with."""
    world = repair_world
    source = world.home / ".codex-4"
    source.mkdir()
    world.store.put_lane(Lane("codex-4", "codex", "codex:four", Credential("codex", str(source), "home"),
                              str(source), LaneOwner.V1, False))
    (world.root / "lanes.json").write_text(json.dumps([{**lane_row("codex-4", "env", "HOME", "codex"),
                                                        "owner": "v1"}]))
    rows = planned(world, "codex-4", "v2")
    move(world, "codex-4", "v2", dry_run=dry_run, confirm_v1_edit=True)
    if not dry_run:
        rows = json.loads((world.root / "lanes.json").read_text())
    [row] = rows
    home = str(world.root / "lanes" / "codex-4")
    assert (row["credential_kind"], row["credential_ref"], row["home"], row["owner"]) == ("home", home, home, "v2")
    assert refused_rows(rows) == []


def test_a_held_lanes_transfer_is_still_refused_with_its_refused_row(repair_world):
    """The control: when the store's own variable is refused too, nothing is
    repaired; the transfer is refused (`transfer-held`) and writes nothing."""
    world = repair_world
    world.store.put_lane(env_lane("claude-6", EIGHT))
    (world.root / "lanes.json").write_text(json.dumps([lane_row("claude-6", "env", "HOME")]))
    disk = (world.root / "lanes.json").read_bytes()
    for dry_run in (True, False):
        with pytest.raises(lanes_transfer.TransferError) as refused:
            move(world, "claude-6", "v1", dry_run=dry_run, confirm_v1_edit=True)
        assert refused.value.reason == "transfer-held"
    assert (world.root / "lanes.json").read_bytes() == disk


ROW_REFS = REFUSED + GOOD + ("/fake/home",)


@settings(max_examples=60, deadline=None, derandomize=True)
@given(stored=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from((None, *REFUSED, *GOOD))),
                       min_size=1, max_size=3, unique_by=lambda lane: lane[0]),
       rows=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(ROW_REFS),
                               st.sampled_from(["env", "home"]), st.booleans()), max_size=4),
       moves=st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(["v1", "v2"]), st.booleans()),
                      min_size=1, max_size=4))
def test_bounded_property_no_transfer_leaves_a_refused_row(stored, rows, moves):
    """Invariant 3 (review r1, P3-3): after any transfer of lane L, dry run or
    not, no lanes.json row for L names a refused variable as startup reads it,
    and no other lane's row changes; so a file whose other rows were clean ends
    with no refused row at all. A transfer whose stored lane is held is refused
    (`transfer-held`) and writes nothing."""
    with (tempfile.TemporaryDirectory() as directory,
          repair_root(Path(directory), [stored_lane(*lane) for lane in stored]) as world):
        roster = [lane_row(lane_id, kind, ref) for lane_id, ref, kind, _ in rows]
        roster = [nested(row) if shaped else row for row, (*_, shaped) in zip(roster, rows)]
        (world.root / "lanes.json").write_text(json.dumps(roster))
        for lane_id, to, dry_run in moves:
            lane = world.store.get_lane(lane_id)
            before = json.loads((world.root / "lanes.json").read_text())
            disk = (world.root / "lanes.json").read_bytes()
            try:
                result = move(world, lane_id, to, dry_run=dry_run, confirm_v1_edit=True)
            except lanes_transfer.TransferError as error:
                held = lane is not None and lane.credential.kind == "env" and refused_reference(lane.credential.ref)
                assert error.reason == ("transfer-held" if held else "unknown-lane"), (lane_id, error.reason)
                assert (world.root / "lanes.json").read_bytes() == disk, "a refused transfer writes nothing"
                continue
            assert lane is not None and leaked(json.dumps(result)) == []
            after = planned(world, lane_id, to) if dry_run else json.loads((world.root / "lanes.json").read_text())
            if dry_run:
                assert (world.root / "lanes.json").read_bytes() == disk, "a dry run writes nothing"
            assert lane_id not in refused_rows(after), lane_id
            assert [row for row in after if row.get("lane_id") != lane_id] == \
                [row for row in before if row.get("lane_id") != lane_id], "another lane's row changed"
            if not [found for found in refused_rows(before) if found != lane_id]:
                assert refused_rows(after) == []
