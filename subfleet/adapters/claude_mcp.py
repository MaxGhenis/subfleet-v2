"""The MCP servers a writable Claude job may name (C-12.9, d714).

A writable Claude launch starts with no MCP servers (`--strict-mcp-config
--mcp-config '{"mcpServers":{}}'`), as a read-only launch always has. A job that
needs some names them (`subfleet run --mcp NAME`). This module finds each name
where Claude Code itself would have found it for the job's workdir, and the
daemon keeps a copy of exactly those entries with the job, so every attempt of
the job, and every resume of it, starts those servers and no others.

How Claude Code 2.1.284 resolves them, read from its binary on 2026-09-30
(`~/.local/share/claude/versions/2.1.284`; the offsets are where each function
sits in that file):

* Project scope (`k7e("project")`, offset 185279201): `.mcp.json` in the launch
  directory and in every ancestor up to, not including, the filesystem root, read
  root-most first and merged with `Object.assign`, so a nearer file's server
  replaces a farther one of the same name. A file that is not a regular file or
  is larger than 2 MiB (`zj=2097152`, `l0t`) is skipped, and so is one that is not
  JSON with an object `mcpServers`.
* User scope (`k7e("user")`): `mcpServers` in the global config, `.claude.json`
  in `$CLAUDE_CONFIG_DIR`, else in the home directory (`M7n`, offset 177412031),
  unless a legacy `.config.json` sits in the config directory (`Lo`).
* Local scope (`k7e("local")`, `Ts`, offset 180301703): `projects[<key>].mcpServers`
  in the same file. The key is the canonical git root of the launch directory
  (`VRe`, `Bo`, `Oe`: a linked worktree counts as its main checkout), else the
  directory itself, normalized to Unicode NFC (`vn`).
* The merge (`px`, offset 185281614) is `{...user, ...project, ...local, ...}`:
  local beats project, project beats user.
* With `--strict-mcp-config` (offset 193750073) none of these is read
  (`kn=(Mt||…)?{servers:{}}:px(…)`) and only `--mcp-config` servers start;
  headless claude.ai connectors stay off as well (`dit()` is `!YA()&&…`, and
  `YA()` is the strict flag).
* Each `--mcp-config` value (offset 193742557) is parsed as JSON, else read as a
  file, both with `${VAR}` expansion, so an entry copied here unchanged expands
  in the launch exactly as it would have in its source.

The official documentation corroborates the scopes and precedence:
https://code.claude.com/docs/en/mcp-quickstart#find-your-configuration-on-disk
https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence
https://code.claude.com/docs/en/cli-reference (``--strict-mcp-config``).
In particular, ``~/.mcp.json`` is a project-scope ancestor when the launch is
under the user's home; the user-scope state file is ``~/.claude.json``.

The definitions are read by the daemon, never taken from a client: a submission
carries names only. Claude still validates each selected server's transport
configuration and expands environment variables at launch.
"""

from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..sessions.transcripts import read_regular

PROJECT_FILE = ".mcp.json"
#: Claude Code skips a project `.mcp.json` larger than this (`zj`).
PROJECT_MAX_BYTES = 2_097_152
#: The global config is Claude Code's own state file, read whole there; bounded here.
GLOBAL_MAX_BYTES = 64 << 20
#: The longest name `--mcp` takes.
NAME_MAX = 128
#: Scopes in the order a name is looked up: the first that has it wins (`px`).
SCOPES = ("local", "project", "user")
#: The job's copy of the servers it named, in its job directory.
JOB_CONFIG_NAME = "mcp.json"


@dataclass(frozen=True)
class Found:
    """One selected definition: where Claude Code reads it, and its entry exactly
    as written there."""
    name: str
    scope: str
    source: str
    config: dict[str, Any]


class UnknownServer(ValueError):
    """A name no source offers. A ValueError, so submit answers it with exit 2."""

    def __init__(self, missing: Iterable[str], offered: Iterable[str], skipped: Iterable[str] = ()):
        self.missing = tuple(missing)
        self.offered = tuple(sorted(offered))
        self.skipped = tuple(skipped)
        names = ", ".join(self.missing)
        known = ", ".join(self.offered) if self.offered else "none"
        message = (f"unknown MCP server{'s' if len(self.missing) > 1 else ''} {names}: Claude Code "
                   f"would offer this workdir {known}")
        if self.skipped:
            message += f" (sources skipped: {'; '.join(self.skipped)})"
        super().__init__(message)


def validate_names(names: Iterable[Any]) -> tuple[str, ...]:
    """The names a job asks for, sorted and once each.

    A name is a string of 1 to `NAME_MAX` characters with no control character
    and no whitespace at either end; whether any source offers it is `resolve`'s
    question."""
    if isinstance(names, (str, bytes)):
        raise ValueError("mcp_servers must be a list of server names")
    checked = set()
    for name in names:
        if not isinstance(name, str) or not name or len(name) > NAME_MAX:
            raise ValueError(f"an MCP server name is 1 to {NAME_MAX} characters: {name!r}")
        if name != name.strip() or any(unicodedata.category(char)[0] == "C" for char in name):
            raise ValueError(f"an MCP server name has no surrounding space or control characters: {name!r}")
        checked.add(name)
    return tuple(sorted(checked))


def project_files(directory: str | Path) -> list[Path]:
    """`.mcp.json` in `directory` and each ancestor but the root, root-most first."""
    current = Path(directory)
    chain = []
    while current != Path(current.anchor):
        chain.append(current / PROJECT_FILE)
        if current.parent == current:
            break
        current = current.parent
    return list(reversed(chain))


def global_config_path(env: Mapping[str, str] | None = None, home: str | Path | None = None) -> Path:
    """The file Claude Code keeps user- and local-scope servers in (`Lo`, `M7n`)."""
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    configured = env.get("CLAUDE_CONFIG_DIR")
    legacy = (Path(configured) if configured else home / ".claude") / ".config.json"
    if legacy.exists():
        return legacy
    return (Path(configured) if configured else home) / ".claude.json"


def _git_root(directory: Path) -> Path | None:
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _small_text(path: Path) -> str:
    return read_regular(path, 1 << 16).decode("utf-8").strip()


def canonical_git_root(directory: str | Path) -> Path | None:
    """The checkout Claude Code keys local-scope settings by (`Bo`, `Oe`).

    The nearest ancestor holding `.git`; when that `.git` is a linked worktree's
    file, the main checkout it belongs to, provided the worktree's records point
    both ways, as `Oe` requires. Anything unexpected keeps the root itself, as
    `Oe` does."""
    root = _git_root(Path(directory))
    if root is None:
        return None
    marker = root / ".git"
    if marker.is_dir():
        return root
    try:
        text = _small_text(marker)
        if not text.startswith("gitdir:"):
            return root
        gitdir = (root / text[len("gitdir:"):].strip()).resolve()
        common = (gitdir / _small_text(gitdir / "commondir")).resolve()
        if gitdir.parent != common / "worktrees":
            return root
        back = (gitdir / _small_text(gitdir / "gitdir")).resolve()
        if back != (root.resolve() / ".git"):
            return root
    except (OSError, UnicodeDecodeError, ValueError):
        return root
    return common.parent if common.name == ".git" else common


def _servers(value: Any) -> dict[str, dict[str, Any]] | None:
    """A config's `mcpServers` when it is an object, keeping only object entries."""
    if not isinstance(value, dict):
        return None
    servers = value.get("mcpServers", {})
    if not isinstance(servers, dict):
        return None
    return {name: entry for name, entry in servers.items() if isinstance(name, str) and isinstance(entry, dict)}


def _read_json(path: Path, limit: int) -> Any:
    return json.loads(read_regular(path, limit).decode("utf-8"))


def offered(workdir: str | Path, *, env: Mapping[str, str] | None = None,
            home: str | Path | None = None) -> tuple[dict[str, Found], list[str]]:
    """Available file-based server definitions for a session in `workdir`, by
    name, and a note for each source skipped. Transport validation and connection
    success remain Claude Code's responsibility at launch."""
    workdir = Path(workdir)
    by_scope: dict[str, dict[str, Found]] = {scope: {} for scope in SCOPES}
    skipped: list[str] = []
    for path in project_files(workdir):
        try:
            servers = _servers(_read_json(path, PROJECT_MAX_BYTES))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            skipped.append(f"{path}: {exc}")
            continue
        if servers is None:
            skipped.append(f"{path}: mcpServers is not an object")
            continue
        for name, entry in servers.items():
            by_scope["project"][name] = Found(name, "project", str(path), entry)
    config_path = global_config_path(env, home)
    try:
        document = _read_json(config_path, GLOBAL_MAX_BYTES)
    except FileNotFoundError:
        document = {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        skipped.append(f"{config_path}: {exc}")
        document = {}
    if isinstance(document, dict):
        user = _servers(document) or {}
        for name, entry in user.items():
            by_scope["user"][name] = Found(name, "user", str(config_path), entry)
        key = unicodedata.normalize("NFC", str(canonical_git_root(workdir) or workdir))
        projects = document.get("projects")
        project = projects.get(key) if isinstance(projects, dict) else None
        for name, entry in (_servers(project) or {}).items():
            by_scope["local"][name] = Found(name, "local", f"{config_path} [project: {key}]", entry)
    merged: dict[str, Found] = {}
    for scope in reversed(SCOPES):
        merged.update(by_scope[scope])
    return merged, skipped


def resolve(workdir: str | Path, names: Iterable[str], *, env: Mapping[str, str] | None = None,
            home: str | Path | None = None) -> dict[str, Found]:
    """Exactly the named servers, as Claude Code would have found each one for
    `workdir`; `UnknownServer` names every name no source offers."""
    wanted = validate_names(names)
    available, skipped = offered(workdir, env=env, home=home)
    missing = [name for name in wanted if name not in available]
    if missing:
        raise UnknownServer(missing, available, skipped)
    return {name: available[name] for name in wanted}


def config_document(found: Mapping[str, Found] | Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """An `--mcp-config` document holding exactly these servers."""
    return {"mcpServers": {name: (entry.config if isinstance(entry, Found) else entry)
                           for name, entry in sorted(found.items())}}


def sources(found: Mapping[str, Found]) -> dict[str, dict[str, str]]:
    """Where each named server came from: what `runs show` reports, never its entry."""
    return {name: {"scope": entry.scope, "source": entry.source} for name, entry in sorted(found.items())}


__all__ = ["Found", "JOB_CONFIG_NAME", "PROJECT_FILE", "PROJECT_MAX_BYTES", "UnknownServer",
           "canonical_git_root", "config_document", "global_config_path", "offered",
           "project_files", "resolve", "sources", "validate_names"]
