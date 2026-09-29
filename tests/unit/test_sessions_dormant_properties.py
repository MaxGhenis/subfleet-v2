"""Dormant sessions, for every input: C-23.56 (the classifier), C-23.57
(liveness) and C-23.59 (pacing). Every test names the clause it proves (C-20.5).

The classifier's inputs are real transcripts, written to disk and read by
`transcripts.turn_state`, so what is tested is the path a scan takes. Beside
the properties sits a differential test against the detector that found the 59
dormant sessions on 2026-09-27 (`/private/tmp/claude-501/pass4/midturn.py`,
transcribed below as `reference_midturn`): on the transcripts that detector was
written for, the two agree on which tails stop mid-turn.
"""

from __future__ import annotations

import json
import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet.sessions import dormant, registry, transcripts
from subfleet.sessions.transcripts import TurnState
from tests import sessions_fixtures as fx

NOW = fx.NOW
PROPERTY = settings(max_examples=300, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.too_slow])

# --- transcript tails ---------------------------------------------------------

TEXTS = ("start the work", "<task-notification> <task-id>w1</task-id> done",
         "[Request interrupted by user]", "[Request interrupted by user for tool use]",
         transcripts.MARKER + " (Claude account switch) with its last turn cut off",
         transcripts.WAKE_MARKER + " and nothing restarted it",
         transcripts.REVIVE_MARKER + " and its process died", "")


def _entry(kind: str, index: int, text: str = "x") -> dict | str:
    at = fx.ago(10_000 - index)
    uuid = f"e{index}"
    if kind == "user_text":
        return fx.user_text(text, uuid=uuid, at=at)
    if kind == "user_tool_result":
        return fx.user_tool_result(uuid=uuid, at=at, tool_id=f"t{index}")
    if kind == "assistant_text":
        return fx.assistant_text("reply", uuid=uuid, at=at, model="claude-opus-5-5")
    if kind == "assistant_tool_use":
        return fx.assistant_tool_use(uuid=uuid, at=at, tool_id=f"t{index}", model="claude-opus-5-5")
    if kind == "assistant_thinking":
        entry = fx.assistant_text("", uuid=uuid, at=at, model="claude-opus-5-5")
        entry["message"]["content"] = [{"type": "thinking", "thinking": "hmm"}]
        return entry
    if kind == "banner":
        return fx.limit_banner(uuid=uuid, at=at)
    if kind == "meta":
        entry = fx.user_text(text, uuid=uuid, at=at)
        entry["isMeta"] = True
        return entry
    if kind == "sidechain":
        entry = fx.assistant_tool_use(uuid=uuid, at=at, tool_id=f"s{index}")
        entry["isSidechain"] = True
        return entry
    if kind == "bookkeeping":
        return {"type": "system", "subtype": "stop_hook_summary", "uuid": uuid, "timestamp": at}
    if kind == "garbage":
        return "{not json"
    raise AssertionError(kind)


MAIN = ("user_text", "user_tool_result", "assistant_text", "assistant_tool_use",
        "assistant_thinking")
NOISE = ("meta", "sidechain", "bookkeeping", "garbage")
tails = st.lists(st.tuples(st.sampled_from(MAIN + NOISE + ("banner", "stub")),
                           st.sampled_from(TEXTS)), max_size=14)
livenesses = st.sampled_from(dormant.LIVENESS + ("bogus", ""))
quiets = st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True),
                   st.floats(min_value=-100.0, max_value=5000.0))
windows = st.one_of(st.floats(min_value=0.0, max_value=4000.0),
                    st.sampled_from([math.inf, 600.0]))


def rows_of(tail) -> list:
    rows: list = []
    for index, (kind, text) in enumerate(tail):
        if kind == "stub":
            rows += fx.resume_stub(user_uuid=f"su{index}", assistant_uuid=f"sa{index}",
                                   at=fx.ago(10_000 - index))
        else:
            rows.append(_entry(kind, index, text))
    return rows


def turn_of(rows) -> TurnState:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "session.jsonl"
        path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n"
                                for row in rows), encoding="utf-8")
        return transcripts.turn_state(path, now=NOW)


@PROPERTY
@given(tail=tails, liveness=livenesses, quiet=quiets, window=windows)
@example(tail=[("assistant_tool_use", "")], liveness="dead", quiet=600.0, window=600.0)
@example(tail=[("user_tool_result", "")], liveness="dead", quiet=599.999, window=600.0)
def test_c23_56_every_tail_maps_to_exactly_one_verdict(tail, liveness, quiet, window):
    """C-23.56: for every transcript tail, liveness and quiet time, the verdict is
    exactly one of completed, interrupted and active, and it is `interrupted`
    only when the process is known dead and quiet for the whole window."""
    turn = turn_of(rows_of(tail))
    verdict = dormant.classify(turn, liveness, quiet, window_s=window)
    assert [state for state in dormant.VERDICTS if state == verdict.state] == [verdict.state]
    assert (verdict.state == "completed") == (turn.state not in dormant.MID_TURN)
    if verdict.state == "interrupted":
        assert liveness == "dead"
        assert quiet is not None and math.isfinite(quiet) and quiet >= window
    if turn.state in dormant.MID_TURN and liveness == "dead" and quiet is not None \
            and math.isfinite(quiet) and math.isfinite(window) and quiet >= window:
        assert verdict.state == "interrupted"


@PROPERTY
@given(tail=tails, window=st.floats(min_value=0.0, max_value=4000.0),
       quiet=st.floats(min_value=0.0, max_value=5000.0), more=st.floats(min_value=0.0, max_value=5000.0))
def test_c23_56_a_longer_quiet_never_undoes_an_interruption(tail, window, quiet, more):
    """C-23.56: quiet only accumulates evidence. A session interrupted after q
    seconds of quiet is still interrupted after q + more."""
    turn = turn_of(rows_of(tail))
    before = dormant.classify(turn, "dead", quiet, window_s=window)
    after = dormant.classify(turn, "dead", quiet + more, window_s=window)
    if before.state == "interrupted":
        assert after.state == "interrupted"


@PROPERTY
@given(tail=tails, noise=st.lists(st.tuples(st.integers(min_value=0, max_value=20),
                                            st.sampled_from(NOISE)), max_size=6))
def test_c23_56_bookkeeping_sidechains_and_bad_lines_never_change_the_shape(tail, noise):
    """C-23.56: rows that are not main-chain turns (meta prompts, subagent
    sidechains, system bookkeeping, lines that are not JSON) do not move the
    verdict, wherever they fall."""
    rows = rows_of(tail)
    noisy = list(rows)
    for position, kind in noise:
        noisy.insert(min(position, len(noisy)), _entry(kind, 900 + position))
    assert turn_of(noisy).state == turn_of(rows).state


@PROPERTY
@given(tail=tails)
def test_c23_56_the_apps_resume_stub_is_transparent(tail):
    """C-23.56 (C-23.34): the resume stub the app writes into a restarted session
    is not a turn, so appending one leaves the shape underneath unchanged."""
    rows = rows_of(tail)
    assert turn_of(rows + fx.resume_stub(at=fx.ago(1))).state == turn_of(rows).state


def reference_midturn(rows) -> str | None:
    """`midturn.py`'s classification, as written on 2026-09-27: the kind of
    mid-turn stop, or None for a tail it does not report."""
    last = None
    for row in rows:
        if isinstance(row, str):
            continue
        if row.get("type") in ("user", "assistant") and not row.get("isMeta"):
            last = row
    if not last:
        return None
    content = last["message"].get("content")
    if last["type"] == "user":
        if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                             for block in content):
            return "tool_result"
        text = content if isinstance(content, str) else " ".join(
            block.get("text", "") for block in content if isinstance(block, dict))
        if "[Request interrupted" in text:
            return None
        return "unanswered_prompt"
    if isinstance(content, list) and content and isinstance(content[-1], dict) \
            and content[-1].get("type") == "tool_use":
        return "dangling_tool_use"
    return None


#: The rows `midturn.py` was written for: main turns, meta prompts, system rows
#: and bad lines. It knew nothing of sidechains, resume stubs or limit banners,
#: which `turn_state` skips on purpose (C-23.34).
differential_tails = st.lists(st.tuples(st.sampled_from(MAIN + ("meta", "bookkeeping", "garbage")),
                                        st.sampled_from(TEXTS)), max_size=14)


@PROPERTY
@given(tail=differential_tails)
def test_c23_56_agrees_with_the_detector_that_found_the_59(tail):
    """C-23.56, differential: on the transcripts the 2026-09-27 detector handled,
    a tail is mid-turn for `turn_state` exactly when that detector reported it."""
    rows = rows_of(tail)
    assert (turn_of(rows).state in dormant.MID_TURN) == (reference_midturn(rows) is not None)


# --- pacing (C-23.59) -----------------------------------------------------------

def window_at(used: float, resets_in_s: float, *, label: str = "provider",
              age_s: float = 0.0) -> dormant.Window:
    return dormant.Window(used, NOW + timedelta(seconds=resets_in_s),
                          as_of=NOW - timedelta(seconds=age_s), label=label)


rules = st.builds(dormant.PaceRule, headroom=st.floats(min_value=0, max_value=30),
                  ceiling=st.floats(min_value=0, max_value=100),
                  batch=st.integers(min_value=0, max_value=10),
                  cost=st.floats(min_value=0, max_value=5))
used_values = st.floats(min_value=0, max_value=120)
resets = st.floats(min_value=1, max_value=5 * 3600)
pendings = st.integers(min_value=0, max_value=10)


def admissible(used, pending, k, elapsed, rule) -> bool:
    projected = used + (pending + k) * rule.cost
    return projected <= elapsed + rule.headroom and projected < rule.ceiling


@PROPERTY
@given(used=used_values, resets_in=resets, pending=pendings, rule=rules)
@example(used=64.0, resets_in=1800.0, pending=2, rule=dormant.PaceRule(batch=10))
@example(used=65.0, resets_in=1800.0, pending=5, rule=dormant.PaceRule(batch=10))
@example(used=40.0, resets_in=4 * 3600.0, pending=3, rule=dormant.PaceRule(batch=10))
def test_c23_59_every_wake_sent_keeps_within_the_rule_and_no_more_could(used, resets_in, pending, rule):
    """C-23.59: the count allowed is exactly the longest run of wakes each of which
    keeps "used <= elapsed + 10 and < 70" at the usage the running wakes and the
    wakes before it add, capped at the batch less the running ones."""
    decision = dormant.pace(window_at(used, resets_in), now=NOW, rule=rule, pending=pending)
    room = max(0, min(rule.batch, dormant.BATCH_LIMIT) - pending)
    elapsed = dormant.elapsed_pct(NOW + timedelta(seconds=resets_in), NOW, rule.window_s)
    assert 0 <= decision.allowed <= room
    assert all(admissible(used, pending, k, elapsed, rule) for k in range(decision.allowed))
    assert decision.allowed == room or not admissible(used, pending, decision.allowed, elapsed, rule)
    assert decision.elapsed_pct is not None and 0 <= decision.elapsed_pct <= 100


@PROPERTY
@given(used=used_values, extra=st.floats(min_value=0, max_value=50), resets_in=resets,
       pending=pendings, more=st.integers(min_value=0, max_value=5), rule=rules,
       later=st.floats(min_value=0, max_value=3600))
def test_c23_59_more_usage_or_more_running_never_allows_more(used, extra, resets_in, pending,
                                                              more, rule, later):
    """C-23.59: monotone. More usage or more running wakes never allows more; a
    later moment in the same window, with the same reading, never allows fewer."""
    base = dormant.pace(window_at(used, resets_in), now=NOW, rule=rule, pending=pending).allowed
    assert dormant.pace(window_at(used + extra, resets_in), now=NOW, rule=rule,
                        pending=pending).allowed <= base
    assert dormant.pace(window_at(used, resets_in), now=NOW, rule=rule,
                        pending=pending + more).allowed <= base
    if later < resets_in:
        moved = dormant.pace(window_at(used, resets_in, age_s=0), now=NOW + timedelta(seconds=later),
                             rule=dormant.PaceRule(**{**rule.__dict__, "max_reading_age_s": 1e9}),
                             pending=pending).allowed
        assert moved >= base


@PROPERTY
@given(used=used_values, resets_in=resets)
def test_c23_59_the_rule_as_written(used, resets_in):
    """C-23.59, the rule as Max wrote it: with no cost modelled, a batch of up to
    six goes out exactly when used <= elapsed% + 10 and used < 70."""
    rule = dormant.PaceRule(cost=0.0)
    decision = dormant.pace(window_at(used, resets_in), now=NOW, rule=rule)
    elapsed = dormant.elapsed_pct(NOW + timedelta(seconds=resets_in), NOW, rule.window_s)
    assert decision.allowed == (6 if used <= elapsed + 10 and used < 70 else 0)


@PROPERTY
@given(used=st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
       resets_in=st.floats(min_value=-5 * 3600, max_value=5 * 3600),
       label=st.sampled_from(["provider", "stale-provider", "admission-observed",
                              "local-backoff", "unknown", ""]),
       age=st.floats(min_value=-10, max_value=10_000), pending=pendings)
def test_c23_59_an_unknown_window_allows_nothing(used, resets_in, label, age, pending):
    """C-23.59, fail closed: no reading, a reading that is not a percentage or not
    a provider reading (C-9.1), one older than an hour, or one whose window has
    already reset allows no wake at all."""
    reading = window_at(used, resets_in, label=label, age_s=age) if used is not None \
        else dormant.Window(None, NOW + timedelta(seconds=resets_in))
    decision = dormant.pace(reading, now=NOW, pending=pending)
    untrusted = (used is None or not math.isfinite(used) or used < 0
                 or label not in dormant.TRUSTED_LABELS or age > 3600 or resets_in <= 0)
    if untrusted:
        assert decision.allowed == 0
    assert dormant.pace(None, now=NOW).allowed == 0


trusted_windows = st.builds(window_at, used_values, resets,
                            label=st.sampled_from(["provider", "provider", "unknown"]),
                            age_s=st.floats(min_value=0, max_value=7200))


@PROPERTY
@given(windows=st.lists(trusted_windows, min_size=1, max_size=4), pending=pendings, rule=rules)
def test_c23_59_the_batch_fits_every_lane_a_turn_may_land_on(windows, pending, rule):
    """C-23.59: across the lanes a turn may be placed on, the count is the least
    any lane with a current reading allows, and nothing unless the first lane
    (the one admission picks now) has one."""
    decision = dormant.pace_lanes(windows, now=NOW, rule=rule, pending=pending)
    first = dormant.pace(windows[0], now=NOW, rule=rule, pending=pending)
    if dormant.distrust(windows[0], now=NOW, rule=rule) is not None:
        assert decision.allowed == 0
        return
    trusted = [dormant.pace(window, now=NOW, rule=rule, pending=pending).allowed for window in windows
               if dormant.distrust(window, now=NOW, rule=rule) is None]
    assert decision.allowed == min(trusted) <= first.allowed


# --- liveness (C-23.57) -----------------------------------------------------------

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
OTHER = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
START = "Mon Sep 28 17:19:00 2026"
LATER = "Mon Sep 28 18:01:07 2026"


def row(session_id: str, pid: int, proc_start: str | None) -> registry.SessionRow:
    return registry.SessionRow(session_id=session_id, pid=pid, socket=None, name=None, cwd=None,
                               started_at=None, alive=False, socket_present=False,
                               registry_path=f"/r/{pid}.json", proc_start=proc_start)


registry_rows = st.lists(st.builds(row, st.sampled_from([SESSION, SESSION.upper(), OTHER]),
                                   st.integers(min_value=1, max_value=6),
                                   st.sampled_from([None, START, LATER])), max_size=4)
tables = st.one_of(st.none(), st.builds(
    dormant.Processes,
    starts=st.dictionaries(st.integers(min_value=1, max_value=6), st.sampled_from([START, LATER]),
                           max_size=6),
    named=st.frozensets(st.sampled_from([SESSION, OTHER]), max_size=2)))
readings = st.builds(registry.Reading, rows=registry_rows.map(tuple),
                     unreadable=st.lists(st.one_of(st.none(), st.integers(min_value=1, max_value=6)),
                                         max_size=2).map(tuple),
                     error=st.sampled_from([None, None, "cannot list"]))


@PROPERTY
@given(reading=readings, processes=tables)
def test_c23_57_dead_only_when_every_reading_succeeded_and_found_nothing(reading, processes):
    """C-23.57 (C-4.2): `dead` needs a process table, a listed registry, no
    unreadable file of a running (or unnamed) pid, no command line naming the
    session and no row of its own process. Without a process table it is never
    `dead`."""
    verdict = dormant.liveness(SESSION, reading=reading, processes=processes)
    assert verdict in dormant.LIVENESS
    own = [item for item in reading.rows if item.session_id.lower() == SESSION]
    if processes is None:
        assert verdict in ("alive", "unknown")
        return
    lives = any(item.pid in processes.starts and (item.proc_start is None
                                                 or item.proc_start == processes.starts[item.pid])
                for item in own) or SESSION in processes.named
    if lives:
        assert verdict == "alive"
    elif reading.error or any(pid is None or pid in processes.starts for pid in reading.unreadable):
        assert verdict == "unknown"
    else:
        assert verdict == "dead"


@PROPERTY
@given(reading=readings, processes=tables, pid=st.integers(min_value=1, max_value=6))
def test_c23_57_more_evidence_of_life_never_makes_a_session_dead(reading, processes, pid):
    """C-23.57: naming the session on a command line, or registering it under a
    running pid, can only move the verdict toward `alive`."""
    before = dormant.liveness(SESSION, reading=reading, processes=processes)
    if processes is not None:
        named = dormant.Processes(starts=processes.starts, named=processes.named | {SESSION})
        assert dormant.liveness(SESSION, reading=reading, processes=named) == "alive"
        if pid in processes.starts:
            joined = registry.Reading(rows=reading.rows + (row(SESSION, pid, None),),
                                      unreadable=reading.unreadable, error=reading.error)
            assert dormant.liveness(SESSION, reading=joined, processes=processes) == "alive"
    assert before != "dead" or processes is not None


forms = st.sampled_from(["--resume={id}", "--resume {id}", "-r {id}", "--session-id {id}",
                         "--resume '{id}'", "--resumed {id}", "x--resume={id}", "{id}"])
commands = st.lists(st.tuples(st.integers(min_value=1, max_value=99_999),
                              st.sampled_from(["S", "R+", "Ss", "Z", "Z+"]), forms,
                              st.sampled_from([SESSION, SESSION.upper(), OTHER])),
                    max_size=6, unique_by=lambda item: item[0])


@PROPERTY
@given(rows=commands)
def test_c23_57_the_process_table_is_read_as_ps_prints_it(rows):
    """C-23.57: `parse_processes` keeps every live pid's start and the sessions
    named after `--resume`, `-r` or `--session-id` (with `=` or a space, quoted or
    not), never a zombie's and never an id that merely appears on a line."""
    text = "\n".join(f"{pid:>6} {stat:<4} {START}     /bin/claude --verbose {form.format(id=sid)}"
                     for pid, stat, form, sid in rows)
    seen = dormant.parse_processes(text)
    live = [(pid, form, sid) for pid, stat, form, sid in rows if not stat.startswith("Z")]
    assert set(seen.starts) == {pid for pid, _form, _sid in live}
    assert all(start == START for start in seen.starts.values())
    flagged = {"--resume={id}", "--resume {id}", "-r {id}", "--session-id {id}", "--resume '{id}'"}
    assert seen.named == {sid.lower() for _pid, form, sid in live if form in flagged}
