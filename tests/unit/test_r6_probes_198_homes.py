"""Review r6 item 2 probes for #198 (not part of the PR): protected provider homes."""

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import folders, home_paths
from subfleet.conversations.service import ConversationError, ConversationService

ROOT = Path(__file__).resolve().parents[2]
RESULTS = os.environ.get("R6_RESULTS")


def report(name, **fields):
    if RESULTS:
        with open(RESULTS, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"probe": name, **fields}, default=str) + "\n")


def round4():
    return [Path(home_paths.home_path("CLAUDE_CONFIG_DIR", lambda: Path.home() / ".claude", test_only=True)),
            home_paths.codex_app_home()]


OUTSIDE = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from subfleet import folders, home_paths
from subfleet.conversations.service import ConversationService
assert not home_paths.under_pytest()
head = home_paths.protected_provider_homes()
r4 = [Path(home_paths.home_path("CLAUDE_CONFIG_DIR", lambda: Path.home() / ".claude", test_only=True)),
      home_paths.codex_app_home()]
base = [Path.home() / ".claude", Path.home() / ".codex"]
seen = []
folders.spelling = lambda p: (seen.append(os.fspath(p)) or (os.fspath(p), None))
svc = SimpleNamespace(root=Path("/nonexistent-r6/state"),
                      daemon=SimpleNamespace(store=SimpleNamespace(lane_rows=lambda: [])))
ConversationService._check_workspace(svc, "claude", "/nonexistent-r6/workspace", {"permission": "bypass"})
print(json.dumps({"head": [os.fspath(p) for p in head], "r4": [os.fspath(p) for p in r4],
                  "base": [os.fspath(p) for p in base], "spelled": seen, "module": home_paths.__file__}))
'''


@pytest.mark.parametrize("extra", ["plain", "overrides-set", "stale-session-markers", "home-unset"])
def test_r6_2a_outside_pytest_list_is_unchanged(tmp_path, extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SUBFLEET_UNDER_PYTEST", "SUBFLEET_PYTEST_SESSION", "SUBFLEET_PYTEST_SESSION_ID",
                        "PYTEST_CURRENT_TEST", "PYTHONPATH", "CLAUDE_CONFIG_DIR", "SUBFLEET_CODEX_APP_HOME")}
    env["HOME"] = str(tmp_path / "fake-home")
    if extra == "overrides-set":
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "cfg")
        env["SUBFLEET_CODEX_APP_HOME"] = str(tmp_path / "codex")
    elif extra == "stale-session-markers":
        env.update(SUBFLEET_UNDER_PYTEST="1", SUBFLEET_PYTEST_SESSION=str(tmp_path / "gone"),
                   SUBFLEET_PYTEST_SESSION_ID="1:2:x")
    elif extra == "home-unset":
        env.pop("HOME")
    done = subprocess.run([sys.executable, "-c", OUTSIDE], cwd=ROOT, env=env, text=True,
                          capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    report("2a", extra=extra, **out)
    assert out["module"].startswith(str(ROOT))
    assert out["head"] == out["r4"] == out["base"]
    assert out["spelled"] == ["/nonexistent-r6/workspace", "/nonexistent-r6/state", *out["base"]]


@pytest.mark.parametrize("how", ["pwd-real-home-string", "pwd-fixture", "pwd-fixture-trailing-slash"])
def test_r6_2b_account_home_matches_round4(tmp_path, monkeypatch, how):
    if how == "pwd-real-home-string":
        # Equality short-circuits before samefile: only the string is used.
        monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
        monkeypatch.setattr(Path, "samefile", lambda *a: pytest.fail("stat of the account home"))
    else:
        personal = tmp_path / "Personal"
        personal.mkdir()
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(personal)))
        monkeypatch.setenv("HOME", str(personal) + ("/" if how.endswith("slash") else ""))
    head, r4 = home_paths.protected_provider_homes(), round4()
    report("2b", how=how, head=head, r4=r4)
    assert head == r4


@pytest.mark.parametrize("layout,refused", [
    ("home", True), ("parent-of-home", True), ("under-home", False), ("inside-dot-claude", False),
    ("sibling", False)])
def test_r6_2c_synthetic_home_workspaces(tmp_path, monkeypatch, layout, refused):
    home = tmp_path / "fake-home"
    (home / "proj").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / "sibling").mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = {"home": home, "parent-of-home": tmp_path, "under-home": home / "proj",
                 "inside-dot-claude": home / ".claude" / "projects", "sibling": tmp_path / "sibling"}[layout]
    svc = SimpleNamespace(root=Path("/nonexistent-r6/state"),
                          daemon=SimpleNamespace(store=SimpleNamespace(lane_rows=lambda: [])))
    try:
        ConversationService._check_workspace(svc, "claude", str(workspace), {"permission": "accept-edits"})
        outcome = "allowed"
    except ConversationError as exc:
        outcome = f"refused: {exc}"
    report("2c", layout=layout, outcome=outcome, protected=home_paths.protected_provider_homes())
    assert outcome.startswith("refused") == refused, outcome
