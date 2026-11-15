"""D-LANE-STARTUP1: the `claudeMdExcludes` matcher the lane-startup tests use is Claude Code's.

`tests/claude_md_glob.py` states Claude Code 2.1.318's `v8t()` matcher for the subset of
picomatch syntax `job_startup_settings` writes. `tests/fixtures/lane_startup/picomatch-2.1.318.json`
holds generated cases (workdirs and homes whose names carry spaces, non-ASCII letters
and every glob metacharacter, paths inside and outside each root) with the verdicts of
the bundle's own picomatch and `FWo`, run under node by `tools/claude_md_glob_record.py`.
These tests need no node: they replay the recording.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.adapters import claude
from subfleet.adapters.claude import STARTUP_FILE_NAMES, glob_literal, job_startup_settings
from tests import claude_md_glob as glob

REPO = Path(__file__).resolve().parents[2]
RECORDING = json.loads((REPO / "tests" / "fixtures" / "lane_startup" / "picomatch-2.1.318.json")
                       .read_text(encoding="utf-8"))
SETS = RECORDING["sets"]
METACHARACTERS = "*?[]{}()!|,+@^$.-#~'\"\\&=`<>%;: \té名"


def _resolve(links):
    return lambda path: links.get(path, path)


def test_the_recording_has_more_than_a_thousand_paths_and_every_label():
    assert RECORDING["bundle"] == "2.1.318"
    assert sum(len(item["paths"]) for item in SETS) >= 1000
    assert {label for item in SETS for label in item["expect"]} >= {"keep", "exclude", "line-terminator"}
    assert any(len(item["links"]) for item in SETS) and any("CLAUDE_CONFIG_DIR" in item["env"] for item in SETS)


@pytest.mark.parametrize("item", SETS, ids=lambda item: item["workdir"].split("/")[2])
def test_the_oracle_gives_the_bundles_verdict_on_every_recorded_path(item):
    """Claude Code's own matcher and `claude_md_glob` agree on every path, and on what
    `FWo` adds to the pattern list."""
    resolve = _resolve(item["links"])
    assert glob.fwo(item["patterns"], resolve) == item["fwo"]
    test = glob.matcher(item["patterns"], resolve)
    assert [test(path) for path in item["paths"]] == item["verdicts"]


@pytest.mark.parametrize("item", SETS, ids=lambda item: item["workdir"].split("/")[2])
def test_i3_by_the_bundles_own_verdicts(item):
    """I3 as the bundle judged it: every file of the root, in either spelling, is kept;
    every outside path is excluded. The residue is pinned: a folder name holding a
    line terminator outside the root is not judged by the negation (JavaScript's `.`),
    and a path that folds onto the root once `\\` is read as `/` is the root's."""
    for path, label, verdict in zip(item["paths"], item["expect"], item["verdicts"]):
        expected = {"keep": False, "exclude": True, "line-terminator": False, "backslash-fold": False,
                    "dot-segment": True, "empty": False}[label]
        assert verdict is expected, (label, path)


@pytest.mark.parametrize("item", SETS, ids=lambda item: item["workdir"].split("/")[2])
def test_job_startup_settings_still_writes_the_recorded_patterns(item):
    """The recording judges what this tree's `job_startup_settings` writes: for each
    case's (absent) workdir, home and link, it writes the recorded patterns again."""
    links = item["links"]
    with mock.patch.object(claude.os.path, "realpath", lambda path, **_: links.get(path, path)):
        written = job_startup_settings(item["workdir"], item["env"])
    assert written["claudeMdExcludes"] == item["patterns"]


# --- glob_literal ---------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.sampled_from(list(METACHARACTERS) + list("abcxyz")), min_size=1, max_size=24)
       .filter(lambda text: text not in (".", "..")))
def test_glob_literal_matches_its_path_and_no_neighbour(text):
    """`glob_literal(p)` matches `p` and none of its one-edit neighbours, as a pattern
    of its own, inside the negation's braces and through `FWo`'s `\\` folding."""
    path = "/sf-lsb-glob/" + text
    assert glob.unglob(glob_literal(path)) == path
    for candidate in (glob_literal(path), "!{" + glob_literal(path) + ",/sf-lsb-other}/**"):
        test = glob.matcher([candidate], lambda p: p)
        negated = candidate.startswith("!")
        assert test(path) is not negated
        for index in range(len(path)):
            for neighbour in (path[:index] + "Z" + path[index + 1:], path[:index] + path[index + 1:],
                              path[:index] + path[index] + path[index:]):
                if neighbour.replace("\\", "/") == path.replace("\\", "/") or not neighbour:
                    continue        # the fold, and picomatch's empty input
                if negated and neighbour.replace("\\", "/").startswith(path.replace("\\", "/") + "/"):
                    continue        # under the root, as `/**` says
                assert test(neighbour) is negated, (candidate, neighbour)


def test_glob_literal_escapes_each_character_picomatch_reads():
    """Bracketed where the bracket holds a regex metacharacter, a range where it would
    not (a comma, a double quote), and otherwise left alone (`[!]` would also match
    the text `[!]`)."""
    assert glob_literal("/a*b?c[d]e{f}g(h)i|j+k.l") == "/a[*]b[?]c[[]d[]]e[{]f[}]g[(]h[)]i[|]j[+]k[.]l"
    assert glob_literal('/a,b"c') == '/a[,-,]b["-"]c'
    assert glob_literal("/a!b@c^d$e#f~g h\\iéj") == "/a!b@c^d$e#f~g h\\iéj"


@pytest.mark.parametrize("pattern", ["/a/*", "/a/?", "/a[!]b", "/a/(x)", "/a,b", '/a"b', "!{/a..b,/c}/**",
                                     "!(a)", "/a{b,c", "/a]b", "!{/a{b,c},/d}/**", "!{/a}/**"])
def test_the_oracle_refuses_syntax_it_does_not_state(pattern):
    with pytest.raises(glob.Unsupported):
        glob.compile_pattern(pattern)


def test_the_oracle_states_picomatch_test_rules():
    """picomatch's `test`: an empty input never matches; an input equal to the glob's
    text matches; `\\` reads as `/` in the path."""
    assert glob.excluded(["!/r/**"], "", lambda p: p) is False
    assert glob.excluded(["/r/a[*]b"], "/r/a[*]b", lambda p: p) is True
    assert glob.excluded(["/r/a/CLAUDE.md"], "\\r\\a\\CLAUDE.md", lambda p: p) is True


def test_fwo_adds_the_real_spelling_of_an_absolute_patterns_folder():
    links = {"/l": "/real/l", "/l/x": "/real/l/x"}
    assert glob.fwo(["/l/x/CLAUDE.md", "!/l/**", "/l/a[*]b/C.md", "rel/C.md"], _resolve(links)) == [
        "/l/x/CLAUDE.md", "!/l/**", "/l/a[*]b/C.md", "rel/C.md", "/real/l/x/CLAUDE.md", "/real/l/a[*]b/C.md"]
    assert glob.js_dirname("/r/") == "/" and glob.js_dirname("/r/a") == "/r" and glob.js_dirname("//") == "/"


def test_startup_file_names_are_the_six_the_design_names():
    assert STARTUP_FILE_NAMES == ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "AGENTS.md",
                                  ".claude/AGENTS.md", ".claude/rules/**")
    assert os.sep == "/"
