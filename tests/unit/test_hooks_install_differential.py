"""C-23.25: agree with release/217 wherever its old installer was right."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from hypothesis import example, given, settings
from hypothesis import strategies as st

from subfleet import hooks as hooks_module
from tests.unit.test_hooks_install_equality import (
    CMD,
    EVENTS,
    JSON_VALUES,
    TIMEOUT,
    canonical,
    exact,
    foreign,
    foreign_groups,
    ours,
    settings_files,
    write,
)


def release_plan(current: dict, *, remove: bool = False) -> dict:
    """Freeze the release loop; placement and equality are intentionally old.

    Source: b51f08f961e1b1dcfb93347eb080f9ddd9a9260d,
    subfleet/hooks.py:911-918. The loop below is verbatim. Its desired_groups
    and _strip_ours dependencies are unchanged by #86.
    """
    desired_groups = hooks_module.desired_groups
    _strip_ours = hooks_module._strip_ours
    command, timeout = CMD, TIMEOUT
    hooks = current.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    proposed_hooks = {key: value for key, value in hooks.items()}
    changed = []
    resolved = command
    for event, group in desired_groups(command, timeout).items():
        existing = hooks.get(event) if isinstance(hooks.get(event), list) else []
        stripped = _strip_ours(list(existing), event, resolved)
        updated = stripped if remove else [*stripped, group]
        if updated != list(existing):
            changed.append(event)
        if updated or event in hooks:
            proposed_hooks[event] = updated
    proposed = {**current, "hooks": proposed_hooks}
    return {"changed_events": changed, "proposed": proposed}


@st.composite
def compatible_install_files(draw):
    """Each owned entry is absent or alone in the last group, without aliases.

    Exact entries may use reversed keys and floating-point number spellings.
    Changed entries differ in ways Python equality already distinguished.
    """
    groups = {"Stop": draw(st.lists(foreign_groups(), max_size=3))}
    for event in draw(st.permutations(EVENTS)):
        if draw(st.booleans()):
            continue
        entries = draw(st.lists(foreign_groups(), max_size=4))
        kind = draw(st.sampled_from(["missing", "exact", "timeout", "extra",
                                    "matcher", "command", "rewake"]))
        if kind != "missing":
            group = copy.deepcopy(draw(exact(event)))
            hook = group["hooks"][0]
            if kind == "timeout":
                hook["timeout"] += draw(st.integers(1, 1000))
            elif kind == "extra":
                hook["statusMessage"] = draw(st.text(max_size=12))
            elif kind == "matcher":
                group["matcher"] = "different matcher"
            elif kind == "command":
                hook["command"] = f"/old/venv/bin/subfleet hook {event}"
            elif kind == "rewake":
                hook["asyncRewake"] = not hook.get("asyncRewake", False)
            if draw(st.booleans()):
                hook["timeout"] = float(hook["timeout"])
            entries.append(group)
        groups[event] = entries
    return {"model": draw(JSON_VALUES), "unowned": draw(JSON_VALUES),
            "hooks": groups}


@st.composite
def list_valued_files(draw):
    """Rich owned/shared/duplicate groups, mixed with arbitrary JSON entries."""
    _, original = draw(settings_files())
    data = copy.deepcopy(original)
    for event in EVENTS:
        if event not in data["hooks"] and draw(st.booleans()):
            continue
        entries = data["hooks"].setdefault(event, [])
        for entry in draw(st.lists(JSON_VALUES, max_size=3)):
            entries.insert(draw(st.integers(0, len(entries))), entry)
    data["unowned"] = draw(JSON_VALUES)
    data["hooks"]["Notification"] = draw(st.lists(JSON_VALUES, max_size=3))
    return data


def assert_unowned_values_preserved(current: dict, proposed: dict) -> None:
    assert canonical({key: value for key, value in proposed.items()
                      if key != "hooks"}) == canonical({
                          key: value for key, value in current.items()
                          if key != "hooks"})
    for event, value in current["hooks"].items():
        if event not in EVENTS:
            assert canonical(proposed["hooks"][event]) == canonical(value)


@settings(max_examples=150, deadline=None)
@given(compatible_install_files())
@example({"hooks": {"PreToolUse": [foreign("/usr/local/bin/other-tool hook")]}})
def test_install_agrees_with_release_when_owned_entries_are_alone_and_last(data):
    """C-23.25 invariant 7: preserve every case the old install loop got right."""
    with tempfile.TemporaryDirectory() as directory:
        path = write(Path(directory), data)
        current = json.loads(path.read_text())
        report = hooks_module.plan(path, command=CMD, timeout=TIMEOUT)
        reference = release_plan(current)
        assert report["ok"] is True
        assert report["changed_events"] == reference["changed_events"]
        assert canonical(report["proposed"]) == canonical(reference["proposed"])
        assert_unowned_values_preserved(current, report["proposed"])


@settings(max_examples=150, deadline=None)
@given(list_valued_files())
def test_uninstall_agrees_with_release_for_list_valued_events(data):
    """C-23.25 invariant 7: uninstall agrees on every generated list shape."""
    with tempfile.TemporaryDirectory() as directory:
        path = write(Path(directory), data)
        current = json.loads(path.read_text())
        report = hooks_module.plan(path, command=CMD, timeout=TIMEOUT, remove=True)
        reference = release_plan(current, remove=True)
        assert report["ok"] is True
        assert report["changed_events"] == reference["changed_events"]
        assert canonical(report["proposed"]) == canonical(reference["proposed"])
        assert_unowned_values_preserved(current, report["proposed"])


def test_uninstall_intentionally_preserves_a_non_list_event_without_drift(tmp_path):
    """Intended C-23.25 difference: another event's removal keeps this value.

    Release replaces the non-list event with [] despite reporting no drift for
    it; #86 leaves an event without drift as the file spelled it.
    """
    data = {"model": "keep", "hooks": {
        "PreToolUse": {"enabled": True}, "SessionStart": [ours("SessionStart")],
        "Stop": [foreign("/usr/local/bin/stop-hook")]}}
    path = write(tmp_path, data)
    report = hooks_module.plan(path, command=CMD, timeout=TIMEOUT, remove=True)
    reference = release_plan(data, remove=True)
    assert report["changed_events"] == reference["changed_events"] == ["SessionStart"]
    assert report["proposed"]["hooks"]["PreToolUse"] == {"enabled": True}
    assert reference["proposed"]["hooks"]["PreToolUse"] == []
    assert report["proposed"]["hooks"]["SessionStart"] == []
    assert_unowned_values_preserved(data, report["proposed"])
