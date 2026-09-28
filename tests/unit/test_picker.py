"""Native picker contracts: conservative recommendation, no launch side effects."""

import copy
import json

import pytest

from subfleet import cli, compat, picker
from subfleet.capacity import build_view
from subfleet.client import DaemonUnavailable
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from tests.caps import capped
from tests.fable_reserve import load_fable_reserve_policy


NOW = "2026-09-20T16:00:00Z"
LATER = "2026-09-21T16:00:00Z"


@pytest.fixture
def policy():
    """The count caps of before 2026-09-27 (`tests/caps.py`): these cases rank
    lanes under them, and a policy may still set them."""
    return capped(load_policy(DEFAULT_POLICY_PATH))


@pytest.fixture
def reserve_policy():
    """Fable still reserved (C-11.7), as shipped until its retirement on 2026-09-27."""
    return load_fable_reserve_policy()


def lane(name="codex-1", **kw):
    provider = name.split("-")[0]
    return {"lane_id": name, "provider": provider, "home": "/lanes/" + name,
            "account_key": provider + ":" + name, "label": name + "@example.org",
            "owner": "v2", "enabled": True, "desktop": False, **kw}


def reading(name="codex-1", used=.2, **kw):
    return {"lane_id": name, "scope": "account", "window": "seven_day",
            "utilization": used, "observed_at": NOW, "resets_at": LATER,
            "label": "provider", "source": "fixture", **kw}


def view(lanes=None, readings=None, **kw):
    return build_view(lanes or [lane()], readings if readings is not None else [reading()], now=NOW, **kw)


def test_codex_uses_weekly_reset_order_and_preserves_input(policy):
    """C-11.5: fresh distinct subscription accounts drain by weekly reset."""
    data = view([lane(), lane("codex-2")], [reading(), reading("codex-2", .8,
                resets_at="2026-09-20T20:00:00Z")])
    saved = copy.deepcopy((policy, data))
    result = picker.rank(policy, data)
    assert result["best"] == "/lanes/codex-2"
    assert [row["lane_id"] for row in result["ranked"]] == ["codex-2", "codex-1"]
    assert result["ranked"][0]["weekly_used_percent"] == 80
    assert result["ranked"][0]["five_hour_used_percent"] is None
    assert (policy, data) == saved
    assert result["advisory"] is True


@pytest.mark.parametrize("changes,reason", [
    ({"owner": "v1"}, "owner-v1"), ({"enabled": False}, "disabled"),
    ({"desktop": True}, "desktop"), ({"identity_status": "mismatch"}, "identity-mismatch"),
    ({"home": None}, "missing-home"), ({"home": "relative"}, "missing-home"),
])
def test_picker_preserves_lane_guards(policy, changes, reason):
    """C-10: the compatibility surface cannot bypass the scheduler's guards."""
    result = picker.rank(policy, view([lane(**changes)]))
    assert result["best"] is None
    assert reason in result["excluded"][0]["reasons"]


@pytest.mark.parametrize("readings", [[], [reading(label="admission-observed", utilization=None)],
    [reading(observed_at="2026-09-20T15:57:59Z")]])
def test_picker_requires_fresh_provider_evidence_without_probing(policy, readings):
    """C-9/C-11.4: a raw picker cannot satisfy a missing admission probe."""
    result = picker.rank(policy, view(readings=readings))
    assert result["best"] is None
    assert "fresh-usage-required" in result["excluded"][0]["reasons"]


def test_policy_ttl_and_headroom_floor_are_not_overridden(policy):
    """C-6.4/C-11: explicit picker knobs can tighten but not weaken policy."""
    policy["caps"]["reading_ttl_s"] = 20
    assert picker.rank(policy, view(readings=[reading(observed_at="2026-09-20T15:59:40Z")]))["best"]
    assert not picker.rank(policy, view(readings=[reading(observed_at="2026-09-20T15:59:39Z")]))["best"]
    assert not picker.rank(policy, view(readings=[reading(used=.96)]), min_headroom=0)["best"]
    assert not picker.rank(policy, view(readings=[reading(used=.8)]), min_headroom=25)["best"]


def test_live_and_quarantined_leases_block_raw_picker(policy):
    """C-5/C-6: a raw caller cannot share a lane with a retained writer."""
    data = view()
    data["lane_leases"] = [{"lease_key": "lane:codex-1:slot:0", "holder": "quarantined/a1"}]
    assert picker.rank(policy, data)["best"] is None
    data.pop("lane_leases")
    data["in_flight"] = {"codex-1": 1}
    assert picker.rank(policy, data)["best"] is None


def test_fleet_cap_and_probe_reservations_apply(policy):
    """C-6.4: even a free lane cannot evade active fleet reservations."""
    data = view()
    data["reserved_probes"] = policy["caps"]["max_active_attempts"]
    assert picker.rank(policy, data)["best"] is None
    data["reserved_probes"] = 0
    data["unavailable_lanes"] = {"codex-1": "probe:held"}
    assert picker.rank(policy, data)["best"] is None


def test_exact_model_keeps_unrelated_closure_out_but_unknown_model_is_conservative(policy):
    """C-11.2: exact scope is honored; unknown caller model cannot evade a closure."""
    closed = {"lane_id": "codex-1", "scope": policy["models"]["terra"]["id"],
              "until_at": LATER, "released_at": None}
    data = view(closures=[closed])
    assert picker.rank(policy, data, model="astra")["best"]
    assert picker.rank(policy, data, model="terra")["best"] is None
    assert picker.rank(policy, data)["best"] is None


def test_claude_email_exact_model_reserve_and_exclusion(reserve_policy):
    """C-11.7: no operator reserve authorization is inherited by a picker."""
    policy = reserve_policy
    data = view([lane("claude-1")], [reading("claude-1")])
    assert picker.rank(policy, data, family="claude", model="fable")["best"] == "claude-1@example.org"
    assert picker.rank(policy, data, family="claude", model="opus")["best"] is None
    assert picker.rank(policy, data, family="claude")["best"] is None
    assert picker.rank(policy, data, family="claude", model="fable",
                       exclusions=["claude-1@example.org"])["best"] is None


def test_shipped_policy_picks_opus_for_a_retired_fable_pick(policy, capsys):
    """C-11.1: with Fable retired and nothing reserved, `pick --model fable` ranks Opus."""
    data = view([lane("claude-1")], [reading("claude-1")])
    assert picker.rank(policy, data, family="claude", model="fable")["best"] == "claude-1@example.org"
    assert picker.rank(policy, data, family="claude", model="opus")["best"] == "claude-1@example.org"
    assert "retired model 'fable' resolves to 'opus'" in capsys.readouterr().err


def test_reserve_slack_requiring_probe_is_not_a_raw_pick(reserve_policy):
    """C-11.4: scoped closure evidence may require supervised admission."""
    policy = reserve_policy
    data = view([lane("claude-1")], [reading("claude-1")], closures=[{
        "lane_id": "claude-1", "scope": policy["models"]["fable"]["id"],
        "reason": "provider-limit", "clock_source": "reported", "until_at": LATER,
    }])
    result = picker.rank(policy, data, family="claude", model="opus")
    assert result["best"] is None
    assert "admission-probe-required" in result["excluded"][0]["reasons"]


def test_matched_stream_windows_can_measure_real_opus_reserve_slack(reserve_policy):
    """C-11.7: same-event shared/Fable measurements need no unknown-quota waiver."""
    policy = reserve_policy
    data = view([lane("claude-1")], [
        reading("claude-1", .2, source="rate_limit_event", attempt_id="old/a1"),
        reading("claude-1", .95, source="rate_limit_event", attempt_id="old/a1",
                scope=policy["models"]["fable"]["id"]),
    ])
    result = picker.rank(policy, data, family="claude", model="opus")
    assert result["best"] == "claude-1@example.org"
    assert result["ranked"][0]["weekly_used_percent"] == 20


def test_aliases_and_duplicate_accounts(policy):
    """C-1.4/C-11.2: repeated homes cannot create another account's capacity."""
    data = view([lane(), lane("codex-2", account_key="codex:codex-1")],
                [reading(), reading("codex-2")])
    result = picker.rank(policy, data, model="gpt-6-astra")
    assert len(result["ranked"]) == 1
    assert result["excluded"][0]["verdict"] == "duplicate-account"


def test_case_insensitive_claude_email_aliases_are_ambiguous(policy):
    """C-1.4: the email-only shell contract cannot distinguish case aliases."""
    data = view([lane("claude-1", label="Person@example.org"),
                 lane("claude-2", label="person@EXAMPLE.org")],
                [reading("claude-1"), reading("claude-2")])
    result = picker.rank(policy, data, family="claude", model="fable")
    assert result["best"] is None
    assert all("ambiguous-email" in row["reasons"] for row in result["excluded"])


@pytest.mark.parametrize("kwargs", [{"family": "other"}, {"model": ""}, {"model": "opus"},
    {"min_headroom": -1}, {"min_headroom": 101}, {"min_headroom": float("nan")},
    {"min_headroom": True}, {"exclusions": "codex-1"}])
def test_bad_picker_arguments_refuse(policy, kwargs):
    """C-16: malformed direct picker requests cannot relax admission."""
    with pytest.raises(ValueError):
        picker.rank(policy, view(), **kwargs)


def test_cli_preserves_path_json_and_no_candidate_exit(policy, monkeypatch, capsys):
    """Plan amendment 1: the PATH shim gets one path and rc0, or rc1."""
    result = picker.rank(policy, view([lane(), lane("codex-2")], [reading(), reading("codex-2")]))
    calls = []
    class Client:
        def call(self, op, args):
            calls.append((op, args))
            return result
    monkeypatch.setattr(cli, "_client", lambda args: Client())
    assert compat.dispatch(["pick", "codex", "--cached"], env={}) == 0
    assert capsys.readouterr().out == "/lanes/codex-1\n"
    assert calls == [("pick", {"family": "codex", "model": None, "exclusions": [], "min_headroom": None})]
    assert cli.main(["pick", "--json"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert len(json.loads(captured.out)["ranked"]) == 1
    assert cli.main(["pick", "--json", "--all"]) == 0
    assert len(json.loads(capsys.readouterr().out)["ranked"]) == 2
    result.update(best=None, ranked=[])
    assert cli.main(["pick", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["best"] is None


def test_cli_never_falls_back_to_v1_when_daemon_is_down(monkeypatch, capsys):
    """C-17.5: picker unavailability cannot dispatch through a different owner."""
    class Client:
        def call(self, *args):
            raise DaemonUnavailable("not running")
    monkeypatch.setattr(cli, "_client", lambda args: Client())
    monkeypatch.setattr(compat, "v1_binary", lambda: pytest.fail("v1 fallback"))
    assert compat.dispatch(["pick", "codex"], env={}) == 69
    assert not capsys.readouterr().out


@pytest.mark.parametrize("raw", [{"OPENAI_API_KEY": "fake-secret"}, {"CODEX_API_KEY": "fake-secret"},
    {"auth_mode": "api_key"}, {"auth_mode": "API-KEY"},
    {"tokens": {"account_id": "subscription"}, "OPENAI_API_KEY": "fake-secret"}])
def test_native_api_lane_check_refuses_all_known_key_forms(tmp_path, monkeypatch, capsys, raw):
    """C-10.2: explicit/mixed API homes refuse even with the v1 override."""
    (tmp_path / "auth.json").write_text(json.dumps(raw))
    monkeypatch.setenv("SUBFLEET_ALLOW_API_LANE", "1")
    monkeypatch.setattr(compat, "v1_binary", lambda: pytest.fail("v1 fallback"))
    assert compat.dispatch(["_api-lane-check", str(tmp_path)], env={}) == 7
    captured = capsys.readouterr()
    assert not captured.out and "legacy" in captured.err
    assert "fake-secret" not in captured.err


def test_native_api_lane_check_is_silent_for_subscription_and_unknown(tmp_path, capsys):
    """Plan amendment 1: API classification is not a provider login probe."""
    assert cli.main(["_api-lane-check", str(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"account_id": "example"}}))
    assert cli.main(["_api-lane-check", str(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("retired,successor,family", [("fable", "opus", "claude"), ("sol", "astra", "codex")])
def test_cli_pick_model_asks_the_daemon_about_the_successor(monkeypatch, capsys, retired, successor, family):
    """C-17.2: `pick --model fable` is a person naming a model, so it is remapped like
    `-m`, even when the daemon's policy still lists Fable."""
    calls = []

    class Client:
        def call(self, op, args):
            calls.append(args["model"])
            return {"best": None, "ranked": [], "excluded": []}
    monkeypatch.setattr(cli, "_client", lambda args: Client())
    assert cli.main(["pick", family, "--model", retired, "--json"]) == 1
    assert calls == [successor]
    assert f"pick: --model {retired} is retired; using {successor}" in capsys.readouterr().err
