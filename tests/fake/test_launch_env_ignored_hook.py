"""Review r1 of #248 (D-ENV2), P3-1, at the daemon: an ignored lanes.json row's
harness hook is withheld from every launch, as a held lane's is.

The release before D-ENV2 refused such a row at startup (exit 2), so no launch
ran beside one. D-ENV2 lets the daemon start and ignores the row, but the daemon
fed `credentials.withheld_references` the store's lanes alone, at both of its
withheld sets (its own `ProviderEnvironment` and the timers'): a variable only
an ignored row names reached every launch. Every value here is a dummy, and
failures name variables, never values.
"""

import json
from pathlib import Path
import tempfile

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store
from tests.fake.test_launch_env_daemon import (
    TOKEN, cli_start, daemon_fixture, launch, lane_launch_environment, launchd_start, naming, nine_root, nine_row,
    roster, store_as_round_four_left_it, stored, write_roster,
)

# Review r1's hook: no credential word, so every launch inherits it unless withheld.
EIGHT = "SUBFLEET_FAKE_LANE_EIGHT"


def carrying(env: dict, names, values=()) -> list[str]:
    """Which of `names` a launch environment carries, and which of its names hold
    one of `values`: names only, so a failure shows no value."""
    return sorted({name for name in names if name in env} | {name for name, value in env.items() if value in values})


def withheld_sets(service) -> tuple[frozenset, frozenset]:
    """The daemon's two withheld sets: its launches', and the timers' own launches'."""
    return service._provider_environment.withheld(), service.timers._provider_environment().withheld()


@pytest.mark.parametrize("case", ["ignored-row", "held-on-the-hook"])
def test_review_r1_an_ignored_rows_harness_hook_reaches_no_launch(tmp_path, monkeypatch, case):
    """Review r1, P3-1: claude-9 stored and healthy on SUBFLEET_TEST_OAUTH, its
    lanes.json row naming SUBFLEET_FAKE_LANE_EIGHT, set to a dummy. Both starts
    went on with one warning, and the dummy then reached codex-1's launch and
    claude-9's own. Now both withheld sets name the hook, and neither launch
    carries it; claude-9 still launches on its own token. The control: claude-9
    held on the hook itself, whose hook was withheld before this round too."""
    root, harness = nine_root(tmp_path, monkeypatch, TOKEN)
    monkeypatch.setenv(EIGHT, "dummy-eight")
    if case == "held-on-the-hook":
        write_roster(root, [nine_row(EIGHT) if row["lane_id"] == "claude-9" else row for row in roster(root)])
        store_as_round_four_left_it(root)
    else:
        store_as_round_four_left_it(root)
        write_roster(root, [nine_row(EIGHT) if row["lane_id"] == "claude-9" else row for row in roster(root)])
    assert stored(root)["claude-9"] == ((EIGHT if case == "held-on-the-hook" else TOKEN), 1)
    code, err, seen = cli_start(root, monkeypatch)
    assert code == 0, err
    [line] = naming(err)
    assert EIGHT in line and "the row is ignored" in line
    assert ("the lane is held" in line) == (case == "held-on-the-hook")
    assert "dummy-eight" not in err

    def check(service):
        for withheld in withheld_sets(service):
            assert EIGHT in withheld, "a withheld set misses the hook"
        _, spawns = launch(service, harness, monkeypatch)
        assert len(spawns) == 1, "codex-1 reached its guardian"
        assert carrying(spawns[0][1], {EIGHT, TOKEN}, {"dummy-eight", "dummy-nine"}) == [], "codex-1's launch"
        if case == "ignored-row":
            own = lane_launch_environment(service, harness, "claude-9")
            assert own["CLAUDE_CODE_OAUTH_TOKEN"] == "dummy-nine", "claude-9 launches on its own token"
            assert carrying(own, {EIGHT, TOKEN}, {"dummy-eight"}) == [], "claude-9's own launch"
        return "served"

    code, err, served = launchd_start(root, monkeypatch, check)
    assert (code, served) == (0, ["served"]), err
    shown = "dummy-eight" in (root / "daemon.log").read_text() + err
    assert not shown, "daemon.log or launchd's stderr shows the hook's value"


# --- invariant 1, at the daemon -------------------------------------------------------------

HOOKS = (EIGHT, "SUBFLEET_FAKE_LANE_NINE")
REFUSED = HOOKS + ("HOME",)
GOOD = (TOKEN, "E2E_LANE_TWO_TOKEN")
LANE_IDS = ("claude-7", "claude-8", "claude-9")


def expected_withheld(store_lanes, roster_lanes):
    """The model, from the rule as review r1's fix states it, not from the code:
    each lane's variable after seeding (a new lane from its first row; a held
    lane repaired by the first row for it naming a good variable), when it is
    good or a hook; and each hook a refused row for a stored lane names."""
    refused, hooks = set(REFUSED).__contains__, set(HOOKS).__contains__
    effective = {}
    for lane_id, ref in store_lanes:
        if refused(ref):
            ref = next((row for row_id, row in roster_lanes if row_id == lane_id and not refused(row)), ref)
        effective[lane_id] = ref
    for lane_id, ref in roster_lanes:
        effective.setdefault(lane_id, ref)
    ids = {lane_id for lane_id, _ in store_lanes}
    return ({ref for ref in effective.values() if not refused(ref) or hooks(ref)}
            | {ref for lane_id, ref in roster_lanes if hooks(ref) and lane_id in ids})


@pytest.fixture
def withheld_property(tmp_path, monkeypatch):
    daemon_fixture(tmp_path, monkeypatch)
    for name in HOOKS + GOOD:
        monkeypatch.setenv(name, "dummy-" + name.lower())
    policy = json.loads((Path(daemon_module.__file__).with_name("default_policy.json")).read_text())
    policy.setdefault("reserve", {})["models"] = []
    policy.setdefault("admission", {})["prove_idle_s"] = None
    policy.setdefault("conversations", {})["catalog_interval_s"] = 0
    return tmp_path, policy


STORE_LANES = st.lists(st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(REFUSED + GOOD)),
                       min_size=1, max_size=3, unique_by=lambda lane: lane[0])


@settings(max_examples=25, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(data=st.data(), store_lanes=STORE_LANES)
def test_bounded_property_the_daemon_withholds_every_lanes_hook(withheld_property, data, store_lanes):
    """Invariant 1 (review r1, P3-1), at a real daemon, for every store and every
    lanes.json the daemon starts with (a refused row only for a stored lane):
    both withheld sets are exactly the model's, and a launch selected from the
    daemon's environment carries no hook any lane names, stored or by an ignored
    row, while a hook no lane names still reaches it."""
    ids = [lane_id for lane_id, _ in store_lanes]
    roster_lanes = data.draw(st.lists(st.one_of(
        st.tuples(st.sampled_from(ids), st.sampled_from(REFUSED + GOOD)),
        st.tuples(st.sampled_from(LANE_IDS), st.sampled_from(GOOD))), max_size=4), label="roster_lanes")
    base, policy = withheld_property
    root = Path(tempfile.mkdtemp(dir=base))
    (root / "policy.json").write_text(json.dumps(policy))
    write_roster(root, [{**nine_row(ref), "lane_id": lane_id, "account_key": "claude:" + lane_id}
                        for lane_id, ref in roster_lanes])
    with Store(root / "state.sqlite3") as store:
        for lane_id, ref in store_lanes:
            store.put_lane(Lane(lane_id, "claude", "claude:" + lane_id, Credential("claude", ref, "env"), None,
                                LaneOwner.V2, False))
    expected = expected_withheld(store_lanes, roster_lanes)
    service = daemon_module.Daemon(root)
    try:
        own, timers = withheld_sets(service)
        assert own == timers == expected
        launched = service._provider_environment.assemble()
        assert carrying(launched, set(HOOKS) | set(GOOD)) == sorted(set(HOOKS) - expected)
        assert "HOME" in launched
    finally:
        service.close()
