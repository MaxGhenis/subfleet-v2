"""Original resolver bodies from the assigned release/217 snapshot.

Captured from 24be965c5ce8343f8b3d07082d69d103ecfb2a4f; execute only with a fake HOME.
"""

BASELINE_COMMIT = '24be965c5ce8343f8b3d07082d69d103ecfb2a4f'

SOURCES = {

    'subfleet.sessions.mirror': '''def store_dir() -> Path:
    override = os.environ.get(STORE_ENV)
    if override:
        return Path(override).expanduser()
    return (Path.home() / "Library" / "Application Support" / "Claude"
            / "claude-code-sessions")''',

    'subfleet.sessions.revive': '''def session_store_dir() -> Path:
    """The desktop app's per-account Claude Code session store.

    `SUBFLEET_SESSION_STORE` is v1's own override and the tests' seam; nothing
    here writes to it.
    """
    override = os.environ.get("SUBFLEET_SESSION_STORE")
    if override:
        return Path(override).expanduser()
    return (Path.home() / "Library" / "Application Support" / "Claude"
            / "claude-code-sessions")''',

    'subfleet.sessions.desktop': '''def log_path() -> Path:
    override = os.environ.get(LOG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Logs" / "Claude" / "main.log"''',

    'subfleet.sessions.transcripts': '''def claude_dir() -> Path:
    """`~/.claude`, overridden by `SUBFLEET_CLAUDE_DIR` exactly as v1 overrides it.

    Every test points this at its own tree; nothing here ever writes.
    """
    override = os.environ.get("SUBFLEET_CLAUDE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"''',

    'subfleet.notify_push': '''def claude_dir() -> Path:
    """`~/.claude`, overridable exactly as v1 overrides it (`paths.claude_dir`)."""
    override = os.environ.get("SUBFLEET_CLAUDE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"''',

    'subfleet.client': '''def state_root(env: dict[str, str] | None = None) -> Path:
    """`$SUBFLEET_HOME`, default `~/.subfleet/`, always absolute (C-2.1).

    A relative `SUBFLEET_HOME` would otherwise mean a different directory for
    every caller's cwd, and a file URI cannot express one at all.
    """
    env = os.environ if env is None else env
    root = Path(env.get("SUBFLEET_HOME") or DEFAULT_STATE_ROOT).expanduser()
    return root if root.is_absolute() else Path.cwd() / root''',

    'subfleet.guard.preflight': '''def _state_root() -> str:
    return os.path.expanduser(os.environ.get("SUBFLEET_HOME", "~/.subfleet"))''',

    'subfleet.hooks': '''def settings_path() -> Path:
    override = os.environ.get(SETTINGS_ENV)
    return (Path(override).expanduser() if override
            else Path("~/.claude/settings.json").expanduser())''',

    'subfleet.adapters.claude': '''def _config_projects_dir(self, credential_env: dict[str, str] | None = None) -> Path:
    if self._projects_dir is not None:
        return self._projects_dir
    config = (credential_env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get(
        "CLAUDE_CONFIG_DIR"
    )
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "projects"''',

    'subfleet.adapters.claude_mcp': '''def global_config_path(env: Mapping[str, str] | None = None, home: str | Path | None = None) -> Path:
    """The file Claude Code keeps user- and local-scope servers in (`Lo`, `M7n`)."""
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    configured = env.get("CLAUDE_CONFIG_DIR")
    legacy = (Path(configured) if configured else home / ".claude") / ".config.json"
    if legacy.exists():
        return legacy
    return (Path(configured) if configured else home) / ".claude.json"''',

    'subfleet.doctor': '''def check_never_rules(settings: Path | None = None) -> dict[str, Any]:
    """C-14.3: the never-rules PreToolUse guard is installed for this user.

    v2 never writes this entry — it belongs to the guard, not to subfleet — so
    the honest report when the file cannot be read is `unknown`, not a failure.
    """
    path = (settings if settings is not None
            else Path("~/.claude/settings.json").expanduser())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return row("never-rules hook in ~/.claude/settings.json", UNKNOWN,
                   f"{path}: {exc.__class__.__name__} — cannot tell (C-14.3)",
                   f"read {path} by hand, or reinstall the guard hook")
    if any(marker in text for marker in
           ("never-rules", "never_rules", "subfleet-guard")):
        return row("never-rules hook in ~/.claude/settings.json", PASS,
                   f"{path}: present", "`subfleet doctor` after editing that file")
    return row("never-rules hook in ~/.claude/settings.json", FAIL,
               f"no never-rules hook in {path} (C-14.3)",
               "install the guard's PreToolUse hook; subfleet does not write it")''',

}
