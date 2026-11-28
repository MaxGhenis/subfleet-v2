"""The delete path's request, its exclusion check, its refusals and its command line (C-8.7, d2660).

Fast units: no git and no store beyond a temporary one. The request is the
operator's (`parse_request`); the CLI and the daemon both check it, so neither
acts on a request the other refuses. `Excluder.resolve` must never stat or open
a place at or below an excluded repository (invariant 7): a spy on `os.stat`,
`os.lstat` and the interpreter's own audit events for `open`, `os.listdir` and
`os.scandir` records every path touched, and the properties compare `resolve`
with `os.path.realpath` wherever no excluded place is in the way.
"""
from __future__ import annotations

import json
import math
import os
import stat
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import folders, protocol, refusals
from subfleet import retention_delete as rd
from subfleet.contracts import Exit
from subfleet.store import Store

GOOD = {"ruling": "d2660:2026-11-09T07:27:26:25680b5d", "jobs": ["job-a", "job-b"], "age_exempt": ["job-b"],
        "min_age_days": 14, "exclude_repositories": ["/Users/someone/PolicyEngine/microcosm"], "min_free_gb": 30}


# --- the request ------------------------------------------------------------------------

def test_a_good_request_round_trips_and_its_digest_ignores_key_order():
    request = rd.parse_request(GOOD)
    assert request.jobs == ("job-a", "job-b") and request.age_exempt == frozenset({"job-b"})
    assert request.min_age_days == 14.0 and request.min_free_gb == 30.0
    assert request.exclude_repositories == ("/Users/someone/PolicyEngine/microcosm",)
    again = rd.parse_request(dict(reversed(list(GOOD.items()))))
    assert again == request and again.digest == request.digest
    assert rd.parse_request(request.as_json()) == request
    assert rd.parse_request({**GOOD, "min_free_gb": 31}).digest != request.digest


def test_optional_fields_default_and_exclusions_are_normalised_once():
    request = rd.parse_request({"ruling": "r", "jobs": ["j"], "min_age_days": 0, "min_free_gb": 0,
                                "exclude_repositories": ["/a/b/", "/a/./b", "/c"]})
    assert request.age_exempt == frozenset() and request.exclude_repositories == ("/a/b", "/c")
    bare = rd.parse_request({"ruling": "r", "jobs": ["j"], "min_age_days": 1.5, "min_free_gb": 0})
    assert bare.exclude_repositories == () and bare.min_age_days == 1.5


BAD = [
    ([], "must be a JSON object"),
    ({**GOOD, "jobz": []}, "unknown field 'jobz'"),
    ({k: v for k, v in GOOD.items() if k != "ruling"}, "missing field 'ruling'"),
    ({k: v for k, v in GOOD.items() if k != "min_free_gb"}, "missing field 'min_free_gb'"),
    ({**GOOD, "ruling": "  "}, "ruling: must name the ruling"),
    ({**GOOD, "ruling": 7}, "ruling: must name the ruling"),
    ({**GOOD, "ruling": "x" * 201}, "at most 200 printable"),
    ({**GOOD, "ruling": "line\nbreak"}, "at most 200 printable"),
    ({**GOOD, "jobs": []}, "jobs: must list at least one job"),
    ({**GOOD, "jobs": "job-a"}, "jobs: must list at least one job"),
    ({**GOOD, "jobs": [f"j{i}" for i in range(rd.MAX_JOBS + 1)], "age_exempt": []}, "at most 100 jobs"),
    ({**GOOD, "jobs": ["job-a", "job-a"], "age_exempt": []}, "a job is listed twice"),
    ({**GOOD, "jobs": ["../escape"], "age_exempt": []}, "is not a job id"),
    ({**GOOD, "jobs": [".hidden"], "age_exempt": []}, "is not a job id"),
    ({**GOOD, "jobs": ["a:b"], "age_exempt": []}, "is not a job id"),
    ({**GOOD, "jobs": [" padded"], "age_exempt": []}, "is not a job id"),
    ({**GOOD, "jobs": [7], "age_exempt": []}, "is not a job id"),
    ({**GOOD, "age_exempt": "job-b"}, "age_exempt: must be a list"),
    ({**GOOD, "age_exempt": ["job-z"]}, "age_exempt: job-z is not in jobs"),
    ({**GOOD, "age_exempt": ["job-b", "job-b"]}, "age_exempt: a job is listed twice"),
    ({**GOOD, "exclude_repositories": "/x"}, "exclude_repositories: must be a list"),
    ({**GOOD, "exclude_repositories": ["relative/path"]}, "is not an absolute path"),
    ({**GOOD, "exclude_repositories": [""]}, "is not an absolute path"),
    ({**GOOD, "exclude_repositories": ["/a\0b"]}, "is not an absolute path"),
    ({**GOOD, "exclude_repositories": ["/"]}, "/ would exclude every repository"),
    ({**GOOD, "exclude_repositories": ["/./"]}, "/ would exclude every repository"),
    ({**GOOD, "min_age_days": -1}, "min_age_days: must be a finite number"),
    ({**GOOD, "min_age_days": True}, "min_age_days: must be a finite number"),
    ({**GOOD, "min_age_days": "14"}, "min_age_days: must be a finite number"),
    ({**GOOD, "min_free_gb": math.inf}, "min_free_gb: must be a finite number"),
    ({**GOOD, "min_free_gb": math.nan}, "min_free_gb: must be a finite number"),
]


@pytest.mark.parametrize("data,words", BAD)
def test_every_bad_request_is_refused_naming_its_field(data, words):
    with pytest.raises(rd.RequestError) as caught:
        rd.parse_request(data)
    assert words in str(caught.value)


def test_read_request_refuses_what_is_not_a_small_json_file(tmp_path):
    good = tmp_path / "request.json"
    good.write_text(json.dumps(GOOD))
    assert rd.read_request(good) == rd.parse_request(GOOD)
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(rd.RequestError, match="not JSON"):
        rd.read_request(tmp_path / "bad.json")
    with pytest.raises(rd.RequestError):
        rd.read_request(tmp_path / "missing.json")
    (tmp_path / "huge.json").write_bytes(b" " * ((1 << 20) + 2))
    with pytest.raises(rd.RequestError):
        rd.read_request(tmp_path / "huge.json")
    os.mkfifo(tmp_path / "pipe.json")
    with pytest.raises(rd.RequestError):
        rd.read_request(tmp_path / "pipe.json")         # never blocks on a FIFO


def test_the_cli_documents_exactly_the_request_fields_the_path_takes():
    """review-notes (hub): the request file's field names must match what the CLI takes."""
    from subfleet import retention_cli
    import argparse
    parser = argparse.ArgumentParser()
    retention_cli.add_verbs(parser.add_subparsers())
    delete = parser._subparsers._group_actions[0].choices["retention"]._subparsers._group_actions[0].choices["delete"]
    help_text = next(a.help for a in delete._actions if "--request" in a.option_strings)
    named = {word.strip(",: ") for word in help_text.split()} & rd.REQUEST_KEYS
    assert named == set(rd.REQUEST_KEYS)


# --- excluded repositories ---------------------------------------------------------------

class Touches:
    """Every path Python stats, opens or lists while it is installed."""

    def __init__(self, monkeypatch):
        self.paths: list[str] = []
        self.active = True
        real_stat, real_lstat = os.stat, os.lstat

        def spy_stat(path, *args, **kwargs):
            self._add(path, kwargs.get("dir_fd"))
            return real_stat(path, *args, **kwargs)

        def spy_lstat(path, *args, **kwargs):
            self._add(path, kwargs.get("dir_fd"))
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", spy_stat)
        monkeypatch.setattr(os, "lstat", spy_lstat)

    def _add(self, path, dir_fd=None) -> None:
        if self.active and isinstance(path, (str, bytes, os.PathLike)) and dir_fd is None:
            self.paths.append(os.fsdecode(path))

    def audit(self, event, args):
        if not self.active:
            return
        if event in ("open", "os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes, os.PathLike)):
            self.paths.append(os.fsdecode(args[0]))

    def under(self, root: str) -> list[str]:
        key = folders.fold(os.path.normpath(root))
        return [p for p in self.paths if folders.within(folders.fold(os.path.normpath(p)), key)]


@pytest.fixture
def touches(monkeypatch):
    spy = Touches(monkeypatch)
    sys.addaudithook(spy.audit)        # audit hooks cannot be removed: `active` turns this one off
    yield spy
    spy.active = False


@pytest.fixture
def place(tmp_path):
    """tmp/real/... with an excluded repository `excluded` and links into and around it."""
    real = Path(os.path.realpath(tmp_path))
    excluded = real / "excluded"
    (excluded / ".git" / "worktrees" / "job").mkdir(parents=True)
    (excluded / ".git" / "worktrees" / "job" / "gitdir").write_text("x")
    (real / "free" / "deep").mkdir(parents=True)
    os.symlink(str(excluded), real / "link-to-excluded")
    os.symlink("excluded/.git", real / "relative-link")
    os.symlink(str(real / "free"), real / "link-to-free")
    os.symlink("loop-b", real / "loop-a")
    os.symlink("loop-a", real / "loop-b")
    return real


def test_covers_compares_folded_and_normalised_names_but_not_prefixes():
    excluder = rd.Excluder(["/Users/x/PolicyEngine/microcosm"])
    assert excluder.covers("/Users/x/PolicyEngine/microcosm")
    assert excluder.covers("/users/X/policyengine/MICROCOSM/.git/worktrees/a")
    assert excluder.covers("/Users/x/PolicyEngine/./microcosm/../microcosm/src")
    assert not excluder.covers("/Users/x/PolicyEngine/microcosm-2")
    assert not excluder.covers("/Users/x/PolicyEngine")
    assert not rd.Excluder([]).covers("/anything")


@pytest.mark.parametrize("text,base", [
    ("{real}/excluded/.git/worktrees/job", "/"),
    ("{real}/EXCLUDED/.git", "/"),                       # another case, the same folder here
    ("../excluded/.git/worktrees/job", "{real}/free"),
    ("{real}/link-to-excluded/.git/worktrees/job", "/"),
    ("{real}/relative-link/worktrees/job", "/"),
    ("{real}/link-to-free/../excluded/.git", "/"),      # `..` after a link is the link target's parent
])
def test_resolve_refuses_an_excluded_place_without_touching_it(place, touches, text, base):
    excluder = rd.Excluder([str(place / "excluded")])
    touches.paths.clear()
    where, why = excluder.resolve(text.format(real=place), base.format(real=place))
    assert (where, why) == (None, "excluded")
    assert touches.under(str(place / "excluded")) == []


def test_resolve_follows_links_reports_loops_nuls_and_spells_missing_parts_as_given(place):
    excluder = rd.Excluder([str(place / "excluded")])
    assert excluder.resolve(str(place / "link-to-free" / "deep"), "/") == (str(place / "free" / "deep"), None)
    assert excluder.resolve("deep/../deep", str(place / "free")) == (str(place / "free" / "deep"), None)
    assert excluder.resolve(str(place / "free" / "gone" / "x"), "/") == (str(place / "free" / "gone" / "x"), None)
    assert excluder.resolve(str(place / "free" / "gone" / ".." / "deep"), "/") == (str(place / "free" / "deep"), None)
    where, why = excluder.resolve(str(place / "loop-a"), "/")
    assert where is None and why.startswith("unresolvable") and "links" in why
    where, why = excluder.resolve("a\0b", "/")
    assert where is None and "NUL" in why
    # A missing part that, spelled out, lands in the excluded repository is excluded.
    assert excluder.resolve(str(place / "free" / "gone" / ".." / ".." / "excluded" / "x"), "/") == (None, "excluded")


def test_an_excluder_spelling_through_a_link_also_excludes_the_real_place(place):
    excluder = rd.Excluder([str(place / "link-to-excluded")])
    assert excluder.covers(str(place / "excluded" / ".git"))
    assert excluder.resolve(str(place / "excluded" / ".git"), "/") == (None, "excluded")


SEGMENTS = ["free", "deep", "excluded", "EXCLUDED", ".git", "worktrees", "job", "..", ".", "link-to-excluded",
            "relative-link", "link-to-free", "loop-a", "gone"]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(parts=st.lists(st.sampled_from(SEGMENTS), min_size=1, max_size=7), relative=st.booleans())
def test_resolve_never_touches_an_excluded_place_and_agrees_with_realpath(place, touches, parts, relative):
    """Invariant 7 at the unit level, and a differential against the OS: for a
    path made of real, linked, looping, missing and excluded parts, `resolve`
    never stats, opens or lists at or below the excluded root, and when it
    answers, its answer is `os.path.realpath`'s (on this volume, which folds case)."""
    excluder = rd.Excluder([str(place / "excluded")])
    text = "/".join(parts) if relative else str(place / "/".join(parts))
    touches.paths.clear()
    where, why = excluder.resolve(text, str(place))
    assert touches.under(str(place / "excluded")) == []
    touches.active = False
    try:
        # The oracle: the OS's own resolution of the whole path and of each prefix.
        expected = os.path.realpath(os.path.join(str(place), *parts), strict=False)
        prefixes = [os.path.realpath(os.path.join(str(place), *parts[:i]), strict=False)
                    for i in range(1, len(parts) + 1)]
    finally:
        touches.active = True
    if where is not None:
        assert why is None
        assert folders.fold(where) == folders.fold(expected)
        assert not any(excluder.covers(p) for p in prefixes)
    elif why == "excluded":
        assert any(excluder.covers(p) for p in prefixes)
    else:
        assert why.startswith("unresolvable") and ("loop-a" in parts)


# --- the op and its refusals -------------------------------------------------------------

@pytest.fixture
def path(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    store = Store(root / "state.sqlite3")
    yield rd.DeletePath(store, root, holders=lambda watches, cancel=None: {}, cwd_pids=lambda p: frozenset())
    store.close()


def args(action, request=None, limit=None):
    return protocol.RetentionDeleteArgs(action=action, request=request, limit=limit)


@pytest.mark.parametrize("call,reason,code", [
    (args("delete-everything"), "retention-delete-action", Exit.INVALID_INPUT),
    (args(None), "retention-delete-action", Exit.INVALID_INPUT),
    (args("apply", GOOD, 0), "retention-delete-limit", Exit.INVALID_INPUT),
    (args("apply", GOOD, True), "retention-delete-limit", Exit.INVALID_INPUT),
    (args("apply", GOOD, "3"), "retention-delete-limit", Exit.INVALID_INPUT),
    (args("dry-run", {**GOOD, "jobs": []}), "retention-delete-request", Exit.INVALID_INPUT),
    (args("apply", None), "retention-delete-request", Exit.INVALID_INPUT),
])
def test_the_op_refuses_with_a_registered_reason_and_its_template(path, call, reason, code):
    with pytest.raises(protocol.ProtocolError) as caught:
        rd.handle(path, call, dormant=False)
    error = caught.value
    assert error.reason == reason and error.code == code
    message, fix = refusals.HANDLER_MESSAGES[reason]
    assert error.fix == fix
    if reason != "retention-delete-request":            # its words go to the log; the wire shows the template
        assert str(error) == message
    assert path.status()["apply"] is None and path.status()["dry_run"] is None


def test_dormant_retention_refuses_a_run_but_answers_status_and_stop(path):
    for action in ("dry-run", "apply"):
        with pytest.raises(protocol.ProtocolError) as caught:
            rd.handle(path, args(action, GOOD), dormant=True)
        assert caught.value.reason == "retention-delete-dormant" and caught.value.code == Exit.REFUSED
    assert rd.handle(path, args("status"), dormant=True) == {"apply": None, "dry_run": None, "journals": []}
    assert rd.handle(path, args("stop"), dormant=True)["stopped"] is False


def test_a_second_run_while_one_runs_is_busy_and_stop_ends_it_at_the_boundary(path):
    started = rd.handle(path, args("dry-run", GOOD), dormant=False)
    assert started["status"] == "running" and started["mode"] == "dry-run" and started["totals"]["pending"] == 2
    with pytest.raises(protocol.ProtocolError) as caught:
        rd.handle(path, args("apply", GOOD, 1), dormant=False)
    assert caught.value.reason == "retention-delete-busy" and caught.value.code == Exit.REFUSED
    stopped = rd.handle(path, args("stop"), dormant=False)
    assert stopped["stopped"] is True and stopped["dry_run"]["status"] == "stopping"
    path.step()
    assert path.status()["dry_run"]["status"] == "stopped" and not path.pending()
    # Neither a dry run nor its stop wrote anything.
    assert not (path.root / "retention-delete").exists()


def test_an_apply_record_persists_and_a_new_path_reads_it_back(path):
    rd.handle(path, args("apply", GOOD, 1), dormant=False)
    record = json.loads((path.root / "retention-delete" / rd.RECORD).read_text())
    assert record["status"] == "running" and record["limit"] == 1 and record["digest"] == rd.parse_request(GOOD).digest
    again = rd.DeletePath(path.store, path.root)
    assert again.status()["apply"]["status"] == "running" and again.pending()
    mode = stat.S_IMODE(os.lstat(path.root / "retention-delete").st_mode)
    assert mode == 0o700


def test_every_new_refusal_template_is_registered_once_with_short_single_line_text():
    names = ["retention-delete-action", "retention-delete-dormant", "retention-delete-limit",
             "retention-delete-request", "retention-delete-busy"]
    for name in names:
        message, fix = refusals.HANDLER_MESSAGES[name]
        assert message.startswith("retention delete: ") and "\n" not in message and len(message) <= 300
        assert fix is None or ("\n" not in fix and len(fix) <= 300)
    assert "retention.delete" in protocol.OPS


# --- the command line -------------------------------------------------------------------

@pytest.mark.parametrize("argv,words", [
    (["--dry-run"], "--dry-run needs --request FILE"),
    (["--apply"], "--apply needs --request FILE"),
    (["--dry-run", "--request", "{good}", "--limit", "2"], "--limit goes with --apply"),
    (["--apply", "--request", "{good}", "--limit", "0"], "--limit must be a positive integer"),
    (["--status", "--request", "{good}"], "--status takes no --request"),
    (["--stop", "--limit", "1"], "--stop takes no --request"),
    (["--apply", "--request", "{bad}"], "jobs: must list at least one job"),
])
def test_the_cli_refuses_bad_arguments_before_it_reaches_the_daemon(tmp_path, monkeypatch, capsys, argv, words):
    from subfleet import cli
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(GOOD))
    bad.write_text(json.dumps({**GOOD, "jobs": [], "age_exempt": []}))
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_client", lambda *a, **k: pytest.fail("the daemon was asked"))
    code = cli.main(["retention", "delete", *[a.format(good=good, bad=bad) for a in argv]])
    assert code == int(Exit.INVALID_INPUT)
    assert words in capsys.readouterr().err


def test_the_cli_needs_exactly_one_action(tmp_path, monkeypatch):
    from subfleet import cli
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_client", lambda *a, **k: pytest.fail("the daemon was asked"))
    assert cli.main(["retention", "delete"]) == 2
    assert cli.main(["retention", "delete", "--status", "--stop"]) == 2


class FakeClient:
    """The socket's stand-in: each call goes to `handle`, and each status read
    runs one step, as the daemon's retention worker would between reads."""

    def __init__(self, path: rd.DeletePath):
        self.path = path
        self.calls: list[tuple[str, dict]] = []

    def call(self, op, payload):
        self.calls.append((op, payload))
        assert op == "retention.delete"
        if payload.get("action") == "status":
            self.path.step()
        return rd.handle(self.path, protocol.coerce_args(protocol.RetentionDeleteArgs, payload), dormant=False)


def test_the_cli_follows_a_run_to_its_summary_one_line_per_job(tmp_path, monkeypatch, capsys, path):
    from subfleet import cli, retention_cli
    request = tmp_path / "request.json"
    request.write_text(json.dumps({**GOOD, "jobs": ["job-a", "job-b", "job-c"]}))
    fake = FakeClient(path)
    monkeypatch.setenv("SUBFLEET_HOME", str(path.root))
    monkeypatch.setattr(cli, "_client", lambda *a, **k: fake)
    monkeypatch.setattr(retention_cli, "DELETE_POLL_S", 0)
    code = cli.main(["retention", "delete", "--dry-run", "--request", str(request)])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # No job rows exist: each is kept at the `finished` gate, one line each, then the summary.
    assert [line.get("job_id") for line in lines[:-1]] == ["job-a", "job-b", "job-c"]
    assert all(line["disposition"] == "kept" and line["gate"] == "finished" for line in lines[:-1])
    assert lines[-1]["status"] == "done" and lines[-1]["summary"]["kept"] == 3 and lines[-1]["mode"] == "dry-run"
    assert code == int(Exit.OK)
    assert fake.calls[0][1]["action"] == "dry-run" and fake.calls[0][1]["limit"] is None


def test_the_cli_detaches_and_reads_status_and_stop(tmp_path, monkeypatch, capsys, path):
    from subfleet import cli
    request = tmp_path / "request.json"
    request.write_text(json.dumps(GOOD))
    fake = FakeClient(path)
    monkeypatch.setenv("SUBFLEET_HOME", str(path.root))
    monkeypatch.setattr(cli, "_client", lambda *a, **k: fake)
    assert cli.main(["retention", "delete", "--apply", "--request", str(request), "--limit", "1", "--detach",
                     "--json"]) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["mode"] == "apply" and started["limit"] == 1 and started["status"] == "running"
    assert cli.main(["retention", "delete", "--stop", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] is True
    assert cli.main(["retention", "delete", "--status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["apply"]["status"] == "stopped"


def test_the_cli_names_an_older_daemon_that_lacks_the_op(tmp_path, monkeypatch, capsys):
    from subfleet import cli
    from subfleet.client import DaemonError

    class Old:
        def call(self, op, payload):
            raise DaemonError(int(Exit.INVALID_INPUT), f"{protocol.UNKNOWN_OP}: retention.delete")
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_client", lambda *a, **k: Old())
    assert cli.main(["retention", "delete", "--status"]) == int(Exit.DAEMON_UNAVAILABLE)
    assert "older than this CLI" in capsys.readouterr().err
