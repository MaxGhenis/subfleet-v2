"""C-23.2–4: fresh, value-free isolation inspection for each provider attempt."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

from .base import AdapterError
from ..contracts import Sandbox

MANAGED_ENV = ("CLAUDE_CODE_MANAGED_SETTINGS_PATH", "CLAUDE_CODE_REMOTE_SETTINGS_PATH",
               "CLAUDE_CODE_MOCK_REMOTE_SETTINGS")
MEMORY_ENV = ("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", "CLAUDE_MEMORY_STORES",
              "CLAUDE_CODE_REMOTE_MEMORY_DIR")
CODEX_DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "hooks", "multi_agent", "multi_agent_v2",
    "tool_suggest", "skill_mcp_dependency_install", "computer_use", "browser_use",
    "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
    "image_generation", "goals", "memories", "shell_snapshot",
)
CODEX_CONFIG = (
    "project_doc_max_bytes=0", *(f"features.{name}=false" for name in CODEX_DISABLED_FEATURES),
    "shell_environment_policy.experimental_use_profile=false", 'history.persistence="none"',
    'web_search="disabled"',
)


def validate_isolated_review(sandbox, review_root, env=None):
    """C-6.5, C-23.3: refuse policy overrides rather than discarding them."""
    if Sandbox(sandbox) != Sandbox.READ_ONLY or not review_root:
        raise AdapterError("isolated review requires -s read-only and -D REVIEW_ROOT",
                           fix="pass -I -s read-only -D REVIEW_ROOT")
    for name in MANAGED_ENV:
        if name in (os.environ if env is None else env):
            raise AdapterError(f"isolated review inherits {name}",
                               fix=f"use an unmanaged review environment without {name}")


def claude_env_remove(env=None):
    """C-23.2: remove memory sources before any read-only Claude initialization."""
    names = os.environ if env is None else env
    return (*MEMORY_ENV, *(name for name in names if name.startswith("CLAUDE_COWORK_MEMORY_")))


def _flags():
    return [arg for config in CODEX_CONFIG for arg in ("-c", config)]


def inspect_codex(codex_bin, *, home, workdir, env, timeout_s=20):
    """Read metadata only: no thread, turn, MCP server or provider request starts."""
    from ..guard.preflight import _stop_probe

    clean_env = {key: value for key, value in env.items()
                 if key not in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    clean_env["CODEX_HOME"] = str(Path(home).expanduser().resolve(strict=True))
    messages = [
        {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "subfleet-isolation", "version": "2"}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "configRequirements/read", "params": {}},
        {"id": 3, "method": "config/read", "params": {"includeLayers": True}},
    ]
    process = subprocess.Popen(
        [codex_bin, "app-server", *_flags()], cwd=workdir, env=clean_env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.stdin.write("".join(json.dumps(message) + "\n" for message in messages).encode())
        process.stdin.flush()
        responses, pending, total = {}, b"", 0
        deadline = time.monotonic() + timeout_s
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while set(responses) != {2, 3}:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("config inspection timed out")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("config inspection ended without a response")
                total += len(chunk)
                if total > 8_000_000:
                    raise ValueError("config inspection exceeded size limit")
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    response = json.loads(line)
                    if response.get("id") in (2, 3):
                        responses[response["id"]] = response["result"]
    finally:
        _stop_probe(process)
    # Validate configuration before the separate inventory command runs.
    validate_codex_metadata(responses[2], responses[3], [])
    result = subprocess.run([codex_bin, "mcp", "list", "--json", *_flags()],
                            cwd=workdir, env=clean_env, capture_output=True,
                            text=True, timeout=timeout_s, check=False)
    if result.returncode:
        raise ValueError("MCP inventory failed")
    return responses[2], responses[3], json.loads(result.stdout)


def validate_codex_metadata(requirements, configuration, servers):
    """C-23.3: never include configuration values in a refusal or launch record."""
    if requirements["requirements"] is not None:
        raise AdapterError("isolated Codex review has managed requirements",
                           fix="use a Codex lane without managed requirements")
    layers = configuration["layers"]
    if not isinstance(layers, list) or not layers:
        raise ValueError("missing config layers")
    for layer in layers:
        source, config = layer["name"]["type"], layer["config"]
        if not isinstance(config, dict):
            raise ValueError("invalid config layer")
        if source in ("sessionFlags", "user"):
            continue
        if source in ("system", "project") and not config:
            continue
        raise AdapterError(f"isolated Codex review has an unsupported {source} configuration layer",
                           fix=f"use a lane with no managed or nonempty {source} configuration layer")
    if not isinstance(servers, list):
        raise ValueError("invalid MCP inventory")
    configs, seen = [], set()
    for server in servers:
        name = server["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in seen:
            raise ValueError("invalid MCP server name")
        seen.add(name)
        transport = server["transport"]["type"]
        if transport == "stdio":
            configs.append(f'mcp_servers.{name}.command="false"')
        elif transport == "streamable_http":
            configs.append(f'mcp_servers.{name}.url="https://invalid.invalid"')
        else:
            raise ValueError("unknown MCP transport")
        configs.append(f"mcp_servers.{name}.enabled=false")
    return configs


def codex_args(codex_bin, *, home, workdir, env, inspector=None):
    """C-23.2, C-23.4: inspect this attempt's lane and disable every named server."""
    try:
        metadata = (inspector or inspect_codex)(codex_bin, home=home, workdir=workdir, env=env)
        configs = validate_codex_metadata(*metadata)
    except AdapterError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        raise AdapterError("cannot safely inspect isolated Codex configuration or MCP inventory",
                           fix="use a lane whose config layers and MCP inventory can be inspected") from None
    return ["--ephemeral", "--ignore-user-config", "--ignore-rules", *_flags(),
            *(arg for config in configs for arg in ("-c", config))]
