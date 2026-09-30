"""C-6.14 at the command line: `run --paths`, the batch `paths` key, and what `run` says."""

from __future__ import annotations

from subfleet import cli
from subfleet.contracts import Exit
from tests.unit.test_cli import JOB, run_cli, submit_ok, terminal
from tests.unit.test_cli_batch import numbered


def test_c6_14_run_paths_are_repeatable_and_comma_separated(daemon, root, capsys, workdir):
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "--paths", "src,docs/",
                    "--paths", " data/big ", "hi"]) == 0
    assert server.args("submit")["checkout_paths"] == ["src", "docs/", "data/big"]
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["checkout_paths"] is None
    capsys.readouterr()


def test_c6_14_run_says_what_a_sparse_worktree_holds(daemon, root, capsys, workdir):
    """C-6.14 the caller hears the checkout is sparse, how to reach the rest, and what was left out."""
    def submitted(request):
        return {**submit_ok(request), "sandbox": "workspace-write", "worktree": f"/state/worktrees/{JOB}",
                "checkout": {"mode": "sparse", "cone": ["src", "docs"], "cone_bytes": 124_600_000,
                             "tree_bytes": 15_042_700_000, "left_out": ["data/corpus"]}}
    daemon({"submit": submitted, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "hi"]) == 0
    err = capsys.readouterr().err
    assert "checkout: sparse (2 dirs, 124.6 MB of the 15.0 GB tree)" in err
    assert "git sparse-checkout add <dir>" in err
    assert "not checked out, over the budget: data/corpus" in err


def test_c6_14_run_is_quiet_about_a_full_checkout(daemon, root, capsys, workdir):
    def submitted(request):
        return {**submit_ok(request), "sandbox": "workspace-write", "worktree": f"/state/worktrees/{JOB}",
                "checkout": {"mode": "full", "reason": "under-threshold"}}
    daemon({"submit": submitted, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "hi"]) == 0
    assert "checkout:" not in capsys.readouterr().err


def test_c6_14_a_batch_entry_names_its_paths(daemon, capsys, tmp_path, workdir):
    """C-17.7, C-6.14 `paths` is a manifest key like the flag, a list of strings."""
    (tmp_path / "a.md").write_text("brief\n")
    manifest = tmp_path / "jobs.json"
    manifest.write_text(f'[{{"prompt": "a.md", "workdir": "{workdir}", "model": "opus", '
                        f'"paths": ["src", "data/big"]}}]')
    server = daemon({"submit": numbered})
    assert cli.main(["run", "--batch", str(manifest), "-d"]) == Exit.OK
    sent = [request.args for request in server.requests if request.op == "submit"]
    assert sent[0]["checkout_paths"] == ["src", "data/big"]
    bad = tmp_path / "bad.json"
    bad.write_text(f'[{{"prompt": "a.md", "workdir": "{workdir}", "paths": "src"}}]')
    assert cli.main(["run", "--batch", str(bad), "-d"]) == Exit.INVALID_INPUT
    assert "paths must be a list of strings" in capsys.readouterr().err
