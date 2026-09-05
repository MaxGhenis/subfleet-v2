"""Verify the copied hook and Codex's runtime trust decision (C-14.1, C-14.2).

The identity and override are ports of v1 ``subfleet-guard``. ``TRUST`` pins
one reference path so the package can move: the same checked computation is
then applied to the installed hook path and compared with ``hooks/list``.
Only config.toml and hooks.json are copied to a scratch Codex home; credentials
and session stores are never copied or modified. There is no persistent cache.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any


HOOK_PATH = Path(__file__).with_name("never-rules-hook.sh")
TRUST_PATH = Path(__file__).with_name("TRUST")
HOOK_KEY = "/<session-flags>/config.toml:pre_tool_use:0:0"
HOOK_MATCHER = "Bash|apply_patch"
HOOK_STATUS = "never-rules guard"
HOOK_TIMEOUT = 60
_SECRET_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_FIX = "Restore the reviewed guard files and pinned Codex version, then rerun subfleet doctor."


@dataclass(frozen=True)
class PreflightResult:
    """Data consumed by doctor and daemon before a Codex launch (C-14.2)."""

    ok: bool
    code: int
    message: str
    fix: str | None = None
    version: str | None = None
    hooks_hash: str | None = None
    override: str | None = None
    warnings: tuple[str, ...] = ()


def _hook_command(hook_path: str | Path) -> str:
    path = str(hook_path)
    if not Path(path).is_absolute() or any(ord(char) < 32 for char in path):
        raise ValueError("guard hook path must be absolute and contain no control characters")
    # v1 uses LC_ALL=C and POSIX single quotes, not shlex.quote's spelling.
    if re.fullmatch(r"[A-Za-z0-9@%+=:,./_-]+", path, flags=re.ASCII):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def hooks_trust_hash(hook_path: str | Path, *, timeout: int = HOOK_TIMEOUT) -> str:
    """Codex's normalized identity hash, with v1's jq -cnS byte encoding."""
    identity = {
        "event_name": "pre_tool_use",
        "matcher": HOOK_MATCHER,
        "hooks": [{"type": "command", "command": _hook_command(hook_path),
                   "timeout": timeout, "async": False, "statusMessage": HOOK_STATUS}],
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def override_string(hook_path: str | Path) -> str:
    """Return v1's complete ``-c`` argument, including the ``hooks=`` prefix."""
    command = _hook_command(hook_path).replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'hooks={{PreToolUse=[{{matcher="{HOOK_MATCHER}",hooks=[{{type="command",'
        f'command="{command}",timeout={HOOK_TIMEOUT},statusMessage="{HOOK_STATUS}"}}]}}],'
        f'state={{"{HOOK_KEY}"={{trusted_hash="{hooks_trust_hash(hook_path)}",enabled=true}}}}}}'
    )


def _jq_available() -> bool:
    # This exact dependency is needed by the unmodified, fail-open v1 hook.
    return bool(shutil.which("jq") or any(
        os.access(path, os.X_OK) for path in ("/opt/homebrew/bin/jq", "/usr/bin/jq")))


def _stop_probe(process: subprocess.Popen[bytes]) -> None:
    """Reap the probe's own new session, including native-launcher children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The launcher can exit before its child; target its recorded group too.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=1)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def _hooks_list(codex_bin: str, *, home: Path, workdir: Path,
                override: str, env: dict[str, str], timeout_s: float) -> dict[str, Any]:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "subfleet-guard-preflight", "title": "subfleet guard",
                           "version": "2.0.0"}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [str(workdir)]}},
    ]
    probe_env = dict(env, CODEX_HOME=str(home))
    # This is a metadata-only app-server probe. No exec request is ever sent.
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [codex_bin, "app-server", "-c", "features.plugins=false", "-c", override],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            cwd=workdir, env=probe_env, start_new_session=True,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            wire = "".join(json.dumps(request) + "\n" for request in requests).encode()
            process.stdin.write(wire)
            process.stdin.flush()  # Keep stdin open until hooks/list responds.
            deadline = time.monotonic() + timeout_s
            pending = b""
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("Codex app-server did not answer hooks/list before the deadline")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise ValueError("Codex app-server exited without a hooks/list response")
                    total += len(chunk)
                    if total > 1_000_000:
                        raise ValueError("Codex app-server response exceeded the preflight limit")
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            response = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(response, dict) and response.get("id") == 2:
                            return response
        finally:
            _stop_probe(process)


def preflight(codex_bin: str | Path, *, home: str | Path | None = None,
              workdir: str | Path | None = None, hook_path: str | Path | None = None,
              trust_path: str | Path | None = None, timeout_s: float = 10) -> PreflightResult:
    """Fail closed on file, version, override, or runtime hooks trust drift.

    Callers should pass the lane home and job workdir. Omitting ``home`` uses
    ``CODEX_HOME`` when set; without either, doctor checks a fresh empty home.
    Omitting ``trust_path`` uses ``SUBFLEET_GUARD_TRUST`` when set, then the
    packaged ``TRUST`` file. An explicit path takes precedence.
    Every refusal has code 7 and an actionable fix. No provider work is run.
    """
    version = None
    hooks_hash = None
    try:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("preflight timeout must be finite and positive")
        hook = Path(hook_path) if hook_path is not None else HOOK_PATH
        hook = hook.resolve(strict=True)
        trust_file = trust_path if trust_path is not None else os.environ.get("SUBFLEET_GUARD_TRUST") or TRUST_PATH
        trust = json.loads(Path(trust_file).read_text())
        if hashlib.sha256(hook.read_bytes()).hexdigest() != trust["hook_sha256"]:
            raise ValueError("never-rules hook SHA-256 does not match TRUST")
        if not os.access(hook, os.X_OK):
            raise ValueError("never-rules hook is not executable")
        reference = trust["reference_hook_path"]
        if hooks_trust_hash(reference) != trust["hooks_trust_hash"]:
            raise ValueError("Codex hooks trust hash does not match TRUST")
        if override_string(reference) != trust["override"]:
            raise ValueError("Codex guard override does not match TRUST")
        if not _jq_available():
            return PreflightResult(False, 7, "jq is required by the never-rules hook",
                                   "Install jq on PATH, then rerun subfleet doctor.")
        executable = shutil.which(str(codex_bin))
        if executable is None:
            raise ValueError("Codex binary was not found or is not executable")
        executable = str(Path(executable).resolve())
        env = {key: value for key, value in os.environ.items() if key not in _SECRET_ENV}
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                env=env, timeout=timeout_s, check=False)
        version = result.stdout.strip()
        if result.returncode != 0 or version != trust["codex_version"]:
            raise ValueError(f"Codex version {version!r} does not match TRUST ({trust['codex_version']})")
        cwd = Path(workdir).resolve(strict=True) if workdir is not None else Path.cwd()
        if not cwd.is_dir():
            raise ValueError("preflight workdir must be an existing directory")
        lane_home = home if home is not None else os.environ.get("CODEX_HOME")
        source_home = Path(lane_home).resolve(strict=True) if lane_home is not None else None
        if source_home is not None and not source_home.is_dir():
            raise ValueError("preflight Codex home must be an existing directory")
        override = override_string(hook)
        hooks_hash = hooks_trust_hash(hook)
        with tempfile.TemporaryDirectory(prefix="subfleet-guard-preflight-", dir=_scratch_root()) as scratch:
            scratch_home = Path(scratch) / "home"
            scratch_home.mkdir(mode=0o700)
            if source_home is not None:
                for name in ("config.toml", "hooks.json"):
                    source = source_home / name
                    if source.is_file():
                        shutil.copyfile(source, scratch_home / name)
                        (scratch_home / name).chmod(0o600)
            response = _hooks_list(executable, home=scratch_home, workdir=cwd,
                                   override=override, env=env, timeout_s=timeout_s)
        if "error" in response:
            raise ValueError("Codex hooks/list returned a JSON-RPC error")
        data = response.get("result", {}).get("data", [])
        if len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("Codex hooks/list did not return exactly one workdir result")
        if data[0].get("cwd") != str(cwd):
            raise ValueError("Codex hooks/list returned trust for a different workdir")
        entries = [entry for entry in data[0].get("hooks", [])
                   if isinstance(entry, dict) and entry.get("key") == HOOK_KEY]
        if len(entries) != 1:
            raise ValueError("Codex hooks/list did not list exactly one never-rules guard")
        entry = entries[0]
        if (entry.get("enabled") is not True or entry.get("trustStatus") != "trusted"
                or entry.get("currentHash") != hooks_hash):
            raise ValueError("Codex hooks/list guard is disabled, untrusted, or has a mismatched hash")
        if data[0].get("errors"):
            raise ValueError("Codex hooks/list reported errors")
        warnings = tuple(str(warning) for warning in data[0].get("warnings", []))
        return PreflightResult(True, 0, "Codex never-rules guard trust verified", version=version,
                               hooks_hash=hooks_hash, override=override, warnings=warnings)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        return PreflightResult(False, 7, f"Guard preflight refused: {exc}", _FIX,
                               version=version, hooks_hash=hooks_hash)

def _scratch_root() -> str:
    """C-2.1, C-23.23: the preflight scratch home lives under the state root, never /tmp."""
    root = os.path.join(os.path.expanduser(os.environ.get("SUBFLEET_HOME", "~/.subfleet")), "tmp")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root
