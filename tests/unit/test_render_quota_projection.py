"""C-11.9: weekly projections reach both human status formats and status.json."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from subfleet import cli, offline
from subfleet.capacity import build_view, from_store
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import Daemon
from subfleet.render import status
from subfleet.status_json import build_status, write_status
from subfleet.store import Store, WEEKLY_HISTORY_SQL, weekly_history_params

NOW = datetime(2026, 10, 5, 21, 12, tzinfo=timezone.utc)
RESET = "2026-10-08T21:12:00Z"


def quota_fixture(*, with_rate=True):
    lane = {"lane_id": "codex-1", "provider": "codex", "account_key": "codex:fixture@example.org",
            "credential_ref": "/fixtures/codex-1", "owner": "v2", "enabled": True, "desktop": False}
    current = {"lane_id": "codex-1", "scope": "account", "window": "seven_day", "utilization": .34,
               "resets_at": RESET, "label": "provider", "source": "wham", "observed_at": NOW.isoformat()}
    rows = ([{**current, "utilization": .29333333333333333,
              "observed_at": (NOW - timedelta(hours=24)).isoformat()}] if with_rate else []) + [current]
    return build_view([lane], rows, now=NOW)


@pytest.mark.parametrize("with_rate, unused, basis", [(True, 52, "trend"), (False, 66, "rate unknown")])
def test_weekly_projection_in_both_human_status_formats(with_rate, unused, basis):
    view = quota_fixture(with_rate=with_rate)
    before = copy.deepcopy(view)
    table = cli.format_status(view)
    detailed = status(view)
    expected = f"~{unused}% unused at reset Thu 21:12Z (projection"
    assert f"seven_day 34% · {expected}" in table
    assert expected in detailed
    total = "0.5" if with_rate else "0.7"
    assert f"codex: ~{total} of 1 lane-weeks projected unused by Thu 21:12Z" in table
    assert ("rate unknown" in detailed) == (basis == "rate unknown")
    if not with_rate:
        assert "(1 rate unknown)" in table
    assert view == before


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("with_rate", [True, False])
def test_status_json_weekly_projection_shape_and_atomic_publication(tmp_path, provider, with_rate):
    view = quota_fixture(with_rate=with_rate)
    view["lanes"][0]["provider"] = provider
    result = write_status(tmp_path, view)
    rows = result[provider]["homes" if provider == "codex" else "accounts"]
    projection = rows[0]["weekly_projections"]["account"]
    assert projection == {"used": .34, "projected_unused": pytest.approx(.52 if with_rate else .66),
                          "rate_per_hour": pytest.approx(.14 / 72) if with_rate else None,
                          "resets_at": RESET, "basis": "trend" if with_rate else "rate unknown"}
    import json
    assert json.loads((tmp_path / "status.json").read_text()) == result


def test_scope_lane_and_reset_histories_stay_separate():
    view = quota_fixture()
    current = view["readings"][0]
    foreign = [{**current, "utilization": 0, "observed_at": (NOW - timedelta(hours=1)).isoformat(),
                "resets_at": "2026-10-09T21:12:00Z"},
               {**current, "utilization": .99, "lane_id": "codex-2"},
               {**current, "utilization": .99, "scope": "gpt-6-astra"}]
    mixed = build_view([*view["lanes"], {**view["lanes"][0], "lane_id": "codex-2"}],
                       [*view["weekly_samples"], *foreign], now=NOW)
    lane = next(row for row in mixed["lanes"] if row["lane_id"] == "codex-1")
    assert lane["weekly_projections"]["account"] == view["lanes"][0]["weekly_projections"]["account"]
    # A model window is visible but never counted as another lane-week.
    assert "codex: ~0.5 of 2 lane-weeks" in status(mixed)
    assert lane["weekly_projections"]["gpt-6-astra"]["basis"] == "rate unknown"


@pytest.mark.parametrize("change", [{"resets_at": NOW.isoformat()}, {"resets_at": None},
                                    {"label": "admission-observed"}, {"utilization": None}])
def test_absent_or_expired_provider_window_has_no_projection(change):
    view = quota_fixture(with_rate=False)
    current = {**view["readings"][0], **change}
    view = build_view(view["lanes"], [current], now=NOW)
    assert "unused at reset" not in cli.format_status(view)
    assert build_status(view)["codex"]["homes"][0]["weekly_projections"] == {}


def test_json_time_override_drops_expired_projection():
    assert build_status(quota_fixture(), now=RESET)["codex"]["homes"][0]["weekly_projections"] == {}


def test_invalid_latest_usage_cannot_inherit_an_older_projection():
    view = quota_fixture()
    current = {**view["readings"][0], "utilization": None}
    view = build_view(view["lanes"], [view["weekly_samples"][0], current], now=NOW)
    assert "unused at reset" not in cli.format_status(view)
    assert build_status(view)["codex"]["homes"][0]["weekly_projections"] == {}


def test_future_latest_reading_cannot_inherit_an_older_projection():
    view = quota_fixture()
    current = {**view["readings"][0], "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
    view = build_view(view["lanes"], [*view["weekly_samples"], current], now=NOW)
    assert build_status(view)["codex"]["homes"][0]["weekly_projections"] == {}


def test_malformed_reset_does_not_break_json_status():
    view = quota_fixture(with_rate=False)
    view["lanes"][0]["readings"][0]["resets_at"] = "not-a-clock"
    result = build_status(view)
    assert result["codex"]["homes"][0]["weekly_projections"] == {}


def test_snapshot_current_reading_id_keeps_its_reported_utilization():
    view = quota_fixture()
    current = {**view["readings"][0], "reading_id": 2}
    older = {**current, "utilization": .8, "reading_id": 1}
    view = build_view(view["lanes"], [*view["weekly_samples"][:-1], older, current], now=NOW)
    projection = view["lanes"][0]["weekly_projections"]["account"]
    assert projection["used"] == .34
    assert projection["projected_unused"] == pytest.approx(.52)


def test_provider_totals_use_separate_reset_clocks_and_exclude_unmeasured_lanes():
    view = quota_fixture()
    current = view["readings"][0]
    lane = view["lanes"][0]
    rows = [*view["weekly_samples"], {**current, "lane_id": "codex-2", "resets_at": "2026-10-09T08:00:00Z"},
            {**current, "lane_id": "claude-1", "utilization": .5}]
    roster = [lane, {**lane, "lane_id": "codex-2"}, {**lane, "lane_id": "codex-3"},
              {**lane, "lane_id": "claude-1", "provider": "claude"}]
    text = status(build_view(roster, rows, now=NOW))
    assert "codex: ~1.2 of 2 lane-weeks projected unused by Fri 08:00Z (1 rate unknown)" in text
    assert "claude: ~0.5 of 1 lane-weeks projected unused by Thu 21:12Z (1 rate unknown)" in text


def fill_store(store):
    store.add_lane(Lane("codex-1", "codex", "codex:fixture@example.org",
                        Credential("codex", "/fixtures/codex-1", "home"),
                        "/fixtures/codex-1", LaneOwner.V2, False))
    for row in quota_fixture()["weekly_samples"]:
        store.add_reading(Reading(**{key: value for key, value in row.items() if key != "reading_id"}))
    # Out-of-range and non-provider history is not returned by the history query.
    base = quota_fixture()["readings"][0]
    for delta, label in [(25, ReadingLabel.PROVIDER), (-1, ReadingLabel.PROVIDER), (1, ReadingLabel.UNKNOWN)]:
        values = {key: value for key, value in base.items() if key not in {"reading_id", "age_s"}}
        values.update(observed_at=(NOW - timedelta(hours=delta)).isoformat(), label=label)
        store.add_reading(Reading(**values))


def test_store_snapshot_retains_trend_and_history_query_uses_index(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        fill_store(store)
        assert len(store.weekly_projection_samples(now=NOW)) == 2
        # Remove deliberately future/unknown latest evidence for the status snapshot.
        with store.transaction("fixture.cleaned") as conn:
            conn.execute("DELETE FROM readings WHERE label='unknown' OR julianday(observed_at)>julianday(?)", (NOW.isoformat(),))
        view = from_store(store, now=NOW)
        full = build_view(store.lane_rows(), store.list_readings(), store.list_closures(),
                          store.list_attempts(), store.list_jobs(), now=NOW)
        assert view == full
        assert view["lanes"][0]["weekly_projections"]["account"]["projected_unused"] == pytest.approx(.52)
        assert len(view["readings"]) == 1
        steps = store.query("EXPLAIN QUERY PLAN " + WEEKLY_HISTORY_SQL, weekly_history_params(NOW))
        details = [row["detail"] for row in steps]
        assert not any("SCAN" in detail or "TEMP B-TREE" in detail for detail in details), details
        searches = [detail for detail in details if detail.startswith("SEARCH readings ")]
        assert len(searches) == 2, details
        assert all(
            "USING INDEX readings_weekly_history" in detail and ">?" in detail and "<?" in detail
            for detail in searches
        ), details


@pytest.mark.parametrize("existing_store", [False, True])
def test_weekly_history_parsed_time_index_preserves_mixed_timestamps(tmp_path, existing_store):
    path = tmp_path / "state.sqlite3"
    if existing_store:
        with Store(path) as store:
            # A database from before the expression index is installed on reopen.
            store.connection.execute("DROP INDEX readings_weekly_history_parsed")
    start = NOW - timedelta(hours=24)
    offset = timezone(timedelta(hours=-4))
    samples = [
        (start.isoformat(timespec="seconds").replace("+00:00", "Z"), True),
        (NOW.isoformat(timespec="seconds").replace("+00:00", "Z"), True),
        (start.astimezone(offset).isoformat(), True),
        (NOW.astimezone(offset).isoformat(), True),
        (start.isoformat(timespec="microseconds").replace("+00:00", "Z"), True),
        ((NOW - timedelta(milliseconds=250)).isoformat().replace("+00:00", "Z"), True),
        ((start - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), False),
        ((NOW + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), False),
        ((start - timedelta(seconds=1)).astimezone(offset).isoformat(), False),
        ((NOW + timedelta(seconds=1)).astimezone(offset).isoformat(), False),
        ((start - timedelta(milliseconds=250)).isoformat().replace("+00:00", "Z"), False),
        ((NOW + timedelta(milliseconds=250)).isoformat().replace("+00:00", "Z"), False),
        ("not-a-clock", False),
    ]
    with Store(path) as store:
        lane = quota_fixture()["lanes"][0]
        store.add_lane(Lane(lane["lane_id"], lane["provider"], lane["account_key"],
                            Credential("codex", "/fixtures/codex-1", "home"),
                            "/fixtures/codex-1", LaneOwner.V2, False))
        base = {key: value for key, value in quota_fixture()["readings"][0].items()
                if key not in {"reading_id", "age_s"}}
        expected_ids = set()
        for index, (observed_at, included) in enumerate(samples):
            values = {**base, "observed_at": observed_at,
                      "label": "stale-provider" if index % 2 else "provider"}
            reading_id = store.add_reading(Reading(**values))
            if included:
                expected_ids.add(reading_id)
        for change in ({"label": "unknown"}, {"window": "five_hour"}):
            store.add_reading(Reading(**{**base, **change}))
        expected = sorted((row for row in store.list_readings() if row["reading_id"] in expected_ids),
                          key=lambda row: row["reading_id"])
        # Compare complete rows: the UNION neither loses nor duplicates samples.
        assert sorted(store.weekly_projection_samples(now=NOW), key=lambda row: row["reading_id"]) == expected
        details = [row["detail"] for row in store.query(
            "EXPLAIN QUERY PLAN " + WEEKLY_HISTORY_SQL, weekly_history_params(NOW))]
        assert not any("SCAN" in detail or "TEMP B-TREE" in detail for detail in details), details


def test_snapshot_weekly_history_is_bounded_and_sorted_independent_of_input_order():
    view = quota_fixture()
    current = view["readings"][0]
    outside = [{**current, "window": "five_hour"}, {**current, "label": "unknown"},
               {**current, "observed_at": (NOW - timedelta(hours=25)).isoformat()}]
    rows = [*view["weekly_samples"], *outside]
    first = build_view(view["lanes"], rows, now=NOW)
    second = build_view(view["lanes"], list(reversed(rows)), now=NOW)
    assert first["weekly_samples"] == second["weekly_samples"] == view["weekly_samples"]


def test_offline_status_reads_the_same_history_without_changing_store(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(offline, "datetime", FrozenDateTime)
    with Store(tmp_path / "state.sqlite3") as store:
        for lane in quota_fixture()["lanes"]:
            store.add_lane(Lane(lane["lane_id"], lane["provider"], lane["account_key"],
                                Credential("codex", "/fixtures/codex-1", "home"),
                                "/fixtures/codex-1", LaneOwner.V2, False))
        for row in quota_fixture()["weekly_samples"]:
            store.add_reading(Reading(**row))
        count = len(store.list_readings())
        view = offline.Offline(tmp_path).status()
        assert view["lanes"][0]["weekly_projections"]["account"]["projected_unused"] == pytest.approx(.52)
        assert "seven_day 34% · ~52% unused at reset Thu 21:12Z (projection)" in cli.format_status(view)
        assert len(store.list_readings()) == count


def test_daemon_status_reads_history_but_route_snapshot_does_not(tmp_path, monkeypatch):
    with Store(tmp_path / "state.sqlite3") as store:
        fill_store(store)
        history = store.weekly_projection_samples
        monkeypatch.setattr(store, "weekly_projection_samples", lambda: history(now=NOW))
        daemon = object.__new__(Daemon)
        daemon.store = store
        daemon.timers = SimpleNamespace(view_rows=lambda lanes: {})
        assert len(daemon._capacity_rows()["view"]["weekly_samples"]) == 2
        assert "weekly_samples" not in daemon._capacity_rows(route=True)["view"]
