"""D-LANE-STARTUP1 (C-12.9): writable Claude job launches stop loading personal startup files.

A writable lane inherits the daemon's HOME and keeps the `user` setting source, so
Claude Code 2.1.318 loaded the operator's `~/.claude/CLAUDE.md`, `~/AGENTS.md` (the
built-in `cc-plugin-agents-md` plugin) and auto-memory into it
(`docs/reports/2026-11-14-lane-startup-design.md`, F1-F9). Design A keeps the sources
and adds, to the launch's one `--settings`, `claudeMdExcludes` for everything outside
the job's repository root `R`, `autoMemoryEnabled: false` with
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, and `disableAllHooks: false`.

The design's invariants, each a test or a group of tests here:

* I1 the writable argv keeps the guard: no `--safe-mode`, `--bare`, `--restricted`,
  every `--setting-sources` names `user`, `disableAllHooks` is false;
* I2 the exclusions name the config home, six files under every ancestor of `R` and
  `R_real` but `/` and under `$HOME`, and one negation spelling both;
* I3 no non-negated entry names a path under `R`, and Claude Code's matcher
  (`tests/claude_md_glob.py`, held to the bundle by `test_lane_startup_glob.py`) keeps
  the root's files and excludes every listed outside path;
* I4 auto-memory is off and `MEMORY_ENV` is removed on writable launches;
* I5 read-only argv is byte for byte what `adf66f2e5` built;
* I6 conversation turns are unchanged;
* I7 no `CLAUDE_CONFIG_DIR` joins `env_add`, and the transcript path is unchanged;
* I8 exactly one `--settings`, valid JSON, with `CLAUDE_CODE_RETRY_WATCHDOG=0`;
* I9 `resume_launch`'s settings equal `build_launch`'s for the same job.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from subfleet.adapters import claude
from subfleet.adapters.claude import (
    JOB_STARTUP_ENV, LANE_SETTINGS_ENV, STARTUP_FILE_NAMES, ClaudeAdapter, encode_project_dir, glob_literal,
    job_startup_settings,
)
from subfleet.adapters.isolation import MEMORY_ENV
from subfleet.contracts import JobSpec, Sandbox
from tests import claude_md_glob as glob
from tests import lane_settings_cases as lsc

REPO = Path(__file__).resolve().parents[2]
RECORDING = json.loads((REPO / "tests" / "fixtures" / "lane_startup" / "argv-adf66f2e5.json")
                       .read_text(encoding="utf-8"))["cases"]
CASES = lsc.cases()
STARTUP_KEYS = ("autoMemoryEnabled", "disableAllHooks", "claudeMdExcludes")
TOKEN = lsc.TOKEN
SESSION = lsc.SESSION
#: The six names of `STARTUP_FILE_NAMES`, as a file each names (`rules/**` as one rule).
FILES = tuple(name.replace("/**", "/x.md") for name in STARTUP_FILE_NAMES)


# --- helpers ----------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return done.stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "base",
         cwd=path)
    return path


def _job(scratch: Path, workdir: Path, *, sandbox: str = "workspace-write", task: str | None = "build",
         mcp: bool = False) -> JobSpec:
    prompt = scratch / "prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Do the thing.\n", encoding="utf-8")
    servers, config = (), None
    if mcp:
        path = scratch / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"docs": {"command": "/usr/bin/true"}}}), encoding="utf-8")
        servers, config = ("docs",), str(path)
    return JobSpec(request_id="req-lane-startup", kind="dispatch", workdir=str(workdir), prompt_path=str(prompt),
                   task=task, tier="standard", pinned_model=None, pinned_lane=None, sandbox=Sandbox(sandbox),
                   out_path=None, name="lane-startup", isolated_review=False, review_root=None,
                   mcp_servers=servers, mcp_config=config)


def _adapter(scratch: Path) -> ClaudeAdapter:
    return lsc._adapter(scratch)


def _build(scratch: Path, job: JobSpec, credentials: dict | None = None):
    credentials = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN} if credentials is None else credentials
    return _adapter(scratch).build_launch(job, "job-1/a1", scratch / "attempt-1", lsc._lane(), dict(credentials),
                                          lsc.MODEL, None, Path(job.prompt_path), None)


def _resume(scratch: Path, job: JobSpec, credentials: dict | None = None):
    credentials = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN} if credentials is None else credentials
    return _adapter(scratch).resume_launch(job, "job-1/a2", scratch / "attempt-2", lsc._lane(), dict(credentials),
                                           SESSION, Path(job.prompt_path), None, model_id=lsc.MODEL)


def _settings(argv) -> dict:
    payloads = lsc.settings_payloads(argv)
    assert len(payloads) == 1, payloads
    return json.loads(payloads[0])


def _toplevel(workdir: Path) -> str:
    """R as the test finds it, independently: git's own answer, else the workdir."""
    done = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--show-toplevel"], capture_output=True,
                          text=True, timeout=60)
    return done.stdout[:-1] if done.returncode == 0 else os.path.abspath(workdir)


def _ancestors(path: str) -> list[str]:
    return [str(parent) for parent in Path(path).parents if str(parent) != "/"]


def _without_startup(argv) -> list[str]:
    """`argv` with job-startup keys taken back out of its one `--settings`."""
    argv = list(argv)
    index = argv.index("--settings")
    settings = json.loads(argv[index + 1])
    for key in STARTUP_KEYS:
        settings.pop(key, None)
    env = {key: value for key, value in settings.get("env", {}).items() if key not in JOB_STARTUP_ENV}
    settings["env"] = env
    return argv[:index + 1] + [json.dumps(settings, separators=(",", ":"))] + argv[index + 2:]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake `$HOME` for the launch (`_launch_home`), with no config dir override."""
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return path


@pytest.fixture
def fixtures(tmp_path, home) -> dict[str, tuple[Path, Path]]:
    """I2's fixture workdirs: name -> (workdir, scratch)."""
    base = tmp_path / "places"
    repo = _repo(base / "repo")
    (repo / "sub" / "deeper").mkdir(parents=True)
    plain = base / "plain" / "folder"
    plain.mkdir(parents=True)
    (base / "links").mkdir()
    linked = base / "links" / "folder-link"
    linked.symlink_to(plain, target_is_directory=True)
    worktree = base / "linked-worktree"
    _git("worktree", "add", "-q", "--detach", str(worktree), cwd=repo)
    places = {"repo-root": repo, "subdirectory": repo / "sub" / "deeper", "symlinked": linked,
              "non-git": plain, "home": home, "linked-worktree": worktree}
    return {name: (path, tmp_path / "scratch" / name) for name, path in places.items()}


def _check_i2(excludes: list[str], workdir: Path, home: Path, configs: tuple[str, ...] = ()) -> None:
    root = _toplevel(workdir)
    spellings = list(dict.fromkeys((root, os.path.realpath(root))))
    for config in (*configs, str(home / ".claude")):
        assert f"{glob_literal(config)}/CLAUDE.md" in excludes
        assert f"{glob_literal(config)}/rules/**" in excludes
    folders = {*[a for spelling in spellings for a in _ancestors(spelling)], str(home)}
    for folder in folders:
        for name in STARTUP_FILE_NAMES:
            assert f"{glob_literal(folder)}/{name}" in excludes, (folder, name)
    negations = [item for item in excludes if item.startswith("!")]
    assert len(negations) == 1
    spared = [glob_literal(spelling) for spelling in spellings]
    assert negations[0] == ("!" + (spared[0] if len(spared) == 1 else "{" + ",".join(spared) + "}") + "/**")
    # Nothing else: every entry is one of those.
    named = {f"{glob_literal(c)}/{n}" for c in (*configs, str(home / ".claude")) for n in ("CLAUDE.md", "rules/**")}
    named |= {f"{glob_literal(f)}/{n}" for f in folders for n in STARTUP_FILE_NAMES}
    assert set(excludes) == named | set(negations)


def _check_i3(excludes: list[str], workdir: Path, home: Path, configs: tuple[str, ...] = ()) -> None:
    root = _toplevel(workdir)
    spellings = list(dict.fromkeys((root, os.path.realpath(root))))
    personal = [*configs, str(home)]            # `$HOME/.claude` is under `$HOME`

    def under(path: str, folders) -> bool:
        return any(path == folder or path.startswith(folder + "/") for folder in folders)
    # Structure: no entry names a path under R, but the personal ones (C, `$HOME`),
    # which name R's own files only when R holds them (the `$HOME` fixture).
    named = [glob.unglob(entry).removesuffix("/**") for entry in excludes if not entry.startswith("!")]
    for path in named:
        assert not under(path, spellings) or under(path, personal), path
    personal_files = {os.path.realpath(path + "/x.md" if path.endswith("/rules") else path)
                      for path in named if under(path, personal)}
    excluded = glob.matcher(excludes)
    for spelling in spellings:          # the root's own guidance, in either spelling
        for path in (f"{spelling}/CLAUDE.md", f"{spelling}/sub/CLAUDE.md", f"{spelling}/AGENTS.md",
                     f"{spelling}/.claude/rules/x.md", f"{spelling}/.claude/CLAUDE.md", f"{spelling}/CLAUDE.local.md",
                     f"{spelling}/.claude/AGENTS.md", f"{spelling}/a/b/c/AGENTS.md"):
            assert excluded(path) is (os.path.realpath(path) in personal_files), path
    for path in named:                  # every listed outside path
        assert excluded(path + "/x.md" if path.endswith("/rules") else path), path
    for spelling in spellings:          # and whatever else lies outside
        parent, leaf = spelling.rsplit("/", 1)
        for path in (f"{parent}/{leaf}x/CLAUDE.md", f"{parent}/other/CLAUDE.md", f"{parent}/CLAUDE.md",
                     "/CLAUDE.md", f"{home}/elsewhere/CLAUDE.md"):
            if not under(path, spellings):
                assert excluded(path), path


# --- I2, I3 over the fixture workdirs ------------------------------------------------


@pytest.mark.parametrize("name", ["repo-root", "subdirectory", "symlinked", "non-git", "home", "linked-worktree"])
def test_i2_i3_each_fixture_workdir(name, fixtures, home):
    """I2 and I3 on the build launch of every fixture workdir; for the subdirectory,
    `R` is the repository's top, and for the symlinked folder both spellings."""
    workdir, scratch = fixtures[name]
    settings = _settings(_build(scratch, _job(scratch, workdir)).argv)
    _check_i2(settings["claudeMdExcludes"], workdir, home)
    _check_i3(settings["claudeMdExcludes"], workdir, home)
    if name == "subdirectory":
        assert _toplevel(workdir) == os.path.realpath(fixtures["repo-root"][0])
    if name == "symlinked":
        assert settings["claudeMdExcludes"][-1].startswith("!{")


def test_i2_a_home_lane_also_excludes_its_own_config_home(tmp_path, home):
    """C is the launch's config home: `CLAUDE_CONFIG_DIR` when the lane sets one, and
    `$HOME/.claude` always."""
    workdir = _repo(tmp_path / "repo")
    lane_home = tmp_path / "lane-home"
    lane_home.mkdir()
    settings = _settings(_build(tmp_path / "scratch", _job(tmp_path / "scratch", workdir),
                                {"CLAUDE_CONFIG_DIR": str(lane_home)}).argv)
    _check_i2(settings["claudeMdExcludes"], workdir, home, configs=(str(lane_home),))
    _check_i3(settings["claudeMdExcludes"], workdir, home, configs=(str(lane_home),))


def test_i3_a_git_spelling_through_a_symlink_spares_both(tmp_path, home, monkeypatch):
    """A stub git that answers with a symlinked spelling of the top: `R` is git's
    spelling and `R_real` its real path, and the one negation spares both."""
    real = _repo(tmp_path / "real-repo")
    link = tmp_path / "link-repo"
    link.symlink_to(real, target_is_directory=True)
    _stub_git(tmp_path, monkeypatch, f'#!/bin/sh\nprintf "%s\\n" "{link}"\n')
    excludes = job_startup_settings(real, {"HOME": str(home)})["claudeMdExcludes"]
    assert excludes[-1] == "!{" + glob_literal(str(link)) + "," + glob_literal(os.path.realpath(real)) + "}/**"
    test = glob.matcher(excludes)
    for spelling in (str(link), os.path.realpath(real)):
        assert not test(f"{spelling}/CLAUDE.md") and not test(f"{spelling}/AGENTS.md")
        assert test(f"{spelling}x/CLAUDE.md")
    for parent in {*_ancestors(str(link)), *_ancestors(os.path.realpath(real))}:
        assert f"{glob_literal(parent)}/CLAUDE.md" in excludes


# --- R when git cannot answer ---------------------------------------------------------


def _stub_git(tmp_path: Path, monkeypatch, script: str) -> None:
    folder = tmp_path / "stub-bin"
    folder.mkdir(exist_ok=True)
    stub = folder / "git"
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.mark.parametrize("script", [
    "#!/bin/sh\necho 'fatal: not a git repository' >&2\nexit 128\n",
    "#!/bin/sh\nexit 1\n",
    "#!/bin/sh\necho relative/top\n",
    "#!/bin/sh\nkill -9 $$\n",
], ids=["not-a-repository", "plain-failure", "relative-answer", "killed"])
def test_r_falls_back_to_the_workdir_when_git_fails(script, tmp_path, home, monkeypatch):
    """Any git failure makes `R` the workdir: inside a repository's subfolder the
    repository's top-level files are then excluded too (fewer kept), and nothing
    outside the workdir is kept."""
    repo = _repo(tmp_path / "repo")
    workdir = repo / "sub"
    workdir.mkdir()
    _stub_git(tmp_path, monkeypatch, script)
    excludes = job_startup_settings(workdir, {"HOME": str(home)})["claudeMdExcludes"]
    real = os.path.realpath(workdir)
    assert excludes[-1] in (f"!{glob_literal(str(workdir))}/**",
                            "!{" + glob_literal(str(workdir)) + "," + glob_literal(real) + "}/**")
    test = glob.matcher(excludes)
    assert not test(f"{workdir}/CLAUDE.md") and test(f"{repo}/CLAUDE.md")


def test_r_falls_back_to_the_workdir_when_git_passes_its_cap(tmp_path, home, monkeypatch):
    """A git that hangs is stopped at `JOB_STARTUP_GIT_TIMEOUT_S`; `R` is the workdir."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    _stub_git(tmp_path, monkeypatch, "#!/bin/sh\nexec sleep 30\n")
    monkeypatch.setattr(claude, "JOB_STARTUP_GIT_TIMEOUT_S", 0.5)
    started = time.monotonic()
    excludes = job_startup_settings(workdir, {"HOME": str(home)})["claudeMdExcludes"]
    assert time.monotonic() - started < 15
    assert excludes[-1] in (f"!{glob_literal(str(workdir))}/**",
                            "!{" + glob_literal(str(workdir)) + "," + glob_literal(os.path.realpath(workdir)) + "}/**")


def test_r_outside_git_is_the_workdir(tmp_path, home):
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir()
    excludes = job_startup_settings(workdir, {"HOME": str(home)})["claudeMdExcludes"]
    assert glob.unglob(excludes[-1]).startswith(f"!{{{workdir},") or excludes[-1] == f"!{glob_literal(str(workdir))}/**"


# --- awkward names, JSON, size --------------------------------------------------------


AWKWARD = "we ird é名 *?[{!,}] (a|b)+@ \"q\" $^#~ ..x"


def test_i3_a_workdir_with_spaces_non_ascii_and_glob_metacharacters(tmp_path, home):
    """Spaces, non-ASCII letters and `* ? [ { ! , } ( ) | + @ " .` in `R` and in an
    ancestor neither widen nor break a pattern: the root's files are kept, siblings a
    widened pattern would catch are excluded, and the payload is valid JSON."""
    parent = tmp_path / AWKWARD
    workdir = _repo(parent / f"repo {AWKWARD}")
    scratch = tmp_path / "scratch"
    argv = _build(scratch, _job(scratch, workdir)).argv
    payload = lsc.settings_payloads(argv)[0]
    settings = json.loads(payload)
    excludes = settings["claudeMdExcludes"]
    _check_i2(excludes, workdir, home)
    _check_i3(excludes, workdir, home)
    test = glob.matcher(excludes)
    root = os.path.realpath(workdir)
    up, leaf = root.rsplit("/", 1)
    decoys = {leaf.replace("*", "x"), leaf.replace("?", "y"), leaf.replace("[", "").replace("]", ""),
              leaf.replace("{!,}", "!"), leaf.replace('"', ""), leaf.replace("..", "."), leaf + "z",
              leaf.replace("(a|b)", "a"), leaf.replace("+", ""), glob_literal(leaf)}
    for decoy in decoys - {leaf}:
        (Path(up) / decoy).mkdir(exist_ok=True)
        assert test(f"{up}/{decoy}/CLAUDE.md"), decoy
    assert not test(f"{root}/CLAUDE.md") and not test(f"{root}/x/AGENTS.md")


@pytest.mark.parametrize("name", ["repo-root", "subdirectory", "symlinked", "non-git", "home", "linked-worktree"])
def test_the_payload_is_valid_json_at_every_fixture(name, fixtures):
    workdir, scratch = fixtures[name]
    for launch in (_build(scratch, _job(scratch, workdir, task="review")),
                   _resume(scratch, _job(scratch, workdir, task=None))):
        payload = lsc.settings_payloads(launch.argv)[0]
        assert json.loads(payload) == json.loads(json.dumps(json.loads(payload)))


@pytest.mark.parametrize("extra", [None, 12], ids=["12-from-root", "12-below-the-temp-root"])
def test_the_payload_size_for_a_workdir_twelve_levels_deep(extra, tmp_path, home, capsys):
    """A workdir 12 folders deep (from `/`, or 12 below the test's temp folder, which is
    itself deep and spelled two ways on macOS) keeps its one `--settings` far under any
    argv limit; the size is printed for the build report."""
    start = len(Path(tmp_path).parts) - 1
    levels = extra if extra is not None else 12 - start
    if levels < 1:
        pytest.skip(f"the temp folder is already {start} deep")
    workdir = tmp_path.joinpath(*[f"level-{n:02d}" for n in range(1, levels + 1)])
    workdir.mkdir(parents=True)
    scratch = tmp_path / "scratch"
    payload = lsc.settings_payloads(_build(scratch, _job(scratch, workdir)).argv)[0]
    settings = json.loads(payload)
    with capsys.disabled():
        print(f"\nlane-startup payload: workdir {len(workdir.parts) - 1} deep "
              f"(real path {len(Path(os.path.realpath(workdir)).parts) - 1}), "
              f"{len(settings['claudeMdExcludes'])} patterns, {len(payload.encode())} bytes")
    assert len(payload.encode()) < 64 * 1024
    _check_i2(settings["claudeMdExcludes"], workdir, home)


# --- I1, I4, I8, I9 over every writable launch -------------------------------------------


WRITABLE = [(task, tmp, mcp) for task in ("build", "review", None) for tmp in (False, True) for mcp in (False, True)]


def _writable_pair(tmp_path, task, tmp, mcp):
    workdir = _repo(tmp_path / "repo")
    scratch = tmp_path / "scratch"
    credentials = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    if tmp:
        credentials["TMPDIR"] = str(scratch / "attempt-tmp")
    job = _job(scratch, workdir, task=task, mcp=mcp)
    return job, _build(scratch, job, credentials), _resume(scratch, job, credentials)


@pytest.mark.parametrize("task,tmp,mcp", WRITABLE)
def test_i1_i4_i8_i9_every_writable_launch(task, tmp, mcp, tmp_path, home):
    """I1: no `--safe-mode`, `--bare` or `--restricted`; every `--setting-sources` names
    `user`; `disableAllHooks` is false. I4: auto-memory off, `MEMORY_ENV` removed. I8:
    one `--settings`, valid JSON, the watchdog off. I9: resume's settings are build's."""
    job, built, resumed = _writable_pair(tmp_path, task, tmp, mcp)
    for launch in (built, resumed):
        argv = list(launch.argv)
        assert not {"--safe-mode", "--bare", "--restricted"} & set(argv)                        # I1
        sources = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--setting-sources"]
        assert all("user" in value.split(",") for value in sources)                                # I1
        assert sources == (["user"] if task == "review" else [])
        assert "--dangerously-skip-permissions" in argv
        settings = _settings(argv)                                                                 # I8
        assert settings["disableAllHooks"] is False                                                # I1
        assert settings["env"][lsc.WATCHDOG] == lsc.OFF                                            # I8
        assert settings["autoMemoryEnabled"] is False                                              # I4
        assert settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"                           # I4
        assert set(MEMORY_ENV) <= set(launch.env_remove)                                           # I4
        assert set(claude.ENV_REMOVE) <= set(launch.env_remove)
        assert ("hooks" in settings) is (task == "review")
    # I9: the same payload; only a review's publish hook names its own attempt's folder.
    payloads = [lsc.settings_payloads(launch.argv)[0].replace(f"/{attempt}/", "/<attempt>/")
                for launch, attempt in ((built, "attempt-1"), (resumed, "attempt-2"))]
    assert payloads[0] == payloads[1]


# --- I5, I6 and the differential against adf66f2e5 ------------------------------------------


@pytest.fixture(scope="module")
def built_cases(tmp_path_factory) -> dict[str, list[str]]:
    root = tmp_path_factory.mktemp("lane-startup-cases")
    with pytest.MonkeyPatch.context() as patch:
        home = root / "user-home"
        (home / ".claude").mkdir(parents=True)
        patch.setenv("HOME", str(home))
        patch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        return {lsc.case_id(case): lsc.build(case, root / f"case-{number:03d}")
                for number, case in enumerate(CASES)}


def _writable_job(case) -> bool:
    return case["path"] in ("job", "resume") and case["sandbox"] == "workspace-write"


def test_the_grid_is_the_recording():
    assert sorted(map(lsc.case_id, CASES)) == sorted(RECORDING)


@pytest.mark.parametrize("case", [c for c in CASES if not _writable_job(c)], ids=lsc.case_id)
def test_i5_i6_every_other_launch_is_byte_for_byte_adf66f2e5s(case, built_cases):
    """I5 (read-only jobs, isolated reviews, probes, timers, gates, read-only turns) and
    I6 (every conversation turn) and the enrolment, probe and heal turns: the argv
    `adf66f2e5` built for the same inputs, flag for flag."""
    assert built_cases[lsc.case_id(case)] == RECORDING[lsc.case_id(case)]


@pytest.mark.parametrize("case", [c for c in CASES if _writable_job(c)], ids=lsc.case_id)
def test_a_writable_job_changes_only_by_the_startup_keys(case, built_cases):
    """A writable job's argv, its startup keys taken back out, is `adf66f2e5`'s."""
    argv = built_cases[lsc.case_id(case)]
    settings = _settings(argv)
    assert set(STARTUP_KEYS) <= set(settings) and settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert _without_startup(argv) == RECORDING[lsc.case_id(case)]


def test_i6_turn_argv_takes_nothing_from_job_startup(tmp_path, home):
    """`claude_turn.argv` never names the startup keys, whatever the permission."""
    from subfleet.conversations.claude_turn import argv as turn_argv
    from subfleet.conversations.turn import TurnSpec
    for permission in lsc.PERMISSIONS:
        spec = TurnSpec(provider="claude", message_id="m-1", text="hi", model_id=lsc.MODEL, permission=permission,
                        native_session_id=None, new_session_id=SESSION, cwd=str(tmp_path))
        settings = _settings(turn_argv(spec, read_only_flags=ClaudeAdapter.permission_args(Sandbox.READ_ONLY)))
        assert "claudeMdExcludes" not in settings and "autoMemoryEnabled" not in settings
        assert settings["env"] == dict(LANE_SETTINGS_ENV)


# --- I7 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("sandbox", ["workspace-write", "read-only"])
def test_i7_no_config_dir_joins_and_the_transcript_path_is_unchanged(sandbox, tmp_path, home):
    workdir = _repo(tmp_path / "repo")
    scratch = tmp_path / "scratch"
    job = _job(scratch, workdir, sandbox=sandbox)
    for launch, session in ((_build(scratch, job), SESSION), (_resume(scratch, job), SESSION)):
        assert "CLAUDE_CONFIG_DIR" not in launch.env_add
        assert launch.env_add["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        expected = scratch / "projects" / encode_project_dir(Path(workdir).resolve()) / f"{session}.jsonl"
        assert launch.notes["transcript_path"] == str(expected)
