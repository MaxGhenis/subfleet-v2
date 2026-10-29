"""A date field can hold anything, and no pass fails on it: C-23.28.

A record is whatever its file held. Four decisions rank records by
`lastActivityAt`, `lastFocusedAt` and `createdAt`: which copy a new folder is
made from (in the full pass and in the hot pass), whether a stale empty record
is the app's newer one, and whose title wins. Each compared the fields as it
found them, so a string, list or object there raised TypeError out of the pass
(second review of #167). The full pass was left recorded as `running`, every
full pass after it failed the same way, and no session was copied or synced
while one such record stayed in the store.

Now only a number is a date (`mirror._rank`): any other value, and NaN, ranks
as no date. Which record the numbers choose is as it was; `old_rank` and
`old_active` below are the rule before, kept as the reference.

Every test names the clause it proves (C-20.5). The desktop store lives under
`tmp_path`; nothing here reads or writes the operator's own.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

ONE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
TWO = "6f1d5f2a-6f0f-4a0a-9f2f-7c1b2d3e4f50"
THREE = "0b5e7c11-2d3f-4a55-8e6d-7f8091a2b3c4"
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: 2026-10-10T12:26:40Z in the app's milliseconds; the passes' clock is three days on.
NEW = 1_791_635_200_000
DAY = 86_400_000
CLOCK = datetime.fromtimestamp((NEW + 3 * DAY) / 1000, timezone.utc)
FIELDS = mirror.RANK_FIELDS
#: Truthy values that are no number: each of these raised.
NOT_NUMBERS = ["soon", [1], {"x": 1}]
ELSEWHERE = "/Users/fixture/elsewhere"


def old_rank(data: dict):
    """The rule before, word for word."""
    return (data.get("lastActivityAt") or data.get("lastFocusedAt")
            or data.get("createdAt") or 0)


def old_active(data: dict):
    """The title winner's rule before, word for word."""
    return data.get("lastActivityAt") or data.get("createdAt") or 0


def is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and not (isinstance(value, float) and math.isnan(value)))


# --- the fixture store ---------------------------------------------------------------

def scene(base: Path, patch) -> tuple[Path, Path, Path]:
    home = fx.claude_home(base, patch)
    store = fx.desktop_store(base, patch)
    for account, org in FOLDERS:
        (store / account / org).mkdir(parents=True)
    root = base / "state"
    root.mkdir()
    return home, store, root


@pytest.fixture
def world(tmp_path, monkeypatch):
    return scene(tmp_path, monkeypatch)


def engine(world) -> mirror.Mirror:
    """A mirror with no embedded hot service: each pass here is one decision."""
    return mirror.Mirror(world[2], fx.policy(mirror_hot_interval_s=0), now=lambda: CLOCK)


def options(running: mirror.Mirror, **overrides) -> mirror.Options:
    return mirror.options_from(running.policy, **overrides)


def path(world, index: int, session: str = ONE) -> Path:
    account, org = FOLDERS[index]
    return world[1] / account / org / f"local_{session}.json"


def put(world, index: int, session: str = ONE, *, dates: dict | None = None,
        holds: str | None = None, **fields) -> Path:
    """One record in folder `index` under `session`'s name, with exactly the
    date fields in `dates` (a missing key is a missing field). `holds` is the
    conversation it opens: the session itself unless said, "" for a stale
    empty record."""
    identity = session if holds is None else holds
    if identity:
        fx.transcript(world[0], identity, fx.completed())
    target = path(world, index, session)
    body = {"sessionId": target.stem, "cliSessionId": identity, "cwd": fx.WORKDIR,
            "permissionMode": "bypassPermissions", "model": "claude-opus-5-5",
            "title": "a session", "titleSource": "auto", "isArchived": False,
            "isStarred": False, "sessionSettings": {"ultracode": True},
            **fields, **(dates if dates is not None else {"lastActivityAt": NEW})}
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def record(world, index: int, session: str = ONE) -> dict:
    return json.loads(path(world, index, session).read_text(encoding="utf-8"))


def holders(world, session: str) -> list[int]:
    """The folders that hold a copy of `session`."""
    found = []
    for index, (account, org) in enumerate(FOLDERS):
        for file in (world[1] / account / org).glob("local_*.json"):
            if json.loads(file.read_text(encoding="utf-8")).get("cliSessionId") == session:
                found.append(index)
    return found


def rewrite(target: Path, **fields) -> None:
    """The app's own write: beside the file, then rename (never in place)."""
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({**json.loads(target.read_text()), **fields}))
    temporary.replace(target)


def only(field: str, value) -> dict:
    """Date fields in which `field` alone decides the rank."""
    return {field: value}


# --- each decision, with a value that raised -----------------------------------------

@pytest.mark.parametrize("bad", NOT_NUMBERS)
@pytest.mark.parametrize("field", FIELDS)
def test_a_full_pass_does_not_fail_on_a_date_that_is_no_number(world, field, bad):
    """C-23.28: choosing the copy a new folder is made from raised TypeError;
    the pass stayed recorded as `running` and the healthy session beside it
    was never copied. The record ranks as one with no date, so the dated copy
    is the one copied."""
    put(world, 0, dates=only(field, bad), model="the-bad-record")
    put(world, 1, dates=only(field, NEW), model="the-dated-record")
    put(world, 0, TWO)
    running = engine(world)
    result = running.run_once(options(running))
    assert result.state == "ok" and result.error is None
    assert running.sidecar()["pass"]["state"] == "ok"
    assert running.health()["status"] == "healthy"
    assert holders(world, TWO) == [0, 1, 2], "the session beside it is copied"
    assert record(world, 2)["model"] == "the-dated-record"
    assert record(world, 0)[field] == bad, "and the record itself is left as the app wrote it"


@pytest.mark.parametrize("bad", NOT_NUMBERS)
@pytest.mark.parametrize("field", FIELDS)
def test_a_hot_pass_does_not_fail_on_a_date_that_is_no_number(world, field, bad):
    """C-23.28: the hot pass picks its best copy by the same comparison. It
    raised, and the new session it was about to spread waited for a full pass
    that raised too."""
    for index in range(3):
        put(world, index, dates=only(field, NEW))
    running = engine(world)
    assert running.run_once(options(running)).state == "ok"
    rewrite(path(world, 0), **{**dict.fromkeys(FIELDS), **only(field, bad)})
    put(world, 0, TWO)
    result = running.run_hot(options(running))
    assert result.kind == "hot" and result.state == "ok" and result.error is None
    assert holders(world, TWO) == [0, 1, 2]
    assert running.run_once(options(running)).state == "ok", "and the full pass after it"


@pytest.mark.parametrize("bad", NOT_NUMBERS)
@pytest.mark.parametrize("field", FIELDS)
def test_the_stale_empty_rule_does_not_fail_on_a_date_that_is_no_number(world, field, bad):
    """C-23.28: a stale empty record from another cwd is kept only when it is
    newer than the copy being spread. With no date it is not newer, so it is
    repaired, as one with a missing date always was."""
    put(world, 0, dates=only(field, NEW))
    put(world, 1, holds="", cwd=ELSEWHERE, dates=only(field, bad))
    running = engine(world)
    result = running.run_once(options(running))
    assert result.state == "ok" and result.repaired == 1
    assert record(world, 1)["cliSessionId"] == ONE


@pytest.mark.parametrize("bad", NOT_NUMBERS)
@pytest.mark.parametrize("field", ("lastActivityAt", "createdAt"))
def test_the_title_winner_does_not_fail_on_a_date_that_is_no_number(world, field, bad):
    """C-23.28: divergent titles go to the most recently active copy, found
    with `max`. It raised in flag sync, after the pass had made its copies.
    The copy with no date loses to a dated one."""
    put(world, 0, dates={"lastFocusedAt": 5, **only(field, bad)}, title="undated")
    put(world, 1, dates={"lastFocusedAt": 6, **only(field, NEW)}, title="dated")
    put(world, 2, dates={"lastFocusedAt": 7, **only(field, NEW)}, title="dated")
    running = engine(world)
    result = running.run_once(options(running))
    assert result.state == "ok" and result.retitled == 1
    assert [record(world, index)["title"] for index in range(3)] == ["dated"] * 3


# --- what a value that is no number ranks as ------------------------------------------

NAN = float("nan")


@pytest.mark.parametrize("data, expected", [
    ({}, 0),
    ({"lastActivityAt": 9, "lastFocusedAt": 8, "createdAt": 7}, 9),
    ({"lastActivityAt": None, "lastFocusedAt": 8, "createdAt": 7}, 8),
    ({"lastActivityAt": 0, "lastFocusedAt": 0.0, "createdAt": 7}, 7),
    ({"lastActivityAt": "soon", "lastFocusedAt": 8}, 8),
    ({"lastActivityAt": [1], "lastFocusedAt": {"x": 1}, "createdAt": 7}, 7),
    ({"lastActivityAt": "soon", "lastFocusedAt": [1], "createdAt": {"x": 1}}, 0),
    ({"lastActivityAt": "1791635200000", "createdAt": 7}, 7),     # a digit string is a string
    ({"lastActivityAt": True, "createdAt": 7}, 7),                # a bool is not a number
    ({"lastActivityAt": NAN, "lastFocusedAt": 8}, 8),
    ({"lastActivityAt": NAN}, 0),
    ({"lastActivityAt": -5, "lastFocusedAt": 8}, -5),             # a number, as before
    ({"lastActivityAt": math.inf}, math.inf),
    ({"lastActivityAt": 10 ** 400, "lastFocusedAt": 8}, 10 ** 400),
    ({"lastActivityAt": 1.5}, 1.5),
])
def test_only_a_number_is_a_date(data, expected):
    """C-23.28: the first of the three fields that holds a number other than
    zero; anything else is passed over as a missing field is."""
    assert mirror._rank(data) == expected
    assert type(mirror._rank(data)) is type(expected)


def test_a_record_is_ranked_by_its_next_date_when_one_is_no_number(world):
    """C-23.28: "no date" is the field's, not the record's. A's `lastActivityAt`
    is a string, and its `lastFocusedAt` is later than B's date: A is still the
    copy a new folder is made from."""
    put(world, 0, dates={"lastActivityAt": "soon", "lastFocusedAt": NEW + 5}, model="from-a")
    put(world, 1, dates={"lastActivityAt": NEW}, model="from-b")
    running = engine(world)
    assert running.run_once(options(running)).state == "ok"
    assert record(world, 2)["model"] == "from-a"


def test_nan_ranks_as_no_date_where_it_ranked_by_its_place(world):
    """C-23.28, the one float whose rank changes, and it is meant to. NaN is
    greater than nothing and nothing is greater than it, so under the old rule
    the first copy read kept the lead against any date and a later one never
    took it. It is not a date: the dated copy is the one copied."""
    undated, dated = {"lastActivityAt": NAN}, {"lastActivityAt": NEW}
    assert not old_rank(dated) > old_rank(undated), "before: read first, it kept the lead"
    assert mirror._rank(dated) > mirror._rank(undated)
    put(world, 0, dates=undated, model="the-nan-record")
    put(world, 1, dates=dated, model="the-dated-record")
    running = engine(world)
    assert running.run_once(options(running)).state == "ok"
    assert record(world, 2)["model"] == "the-dated-record"


# --- for every value -------------------------------------------------------------------

#: Any value Python's `json` reads, NaN and the infinities among them.
JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.text(max_size=6)
    | st.integers(min_value=-10 ** 30, max_value=10 ** 30)
    | st.sampled_from([0, 1, -1, 2 ** 53, 10 ** 400, -(10 ** 400), NEW])
    | st.floats(allow_nan=True, allow_infinity=True),
    lambda inner: (st.lists(inner, max_size=3)
                   | st.dictionaries(st.text(max_size=3), inner, max_size=3)),
    max_leaves=5)
#: Any number but NaN: an integer of any size, a float, an infinity.
NUMBERS = (st.integers(min_value=-10 ** 30, max_value=10 ** 30)
           | st.sampled_from([0, 1, -1, 2 ** 53, 10 ** 400, -(10 ** 400), NEW, NEW + 1])
           | st.floats(allow_nan=False, allow_infinity=True))


def records(values) -> st.SearchStrategy[dict]:
    """A record's date fields: each missing, or one of `values`."""
    return st.fixed_dictionaries({}, optional={field: values for field in FIELDS})


@settings(max_examples=500, deadline=None)
@given(first=records(JSON_VALUES), second=records(JSON_VALUES))
def test_a_rank_is_a_number_with_a_place_in_the_order(first, second):
    """C-23.28: whatever the fields hold, a rank is an int or a float that is
    not NaN, so any two compare, and exactly one of greater, less and equal
    holds. This is what lets no value fail a comparison."""
    for fields in (FIELDS, ("lastActivityAt", "createdAt")):
        one, other = mirror._rank(first, fields), mirror._rank(second, fields)
        for rank in (one, other):
            assert type(rank) in (int, float) and rank == rank
        assert [one > other, one < other, one == other].count(True) == 1
    assert max([first, second], key=mirror._rank) in (first, second)


@settings(max_examples=500, deadline=None)
@given(data=records(JSON_VALUES))
def test_a_value_that_is_no_number_ranks_as_a_missing_field(data):
    """C-23.28: the record ranks exactly as it would with those fields gone."""
    without = {field: value for field, value in data.items() if is_number(value)}
    rank = mirror._rank(data)
    assert rank == mirror._rank(without) and type(rank) is type(mirror._rank(without))
    assert rank == old_rank(without), "and that is the old rule's rank of what is left"


@settings(max_examples=500, deadline=None)
@given(data=records(st.none() | NUMBERS))
def test_numbers_rank_as_they_did(data):
    """C-23.28: for fields that hold numbers (any size, any sign, the
    infinities) or nothing, the rank is the old rule's own value."""
    for new, old in ((mirror._rank(data), old_rank(data)),
                     (mirror._rank(data, ("lastActivityAt", "createdAt")), old_active(data))):
        assert new == old and type(new) is type(old)


@settings(max_examples=300, deadline=None)
@given(copies=st.lists(records(st.none() | NUMBERS), min_size=1, max_size=5))
def test_the_choices_among_numbers_are_the_old_rules(copies):
    """C-23.28: the three shapes the callers decide in, on the same records
    under both rules: the first copy no later one outranks (`_pass`, `_hot`),
    `max` by the title rule (`sync_flags`), and one pair (`_spread`)."""
    def first_unbeaten(rank) -> int:
        best = 0
        for index in range(1, len(copies)):
            if rank(copies[index]) > rank(copies[best]):
                best = index
        return best

    assert first_unbeaten(mirror._rank) == first_unbeaten(old_rank)
    indices = range(len(copies))
    assert (max(indices, key=lambda index: mirror._rank(copies[index],
                                                        ("lastActivityAt", "createdAt")))
            == max(indices, key=lambda index: old_active(copies[index])))
    assert ((mirror._rank(copies[0]) > mirror._rank(copies[-1]))
            == (old_rank(copies[0]) > old_rank(copies[-1])))


# --- for every value, on real files ---------------------------------------------------

@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(first=records(JSON_VALUES), second=records(JSON_VALUES), empty=records(JSON_VALUES),
       saved=records(JSON_VALUES))
def test_no_value_in_a_date_field_fails_a_pass(first, second, empty, saved,
                                               tmp_path_factory, monkeypatch):
    """C-23.28: any JSON in the three fields of two copies with different
    titles and of a stale empty record from another cwd, which puts every
    ranking decision in play. A full pass, the hot pass after the app saves
    more of the same, and the next full pass all finish `ok`, and the healthy
    sessions beside it reach every folder."""
    with monkeypatch.context() as patch:
        world = scene(tmp_path_factory.mktemp("values"), patch)
        put(world, 0, dates=first, title="first")
        put(world, 1, dates=second, title="second")
        put(world, 2, holds="", cwd=ELSEWHERE, dates=empty)
        put(world, 0, TWO)
        running = engine(world)
        full = running.run_once(options(running))
        assert full.state == "ok" and full.error is None
        assert holders(world, TWO) == [0, 1, 2]
        rewrite(path(world, 0), **{**dict.fromkeys(FIELDS), **saved})
        put(world, 0, THREE)
        hot = running.run_hot(options(running))
        assert hot.kind == "hot" and hot.state == "ok" and hot.error is None
        assert holders(world, THREE) == [0, 1, 2]
        again = running.run_once(options(running))
        assert again.state == "ok" and running.sidecar()["pass"]["state"] == "ok"


def expected_donor(first: dict, second: dict) -> int:
    """The copy the old rule made a new folder from: the first, unless the
    second outranks it."""
    return 1 if old_rank(second) > old_rank(first) else 0


def expected_title(first: dict, second: dict) -> int:
    return max((0, 1), key=lambda index: old_active((first, second)[index]))


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(first=records(st.none() | NUMBERS), second=records(st.none() | NUMBERS))
def test_a_full_pass_chooses_among_numbers_as_the_old_rule_did(first, second,
                                                               tmp_path_factory, monkeypatch):
    """C-23.28: with numbers in the fields, on real files, the new folder is
    copied from the record the old rule chose and the title goes to the copy
    the old rule gave it to. The two rules read different fields, so they can
    and do pick different copies."""
    with monkeypatch.context() as patch:
        world = scene(tmp_path_factory.mktemp("full"), patch)
        put(world, 0, dates=first, model="copy-0", title="title-0")
        put(world, 1, dates=second, model="copy-1", title="title-1")
        running = engine(world)
        assert running.run_once(options(running)).state == "ok"
        assert record(world, 2)["model"] == f"copy-{expected_donor(first, second)}"
        assert ([record(world, index)["title"] for index in range(3)]
                == [f"title-{expected_title(first, second)}"] * 3)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(first=records(st.none() | NUMBERS), second=records(st.none() | NUMBERS))
def test_a_hot_pass_chooses_among_numbers_as_the_old_rule_did(first, second,
                                                              tmp_path_factory, monkeypatch):
    """C-23.28: the same for the hot pass's best copy, on records that
    arrived after the instance's inventory."""
    with monkeypatch.context() as patch:
        world = scene(tmp_path_factory.mktemp("hot"), patch)
        for index in range(3):
            put(world, index, TWO)
        running = engine(world)
        assert running.run_once(options(running)).state == "ok"
        put(world, 0, dates=first, model="copy-0", title="title-0")
        put(world, 1, dates=second, model="copy-1", title="title-1")
        result = running.run_hot(options(running))
        assert result.kind == "hot" and result.state == "ok"
        assert record(world, 2)["model"] == f"copy-{expected_donor(first, second)}"
        assert ([record(world, index)["title"] for index in range(3)]
                == [f"title-{expected_title(first, second)}"] * 3)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(copy=records(st.none() | NUMBERS), empty=records(st.none() | NUMBERS))
def test_the_stale_empty_rule_decides_among_numbers_as_it_did(copy, empty,
                                                              tmp_path_factory, monkeypatch):
    """C-23.28: a stale empty record from another cwd is kept exactly when the
    old rule ranked it above the copy being spread, and repaired otherwise."""
    with monkeypatch.context() as patch:
        world = scene(tmp_path_factory.mktemp("empty"), patch)
        put(world, 0, dates=copy)
        put(world, 1, holds="", cwd=ELSEWHERE, dates=empty)
        running = engine(world)
        assert running.run_once(options(running)).state == "ok"
        kept = old_rank(empty) > old_rank(copy)
        assert record(world, 1)["cliSessionId"] == ("" if kept else ONE)
