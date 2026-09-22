"""Verify the copied hook and Codex's runtime trust decision (C-14.1, C-14.2).

The identity and override are ports of v1 ``subfleet-guard``. ``TRUST`` pins
one reference path so the package can move: the same checked computation is
then applied to the installed hook path and compared with ``hooks/list``.
Only config.toml and hooks.json are copied to a scratch Codex home; credentials
and session stores are never copied or modified.

A verified verdict is cached the way v1 cached it (C-23.5, invariant 52): the
marker is keyed on the installed Codex version, the lane home, the override
string, and a fingerprint of the seeded ``config.toml`` and ``hooks.json``, it
is discarded after 30 days, and only the app-server probe is skipped on a hit —
the hook bytes, the TRUST pins, ``jq`` and the Codex version are checked on
every call. A refusal is never cached.

The hooks/list deadline is ``CODEX_GUARD_PREFLIGHT_TIMEOUT`` seconds (v1's
name; default 60). A timeout is reported as a timeout: trust is unverified, not
mismatched, and the refusal carries the probe pid, elapsed time, the request
and response lines, and the app-server's stderr tail so the next one can be
diagnosed from the attempt directory alone (2026-09-20 incident).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


HOOK_KEY = "/<session-flags>/config.toml:pre_tool_use:0:0"
HOOK_MATCHER = "Bash|apply_patch"
HOOK_STATUS = "never-rules guard"
HOOK_TIMEOUT = 60
#: v1's variable, v1's meaning: seconds to wait for the hooks/list answer.
TIMEOUT_ENV = "CODEX_GUARD_PREFLIGHT_TIMEOUT"
DEFAULT_TIMEOUT_S = 60.0
#: v1's variable: where verified markers live (default `$SUBFLEET_HOME/guard-cache`).
CACHE_ENV = "SUBFLEET_CODEX_GUARD_CACHE"
CACHE_TTL = timedelta(days=30)
SEED_FILES = ("config.toml", "hooks.json")
#: How much of the probe's conversation and stderr a refusal keeps.
TRANSCRIPT_LINES = 24
TRANSCRIPT_LINE_CHARS = 2000
STDERR_TAIL_BYTES = 4096
_SECRET_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_FIX = ("Stage the reviewed never-rules-hook.sh and TRUST in <state root>/guard, "
        "restore the pinned Codex version, then rerun subfleet doctor.")
_TIMEOUT_FIX = ("Check local CPU/I/O pressure and daemon scheduling, then repeat the guard preflight; "
                f"raise {TIMEOUT_ENV} (seconds, default {DEFAULT_TIMEOUT_S:g}) for the daemon if Codex "
                "startup is legitimately slow on this machine. A timeout does not establish guard-file "
                "or version drift; trust remains unverified.")
_CONFIG_FIX = f"Set {TIMEOUT_ENV} to a positive number of seconds (or unset it for the default), then retry."
_CACHE_CONFIG_FIX = (f"Set {CACHE_ENV} to an absolute path or to a relative path inside the state root "
                     "(or unset it for <state root>/guard-cache), then retry.")
_PROBE_FIX = ("Read stderr_tail and exit_status in the attempt's guard-preflight.json (or run "
              "`subfleet doctor --live`) for the app-server's own error; trust remains unverified.")
_ENVIRONMENT_FIX = "Put back the missing directory or binary named in the message, then rerun subfleet doctor."

# Refusal kinds (PreflightResult.kind): what the verdict rests on.
VERIFIED = "verified"        # hooks/list answered and matched
CACHED = "cached"            # a verified marker for this exact key was reused
TIMEOUT = "timeout"          # no hooks/list answer (or no --version) before the deadline
TRUST = "trust"              # hook bytes, TRUST pins, or the runtime hooks/list answer mismatch
VERSION = "version"          # installed Codex differs from the pinned version
CONFIG = "config"            # the preflight itself was misconfigured (deadline)
ENVIRONMENT = "environment"  # jq, the binary, the home, the workdir or the scratch root is missing
PROBE = "probe"              # app-server died, could not be started, or answered unusably


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
    kind: str = ""
    cached: bool = False
    cache_key: str | None = None
    timeout_s: float | None = None
    elapsed_s: float | None = None
    probe_pid: int | None = None
    executable: str | None = None
    stderr_tail: str = ""
    transcript: tuple[str, ...] = ()
    first_byte_s: float | None = None   # launcher start to the app-server's first stdout byte
    exit_status: int | None = None      # the probe's exit status after reaping, when known

    def record(self) -> dict[str, Any]:
        """A JSON-safe diagnostic record; never contains credentials (C-10.5)."""
        return {
            "ok": self.ok, "code": self.code, "kind": self.kind, "message": self.message,
            "fix": self.fix, "cached": self.cached, "cache_key": self.cache_key,
            "timeout_s": self.timeout_s, "elapsed_s": self.elapsed_s, "first_byte_s": self.first_byte_s,
            "probe_pid": self.probe_pid, "exit_status": self.exit_status,
            "executable": self.executable, "version": self.version, "hooks_hash": self.hooks_hash,
            "warnings": list(self.warnings), "stderr_tail": self.stderr_tail,
            "transcript": list(self.transcript),
        }


class _Refusal(Exception):
    """Carries the kind of a refusal through the single except clause below."""

    def __init__(self, kind: str, message: str, fix: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.fix = fix


class _ProbeError(Exception):
    """A probe failure with the diagnostics gathered up to that point."""

    def __init__(self, message: str, diagnostics: dict[str, Any], *, timed_out: bool = False):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.timed_out = timed_out


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


def guard_paths(state_root: str | Path | None = None, *,
                hook_path: str | Path | None = None,
                trust_path: str | Path | None = None) -> tuple[Path, Path]:
    """Resolve operator-owned overlay locations without creating files.

    There is deliberately no bundled or test-fixture fallback. The legacy
    TRUST override remains supported; an explicit argument takes precedence.
    """
    directory = Path(state_root if state_root is not None else _state_root()).expanduser() / "guard"
    return (Path(hook_path) if hook_path is not None else directory / "never-rules-hook.sh",
            Path(trust_path if trust_path is not None else
                 os.environ.get("SUBFLEET_GUARD_TRUST") or directory / "TRUST"))


def load_guard(state_root: str | Path | None = None, *,
               hook_path: str | Path | None = None,
               trust_path: str | Path | None = None) -> tuple[Path, Path, dict[str, Any]]:
    """Read and validate the reviewed overlay; doctor and launches share this check.

    This checks only local files. It cannot establish runtime Codex trust, which
    still requires the version check and hooks/list preflight. Raises ValueError
    on an absent, malformed or mismatched overlay, without writing anything.
    """
    hook, pin = guard_paths(state_root, hook_path=hook_path, trust_path=trust_path)
    try:
        hook, pin = hook.resolve(strict=True), pin.resolve(strict=True)
        trust = json.loads(pin.read_text())
        hook_bytes = hook.read_bytes()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"guard file unreadable: {exc}") from None
    except (ValueError, UnicodeError):
        raise ValueError("guard TRUST is not valid UTF-8 JSON") from None
    fields = ("hook_sha256", "codex_version", "reference_hook_path", "hooks_trust_hash", "override")
    if not isinstance(trust, dict) or any(
            not isinstance(trust.get(field), str) or not trust[field].strip() for field in fields):
        raise ValueError("guard TRUST must contain nonempty string pins: " + ", ".join(fields))
    if hashlib.sha256(hook_bytes).hexdigest() != trust["hook_sha256"]:
        raise ValueError("never-rules hook SHA-256 does not match TRUST")
    if not os.access(hook, os.X_OK):
        raise ValueError("never-rules hook is not executable")
    reference = trust["reference_hook_path"]
    if hooks_trust_hash(reference) != trust["hooks_trust_hash"]:
        raise ValueError("Codex hooks trust hash does not match TRUST")
    if override_string(reference) != trust["override"]:
        raise ValueError("Codex guard override does not match TRUST")
    return hook, pin, trust


def resolve_timeout(timeout_s: float | None = None) -> float:
    """The hooks/list deadline: an explicit value, else ``CODEX_GUARD_PREFLIGHT_TIMEOUT``,
    else 60 s. Raises ValueError naming the variable for an unusable setting."""
    if timeout_s is not None:
        value = timeout_s
        source = "preflight timeout"
    else:
        raw = os.environ.get(TIMEOUT_ENV)
        if raw is None or not raw.strip():
            return DEFAULT_TIMEOUT_S
        source = f"{TIMEOUT_ENV}={raw!r}"
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{source} is not a number of seconds") from None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{source} must be a number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{source} must be finite and positive")
    return float(value)


def read_seed_files(home: str | Path | None) -> dict[str, bytes]:
    """The seed files (``config.toml``, ``hooks.json``) present in a lane home, as bytes."""
    seeds: dict[str, bytes] = {}
    if home is not None:
        for name in SEED_FILES:
            path = Path(home) / name
            if path.is_file():
                seeds[name] = path.read_bytes()
    return seeds


def seed_fingerprint(home: str | Path | None = None, *, seeds: dict[str, bytes] | None = None) -> str:
    """sha256 over the seed files (name + content) present in a lane home (v1).

    Pass ``seeds`` to fingerprint the exact bytes that were copied into the
    scratch home, so a concurrent edit cannot produce a marker for content that
    was never probed.
    """
    digest = hashlib.sha256()
    if seeds is None:
        seeds = read_seed_files(home)
    for name in SEED_FILES:
        if name in seeds:
            digest.update(name.encode() + b"\n" + seeds[name] + b"\n")
    return digest.hexdigest()


def cache_key(version: str, home: str | Path, override: str, fingerprint: str,
              *, overlay: dict[str, Any] | None = None) -> str:
    """Key the runtime verdict on the launch inputs and reviewed overlay pins.

    The overlay includes its hook SHA, so even an approved replacement at the
    same path forces re-verification. Old markers remain harmless on disk.
    """
    material = f"{version}|{home}|{override}|{fingerprint}"
    if overlay is not None:
        material += "|" + json.dumps(overlay, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def cache_dir(state_root: str | Path | None = None) -> Path:
    """``$SUBFLEET_CODEX_GUARD_CACHE`` (v1's name), else ``<state root>/guard-cache``.

    A relative override resolves under the state root, never the process cwd
    (C-2.1). ``state_root`` is the daemon's root when it has one; otherwise
    ``$SUBFLEET_HOME``.
    """
    root = Path(state_root) if state_root is not None else Path(_state_root())
    override = os.environ.get(CACHE_ENV)
    if override:
        path = Path(os.path.expanduser(override))
        if path.is_absolute():
            return path  # the operator's explicit choice (v1 kept markers under ~/.cache)
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError(f"{CACHE_ENV}={override!r} is relative and leaves the state root {root}")
        return root / path
    return root / "guard-cache"


def _marker_path(directory: Path, key: str) -> Path:
    return directory / f"guard-ok-{key}.json"


def read_cached_verdict(directory: Path, key: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The marker for ``key`` when it is readable and younger than 30 days (C-23.5)."""
    path = _marker_path(directory, key)
    try:
        marker = json.loads(path.read_bytes())
        verified_at = datetime.fromisoformat(marker["verified_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    age = (now or datetime.now(timezone.utc)) - verified_at
    if age > CACHE_TTL or age < timedelta(0):
        return None  # expired, or stamped in the future (a clock or a hand edit)
    return marker if isinstance(marker, dict) else None


def write_cached_verdict(directory: Path, key: str, marker: dict[str, Any]) -> None:
    """Publish a marker atomically, then prune markers nobody refreshed in 30 days."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _marker_path(directory, key)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(marker, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    prune_cached_verdicts(directory)


def prune_cached_verdicts(directory: Path, *, now: datetime | None = None) -> int:
    """Remove markers older than 30 days (v1 pruned on every write)."""
    removed = 0
    try:
        entries = list(directory.glob("guard-ok-*.json"))
    except OSError:
        return 0
    for path in entries:
        key = path.name[len("guard-ok-"):-len(".json")]
        if read_cached_verdict(directory, key, now=now) is None:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _jq_available() -> bool:
    # This exact dependency is needed by the unmodified, fail-open v1 hook.
    return bool(shutil.which("jq") or any(
        os.access(path, os.X_OK) for path in ("/opt/homebrew/bin/jq", "/usr/bin/jq")))


def _stop_probe(process: subprocess.Popen[bytes]) -> None:
    """Reap the probe's own new session, including native-launcher children."""
    # ESRCH: the group is gone. EPERM: macOS answers it for a group whose only
    # member is the exited, not yet reaped, leader; wait() below reaps it.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The launcher can exit before its child; target its recorded group too.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass  # SIGKILL is delivered; a launcher mid page-in reaps a moment later.
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _clip(text: str, limit: int = TRANSCRIPT_LINE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def _stderr_tail(stream) -> str:
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - STDERR_TAIL_BYTES))
        return stream.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _version(executable: str, *, env: dict[str, str], timeout_s: float) -> tuple[int | None, str, str]:
    """``codex --version`` in its own session, TERM then KILL at the deadline.

    Returns (exit status, stdout, stderr tail); a None status means the deadline
    passed and the launcher and its native child were reaped.
    """
    process = subprocess.Popen([executable, "--version"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _stop_probe(process)
        partial = exc.stderr if isinstance(exc.stderr, bytes) else b""
        return None, "", partial[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
    except BaseException:
        _stop_probe(process)
        raise
    return (process.returncode, stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_BYTES:])


def _hooks_list(codex_bin: str, *, home: Path, workdir: Path,
                override: str, env: dict[str, str], timeout_s: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask app-server for hooks/list; return (response, diagnostics).

    Diagnostics: the probe pid, elapsed seconds, the request lines sent and the
    response lines received (both clipped), the app-server's stderr tail, and
    whether the deadline passed. A failure raises _ProbeError carrying the same.
    """
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "subfleet-guard-preflight", "title": "subfleet guard",
                           "version": "2.0.0"}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [str(workdir)]}},
    ]
    probe_env = dict(env, CODEX_HOME=str(home))
    sent: list[str] = []
    # The lines just before a hang, and the hooks/list answer after a chatty
    # start, are the useful ones: keep the last responses, not the first.
    received: collections.deque[str] = collections.deque(maxlen=max(TRANSCRIPT_LINES - 3, 1))
    diagnostics: dict[str, Any] = {"probe_pid": None, "elapsed_s": None, "transcript": [],
                                   "stderr_tail": "", "timed_out": False, "first_byte_s": None}
    started = time.monotonic()
    # This is a metadata-only app-server probe. No exec request is ever sent.
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [codex_bin, "app-server", "-c", "features.plugins=false", "-c", override],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            cwd=workdir, env=probe_env, start_new_session=True,
        )
        diagnostics["probe_pid"] = process.pid
        try:
            assert process.stdin is not None and process.stdout is not None
            wire = "".join(json.dumps(request) + "\n" for request in requests).encode()
            for request in requests:
                sent.append("> " + _clip(json.dumps(request)))
            try:
                process.stdin.write(wire)
                process.stdin.flush()  # Keep stdin open until hooks/list responds.
            except OSError as exc:
                raise _ProbeError(f"Codex app-server exited before reading the request ({exc})",
                                  diagnostics) from None
            deadline = started + timeout_s
            pending = b""
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    # A paused daemon (SIGSTOP from a lid-closed guard, a debugger,
                    # App Nap) wakes with the deadline already past while the answer
                    # sits in the pipe: look once, without waiting, before giving up.
                    if not selector.select(max(remaining, 0)):
                        diagnostics["timed_out"] = True
                        raise _ProbeError(
                            f"Codex app-server did not answer hooks/list before the {timeout_s:g}s deadline",
                            diagnostics, timed_out=True)
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise _ProbeError("Codex app-server exited without a hooks/list response", diagnostics)
                    if diagnostics["first_byte_s"] is None:
                        diagnostics["first_byte_s"] = round(time.monotonic() - started, 3)
                    total += len(chunk)
                    if total > 1_000_000:
                        raise _ProbeError("Codex app-server response exceeded the preflight limit", diagnostics)
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        received.append("< " + _clip(line.decode("utf-8", errors="replace")))
                        try:
                            response = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(response, dict) and response.get("id") == 2:
                            return response, diagnostics
        except OSError as exc:
            # Reading the pipe failed; the probe, not trust, is what is broken.
            raise _ProbeError(f"Codex app-server probe I/O failed ({exc})", diagnostics) from None
        finally:
            try:
                _stop_probe(process)
            except OSError as exc:
                diagnostics["reap_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                diagnostics["transcript"] = [*sent, *received]
                diagnostics["elapsed_s"] = round(time.monotonic() - started, 3)
                diagnostics["exit_status"] = process.returncode
                diagnostics["stderr_tail"] = _stderr_tail(stderr)


def preflight(codex_bin: str | Path, *, home: str | Path | None = None,
              workdir: str | Path | None = None, hook_path: str | Path | None = None,
              trust_path: str | Path | None = None, timeout_s: float | None = None,
              use_cache: bool = True, cache_directory: str | Path | None = None,
              state_root: str | Path | None = None) -> PreflightResult:
    """Fail closed on file, version, override, or runtime hooks trust drift.

    Callers should pass the lane home and job workdir. Omitting ``home`` uses
    ``CODEX_HOME`` when set; without either, doctor checks a fresh empty home
    and nothing is cached. Omitting ``trust_path`` uses ``SUBFLEET_GUARD_TRUST``
    when set, then ``<state root>/guard/TRUST``. An explicit path takes precedence.
    The hook defaults to ``<state root>/guard/never-rules-hook.sh``. Neither file
    is supplied by the portable core; an absent overlay refuses executable work.
    Omitting ``timeout_s`` uses ``CODEX_GUARD_PREFLIGHT_TIMEOUT``, then 60 s; the
    deadline bounds each of the two Codex calls (``--version`` and hooks/list).
    ``state_root`` (the daemon's root) places the scratch home and the markers;
    it defaults to ``$SUBFLEET_HOME``. Every refusal has code 7, a ``kind`` and an
    actionable fix. No provider work is run.
    """
    version = None
    hooks_hash = None
    override = None
    executable = None
    key = None
    deadline = None
    diagnostics: dict[str, Any] = {}
    started = time.monotonic()
    try:
        try:
            deadline = resolve_timeout(timeout_s)
        except ValueError as exc:
            raise _Refusal(CONFIG, str(exc), _CONFIG_FIX) from None
        try:
            hook, _, trust = load_guard(state_root, hook_path=hook_path, trust_path=trust_path)
        except ValueError as exc:
            raise _Refusal(TRUST, str(exc)) from None
        if not _jq_available():
            raise _Refusal(ENVIRONMENT, "jq is required by the never-rules hook",
                           "Install jq on PATH, then rerun subfleet doctor.")
        found = shutil.which(str(codex_bin))
        if found is None:
            raise _Refusal(ENVIRONMENT, "Codex binary was not found or is not executable", _ENVIRONMENT_FIX)
        executable = str(Path(found).resolve())
        env = {key_name: value for key_name, value in os.environ.items() if key_name not in _SECRET_ENV}
        status, stdout, stderr_text = _version(executable, env=env, timeout_s=deadline)
        if status is None:
            said = f"; stderr: {_clip(stderr_text.strip(), 400)}" if stderr_text.strip() else ""
            raise _Refusal(TIMEOUT, f"Codex --version did not answer before the {deadline:g}s deadline "
                                    f"({executable}); guard trust is unverified, not mismatched{said}",
                           _TIMEOUT_FIX)
        if status != 0:
            raise _Refusal(ENVIRONMENT, f"Codex --version exited {status} ({executable}): "
                                        f"{_clip(stderr_text.strip(), 400) or 'no stderr'}", _ENVIRONMENT_FIX)
        version = stdout.strip()
        if version != trust["codex_version"]:
            raise _Refusal(VERSION, f"Codex version {version!r} does not match TRUST ({trust['codex_version']})")
        try:
            cwd = Path(workdir).resolve(strict=True) if workdir is not None else Path.cwd()
        except OSError as exc:
            raise _Refusal(ENVIRONMENT, f"preflight workdir is missing: {exc}", _ENVIRONMENT_FIX) from None
        if not cwd.is_dir():
            raise _Refusal(ENVIRONMENT, "preflight workdir must be an existing directory", _ENVIRONMENT_FIX)
        lane_home = home if home is not None else os.environ.get("CODEX_HOME")
        try:
            source_home = Path(lane_home).resolve(strict=True) if lane_home is not None else None
        except OSError as exc:
            raise _Refusal(ENVIRONMENT, f"preflight Codex home is missing: {exc}", _ENVIRONMENT_FIX) from None
        if source_home is not None and not source_home.is_dir():
            raise _Refusal(ENVIRONMENT, "preflight Codex home must be an existing directory", _ENVIRONMENT_FIX)
        override = override_string(hook)
        hooks_hash = hooks_trust_hash(hook)
        try:
            markers = Path(cache_directory) if cache_directory is not None else cache_dir(state_root)
        except ValueError as exc:
            raise _Refusal(CONFIG, str(exc), _CACHE_CONFIG_FIX) from None
        seeds: dict[str, bytes] = {}
        if source_home is not None:
            try:
                seeds = read_seed_files(source_home)
            except OSError as exc:
                raise _Refusal(ENVIRONMENT, f"preflight cannot read the lane's seed files: {exc}",
                               _ENVIRONMENT_FIX) from None
            fingerprint = seed_fingerprint(seeds=seeds)
            key = cache_key(version, source_home, override, fingerprint, overlay=trust)
            if use_cache and read_cached_verdict(markers, key) is not None:
                return PreflightResult(True, 0, "Codex never-rules guard trust verified (cached verdict)",
                                       version=version, hooks_hash=hooks_hash, override=override,
                                       kind=CACHED, cached=True, cache_key=key, timeout_s=deadline,
                                       elapsed_s=round(time.monotonic() - started, 3), executable=executable)
        try:
            scratch_root = _scratch_root(state_root)
        except OSError as exc:
            raise _Refusal(ENVIRONMENT, f"preflight scratch root is unusable: {exc}", _ENVIRONMENT_FIX) from None
        with tempfile.TemporaryDirectory(prefix="subfleet-guard-preflight-", dir=scratch_root) as scratch:
            scratch_home = Path(scratch) / "home"
            scratch_home.mkdir(mode=0o700)
            for name, content in seeds.items():
                # The bytes that were fingerprinted are the bytes that are probed.
                (scratch_home / name).write_bytes(content)
                (scratch_home / name).chmod(0o600)
            try:
                response, diagnostics = _hooks_list(executable, home=scratch_home, workdir=cwd,
                                                    override=override, env=env, timeout_s=deadline)
            except _ProbeError as exc:
                diagnostics = exc.diagnostics
                if exc.timed_out:
                    raise _Refusal(TIMEOUT, f"{exc} (probe pid {diagnostics.get('probe_pid')}, "
                                            f"{diagnostics.get('elapsed_s')}s elapsed, {executable}); "
                                            "guard trust is unverified, not mismatched", _TIMEOUT_FIX) from None
                raise _Refusal(PROBE, f"{exc} (probe pid {diagnostics.get('probe_pid')}, exit status "
                                      f"{diagnostics.get('exit_status')}, {executable}); "
                                      "guard trust is unverified, not mismatched", _PROBE_FIX) from None
        if "error" in response:
            raise _Refusal(TRUST, "Codex hooks/list returned a JSON-RPC error")
        data = response.get("result", {}).get("data", [])
        if len(data) != 1 or not isinstance(data[0], dict):
            raise _Refusal(TRUST, "Codex hooks/list did not return exactly one workdir result")
        if data[0].get("cwd") != str(cwd):
            raise _Refusal(TRUST, "Codex hooks/list returned trust for a different workdir")
        entries = [entry for entry in data[0].get("hooks", [])
                   if isinstance(entry, dict) and entry.get("key") == HOOK_KEY]
        if len(entries) != 1:
            raise _Refusal(TRUST, "Codex hooks/list did not list exactly one never-rules guard")
        entry = entries[0]
        if (entry.get("enabled") is not True or entry.get("trustStatus") != "trusted"
                or entry.get("currentHash") != hooks_hash):
            raise _Refusal(TRUST, "Codex hooks/list guard is disabled, untrusted, or has a mismatched hash")
        if data[0].get("errors"):
            raise _Refusal(TRUST, "Codex hooks/list reported errors")
        warnings = tuple(str(warning) for warning in data[0].get("warnings", []))
        if use_cache and key is not None and source_home is not None:
            try:
                write_cached_verdict(markers, key, {
                    "version": version, "home": str(source_home), "executable": executable,
                    "config_fingerprint": fingerprint, "hooks_trust_hash": hooks_hash,
                    "override_sha256": hashlib.sha256(override.encode()).hexdigest(),
                    "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "elapsed_s": diagnostics.get("elapsed_s"),
                })
            except OSError:
                pass  # A verdict that cannot be recorded is still a verdict.
        return PreflightResult(True, 0, "Codex never-rules guard trust verified", version=version,
                               hooks_hash=hooks_hash, override=override, warnings=warnings,
                               kind=VERIFIED, cache_key=key, timeout_s=deadline,
                               elapsed_s=round(time.monotonic() - started, 3),
                               probe_pid=diagnostics.get("probe_pid"), executable=executable,
                               stderr_tail=diagnostics.get("stderr_tail", ""),
                               transcript=tuple(diagnostics.get("transcript", ())),
                               first_byte_s=diagnostics.get("first_byte_s"),
                               exit_status=diagnostics.get("exit_status"))
    except (_Refusal, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, _Refusal):
            kind, fix = exc.kind, exc.fix or _FIX
        elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            kind, fix = TIMEOUT, _TIMEOUT_FIX
        elif isinstance(exc, (OSError, subprocess.SubprocessError)):
            # A missing file, an unwritable root or a process that could not be
            # driven says nothing about trust; only TRUST/response shape does.
            kind, fix = (PROBE, _PROBE_FIX) if diagnostics.get("probe_pid") else (ENVIRONMENT, _ENVIRONMENT_FIX)
        else:
            kind, fix = TRUST, _FIX
        prefix = "Guard preflight timed out" if kind == TIMEOUT else "Guard preflight refused"
        return PreflightResult(False, 7, f"{prefix}: {exc}", fix, version=version, hooks_hash=hooks_hash,
                               kind=kind, cache_key=key, timeout_s=deadline,
                               elapsed_s=round(time.monotonic() - started, 3),
                               probe_pid=diagnostics.get("probe_pid"), executable=executable,
                               stderr_tail=diagnostics.get("stderr_tail", ""),
                               transcript=tuple(diagnostics.get("transcript", ())),
                               first_byte_s=diagnostics.get("first_byte_s"),
                               exit_status=diagnostics.get("exit_status"))


def _state_root() -> str:
    return os.path.expanduser(os.environ.get("SUBFLEET_HOME", "~/.subfleet"))


def _scratch_root(state_root: str | Path | None = None) -> str:
    """C-2.1, C-23.23: the preflight scratch home lives under the state root, never /tmp."""
    root = os.path.join(str(state_root) if state_root is not None else _state_root(), "tmp")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root
