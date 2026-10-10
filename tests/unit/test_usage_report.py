"""C-17.8: observed attempt totals, windows, native groups and read-only backfill."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, compat, protocol
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store
from subfleet.usage_report import FIELDS, aggregate_usage, build_report, parse_since

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
STAMP = "2026-10-03T11:00:00Z"


def _usage(prompt=100, cache_read=80, cache_write=None, output=10, **extra):
    return {"provider": "codex", "raw": {}, "normalized": {
        "prompt": prompt, "cache_read": cache_read, "cache_write": cache_write, "output": output,
        "cache_ttl": None, "cache_hit_share": cache_read / prompt if prompt and cache_read is not None else None},
            **extra}


@pytest.fixture
def usage_store(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for provider in ("codex", "claude"):
        store.put_lane(Lane(f"{provider}-1", provider, f"{provider}:test",
                            Credential(provider, "fixture", "home"), str(tmp_path), LaneOwner.V2, False))
    yield store
    store.close()


def _seed(store, root, job_id, *, kind="dispatch", provider="codex", usage=None,
          session="native-1", at=STAMP, conversation=None):
    store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind=kind, state="succeeded",
                  workdir=str(root), prompt_path=str(root / "prompt"), sandbox="read-only", created_at=at)
    attempt_id = f"{job_id}/a1"
    store.add_attempt(attempt_id=attempt_id, job_id=job_id, seq=1, lane_id=f"{provider}-1",
                      model_requested="fixture", state="succeeded", native_session_id=session,
                      reserved_at=at, finished_at=at,
                      evidence_json=json.dumps({"classification": "ok", **({"usage": usage} if usage else {})}))
    if conversation:
        jdir = root / "jobs" / job_id
        jdir.mkdir(parents=True)
        (jdir / "manifest.json").write_text(json.dumps({"turn": {"conversation_id": conversation}}))
    return attempt_id


@pytest.mark.parametrize("since,expected", [
    ("24h", NOW - timedelta(days=1)), ("7d", NOW - timedelta(days=7)),
    ("30m", NOW - timedelta(minutes=30)), ("2026-10-02T07:00:00-04:00", datetime(2026, 10, 2, 11, tzinfo=UTC)),
    ("2026-10-02T11:00:00Z", datetime(2026, 10, 2, 11, tzinfo=UTC)),
])
def test_c17_8_since_accepts_durations_and_iso(since, expected):
    """C-17.8: duration and ISO windows resolve at one report clock."""
    assert parse_since(since, now=NOW) == expected


@pytest.mark.parametrize("value", ["", "0h", "-1d", "lots", "3months", None, "9" * 500 + "d"])
def test_c17_8_bad_since_is_invalid_input(value):
    """C-17.8: an invalid window is exit 2 instead of silently all history."""
    with pytest.raises(protocol.ProtocolError):
        parse_since(value, now=NOW)


def test_c17_8_coverage_distinguishes_missing_zero_and_cumulative():
    """C-17.8: absent fields are null and cumulative totals do not enter attempt sums."""
    rows = [{"usage": _usage(cache_write=0)}, {},
            {"usage": _usage(prompt=999, cache_read=999, cumulative_thread=True)}]
    totals = aggregate_usage(rows)
    assert totals["attempts"] == 3
    assert totals["attempts_with_usage"] == 2
    assert totals["attempts_without_usage"] == 1
    assert totals["cumulative_thread_attempts"] == 1
    assert totals["prompt"] == 100 and totals["cache_read"] == 80
    assert totals["cache_write"] == 0 and totals["cache_hit_share"] == .8
    assert totals["field_attempts"] == dict.fromkeys(FIELDS, 1)
    missing = aggregate_usage([{}, {"usage": _usage(cumulative_thread=True)}])
    assert all(missing[field] is None for field in (*FIELDS, "cache_hit_share"))
    assert all(aggregate_usage([])[field] is None for field in FIELDS)


def test_c17_8_partial_pairs_have_no_group_share():
    """C-17.8: a partially reported prompt/read pair cannot imply a whole-group share."""
    totals = aggregate_usage([{"usage": _usage()}, {"usage": _usage(prompt=None, cache_read=200)}])
    assert totals["prompt"] == 100 and totals["cache_read"] == 280
    assert totals["cache_hit_share"] is None


@st.composite
def _attempt(draw):
    prompt = draw(st.integers(min_value=0, max_value=1_000_000))
    read = draw(st.integers(min_value=0, max_value=prompt))
    write = draw(st.integers(min_value=0, max_value=prompt))
    output = draw(st.integers(min_value=0, max_value=1_000_000))
    present = draw(st.integers(min_value=0, max_value=15))
    values = [value if present & (1 << index) else None for index, value in enumerate((prompt, read, write, output))]
    usage = _usage(*values, cumulative_thread=draw(st.booleans())) if draw(st.booleans()) else None
    return {"usage": usage, "group": draw(st.sampled_from(["a", "b"])), "backfilled": draw(st.booleans())}


@given(st.lists(_attempt(), max_size=50))
@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_c17_8_report_sums_equal_per_attempt_rows_and_share_is_bounded(attempts):
    """C-17.8: deterministic report sums are exactly reported per-attempt values; share is [0,1] or null."""
    totals = aggregate_usage(attempts)
    assert totals == aggregate_usage(reversed(attempts))
    for field in FIELDS:
        reported = [row["usage"]["normalized"][field] for row in attempts
                    if row["usage"] and not row["usage"].get("cumulative_thread")
                    and row["usage"]["normalized"][field] is not None]
        assert totals[field] == (sum(reported) if reported else None)
        grouped = [aggregate_usage(row for row in attempts if row["group"] == group)[field] for group in ("a", "b")]
        known = [value for value in grouped if value is not None]
        assert totals[field] == (sum(known) if known else None)
    share = totals["cache_hit_share"]
    assert share is None or 0 <= share <= 1
    assert totals["attempts"] == totals["attempts_with_usage"] + totals["attempts_without_usage"]


def test_c17_8_lane_window_filters_clock_and_tracks_old_attempts(usage_store, tmp_path):
    """C-17.8: reports include only the chosen window and retain pre-measurement missing attempts."""
    _seed(usage_store, tmp_path, "measured", usage=_usage())
    _seed(usage_store, tmp_path, "missing")
    _seed(usage_store, tmp_path, "old", at="2026-09-01T00:00:00Z", usage=_usage())
    _seed(usage_store, tmp_path, "future", at="2026-10-04T00:00:00Z", usage=_usage())
    report = build_report(usage_store, tmp_path, now=NOW)
    assert len(report["rows"]) == 1
    assert report["rows"][0]["group"] == "codex-1"
    assert report["totals"]["attempts"] == 2
    assert report["totals"]["attempts_without_usage"] == 1
    assert report["totals"]["prompt"] == 100
    assert {row["job_id"] for row in report["attempt_rows"]} == {"measured", "missing"}


def test_c17_8_native_resume_sessions_and_turn_conversations_group_together(usage_store, tmp_path):
    """C-17.8: native ids group resume chains; conversation ids group only turn jobs."""
    _seed(usage_store, tmp_path, "dispatch", usage=_usage(), session="shared")
    _seed(usage_store, tmp_path, "resume", kind="resume", usage=_usage(cumulative_thread=True), session="shared")
    _seed(usage_store, tmp_path, "turn-one", kind="turn", provider="claude", usage=_usage(),
          session="claude-session", conversation="cv-1")
    _seed(usage_store, tmp_path, "turn-two", kind="turn", provider="claude", usage=_usage(),
          session="claude-session", conversation="cv-1")
    _seed(usage_store, tmp_path, "unbound", session=None)
    sessions = build_report(usage_store, tmp_path, by="session", now=NOW)
    resumed = next(row for row in sessions["rows"] if row["group"] == "codex:shared")
    assert resumed["attempts"] == 2 and resumed["prompt"] == 100 and resumed["cumulative_thread_attempts"] == 1
    assert len(sessions["rows"]) == 3
    conversations = build_report(usage_store, tmp_path, by="conversation", now=NOW)
    assert len(conversations["rows"]) == 1
    assert conversations["rows"][0]["group"] == "cv-1"
    assert conversations["totals"]["attempts"] == 2 and conversations["totals"]["prompt"] == 200


def test_c17_8_backfill_reads_retained_artifacts_and_does_not_write(usage_store, tmp_path):
    """C-17.8: artifact backfill uses the provider parser and labels rows without changing evidence or store."""
    aid = _seed(usage_store, tmp_path, "legacy")
    path = tmp_path / "retained-stream"
    data = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80,
                                                          "output_tokens": 10}}).encode()
    path.write_bytes(data + b"\n")
    usage_store.add_artifact(aid, "raw-stream", str(path), hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    before = usage_store.connection.total_changes
    evidence = usage_store.get_attempt(aid)["evidence_json"]
    assert build_report(usage_store, tmp_path, now=NOW)["totals"]["prompt"] is None
    report = build_report(usage_store, tmp_path, backfill=True, now=NOW)
    assert report["totals"]["prompt"] == 100 and report["totals"]["cache_read"] == 80
    assert report["rows"][0]["backfilled"] is True
    assert report["attempt_rows"][0]["source"] == "backfilled"
    assert report["totals"]["cache_write"] is None
    assert usage_store.connection.total_changes == before
    assert usage_store.get_attempt(aid)["evidence_json"] == evidence


def test_c17_8_backfill_ignores_fifo_and_symlink(usage_store, tmp_path):
    """C-17.8: retained-state reads cannot block on a FIFO or follow a substituted symlink."""
    import os
    aid = _seed(usage_store, tmp_path, "legacy")
    adir = tmp_path / "jobs" / "legacy" / "a1"
    adir.mkdir(parents=True)
    os.mkfifo(adir / "stream.jsonl")
    target = tmp_path / "not-an-artifact"
    target.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12}}))
    (adir / "stdout").symlink_to(target)
    report = build_report(usage_store, tmp_path, backfill=True, now=NOW)
    assert report["attempt_rows"][0]["attempt_id"] == aid
    assert report["totals"]["attempts_without_usage"] == 1


@pytest.mark.parametrize("by,backfill", [("wrong", False), ([], False), (None, False), ("lane", "yes")])
def test_c17_8_daemon_arguments_are_validated(usage_store, tmp_path, by, backfill):
    """C-17.8: arbitrary socket callers cannot silently request an unsupported grouping or backfill flag."""
    with pytest.raises(protocol.ProtocolError):
        build_report(usage_store, tmp_path, by=by, backfill=backfill, now=NOW)


def test_c17_8_cli_reaches_read_only_op_and_emits_complete_json(daemon, capsys):
    """C-17.8/C-17.4: front-door usage flags reach one read-only op and JSON has no prose."""
    report = {"rows": [], "totals": aggregate_usage([]), "by": "session", "attempt_rows": []}
    server = daemon({"usage": lambda request: report})
    argv = ["usage", "--since", "7d", "--by", "session", "--backfill", "--json"]
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "map" and mapping.argv == argv and not mapping.notes
    assert cli.main(argv) == 0
    assert server.args("usage") == {"since": "7d", "by": "session", "backfill": True}
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == report


def test_c17_8_cli_human_labels_backfill_unknowns_and_cumulative(daemon, capsys):
    """C-17.8: human output identifies recovered evidence and never presents cumulative totals as attempt totals."""
    totals = aggregate_usage([{"usage": _usage(cumulative_thread=True), "backfilled": True}])
    daemon({"usage": lambda request: {"rows": [{"group": "codex-1", **totals}], "totals": totals}})
    assert cli.main(["usage"]) == 0
    captured = capsys.readouterr()
    assert "backfilled" in captured.out and "cumulative thread total(s) excluded" in captured.out
    assert "?" in captured.out and "999" not in captured.out


def test_c17_8_cli_rejects_bad_window_before_daemon(daemon, capsys):
    """C-17.8: invalid windows return exit 2 without issuing a daemon request."""
    server = daemon({"usage": lambda request: {}})
    assert cli.main(["usage", "--since", "nonsense"]) == 2
    assert server.requests == []
    assert "since" in capsys.readouterr().err


@pytest.mark.parametrize("provider,events", [
    ("claude", [{"type": "result", "usage": {"input_tokens": 10, "cache_read_input_tokens": 80,
                                                "cache_creation_input_tokens": 10, "output_tokens": 10}}]),
    ("codex", [{"method": "turn/started", "params": {"turn": {"id": "turn-1"}}},
               {"method": "thread/tokenUsage/updated", "params": {"turnId": "turn-1", "tokenUsage": {
                   "total": {"inputTokens": 50, "cachedInputTokens": 40, "outputTokens": 5},
                   "last": {"inputTokens": 50, "cachedInputTokens": 40, "outputTokens": 5}}}},
               {"method": "thread/tokenUsage/updated", "params": {"turnId": "turn-1", "tokenUsage": {
                   "total": {"inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 10},
                   "last": {"inputTokens": 50, "cachedInputTokens": 40, "outputTokens": 5}}}}]),
])
def test_c17_8_backfill_detects_claude_and_app_server_turn_streams(usage_store, tmp_path, provider, events):
    """C-17.8/C-12.10: retained Claude and app-server streams use the same normalized parser as finalization."""
    _seed(usage_store, tmp_path, "legacy-turn", kind="turn", provider=provider, conversation="cv-1")
    adir = tmp_path / "jobs" / "legacy-turn" / "a1"
    adir.mkdir()
    (adir / "stdout").write_text("\n".join(json.dumps(event) for event in events) + "\n")
    totals = build_report(usage_store, tmp_path, backfill=True, now=NOW)["totals"]
    assert totals["attempts_with_usage"] == 1 and totals["backfilled_attempts"] == 1
    assert totals["prompt"] == 100 and totals["cache_read"] == 80 and totals["cache_hit_share"] == .8


def test_c17_8_backfill_launch_resume_does_not_charge_cumulative_totals(usage_store, tmp_path):
    """C-17.8/C-12.10: a resumed exec launch is cumulative even when its job kind is revive."""
    _seed(usage_store, tmp_path, "revived", kind="revive")
    adir = tmp_path / "jobs" / "revived" / "a1"
    adir.mkdir(parents=True)
    (adir / "launch.json").write_text(json.dumps({"argv": ["codex", "exec", "resume", "native-1"]}))
    (adir / "stdout").write_text(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}}) + "\n")
    report = build_report(usage_store, tmp_path, backfill=True, now=NOW)
    assert report["totals"]["attempts_with_usage"] == 1
    assert report["totals"]["cumulative_thread_attempts"] == 1 and report["totals"]["prompt"] is None
    assert report["attempt_rows"][0]["usage"]["raw"]["usage"]["input_tokens"] == 100


def test_c17_8_backfill_preserves_stored_measurements(usage_store, tmp_path):
    """C-17.8: stored evidence remains authoritative even if retained stdout carries different counters."""
    _seed(usage_store, tmp_path, "stored", usage=_usage())
    adir = tmp_path / "jobs" / "stored" / "a1"
    adir.mkdir(parents=True)
    (adir / "stdout").write_text(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 100}}) + "\n")
    report = build_report(usage_store, tmp_path, backfill=True, now=NOW)
    assert report["totals"]["prompt"] == 100 and report["totals"]["backfilled"] is False
