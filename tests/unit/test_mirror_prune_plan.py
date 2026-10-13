"""The mirror's scope rules and removal plan, as pure functions: C-23.28a.

`subfleet/sessions/prune.py` decides where each session belongs and which
copies outside that scope a pass may remove. Its invariants hold for every
input, so they are stated as Hypothesis properties over random stores (up to
six folders of three kinds plus one the pass did not list, up to seven
sessions, records with no session id, two names for one session in a folder,
activity and focus times that diverge, random flags, home, loaded folder,
learning state and rules), with a few examples for the readings that matter
most. `test_mirror_prune.py` holds the real mirror to the same plan on real
files.

Invariants:

* **Never the only copy.** Every session keeps at least one copy.
* **The newest survives.** A session's newest copy by its activity times,
  and its newest by the spread's own ranking (`lastActivityAt` first), are
  never removed; nor is a copy with no session id or in an unlisted folder.
* **Minimal.** A copy outside the scope is kept as a newest copy only when
  no copy in scope is as new by that measure.
* **Hands off.** Nothing is removed from the loaded folder or the home
  folder, and nothing at all while the loaded folder is unknown, removal is
  off, the pass is blocked, or the session's flags are unsettled; nothing
  from a switch folder for a live session while the registry is learning.
* **No churn.** Every removed copy is outside its session's scope, and stays
  outside it once the plan is applied, so the spread never puts it back.
* **Idempotence.** Applying a plan leaves nothing for the next plan to remove.
* **Accounting.** Copies outside their scope = newest kept + held + removed,
  and the defensive holds the construction makes unreachable never occur.
* **Monotone in the thresholds.** Longer thresholds never remove more.
* **Off is off.** With every rule off, every session belongs everywhere and
  nothing is outside.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet.sessions import prune
from subfleet.sessions.folder_kinds import SETTLED, SWITCH, UNKNOWN

NOW = 1_790_000_000.0          # 2026-09-21 13:33 UTC, the planner's clock
DAY = prune.DAY_S
FOLDERS = ("a/o1", "b/o2", "c/o3", "d/o1", "e/o2", "f/o4")
UNLISTED = "z/o9"


def ms(days: float) -> int:
    return int((NOW - days * DAY) * 1000)


@st.composite
def worlds(draw):
    """A random store the planner could meet, with flags agreeing per session.

    Flag sync runs before the plan and holds or rewrites any session whose
    copies disagree, so a session reaching the plan unheld has one archived
    and one starred value; `held` covers the rest.
    """
    count = draw(st.integers(min_value=1, max_value=len(FOLDERS)))
    folders = FOLDERS[:count]
    kinds = {folder: draw(st.sampled_from((SETTLED, SWITCH, UNKNOWN))) for folder in folders}
    plain = [folder for folder in folders if kinds[folder] != SWITCH]
    home = draw(st.one_of(st.none(), st.sampled_from(plain))) if plain else None
    loaded = draw(st.one_of(st.none(), st.sampled_from(folders)))
    sessions = draw(st.integers(min_value=1, max_value=7))
    copies, openable, archived_at, held = [], set(), {}, set()
    for number in range(sessions):
        identity = f"s{number}"
        archived = draw(st.booleans())
        starred = draw(st.booleans()) and draw(st.booleans())
        disagree = draw(st.booleans()) and draw(st.booleans())
        where = draw(st.lists(st.sampled_from(folders + (UNLISTED,)), min_size=1,
                              max_size=len(folders) + 1, unique=True))
        for place in where:
            names = [f"local_{identity}.json"]
            if draw(st.integers(0, 5)) == 0:
                names.append(f"local_{identity}-cli.json")   # a collision's fallback name
            for name in names:
                flag = (not archived) if disagree and draw(st.booleans()) else archived
                active = draw(st.floats(0, 60))
                focused = active - draw(st.floats(0, 20)) if draw(st.booleans()) else None
                rank = ms(active)
                activity = max(rank, ms(focused)) if focused is not None else rank
                copies.append(prune.Copy(
                    folder=place, name=name, identity=identity, archived=flag,
                    starred=starred, activity_ms=activity, rank_ms=rank,
                    mtime=NOW - draw(st.floats(0, 60)) * DAY))
        if disagree or draw(st.booleans()) and draw(st.booleans()):
            held.add(identity)
        if draw(st.booleans()) or draw(st.booleans()):
            openable.add(identity)
        if draw(st.booleans()) and draw(st.booleans()):
            archived_at[identity] = NOW - draw(st.floats(0, 60)) * DAY
    for number in range(draw(st.integers(0, 2))):           # records with no session id
        copies.append(prune.Copy(folder=draw(st.sampled_from(folders)),
                                 name=f"local_empty{number}.json", identity="",
                                 activity_ms=ms(draw(st.floats(0, 60)))))
    rules = prune.Rules(skip_switch_folders=draw(st.booleans()),
                        archive_days=draw(st.sampled_from((0.0, 1.0, 7.0, 14.0, 30.0))),
                        dead_days=draw(st.sampled_from((0.0, 1.0, 7.0, 30.0))),
                        prune=draw(st.booleans()) or draw(st.booleans()))
    situation = prune.Situation(folders=folders, kinds=kinds, home=home, loaded=loaded,
                                openable=frozenset(openable), archived_at=archived_at, now=NOW,
                                switch_settled=draw(st.booleans()) or draw(st.booleans()))
    return copies, situation, rules, frozenset(held)


def planned(copies, situation):
    """The copies a plan considers: with a session id, in a listed folder."""
    listed = set(situation.folders)
    return [copy for copy in copies if copy.identity and copy.folder in listed]


def by_session(copies):
    groups = {}
    for copy in copies:
        groups.setdefault(copy.identity, []).append(copy)
    return groups


def apply(copies, decided):
    gone = {(item.folder, item.name) for item in decided.removals}
    return [copy for copy in copies if (copy.folder, copy.name) not in gone]


SETTINGS = settings(max_examples=400, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(worlds())
def test_never_the_only_copy_and_the_newest_by_either_measure_survives(world):
    """C-23.28a: every session keeps a copy, its newest copy by activity and
    its newest by the spread's ranking; copies the plan does not consider
    (no session id, an unlisted folder) are never removed."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    removed = {(item.folder, item.name) for item in decided.removals}
    considered = {(copy.folder, copy.name) for copy in planned(copies, situation)}
    assert removed <= considered
    before = by_session(planned(copies, situation))
    after = by_session(planned(apply(copies, decided), situation))
    for identity, group in before.items():
        assert after.get(identity), f"{identity} lost its last copy"
        for measure in (lambda copy: copy.activity_ms, lambda copy: copy.rank_ms):
            assert (max(measure(copy) for copy in after[identity])
                    == max(measure(copy) for copy in group)), f"{identity} lost a newest copy"


@SETTINGS
@given(worlds())
def test_a_newest_copy_outside_the_scope_is_kept_only_when_needed(world):
    """C-23.28a: minimal. A survivor outside the scope is the newest by some
    measure that no copy in scope reaches."""
    copies, situation, rules, _held = world
    for identity, group in by_session(planned(copies, situation)).items():
        inside = prune.scope(identity, group, situation, rules).folders
        within = [copy for copy in group if copy.folder in inside]
        extra = [copy for copy in prune.survivors(group, inside, situation)
                 if copy.folder not in inside]
        assert len(extra) <= 2
        for copy in extra:
            reaches = [measure for measure in (lambda item: item.activity_ms,
                                               lambda item: item.rank_ms)
                       if measure(copy) == max(measure(item) for item in group)
                       and not any(measure(item) >= measure(copy) for item in within)]
            assert reaches, f"{identity}: {copy.folder} kept without being a needed newest"


@SETTINGS
@given(worlds())
def test_hands_off_the_loaded_folder_the_home_and_unsettled_sessions(world):
    """C-23.28a: the loaded folder and the home are never touched, and nothing
    is removed while the load is unknown, removal is off, or flags are
    unsettled; a live session's switch-folder copies wait for the registry."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    for removal in decided.removals:
        assert removal.folder != situation.loaded
        assert removal.folder != situation.home
        assert removal.identity not in held
        if removal.reason == prune.SWITCH_FOLDER:
            assert situation.switch_settled
    if situation.loaded is None or not rules.prune:
        assert not decided.removals
    assert not prune.plan(copies, situation, rules, held=held,
                          blocked="the inventory is incomplete this pass").removals


@SETTINGS
@given(worlds())
def test_no_churn_what_is_removed_stays_out_of_scope(world):
    """C-23.28a: a removed copy is outside its session's scope before and after
    the plan is applied, so neither pass spreads it back."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    before = by_session(planned(copies, situation))
    after = by_session(planned(apply(copies, decided), situation))
    for removal in decided.removals:
        for group in (before, after):
            placed = prune.scope(removal.identity, group[removal.identity], situation, rules)
            assert removal.folder not in placed.folders


@SETTINGS
@given(worlds())
def test_a_plan_applied_leaves_nothing_to_remove(world):
    """C-23.28a: idempotence. The next pass over what a plan left removes nothing."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    again = prune.plan(apply(copies, decided), situation, rules, held=held)
    assert again.removals == []


@SETTINGS
@given(worlds())
def test_every_copy_outside_its_scope_is_accounted_for(world):
    """C-23.28a: outside = the newest kept + held + removed; the holds the
    construction makes unreachable never occur; and each removal names
    keepers that survive, hold the session, and are at least as new by
    both measures."""
    copies, situation, rules, held = world
    decided = prune.plan(copies, situation, rules, held=held)
    assert (sum(decided.outside.values())
            == sum(decided.kept.values()) + sum(decided.held.values()) + len(decided.removals))
    assert prune.HELD_HOME not in decided.held
    assert prune.HELD_NO_SURVIVOR not in decided.held
    left = {(copy.folder, copy.name): copy for copy in apply(copies, decided)}
    for removal in decided.removals:
        keepers = [left[keeper] for keeper in removal.keepers]
        assert all(keeper.identity == removal.identity for keeper in keepers)
        assert max(keeper.activity_ms for keeper in keepers) >= removal.activity_ms
        assert max(keeper.rank_ms for keeper in keepers) >= removal.rank_ms
    assert sum(decided.sessions.values()) == len(by_session(planned(copies, situation)))


@SETTINGS
@given(worlds(), st.sampled_from((1.0, 7.0, 30.0)))
def test_longer_thresholds_never_remove_more(world, extra):
    """C-23.28a: monotone. Raising either threshold, seeding switch folders,
    or the registry still learning, can only keep more."""
    copies, situation, rules, held = world
    removed = {(item.folder, item.name) for item in
               prune.plan(copies, situation, rules, held=held).removals}
    stricter = []
    if rules.archive_days:
        stricter.append(replace(rules, archive_days=rules.archive_days + extra))
    if rules.dead_days:
        stricter.append(replace(rules, dead_days=rules.dead_days + extra))
    stricter.append(replace(rules, skip_switch_folders=False))
    for other in stricter:
        fewer = {(item.folder, item.name) for item in
                 prune.plan(copies, situation, other, held=held).removals}
        assert fewer <= removed
    learning = {(item.folder, item.name) for item in
                prune.plan(copies, replace(situation, switch_settled=False), rules,
                           held=held).removals}
    assert learning <= removed


@SETTINGS
@given(worlds())
def test_with_every_rule_off_every_session_belongs_everywhere(world):
    """C-23.28a: off is off. The mirror spreads and keeps as it did before."""
    copies, situation, rules, held = world
    off = prune.Rules(prune=rules.prune)
    decided = prune.plan(copies, situation, off, held=held)
    assert not decided.removals and not decided.outside
    for identity, group in by_session(planned(copies, situation)).items():
        placed = prune.scope(identity, group, situation, off)
        assert placed.folders == frozenset(situation.folders)


@SETTINGS
@given(worlds())
def test_scope_is_one_of_three_shapes(world):
    """C-23.28a: a live session belongs in every seeded folder, an old archived
    one in the home only, an old dead one in the home only (or nowhere)."""
    copies, situation, rules, _held = world
    seeded = frozenset(folder for folder in situation.folders
                       if prune.seeded(folder, situation, rules))
    for identity, group in by_session(planned(copies, situation)).items():
        placed = prune.scope(identity, group, situation, rules)
        if placed.kind in ("live", "dead-young"):
            assert placed.folders == seeded
        else:
            assert placed.folders == (frozenset({situation.home}) if situation.home
                                      else frozenset())
        if placed.kind == "archived":
            assert all(copy.archived for copy in group)
            assert not any(copy.starred for copy in group)
            assert identity in situation.openable
            assert situation.now - prune.quiet_since(identity, group, situation) \
                >= rules.archive_days * DAY > 0
        if placed.kind == "dead":
            assert identity not in situation.openable and rules.dead_days > 0
    for folder in situation.folders:
        if situation.kinds.get(folder) == SWITCH and rules.skip_switch_folders:
            assert folder not in seeded


@SETTINGS
@given(worlds())
def test_the_sessions_left_unplanned_have_nothing_outside_their_scope(world):
    """C-23.28a: the mirror plans only sessions `may_reach_outside` names, and
    decides every scope from a `Summary`; skipping the others loses nothing,
    and the summary decides as the copies do."""
    copies, situation, rules, held = world
    for identity, group in by_session(planned(copies, situation)).items():
        summary = prune.summarize(group, situation)
        placed = prune.scope_of(identity, summary, situation, rules)
        assert placed == prune.scope(identity, group, situation, rules)
        if not prune.may_reach_outside(placed, summary, rules):
            assert not prune.plan(group, situation, rules, held=held).outside


# --- examples -----------------------------------------------------------------

def situation(**changes) -> prune.Situation:
    base = prune.Situation(
        folders=("home/h", "work/w", "other/o", "work/h"),
        kinds={"home/h": SETTLED, "work/w": SETTLED, "other/o": UNKNOWN, "work/h": SWITCH},
        home="home/h", loaded="work/w", openable=frozenset({"s"}), archived_at={}, now=NOW)
    return replace(base, **changes)


def copy(folder: str, *, days: float = 30, archived: bool = True, starred: bool = False,
         written: float | None = None, focused: float | None = None) -> prune.Copy:
    rank = ms(days)
    activity = max(rank, ms(focused)) if focused is not None else rank
    return prune.Copy(folder=folder, name="local_s.json", identity="s", archived=archived,
                      starred=starred, activity_ms=activity, rank_ms=rank,
                      mtime=NOW - (written if written is not None else days) * DAY)


RULES = prune.Rules(skip_switch_folders=True, archive_days=14, dead_days=7, prune=True)


def test_an_old_archived_session_keeps_its_home_and_the_loaded_copy():
    """C-23.28a: the example the rule exists for. Quiet 30 days and archived in
    every account: the home keeps it, the loaded folder keeps it until the
    app leaves it, and the other copies go."""
    copies = [copy(folder) for folder in situation().folders]
    decided = prune.plan(copies, situation(), RULES)
    assert {item.folder for item in decided.removals} == {"other/o", "work/h"}
    assert decided.held == {prune.HELD_LOADED: 1}
    assert all(item.keepers == (("home/h", "local_s.json"),) for item in decided.removals)


def test_the_archive_stamp_starts_the_clock_and_a_re_save_does_not():
    """C-23.28a: archiving leaves the activity times alone (of 850 archived
    records the app wrote on 2026-09-22, 621 were 30 days quiet), so the merge
    base's archive stamp starts the clock. The app also re-saves records it
    holds (191 archived ones in the loaded folder within a day on
    2026-09-25), so a fresh file write alone does not."""
    quiet = [copy(folder, days=40) for folder in situation().folders]
    stamped = situation(archived_at={"s": NOW - 3 * DAY})
    assert prune.scope("s", quiet, stamped, RULES).kind == "live"
    assert [item.folder for item in prune.plan(quiet, stamped, RULES).removals] == ["work/h"], \
        "live: only the switch folder's copy goes"
    resaved = [copy(folder, days=40, written=0.5) for folder in situation().folders]
    assert prune.scope("s", resaved, situation(archived_at={"s": NOW - 20 * DAY}),
                       RULES).kind == "archived"
    focused = [copy(folder, days=2) for folder in situation().folders]
    assert prune.scope("s", focused, situation(archived_at={"s": NOW - 20 * DAY}),
                       RULES).kind == "live", "looking at a session is activity"


def test_a_newer_copy_outside_the_home_is_kept_beside_it():
    """C-23.28a: copies of one session differ in activity; when the home's is
    older, the newest stays too, and only the rest go."""
    copies = [copy("home/h", days=40), copy("work/w", days=30), copy("other/o", days=20),
              copy("work/h", days=35)]
    decided = prune.plan(copies, situation(loaded="work/w"), RULES)
    assert decided.kept == {prune.ARCHIVED: 1}
    assert {item.folder for item in decided.removals} == {"work/h"}
    assert ("other/o", "local_s.json") in decided.removals[0].keepers


def test_the_copy_where_the_work_happened_outlives_a_later_focus_elsewhere():
    """C-23.28a, review r1 P1-2: the copy with the newest `lastActivityAt`
    carries the newest record (a moved cwd, say); a mere focus in another
    account makes that copy newest by focus alone. Both stay."""
    worked = copy("other/o", days=20)                    # the work happened here
    looked = copy("work/h", days=40, focused=16)         # only clicked on here, later
    copies = [copy("home/h", days=40), worked, looked]
    decided = prune.plan(copies, situation(loaded="work/w"), RULES)
    assert decided.removals == []
    assert decided.kept == {prune.ARCHIVED: 2}


def test_a_switch_folders_copies_wait_while_the_registry_learns():
    """C-23.28a, review r1 P1-4: a folder used less often than the registry
    has learned looks like a switch folder, so a live session's copies there
    stay until the registry has learned `SWITCH_PRUNE_AFTER_S`."""
    copies = [copy(folder, archived=False, days=1) for folder in situation().folders]
    learning = situation(switch_settled=False)
    assert prune.plan(copies, learning, RULES).held == {prune.HELD_LEARNING: 1}
    assert [item.folder for item in prune.plan(copies, situation(), RULES).removals] == ["work/h"]


@pytest.mark.parametrize("change", [
    {"archived": False}, {"starred": True}, {"days": 3},
])
def test_what_keeps_an_archived_session_everywhere(change):
    """C-23.28a: unarchived, starred, or quiet less than the rule: live."""
    copies = [copy(folder, **change) for folder in situation().folders]
    placed = prune.scope("s", copies, situation(), RULES)
    assert placed.kind == "live"
    assert placed.folders == frozenset({"home/h", "work/w", "other/o"})
    assert {item.folder for item in prune.plan(copies, situation(), RULES).removals} == {"work/h"}


def test_without_a_home_an_archived_session_stays_everywhere():
    """C-23.28a: the archived rule needs a home; without one it does nothing."""
    copies = [copy(folder) for folder in situation().folders]
    assert prune.scope("s", copies, situation(home=None), RULES).kind == "live"


def test_an_old_dead_session_keeps_its_newest_copy():
    """C-23.28a: a dead session (no transcript) is never spread; once quiet
    `mirror_dead_days` it keeps the home's copy, or else the newest."""
    dead = situation(openable=frozenset(), home=None)
    copies = [copy("home/h", days=20), copy("work/w", days=10), copy("other/o", days=30)]
    decided = prune.plan(copies, replace(dead, loaded="work/h"), RULES)
    assert {item.folder for item in decided.removals} == {"home/h", "other/o"}
    young = [copy("home/h", days=2), copy("other/o", days=3)]
    assert prune.scope("s", young, dead, RULES).kind == "dead-young"
