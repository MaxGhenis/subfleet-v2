"""C-30.2, IR-15: one Claude transcript's facts for a conversation row.

`conversation.open` of a native session and the legacy cockpit import (C-30.4)
both create a conversation from `catalog.claude_session`, so the two can never
disagree about a session's workspace, model value, permission or continuability.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import hypothesis

from subfleet.conversations import catalog
from subfleet.sessions import transcripts

SESSION = "5e551011-0000-4000-8000-0000000000c1"


def write_transcript(projects: Path, session_id: str, cwd: str, *, mode: str | None = "default",
                     source: str | None = None, title: str | None = None,
                     model: str = "claude-opus-5-5") -> Path:
    directory = projects / cwd.replace("/", "-")
    directory.mkdir(parents=True, exist_ok=True)
    user = {"type": "user", "uuid": "u1", "cwd": cwd, "sessionId": session_id,
            "message": {"role": "user", "content": "fix the importer"}}
    if mode:
        user["permissionMode"] = mode
    if source:
        user["promptSource"] = source
    rows = [user, {"type": "assistant", "uuid": "a1", "cwd": cwd, "sessionId": session_id,
                   "message": {"role": "assistant", "model": model,
                               "content": [{"type": "text", "text": "done"}]}}]
    if title:
        rows.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_a_session_continues_in_its_transcripts_workspace(tmp_path):
    """C-30.2, design D-9: cwd, title, model value and the mapped permission."""
    work = tmp_path / "work"
    work.mkdir()
    path = write_transcript(tmp_path / "projects", SESSION, str(work), mode="bypassPermissions",
                            title="Importer fix")
    facts = catalog.claude_session(path)
    assert facts == {"cwd": str(work), "title": "Importer fix", "model_value": "claude-opus-5-5",
                     "permission": "bypass", "permission_source": "bypassPermissions",
                     "continuable": True, "lane_id": None}


def test_a_lane_run_a_missing_directory_and_tmp_do_not_continue(tmp_path):
    """C-30.2, IR-15: a lane run, a vanished cwd and a /tmp cwd are not continuable."""
    work = tmp_path / "work"
    work.mkdir()
    lane = write_transcript(tmp_path / "a", SESSION, str(work), source="sdk")
    gone = write_transcript(tmp_path / "b", SESSION, str(tmp_path / "gone"))
    assert catalog.claude_session(lane)["continue_blocker"] == "a Subfleet lane run"
    assert catalog.claude_session(gone)["continue_blocker"] == "its working directory no longer exists"
    for path in (lane, gone):
        assert catalog.claude_session(path)["continuable"] is False
    # An existing directory under /private/tmp: the tmp rule, not the missing one, refuses it.
    scratch = tempfile.mkdtemp(prefix="sf-catalog-", dir="/private/tmp")
    try:
        temporary = write_transcript(tmp_path / "c", SESSION, scratch)
        assert catalog.claude_session(temporary)["continue_blocker"] == "tmp-workspace"
    finally:
        os.rmdir(scratch)


def test_a_transcript_is_found_under_a_named_projects_directory(tmp_path, monkeypatch):
    """C-30.4's `--claude-dir`: a projects directory other than `~/.claude/projects`."""
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "elsewhere"))
    work = tmp_path / "work"
    work.mkdir()
    path = write_transcript(tmp_path / "projects", SESSION, str(work))
    assert transcripts.transcript_path(SESSION) is None
    assert transcripts.transcript_path(SESSION, tmp_path / "projects") == path


def test_a_line_that_is_not_an_object_is_not_an_entry(tmp_path):
    """C-30.2, C-30.4 (review L2): a valid JSON line that is not an object, and an
    entry whose message or cwd has another shape, are skipped; reading such a
    transcript never raises, for `conversation.open` or the legacy import."""
    work = tmp_path / "work"
    work.mkdir()
    path = write_transcript(tmp_path / "projects", SESSION, str(work), title="Importer fix")
    odd = ["[]", '"text"', json.dumps({"type": "user", "message": ["not", "an", "object"], "cwd": 7})]
    path.write_text("\n".join([*odd, *path.read_text().splitlines(), "[1]",
                               json.dumps({"type": "assistant", "message": "x"})]) + "\n", encoding="utf-8")
    facts = catalog.claude_session(path)
    assert (facts["continuable"], facts["cwd"], facts["title"], facts["model_value"]) == (
        True, str(work), "Importer fix", "claude-opus-5-5")
    assert transcripts.headless_transcript(path) is False


def _copy(projects: Path, cwd: Path, rows: list[dict], mtime: float) -> Path:
    """One copy of SESSION's transcript under `cwd`'s project directory, as Claude Code names it."""
    from subfleet.adapters.claude import encode_project_dir
    directory = projects / encode_project_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _turn(cwd: Path, uuid: str, **user) -> list[dict]:
    return [{"type": "user", "uuid": f"u{uuid}", "cwd": str(cwd), "sessionId": SESSION,
             "message": {"role": "user", "content": f"prompt {uuid}"}, **user},
            {"type": "assistant", "uuid": f"a{uuid}", "cwd": str(cwd), "sessionId": SESSION,
             "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}]}}]


def test_a_moved_session_continues_where_its_live_copy_is(tmp_path):
    """C-30.2, C-30.4 (review L7): a session moved to another worktree leaves a
    copy under each project directory. The newest copy, the one resumed, starts
    with the old rows; its workspace is the cwd whose project directory holds
    it, with the permission its last rows carry, never the old worktree's."""
    old, new = tmp_path / "work" / "old_tree", tmp_path / "work" / "new.tree"
    for directory in (old, new):
        directory.mkdir(parents=True)
    projects = tmp_path / "projects"
    head = _turn(old, "1", permissionMode="default")
    stale = _copy(projects, old, head, 1_700_000_000)
    live = _copy(projects, new, [*head, *_turn(new, "2", permissionMode="acceptEdits")], 1_800_000_000)
    assert transcripts.transcript_path(SESSION, projects) == live
    facts = catalog.claude_session(live)
    assert (facts["cwd"], facts["permission"]) == (str(new), "accept-edits")
    assert catalog.claude_session(stale)["cwd"] == str(old)


def test_a_session_that_moves_within_its_project_keeps_its_project(tmp_path):
    """C-30.2 (review L7): a session whose later rows name a directory inside its
    project still continues from the project's own directory, where its one
    copy lives."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    path = _copy(tmp_path / "projects", root, [*_turn(root, "1"), *_turn(root / "sub", "2")], 1_800_000_000)
    assert catalog.claude_session(path)["cwd"] == str(root)


def test_a_session_that_moved_and_then_moved_within_its_new_project_keeps_the_new_one(tmp_path):
    """C-30.2 (review L7 follow-up): a session moved to a new worktree and then
    into a directory inside it continues from the new worktree, the latest cwd
    that names its copy's project directory."""
    old, new = tmp_path / "work" / "old", tmp_path / "work" / "new"
    (new / "sub").mkdir(parents=True)
    old.mkdir(parents=True)
    projects = tmp_path / "projects"
    head = _turn(old, "1")
    _copy(projects, old, head, 1_700_000_000)
    live = _copy(projects, new, [*head, *_turn(new, "2"), *_turn(new / "sub", "3")], 1_800_000_000)
    assert catalog.claude_session(live)["cwd"] == str(new)


# --- discovery shares the workspace opening uses (C-30.2; review of 3c1a34e, finding 7) --------


def _discovered(root: Path, projects: Path) -> list[dict]:
    """A catalog run's Claude items (C-30.1), with no live registry and no Codex home."""
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(catalog, "_live_claude_sessions", return_value=set()):
        return catalog.build(state, lanes=[], claude_projects=projects, codex_app_home=root / "no-codex")["items"]


def test_the_catalog_shows_a_moved_session_where_it_opens(tmp_path):
    """C-30.2 (review of 3c1a34e, finding 7): a session that moved from a /tmp
    directory into a permanent project is discovered with the workspace its
    newest copy continues from, continuable, exactly as `conversation.open`
    opens it; the app had refused to open it as a /tmp session. A move between
    two permanent directories shows the new one."""
    scratch = Path(tempfile.mkdtemp(prefix="sf-moved-", dir="/private/tmp"))
    try:
        new = tmp_path / "work" / "new"
        new.mkdir(parents=True)
        projects = tmp_path / "projects"
        live = _copy(projects, new, [*_turn(scratch, "1"), *_turn(new, "2")], 1_800_000_000)
        (item,) = _discovered(tmp_path, projects)
        facts = catalog.claude_session(live)
        assert (item["cwd"], item["continuable"], item["continue_blocker"]) == (str(new), True, None)
        assert (facts["cwd"], facts["continuable"]) == (str(new), True)
    finally:
        os.rmdir(scratch)
    old = tmp_path / "work" / "old"
    old.mkdir()
    # Each copy left behind is not a second item: only the newest, the one opened, is listed.
    stale = _copy(projects, old, _turn(old, "0"), 1_700_000_000)
    (item,) = _discovered(tmp_path / "again", projects)
    assert (item["path"], item["cwd"]) == (str(live), str(new)) and stale.exists()
    elsewhere = tmp_path / "elsewhere"
    _copy(elsewhere, new, [*_turn(old, "1"), *_turn(new, "2")], 1_800_000_000)
    (item,) = _discovered(tmp_path / "second", elsewhere)
    assert item["cwd"] == str(new) and item["continuable"] is True


def test_a_record_cached_before_discovery_knew_the_workspace_is_read_again(tmp_path):
    """C-30.1, C-30.2 (review of 3c1a34e, finding 7): a transcript whose size and
    mtime have not changed since a run cached its record without the workspace
    (the first `cwd` only) is read again, so the catalog does not keep showing a
    moved session at its old directory."""
    new = tmp_path / "work" / "new"
    new.mkdir(parents=True)
    old = "/tmp/previous-project"
    projects = tmp_path / "projects"
    live = _copy(projects, new, [*_turn(Path(old), "1"), *_turn(new, "2")], 1_800_000_000)
    state = tmp_path / "state"
    state.mkdir()
    st = live.stat()
    stale = {k: v for k, v in catalog._claude_record(live).items() if k != "workspace"}
    (state / "catalog-cache.json").write_text(json.dumps(
        {str(live): {"size": st.st_size, "mtime": st.st_mtime, "record": {**stale, "cwd": old}}}))
    (item,) = _discovered(tmp_path, projects)
    assert (item["cwd"], item["continuable"]) == (str(new), True)
    cached = json.loads((state / "catalog-cache.json").read_text())[str(live)]
    assert cached["version"] == catalog.CLAUDE_RECORD_VERSION and cached["record"]["workspace"] == str(new)
    assert _discovered(tmp_path, projects)[0]["cwd"] == str(new)            # and read from the cache after


@hypothesis.settings(max_examples=60, deadline=None)
@hypothesis.given(moves=hypothesis.strategies.lists(hypothesis.strategies.integers(0, 4), min_size=1, max_size=6),
                  copy=hypothesis.strategies.integers(0, 4))
def test_discovery_and_opening_agree_on_every_transcript(moves, copy):
    """C-30.2 (review of 3c1a34e, finding 7), differential: for a transcript whose
    rows move through any sequence of directories (two under /tmp, two
    permanent, one inside a permanent one), kept under any one of their project
    directories, the catalog item names the workspace `claude_session` opens
    and refuses the /tmp ones exactly when it does."""
    with tempfile.TemporaryDirectory(prefix="sf-agree-", dir=Path(__file__).parent) as base, \
            tempfile.TemporaryDirectory(prefix="sf-agree-", dir="/private/tmp") as scratch:
        base, scratch = Path(base), Path(scratch)
        places = [scratch / "a", scratch / "b", base / "work" / "c", base / "work" / "d", base / "work" / "c" / "sub"]
        for place in places:
            place.mkdir(parents=True, exist_ok=True)
        rows = [row for n, index in enumerate(moves) for row in _turn(places[index], str(n))]
        live = _copy(base / "projects", places[copy], rows, 1_800_000_000)
        (item,) = _discovered(base, base / "projects")
        facts = catalog.claude_session(live)
        if facts["continuable"]:
            assert item["cwd"] == facts["cwd"]
        else:
            assert catalog._temporary(item["cwd"])            # opening names no cwd for a /tmp session
        assert item["continuable"] == facts["continuable"]
        assert item["continue_blocker"] == facts.get("continue_blocker")


# --- catalog.lock: a reader's probe is not a run (C-30.1) -------------------------------------

def _held(root: Path, how: int) -> int:
    import fcntl
    fd = os.open(root / "catalog.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, how | fcntl.LOCK_NB)
    return fd


def test_a_readers_probe_is_not_read_as_a_run(tmp_path):
    """C-30.1: `conversation.list` probes `catalog.lock` on every list, and the tick
    probes it before starting a run. Each probe took the lock exclusively, so one
    that met another in its instant read it as a run: the tick started none, and
    none started for a whole interval. A probe takes it shared; only a run's hold
    reads as a run."""
    import fcntl
    probe = _held(tmp_path, fcntl.LOCK_SH)             # another probe, in the instant it holds the lock
    try:
        assert catalog.refresh_running(tmp_path) is False
    finally:
        os.close(probe)
    run = _held(tmp_path, fcntl.LOCK_EX)
    try:
        assert catalog.refresh_running(tmp_path) is True
    finally:
        os.close(run)


def test_a_run_that_meets_a_probe_still_publishes(tmp_path, monkeypatch):
    """C-30.1: a run that found the lock taken exited 0 at once, as for another run
    holding it, and published nothing: meeting a list's probe in its instant cost a
    whole interval of catalog. A run tries again for a moment, which outlasts any
    probe (`LOCK_TRIES`), and publishes."""
    import fcntl
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home / ".claude"))
    root = tmp_path / "state"
    root.mkdir()
    probe = [_held(root, fcntl.LOCK_SH)]

    def the_probe_ends():
        while probe:
            os.close(probe.pop())

    monkeypatch.setattr(catalog, "_lock_wait", the_probe_ends, raising=False)
    try:
        assert catalog.main(["--state-root", str(root)]) == 0
    finally:
        the_probe_ends()
    assert (root / "catalog.json").exists(), "the run gave up at the probe and published nothing"


def test_one_threads_name_scrubs_only_its_own_row_and_the_index_is_read_to_its_cap(tmp_path, monkeypatch):
    """C-25.3 (review of aa41312, finding 2): `conversation.open` of a Codex thread
    read Codex's whole `session_index.jsonl` and scrubbed every row for one name (a
    200,000-row index took 100 s at load 130). It reads at most `INDEX_MAX`, and for
    one thread parses and scrubs only the rows that name it."""
    home = tmp_path / ".codex"
    home.mkdir()
    rows = [{"id": f"0f0e0d0c-1111-2222-3333-{n:012d}", "thread_name": f"thread {n}"} for n in range(2_000)]
    (home / "session_index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    scrubbed = []
    real = catalog.scrub
    monkeypatch.setattr(catalog, "scrub", lambda text: (scrubbed.append(text), real(text))[1])
    wanted = rows[1_500]["id"]
    assert catalog._codex_names(home, only=wanted) == {wanted: "thread 1500"}
    assert scrubbed == ["thread 1500"]
    monkeypatch.setattr(catalog, "INDEX_MAX", 1_000, raising=False)
    assert wanted not in catalog._codex_names(home)                     # past the cap: not read
