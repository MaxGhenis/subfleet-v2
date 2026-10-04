"""C-12.9: a writable job's MCP opt-in selects exactly named definitions."""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from subfleet.adapters import claude_mcp as mcp


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def _server(command: str) -> dict[str, object]:
    return {"command": command, "args": ["--serve"]}


@pytest.mark.parametrize("names", [
    "gitnexus", b"gitnexus", [None], [1], [""], ["x" * 129],
    [" padded"], ["padded "], ["new\nline"], ["nul\x00byte"], ["hidden\u200b"],
])
def test_invalid_names_refused(names):
    with pytest.raises(ValueError):
        mcp.validate_names(names)


def test_names_normalized_without_changing_case():
    assert mcp.validate_names(["z", "GitNexus", "z", "gitnexus"]) == (
        "GitNexus", "gitnexus", "z",
    )
    assert mcp.validate_names([]) == ()


def test_project_files_are_root_most_first_and_exclude_filesystem_root():
    assert mcp.project_files("/one/two/three") == [
        Path("/one/.mcp.json"), Path("/one/two/.mcp.json"),
        Path("/one/two/three/.mcp.json"),
    ]
    assert mcp.project_files("/") == []


def test_home_ancestor_and_scope_precedence_match_claude(tmp_path):
    home = tmp_path / "home"
    project = home / "repo"
    workdir = project / "nested"
    workdir.mkdir(parents=True)
    (project / ".git").mkdir()
    _write(home / ".mcp.json", {"mcpServers": {
        "home-ancestor": _server("home"), "same": _server("home"),
    }})
    _write(project / ".mcp.json", {"mcpServers": {
        "inherited": _server("project"), "same": _server("project"),
    }})
    _write(workdir / ".mcp.json", {"mcpServers": {
        "nearer": _server("nearer"), "same": {"command": "nearest"},
    }})
    _write(home / ".claude.json", {
        "mcpServers": {"user": _server("user"), "same": _server("user")},
        "projects": {str(project): {"mcpServers": {
            "local": _server("local"), "same": {"command": "local"},
        }}},
    })

    found, skipped = mcp.offered(workdir, env={}, home=home)

    assert not skipped
    assert set(found) == {"home-ancestor", "inherited", "nearer", "user", "local", "same"}
    assert found["same"].scope == "local"
    assert found["same"].config == {"command": "local"}  # whole entry, no field merge
    assert found["home-ancestor"].scope == "project"
    assert found["home-ancestor"].source == str(home / ".mcp.json")
    assert found["user"].source == str(home / ".claude.json")

    # Without a local duplicate, the nearest project definition beats user scope.
    _write(home / ".claude.json", {"mcpServers": {"same": _server("user")}})
    found, _ = mcp.offered(workdir, env={}, home=home)
    assert found["same"].config == {"command": "nearest"}


def test_opt_in_copies_only_named_entries_and_preserves_variable_references(tmp_path):
    home = tmp_path / "home"
    workdir = home / "project"
    workdir.mkdir(parents=True)
    selected = {
        "type": "http", "url": "https://${HOST:-example.com}/mcp",
        "headers": {"Authorization": "Bearer ${TOKEN}"},
    }
    _write(home / ".claude.json", {"mcpServers": {
        "selected": selected, "unselected": _server("never-run"),
    }})

    found = mcp.resolve(workdir, ["selected", "selected"], env={}, home=home)

    assert mcp.config_document(found) == {"mcpServers": {"selected": selected}}
    assert mcp.sources(found) == {"selected": {
        "scope": "user", "source": str(home / ".claude.json"),
    }}
    assert mcp.config_document(mcp.resolve(workdir, [], env={}, home=home)) == {"mcpServers": {}}
    assert "TOKEN" not in json.dumps(mcp.sources(found))


def test_unknown_names_refused_with_available_names_without_config_values(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write(home / ".claude.json", {"mcpServers": {
        "z": {"command": "secret-command"}, "a": _server("a"),
    }})

    with pytest.raises(mcp.UnknownServer) as raised:
        mcp.resolve(home, ["missing", "also-missing"], env={}, home=home)

    assert raised.value.missing == ("also-missing", "missing")
    assert raised.value.offered == ("a", "z")
    assert "unknown MCP servers also-missing, missing" in str(raised.value)
    assert "a, z" in str(raised.value)
    assert "secret-command" not in str(raised.value)


def test_global_config_dir_and_legacy_location(tmp_path):
    home = tmp_path / "home"
    config_dir = tmp_path / "configured"
    assert mcp.global_config_path({}, home) == home / ".claude.json"
    assert mcp.global_config_path({"CLAUDE_CONFIG_DIR": str(config_dir)}, home) == (
        config_dir / ".claude.json"
    )
    _write(home / ".claude" / ".config.json", {})
    assert mcp.global_config_path({}, home) == home / ".claude" / ".config.json"
    _write(config_dir / ".config.json", {})
    assert mcp.global_config_path({"CLAUDE_CONFIG_DIR": str(config_dir)}, home) == (
        config_dir / ".config.json"
    )


def test_config_dir_definitions_replace_home_user_definitions(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_dir = tmp_path / "configured"
    _write(home / ".claude.json", {"mcpServers": {"home-only": _server("home")}})
    _write(config_dir / ".claude.json", {"mcpServers": {"configured": _server("chosen")}})
    env = {"CLAUDE_CONFIG_DIR": str(config_dir)}

    found, skipped = mcp.offered(home, env=env, home=home)

    assert not skipped
    assert set(found) == {"configured"}
    assert found["configured"].source == str(config_dir / ".claude.json")


@pytest.mark.parametrize("document", [[], None, {"mcpServers": []}, {"mcpServers": "wrong"}])
def test_invalid_project_documents_are_skipped(tmp_path, document):
    home = tmp_path / "home"
    _write(home / ".mcp.json", document)

    found, skipped = mcp.offered(home, env={}, home=home)

    assert found == {}
    assert len(skipped) == 1
    assert str(home / ".mcp.json") in skipped[0]


def test_non_object_server_entries_do_not_opt_in(tmp_path):
    home = tmp_path / "home"
    _write(home / ".mcp.json", {"mcpServers": {
        "valid": _server("valid"), "null": None, "list": [], "string": "wrong",
    }})

    found, _ = mcp.offered(home, env={}, home=home)

    assert set(found) == {"valid"}
    with pytest.raises(mcp.UnknownServer):
        mcp.resolve(home, ["null"], env={}, home=home)


def test_malformed_json_is_skipped_and_unknown_message_explains_source(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    path = home / ".mcp.json"
    path.write_text("{broken")

    with pytest.raises(mcp.UnknownServer) as raised:
        mcp.resolve(home, ["missing"], env={}, home=home)

    assert len(raised.value.skipped) == 1
    assert "sources skipped" in str(raised.value)
    assert str(path) in str(raised.value)


def test_oversized_project_config_is_skipped(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / ".mcp.json", {"mcpServers": {"large": _server("oversized")}})
    monkeypatch.setattr(mcp, "PROJECT_MAX_BYTES", 20)

    found, skipped = mcp.offered(home, env={}, home=home)

    assert not found
    assert len(skipped) == 1
    assert "longer than 20 bytes" in skipped[0]


def test_fifo_project_config_is_skipped_without_waiting_for_a_writer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    os.mkfifo(home / ".mcp.json")

    found, skipped = mcp.offered(home, env={}, home=home)

    assert not found
    assert len(skipped) == 1
    assert "not a regular file" in skipped[0]


def test_symlink_to_regular_project_config_loads(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "config.json"
    _write(target, {"mcpServers": {"linked": _server("linked")}})
    (home / ".mcp.json").symlink_to(target)

    found, skipped = mcp.offered(home, env={}, home=home)

    assert not skipped
    assert set(found) == {"linked"}


def test_local_scope_outside_git_uses_launch_directory(tmp_path):
    home = tmp_path / "home"
    workdir = home / "standalone"
    workdir.mkdir(parents=True)
    _write(home / ".claude.json", {"projects": {
        str(workdir): {"mcpServers": {"local": _server("local")}},
        str(home): {"mcpServers": {"wrong-project": _server("wrong")}},
    }})

    found, _ = mcp.offered(workdir, env={}, home=home)

    assert mcp.canonical_git_root(workdir) is None
    assert set(found) == {"local"}


def test_linked_worktree_uses_main_checkout_local_scope(tmp_path):
    home = tmp_path / "home"
    main = home / "main"
    worktree = home / "linked"
    gitdir = main / ".git" / "worktrees" / "linked"
    gitdir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../main/.git/worktrees/linked\n")
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "gitdir").write_text(str(worktree / ".git") + "\n")
    _write(home / ".claude.json", {"projects": {
        str(main): {"mcpServers": {"main-local": _server("main")}},
        str(worktree): {"mcpServers": {"wrong-key": _server("wrong")}},
    }})

    found, _ = mcp.offered(worktree, env={}, home=home)

    assert mcp.canonical_git_root(worktree) == main
    assert set(found) == {"main-local"}
    # A broken backlink cannot redirect the local-scope lookup elsewhere.
    (gitdir / "gitdir").write_text(str(home / "other" / ".git"))
    assert mcp.canonical_git_root(worktree) == worktree


def test_malformed_git_marker_retains_checkout_root(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (project / ".git").write_text("unrecognized marker")
    assert mcp.canonical_git_root(nested) == project


def test_local_scope_normalizes_project_key_to_unicode_nfc(tmp_path):
    home = tmp_path / "home"
    workdir = home / "cafe\u0301"
    workdir.mkdir(parents=True)
    key = unicodedata.normalize("NFC", str(workdir))
    _write(home / ".claude.json", {"projects": {
        key: {"mcpServers": {"local": _server("local")}},
    }})

    found, skipped = mcp.offered(workdir, env={}, home=home)

    assert not skipped
    assert set(found) == {"local"}


@given(st.sets(st.sampled_from(["alpha", "beta", "gamma", "delta"])))
def test_config_document_has_exactly_opted_in_names(names):
    """Selection never pulls an unrequested server into the launch document."""
    all_servers = {name: _server(name) for name in ("alpha", "beta", "gamma", "delta")}
    selected = {name: mcp.Found(name, "user", "/config", all_servers[name]) for name in names}
    document = mcp.config_document(selected)
    assert set(document["mcpServers"]) == names
    for name in names:
        assert document["mcpServers"][name] == all_servers[name]
