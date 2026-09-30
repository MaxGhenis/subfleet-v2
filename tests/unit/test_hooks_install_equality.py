"""`daemon install --hooks` and `doctor`: when is v2's entry installed? (C-23.25)

Every test names the clause it proves (C-20.5). The rule: for each of the four
events, v2's entry is installed when the event's list holds it exactly once and
equal (as JSON) to what `--hooks` would write, at any position. Drift, and
therefore a rewrite, is exactly: missing, duplicated, or differs. A rewrite
keeps every other tool's entry and its order, and puts v2's one entry where its
first entry was (last when there was none).

The properties below hold for every generated settings file, not one example:

- installed-anywhere: any permutation of a list holding v2's entry exactly once
  is no drift, `doctor` passes, and install writes nothing (no backup either);
- drift iff: an event drifts exactly when its v2 entries are not one exact
  entry, and `drift` names why (`missing`, `duplicated`, `differs`);
- preservation: after install, the other tools' entries of every event are
  exactly what they were, in the same order, and every key outside `hooks` and
  every event install does not own is untouched;
- placement: the one entry lands where v2's first entry was, or last;
- idempotence: installing twice leaves the file as installing once did.

Nothing here reads or writes the real `~/.claude/settings.json`: every file is
a temporary one, and `SUBFLEET_CLAUDE_SETTINGS` is pointed at it where a test
goes through the CLI or `doctor`.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import cli, doctor, hooks

#: A command with the default spelling's `subfleet hook` marker, as the live
#: file's `/Users/<user>/bin/subfleet hook` has.
CMD = "/home/user/bin/subfleet hook"
TIMEOUT = 600
EVENTS = ("PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse")
DESIRED = hooks.desired_groups(CMD, TIMEOUT)
#: Other tools' commands: none ends in `hook <Event>` or carries `subfleet hook`,
#: so none is v2's (`hooks._is_ours`). v1's entry is here on purpose: install
#: never owns it.
FOREIGN_COMMANDS = (
    "/home/user/.claude/hooks/mask-credentials.py",
    "/home/user/.claude/hooks/guard-never-rules.sh",
    'node "/home/user/.claude/hooks/gitnexus/gitnexus-hook.cjs"',
    "/home/user/.claude/hooks/check-model-downgrade.sh",
    "~/cos/subfleet/bin/subfleet-hook pre-bash",
    "~/cos/subfleet/bin/subfleet-hook session-start",
    "/usr/local/bin/other-tool hook",
)
HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    """`doctor` and the CLI resolve the command and timeout from the
    environment; pin both so the file under test is the only input."""
    monkeypatch.setenv("SUBFLEET_HOOK_COMMAND", CMD)
    monkeypatch.delenv("SUBFLEET_HOOK_TIMEOUT_S", raising=False)


def ours(event: str) -> dict:
    return copy.deepcopy(DESIRED[event])


def foreign(command: str, matcher: str | None = "*", **extra) -> dict:
    group: dict = {"hooks": [{"type": "command", "command": command, **extra}]}
    return group if matcher is None else {"matcher": matcher, **group}


def live_shape() -> dict:
    """The shape of the 2026-09-28 file doctor failed on: v2's PreToolUse and
    PostToolUse entries before a `mask-credentials.py` entry another installer
    appended, and the PostToolUse entry spelled `timeout` before `asyncRewake`."""
    mask = foreign("/home/user/.claude/hooks/mask-credentials.py")
    post = ours("PostToolUse")
    post["hooks"][0] = {"type": "command", "command": f"{CMD} PostToolUse",
                        "timeout": TIMEOUT, "asyncRewake": True}
    return {
        "model": "claude-opus-5-5",
        "hooks": {
            "PreToolUse": [
                foreign("/home/user/.claude/hooks/guard-never-rules.sh", "Bash"),
                foreign('node "/home/user/.claude/hooks/gitnexus/gitnexus-hook.cjs"',
                        "Grep|Glob", timeout=10),
                ours("PreToolUse"), copy.deepcopy(mask)],
            "PreCompact": [foreign("/home/user/.claude/hooks/auto-commit-wip.sh", None)],
            "PostToolUse": [
                foreign("/home/user/.claude/hooks/check-model-downgrade.sh", timeout=10),
                post, copy.deepcopy(mask)],
            "UserPromptSubmit": [
                foreign("/home/user/.claude/hooks/check-model-downgrade.sh", None),
                ours("UserPromptSubmit")],
            "SessionStart": [
                foreign("/home/user/.claude/hooks/ensure-plugin-manifests.sh", None),
                ours("SessionStart")],
        },
    }


def write(directory: Path, data: dict) -> Path:
    path = directory / "settings.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


# --- examples -----------------------------------------------------------------

def test_reordered_equal_entries_are_installed_and_nothing_is_written(tmp_path):
    """C-23.25 v2's entries after, before or between other tools' entries are
    installed: no drift, doctor passes, install writes no file and no backup."""
    path = write(tmp_path, live_shape())
    before = path.read_bytes()
    report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
    assert report["changed_events"] == [] and report["drift"] == {}
    assert report["diff"] == ""
    assert hooks.installed(path, command=CMD, timeout=TIMEOUT)["matches"] is True
    assert doctor.check_hook_entries(path)["status"] == doctor.PASS
    assert hooks.apply(path, command=CMD, timeout=TIMEOUT)["written"] is False
    assert path.read_bytes() == before and backups(path) == []


def test_the_cli_says_it_already_matches(tmp_path, monkeypatch, capsys):
    """C-23.25 `daemon install --hooks` on a reordered file prints no diff."""
    path = write(tmp_path, live_shape())
    before = path.read_bytes()
    monkeypatch.setenv(hooks.SETTINGS_ENV, str(path))
    assert cli.main(["daemon", "install", "--hooks"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "already matches" in captured.err
    assert path.read_bytes() == before and backups(path) == []


def test_a_missing_entry_is_drift_and_is_appended(tmp_path, monkeypatch, capsys):
    """C-23.25 a missing entry is drift; install appends it last and leaves
    every other entry where it was."""
    data = live_shape()
    data["hooks"]["PostToolUse"] = [g for g in data["hooks"]["PostToolUse"]
                                    if not hooks._holds_ours(g, "PostToolUse", CMD)]
    others = copy.deepcopy(data["hooks"]["PostToolUse"])
    path = write(tmp_path, data)
    report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
    assert report["changed_events"] == ["PostToolUse"]
    assert report["drift"] == {"PostToolUse": "missing"}
    item = doctor.check_hook_entries(path)
    assert item["status"] == doctor.FAIL and "PostToolUse (missing)" in item["detail"]
    monkeypatch.setenv(hooks.SETTINGS_ENV, str(path))
    assert cli.main(["daemon", "install", "--hooks", "--dry-run"]) == 0
    assert "(PostToolUse: missing)" in capsys.readouterr().err
    written = hooks.apply(path, command=CMD, timeout=TIMEOUT)
    assert written["written"] is True and len(backups(path)) == 1
    after = json.loads(path.read_text())
    assert after["hooks"]["PostToolUse"] == [*others, ours("PostToolUse")]
    for event in ("PreToolUse", "PreCompact", "UserPromptSubmit", "SessionStart"):
        assert after["hooks"][event] == data["hooks"][event]


def _timeout(group: dict) -> None:
    group["hooks"][0]["timeout"] = 300


def _no_rewake(group: dict) -> None:
    del group["hooks"][0]["asyncRewake"]


def _rewake_one(group: dict) -> None:
    group["hooks"][0]["asyncRewake"] = 1          # truthy, but not `true`


def _matcher(group: dict) -> None:
    group["matcher"] = "*"


def _extra_key(group: dict) -> None:
    group["hooks"][0]["statusMessage"] = "delivering notices"


def _moved(group: dict) -> None:
    group["hooks"][0]["command"] = "/old/venv/bin/python -m subfleet hook PostToolUse"


@pytest.mark.parametrize("change", [_timeout, _no_rewake, _rewake_one, _matcher,
                                    _extra_key, _moved])
def test_changed_content_is_drift_and_is_replaced_in_place(tmp_path, change):
    """C-23.25 an entry that differs from what `--hooks` writes is drift; install
    replaces it where it stands, so the other entries neither move nor change."""
    data = live_shape()
    change(data["hooks"]["PostToolUse"][1])
    path = write(tmp_path, data)
    report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
    assert report["changed_events"] == ["PostToolUse"]
    assert report["drift"] == {"PostToolUse": "differs"}
    item = doctor.check_hook_entries(path)
    assert item["status"] == doctor.FAIL and "PostToolUse (differs)" in item["detail"]
    assert hooks.apply(path, command=CMD, timeout=TIMEOUT)["written"] is True
    after = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert after == [data["hooks"]["PostToolUse"][0], ours("PostToolUse"),
                     data["hooks"]["PostToolUse"][2]]


def test_an_entry_shared_with_another_tools_hook_differs(tmp_path):
    """C-23.25 v2's hook inside another tool's group is not v2's entry as
    written: drift, and install splits it out without touching the other hook."""
    data = live_shape()
    data["hooks"]["PreToolUse"] = [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/home/user/.claude/hooks/guard-never-rules.sh"},
            ours("PreToolUse")["hooks"][0]]}]
    path = write(tmp_path, data)
    assert hooks.plan(path, command=CMD, timeout=TIMEOUT)["drift"] == {
        "PreToolUse": "differs"}
    hooks.apply(path, command=CMD, timeout=TIMEOUT)
    assert json.loads(path.read_text())["hooks"]["PreToolUse"] == [
        ours("PreToolUse"),
        foreign("/home/user/.claude/hooks/guard-never-rules.sh", "Bash")]


def test_a_duplicate_is_drift_and_install_keeps_the_first(tmp_path):
    """C-23.25 two equal entries would deliver every notice twice: drift, and
    install keeps one, at the first one's place."""
    data = live_shape()
    data["hooks"]["PreToolUse"].append(ours("PreToolUse"))
    path = write(tmp_path, data)
    report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
    assert report["changed_events"] == ["PreToolUse"]
    assert report["drift"] == {"PreToolUse": "duplicated"}
    item = doctor.check_hook_entries(path)
    assert item["status"] == doctor.FAIL and "PreToolUse (duplicated)" in item["detail"]
    hooks.apply(path, command=CMD, timeout=TIMEOUT)
    assert json.loads(path.read_text())["hooks"]["PreToolUse"] == live_shape()[
        "hooks"]["PreToolUse"]


def test_json_numbers_compare_as_numbers(tmp_path):
    """C-23.25 JSON has one number type: `600.0` is the value `--hooks` writes,
    while `1` for `true` is not (the `asyncRewake: 1` case above)."""
    data = live_shape()
    data["hooks"]["PostToolUse"][1]["hooks"][0]["timeout"] = 600.0
    path = write(tmp_path, data)
    assert hooks.plan(path, command=CMD, timeout=TIMEOUT)["changed_events"] == []


def test_an_unchanged_event_keeps_its_own_spelling_when_another_is_rewritten(tmp_path):
    """C-23.25 an event without drift is copied, not re-rendered: its key order
    survives a rewrite that another event's drift causes."""
    data = live_shape()
    data["hooks"]["SessionStart"] = [data["hooks"]["SessionStart"][0]]   # missing
    path = write(tmp_path, data)
    hooks.apply(path, command=CMD, timeout=TIMEOUT)
    entry = json.loads(path.read_text())["hooks"]["PostToolUse"][1]["hooks"][0]
    assert list(entry) == ["type", "command", "timeout", "asyncRewake"]


# --- properties ---------------------------------------------------------------

def foreign_hooks() -> st.SearchStrategy[dict]:
    return st.builds(
        lambda command, extra: {"type": "command", "command": command, **extra},
        st.sampled_from(FOREIGN_COMMANDS),
        st.fixed_dictionaries({}, optional={
            "timeout": st.integers(1, 900),
            "statusMessage": st.sampled_from(["Checking...", ""]),
            "asyncRewake": st.booleans()}))


def foreign_groups() -> st.SearchStrategy:
    """Another tool's group, including the malformed shapes `_strip_ours` keeps
    as they are: an empty `hooks` list, no `hooks` key, a non-object."""
    return st.one_of(
        st.builds(lambda matcher, hooks_: {**matcher, "hooks": hooks_},
                  st.sampled_from([{}, {"matcher": "*"}, {"matcher": "Bash"},
                                   {"matcher": "Edit|Write"}]),
                  st.lists(foreign_hooks(), min_size=1, max_size=3)),
        st.just({"matcher": "Bash", "hooks": []}),
        st.just({"matcher": "Bash"}),
        st.just("not a group"))


def exact(event: str) -> st.SearchStrategy[dict]:
    """v2's entry as `--hooks` writes it, keys in either order."""
    def reversed_keys(group: dict) -> dict:
        flipped = {key: group[key] for key in reversed(group)}
        flipped["hooks"] = [{key: hook[key] for key in reversed(hook)}
                            for hook in group["hooks"]]
        return flipped
    return st.sampled_from([ours(event), reversed_keys(ours(event))])


def mutated(event: str) -> st.SearchStrategy[dict]:
    """v2's entry, still recognisably v2's, that differs from what `--hooks`
    writes in exactly one way."""
    base = DESIRED[event]
    hook = base["hooks"][0]

    def with_hook(**changes) -> dict:
        group = ours(event)
        for key, value in changes.items():
            if value is None:
                group["hooks"][0].pop(key, None)
            else:
                group["hooks"][0][key] = value
        return group

    def with_matcher() -> dict:
        group = ours(event)
        if "matcher" in group:
            del group["matcher"]
        else:
            group["matcher"] = "startup"
        return group

    rewake = [value for value in (None, True, False, 1)
              if not hooks._same(value, hook.get("asyncRewake"))]
    return st.one_of(
        st.integers(1, 5000).filter(lambda n: n != hook["timeout"]).map(
            lambda n: with_hook(timeout=n)),
        st.sampled_from(rewake).map(lambda value: with_hook(asyncRewake=value)),
        st.just(None).map(lambda _: with_matcher()),
        st.just(None).map(lambda _: with_hook(statusMessage="notices")),
        st.sampled_from([f"/moved/sf hook {event}",
                         f"/old/venv/bin/python -m subfleet hook {event}"]).map(
            lambda command: with_hook(command=command)))


@st.composite
def event_list(draw, event: str):
    """One event's list as tokens, so the expected result is computed from how
    the list was built rather than by the code under test.

    Tokens: ("foreign", group), ("exact", group), ("mutated", group), and
    ("shared", group, remainder): v2's hook inside another tool's group, where
    `remainder` is that group without it. None means the event is absent.
    """
    if draw(st.integers(0, 9)) == 0:
        return None
    tokens: list[tuple] = [("foreign", group)
                           for group in draw(st.lists(foreign_groups(), max_size=4))]
    for _ in range(draw(st.integers(0, 3))):
        kind = draw(st.sampled_from(["exact", "mutated", "shared"]))
        if kind == "exact":
            token: tuple = ("exact", draw(exact(event)))
        elif kind == "mutated":
            token = ("mutated", draw(mutated(event)))
        else:
            remainder = {"matcher": "Bash",
                         "hooks": draw(st.lists(foreign_hooks(), min_size=1, max_size=2))}
            hooks_ = list(remainder["hooks"])
            hooks_.insert(draw(st.integers(0, len(hooks_))), ours(event)["hooks"][0])
            token = ("shared", {**remainder, "hooks": hooks_}, remainder)
        tokens.insert(draw(st.integers(0, len(tokens))), token)
    return tokens


def expected(tokens: list[tuple] | None, event: str, *, remove: bool = False):
    """(drift reason or None, the event's list after install or uninstall)."""
    tokens = tokens or []
    others: list = []
    at = None
    for token in tokens:
        if token[0] != "foreign" and at is None:
            at = len(others)
        if token[0] == "foreign":
            others.append(token[1])
        elif token[0] == "shared":
            others.append(token[2])
    v2 = [token for token in tokens if token[0] != "foreign"]
    reason = ("missing" if not v2 else "duplicated" if len(v2) > 1
              else None if v2[0][0] == "exact" else "differs")
    if remove:
        return reason, others
    after = list(others)
    after.insert(len(after) if at is None else at, DESIRED[event])
    return reason, after


@st.composite
def settings_files(draw):
    """A settings file: the four events as token lists, plus an event and a
    top-level key install does not own."""
    lists = {event: draw(event_list(event)) for event in EVENTS}
    data: dict = {"model": "claude-opus-5-5", "hooks": {
        "Stop": [foreign("/home/user/.claude/hooks/warn-uncommitted.sh", None)]}}
    for event in draw(st.permutations(EVENTS)):
        if lists[event] is not None:
            data["hooks"][event] = [token[1] for token in lists[event]]
    return lists, data


@settings(max_examples=150, deadline=None, suppress_health_check=HEALTH)
@given(st.data())
def test_any_permutation_holding_the_entry_once_is_installed(data):
    """C-23.25 for any permutation of any list that holds v2's exact entry once
    (in every event), the check reports no drift and install is a no-op."""
    with tempfile.TemporaryDirectory() as directory:
        hooks_: dict = {"Stop": [foreign("/usr/local/bin/stop-hook", None)]}
        for event in EVENTS:
            entries = data.draw(st.lists(foreign_groups(), max_size=5))
            hooks_[event] = data.draw(st.permutations(
                [*entries, data.draw(exact(event))]))
        path = write(Path(directory), {"model": "x", "hooks": hooks_})
        before = path.read_bytes()
        report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
        assert report["changed_events"] == [] and report["drift"] == {}
        assert hooks.installed(path, command=CMD, timeout=TIMEOUT)["matches"] is True
        assert doctor.check_hook_entries(path)["status"] == doctor.PASS
        assert hooks.apply(path, command=CMD, timeout=TIMEOUT)["written"] is False
        assert path.read_bytes() == before and backups(path) == []


@settings(max_examples=150, deadline=None, suppress_health_check=HEALTH)
@given(settings_files())
def test_install_drifts_exactly_when_it_should_and_preserves_the_rest(case):
    """C-23.25 drift iff v2's entries are not one exact entry, with the reason;
    install keeps every other entry in order, places v2's one entry where its
    first was (or last), touches nothing it does not own, and is idempotent."""
    lists, data = case
    with tempfile.TemporaryDirectory() as directory:
        path = write(Path(directory), data)
        report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
        want = {event: expected(lists[event], event) for event in EVENTS}
        assert report["drift"] == {event: reason for event, (reason, _) in want.items()
                                   if reason is not None}
        assert set(report["changed_events"]) == set(report["drift"])
        item = doctor.check_hook_entries(path)
        assert item["status"] == (doctor.FAIL if report["drift"] else doctor.PASS)

        once = hooks.apply(path, command=CMD, timeout=TIMEOUT)
        assert once["written"] is bool(report["drift"])
        assert len(backups(path)) == int(bool(report["drift"]))
        after = json.loads(path.read_text())
        assert {key: value for key, value in after.items() if key != "hooks"} == {
            key: value for key, value in data.items() if key != "hooks"}
        assert after["hooks"]["Stop"] == data["hooks"]["Stop"]
        for event in EVENTS:
            assert hooks._same(after["hooks"][event], want[event][1]), event

        snapshot = path.read_bytes()
        twice = hooks.apply(path, command=CMD, timeout=TIMEOUT)
        assert twice["written"] is False and path.read_bytes() == snapshot
        assert hooks.plan(path, command=CMD, timeout=TIMEOUT)["changed_events"] == []
        assert len(backups(path)) == int(bool(report["drift"]))


@settings(max_examples=200, deadline=None, suppress_health_check=HEALTH)
@given(st.data())
def test_any_single_change_to_the_entry_is_drift_and_is_replaced_in_place(data):
    """C-23.25 for any one-way change to v2's entry (timeout, `asyncRewake`,
    matcher, an extra key, an old command path), at any position among any
    other entries: drift `differs`, and install puts the entry as written at
    the same position, leaving the other entries as they were."""
    event = data.draw(st.sampled_from(EVENTS))
    entries = data.draw(st.lists(foreign_groups(), max_size=5))
    at = data.draw(st.integers(0, len(entries)))
    groups = list(entries)
    groups.insert(at, data.draw(mutated(event)))
    hooks_ = {name: [ours(name)] for name in EVENTS}
    hooks_[event] = groups
    with tempfile.TemporaryDirectory() as directory:
        path = write(Path(directory), {"hooks": hooks_})
        report = hooks.plan(path, command=CMD, timeout=TIMEOUT)
        assert report["drift"] == {event: "differs"}
        assert report["changed_events"] == [event]
        assert hooks.apply(path, command=CMD, timeout=TIMEOUT)["written"] is True
        want = list(entries)
        want.insert(at, DESIRED[event])
        assert hooks._same(json.loads(path.read_text())["hooks"][event], want)


@settings(max_examples=80, deadline=None, suppress_health_check=HEALTH)
@given(settings_files())
def test_uninstall_removes_only_v2s_entries_in_place(case):
    """C-23.25 `--remove` takes out every v2 entry and nothing else; the other
    entries keep their order, and a second removal changes nothing."""
    lists, data = case
    with tempfile.TemporaryDirectory() as directory:
        path = write(Path(directory), data)
        report = hooks.plan(path, command=CMD, timeout=TIMEOUT, remove=True)
        present = {event for event in EVENTS
                   if lists[event] and any(t[0] != "foreign" for t in lists[event])}
        assert set(report["changed_events"]) == present and report["drift"] == {}
        hooks.apply(path, command=CMD, timeout=TIMEOUT, remove=True)
        after = json.loads(path.read_text())
        for event in EVENTS:
            if lists[event] is None:
                assert event not in after["hooks"]
            else:
                assert hooks._same(after["hooks"][event],
                                   expected(lists[event], event, remove=True)[1])
        assert hooks.plan(path, command=CMD, timeout=TIMEOUT,
                          remove=True)["changed_events"] == []


JSON_LEAVES = st.one_of(st.none(), st.booleans(), st.integers(-3, 3),
                        st.sampled_from([0.0, 1.0, -1.0, 0.5, 600.0]),
                        st.sampled_from(["", "a", "1", "true"]))
JSON_VALUES = st.recursive(
    JSON_LEAVES,
    lambda inner: st.one_of(st.lists(inner, max_size=3),
                            st.dictionaries(st.sampled_from("abc"), inner, max_size=3)),
    max_leaves=12)
#: What a leaf is most easily confused with: `true` and `1`, `1` and `1.0`,
#: `"1"` and `1`, `null` and `false`.
CONFUSABLE = {True: [1, 1.0, "true"], False: [0, 0.0, None], None: [False, 0, ""],
              "1": [1], "true": [True]}


@st.composite
def near(draw, value):
    """`value` with some leaves swapped for a confusable one, an item dropped,
    or a key renamed: the pairs `_same` must tell apart, and some it must not."""
    if isinstance(value, list):
        items = [draw(near(item)) for item in value]
        if items and draw(st.integers(0, 5)) == 0:
            del items[draw(st.integers(0, len(items) - 1))]
        return items
    if isinstance(value, dict):
        out = {key: draw(near(item)) for key, item in value.items()}
        if out and draw(st.integers(0, 5)) == 0:
            key = draw(st.sampled_from(sorted(out)))
            out[key.upper()] = out.pop(key)
        return out
    if draw(st.integers(0, 2)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return draw(st.sampled_from([float(value), int(value), value == 1, str(value)]))
    return draw(st.sampled_from(CONFUSABLE.get(value, [value])))


@settings(max_examples=300, deadline=None)
@given(JSON_VALUES, st.data())
def test_same_is_json_value_equality(value, data):
    """C-23.25 `_same` agrees with equality of the values the JSON text denotes:
    it is reflexive and symmetric, survives a JSON round trip, tells `true`
    from `1` and `null` from `false`, and holds `1` and `1.0` equal."""
    other = data.draw(st.one_of(near(value), JSON_VALUES))
    assert hooks._same(value, value)
    assert hooks._same(value, json.loads(json.dumps(value)))
    assert hooks._same(value, other) == hooks._same(other, value)
    assert hooks._same(value, other) == (canonical(value) == canonical(other))


def canonical(value):
    """A reference encoding of a JSON value: booleans tagged apart from
    numbers, numbers by value, objects by sorted key."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, list):
        return ("array", tuple(canonical(item) for item in value))
    if isinstance(value, dict):
        return ("object", tuple(sorted((key, canonical(item))
                                       for key, item in value.items())))
    return (type(value).__name__, value)
