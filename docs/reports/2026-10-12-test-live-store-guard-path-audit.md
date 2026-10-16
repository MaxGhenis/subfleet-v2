# Personal-path and keychain audit

Command: `git grep -n "Path.home()\|expanduser\|Application Support\|\.claude\|\.codex\|\.subfleet" subfleet/`

Round 1 snapshot at `0f9eae1a0`: 235 hits, individually classified below. The round 2 changes and complete keychain search follow this historical table. The legacy defaults retain release/217 import-time values but are guarded at use; provider discovery and all desktop/session defaults use the guard. No `~/.subfleet-v1` default remains. Explicit caller paths retain precedence. Launch-agent installation and the optional ~/bin credential executable are outside desktop/session/account stores; they are identified separately.

| Hit | Source | Why safe |
| --- | --- | --- |
| `subfleet/actions.py:14` | `from .adapters.codex import CodexAdapter` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude.py:56` | `from .claude_stream import (` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude.py:163` | `"""Claude Code's &#96;~/.claude/projects/&#96; directory name for a working directory.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude.py:378` | `digest = hashlib.sha256(str(Path(home).expanduser()).encode("utf-8")).hexdigest()[:8]` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/claude.py:399` | `path = Path(home).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/claude.py:536` | `self.claude_bin = claude_bin` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude.py:631` | `return {"CLAUDE_CONFIG_DIR": str(Path(credential.ref).expanduser())}` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/claude.py:801` | `&#96;~/.claude.json&#96; carries &#96;oauthAccount.emailAddress&#96; (C-10.3 reads the same` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude.py:805` | `path = Path(home).expanduser() / ".claude.json"` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/claude.py:820` | `self.claude_bin, "-p", ENROLL_PROMPT,` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude.py:849` | `return 127, "", f"claude: could not run {self.claude_bin}: {exc}"` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude.py:907` | `"home whose .claude.json carries oauthAccount.emailAddress"` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/adapters/claude.py:1291` | `base = Path(config).expanduser() if config else Path(home_path(` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/adapters/claude.py:1292` | `"CLAUDE_CONFIG_DIR", lambda: Path.home() / ".claude", expand_override=True))` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/adapters/claude.py:1344` | `never-rules hook in &#96;~/.claude/settings.json&#96;, and &#96;doctor&#96; reports when that` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude.py:1353` | `self.claude_bin, "-p",` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude.py:1406` | `argv: list[str] = [self.claude_bin, "-p", "--resume", native_session_id]` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/claude_mcp.py:20` | `* User scope (&#96;k7e("user")&#96;): &#96;mcpServers&#96; in the global config, &#96;.claude.json&#96;` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude_mcp.py:38` | `https://code.claude.com/docs/en/mcp-quickstart#find-your-configuration-on-disk` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude_mcp.py:39` | `https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude_mcp.py:40` | `https://code.claude.com/docs/en/cli-reference (&#96;&#96;--strict-mcp-config&#96;&#96;).` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude_mcp.py:42` | `under the user's home; the user-scope state file is &#96;&#96;~/.claude.json&#96;&#96;.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/adapters/claude_mcp.py:139` | `base = home / ".claude"` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/adapters/claude_mcp.py:141` | `base = Path(home_path("CLAUDE_CONFIG_DIR", lambda: Path.home() / ".claude", env=env))` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/adapters/claude_mcp.py:145` | `return (base if configured else base.parent) / ".claude.json"` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/adapters/codex.py:129` | `return _api_key(_read_auth(Path(home).expanduser()))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:257` | `self.codex_bin = str(codex_bin)` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/codex.py:318` | `home = Path(credential.ref).expanduser().resolve()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:343` | `home = Path(lane.home or credential_env.get("CODEX_HOME") or lane.credential.ref).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:432` | `result = self._request(_read_auth(Path(lane.home or lane.credential.ref).expanduser()),` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:454` | `result = self._request(_read_auth(Path(lane.home or lane.credential.ref).expanduser()),` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:468` | `if not home or (credential_env.get("CODEX_HOME") and Path(credential_env["CODEX_HOME"]).expanduser().resolve() != Path(home).expanduser().resolve()):` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:470` | `argv = [self.codex_bin, "exec", "--json"]` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/codex.py:476` | `argv += codex_args(self.codex_bin, home=home, workdir=job.workdir, env=env,` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/adapters/codex.py:509` | `env["CODEX_HOME"] = str(Path(home).expanduser())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/codex.py:663` | `for path in (Path(home).expanduser() / "sessions").rglob("*.jsonl"):` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/adapters/isolation.py:59` | `clean_env["CODEX_HOME"] = str(Path(home).expanduser().resolve(strict=True))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/capacity.py:94` | `&#96;~/.claude.json&#96; carries the desktop app's own idea of who it is signed in` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/capacity.py:139` | `app's own keychain credential — never from &#96;~/.claude.json&#96;, which is carried` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/capacity.py:148` | `cached_label: str \| None = None  # ~/.claude.json oauthAccount, a hint only` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/capacity.py:410` | `the desktop app's own credential, with the cached &#96;~/.claude.json&#96; email kept` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/claude_cards.py:1019` | `folder = Path(str(settings(policy)["logins_dir"])).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:87` | `PLIST_LABEL = "com.subfleet.daemon"` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/cli.py:88` | `PLIST_PATH = "~/Library/LaunchAgents/com.subfleet.daemon.plist"` | Daemon launch-agent plist, outside session/account stores. Installer tests redirect PLIST_PATH; doctor tests supply plists. This job also uses fake HOME. |
| `subfleet/cli.py:557` | `from .adapters.codex import api_key_login` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/cli.py:560` | `message = f"{Path(args.home).expanduser()} is an OpenAI API-key login; v2 lanes use subscriptions only"` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:628` | `path = Path(args.p).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:663` | `from .adapters.claude_mcp import validate_names` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/cli.py:674` | `workdir = Path(args.C).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:687` | `parent = Path(args.o).expanduser().absolute().parent` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:741` | `out_path=str(Path(args.o).expanduser().absolute()) if args.o else None,` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:755` | `review_root=str(Path(args.review_root).expanduser().resolve()) if getattr(args, "review_root", None) else None,` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:967` | `value = str((base / Path(value).expanduser()).resolve() if not Path(value).expanduser().is_absolute()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:968` | `else Path(value).expanduser())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:980` | `source = Path(path).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:1080` | `rerun = (f"{PROG} run --batch {shlex.quote(str(Path(args.batch).expanduser().resolve()))} "` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:1994` | `out_path=str(Path(args.output).expanduser().absolute()) if args.output` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/cli.py:2678` | `without it before writing. &#96;~/.claude/settings.json&#96; is a file the user` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/cli.py:2718` | `target = Path(PLIST_PATH).expanduser()` | Daemon launch-agent plist, outside session/account stores. Installer tests redirect PLIST_PATH; doctor tests supply plists. This job also uses fake HOME. |
| `subfleet/cli.py:2850` | `&#96;~/.claude.json&#96; says who the desktop app believes it is signed in as; the` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/cli.py:2861` | `from .adapters.claude import ClaudeAdapter` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/cli.py:2873` | `check = "cached ~/.claude.json agrees with the desktop credential"` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/cli.py:2892` | `"detail": f"they disagree: ~/.claude.json says "` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/cli.py:3162` | `"~/.claude/settings.json instead of the plist "` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/client.py:63` | `DEFAULT_STATE_ROOT = "~/.subfleet"` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/client.py:82` | `"""&#96;$SUBFLEET_HOME&#96;, default &#96;~/.subfleet/&#96;, always absolute (C-2.1).` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/client.py:89` | `root = Path(home_path("SUBFLEET_HOME", lambda: DEFAULT_STATE_ROOT, env=env)).expanduser()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/client.py:327` | `self.root = Path(root).expanduser() if root is not None else state_root()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/compat.py:32` | `verbs the agent contract tells sessions never to call (&#96;~/.claude/CLAUDE.md&#96;` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/compat.py:165` | `"(\"hook entries in ~/.claude/settings.json\")"),` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/compat.py:461` | `"(default ~/.subfleet) and the two are different "` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/compat.py:515` | `path = Path(candidate).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/contracts.py:399` | `# (&#96;network.codex_workspace_write&#96;), not the job row; Claude's writable` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/contracts.py:428` | `under &#96;~/.claude/projects/&#96; with its byte size at launch (&#96;transcript_offset&#96;),` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/conversations/catalog.py:5` | `rollout under the enrolled lane homes and &#96;~/.codex&#96;, writing` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/conversations/catalog.py:694` | `adapter's encoding (&#96;adapters.claude.encode_project_dir&#96;: &#96;/&#96;, &#96;.&#96;, &#96;_&#96; to` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/conversations/classify.py:28` | `from ..adapters.claude import ClaudeAdapter` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/claude_turn.py:23` | `from ..adapters.claude import model_matches_requested` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/claude_turn.py:24` | `from ..adapters.claude_stream import is_synthetic_api_error` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/codex_brief.py:79` | `homes and &#96;~/.codex&#96; for a thread no conversation holds); the caller runs on` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/conversations/history.py:215` | `from ..adapters.claude import encode_project_dir` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/launch.py:16` | `from ..adapters.claude import ENV_REMOVE, ClaudeAdapter` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/launch.py:59` | `argv = claude_turn.argv(spec, claude_bin=adapter.claude_bin, read_only_flags=read_only)` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/launch.py:89` | `env["CODEX_HOME"] = str(Path(home).expanduser())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/conversations/legacy.py:45` | `(&#96;catalog.claude_session&#96;); a conversation the session already has keeps its` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/conversations/legacy.py:638` | `sessions = (Path(claude_dir) if claude_dir is not None else transcripts.claude_dir()) / "sessions"` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/conversations/runner.py:31` | `from .claude_turn import ClaudeTurn` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/runner.py:32` | `from .codex_turn import CodexTurn` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/conversations/service.py:274` | `~/.subfleet) also accepts the development build named when it started."""` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/conversations/service.py:276` | `if dev and self.root.resolve() != (provider_home() / ".subfleet").resolve():` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/conversations/service.py:1639` | `allowed = [(Path(r["home"]).expanduser().resolve(), r["lane_id"]) for r in lanes]` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/conversations/service.py:1642` | `wanted = Path(str(home)).expanduser().resolve()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/conversations/service.py:1646` | `"or ~/.codex")` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/conversations/service.py:1772` | `protected = [self.root, Path(home_path("CLAUDE_CONFIG_DIR", lambda: Path.home() / ".claude",` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/conversations/service.py:1776` | `# &#96;~/.CLAUDE&#96; is &#96;~/.claude&#96;, and a comparison of the spellings as typed missed` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/credentials.py:20` | `default = Path.home() / "bin" / "agent-secret"` | Optional per-lane credential helper executable under ~/bin; outside session/account stores. Credential tests supply explicit homes or stub the command. |
| `subfleet/credentials.py:32` | `return {key: str(Path(credential.ref).expanduser().resolve())}` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:608` | `self.root = Path(state_root).expanduser().resolve()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:980` | `path = Path(text).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:1031` | `from .adapters.claude import ClaudeAdapter` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/daemon.py:1641` | `keychain item it has no reason to read. &#96;~/.claude.json&#96; rides along as` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/daemon.py:1647` | `hint = capacity.read_desktop_account()   # the cached ~/.claude.json hint (C-10.3), never the authority` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/daemon.py:1663` | `Only the network answer is cached: &#96;~/.claude.json&#96; is re-read every` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/daemon.py:1964` | `workdir = Path(args.workdir).expanduser().resolve(strict=True)` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:1971` | `review_root = str(Path(args.review_root).expanduser().resolve(strict=True))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:1992` | `prompt = read_regular(Path(args.prompt_path).expanduser())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:1996` | `out = str(_resolve_output_path(Path(args.out_path).expanduser()))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:2826` | `path = Path(lane.credential.ref).expanduser() / "auth.json"` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/daemon.py:7513` | `"""C-8.5: where the job writes &#96;.subfleet/push.bundle&#96;: the directory it` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/daemon.py:8413` | `args.state_root = home_path("SUBFLEET_HOME", lambda: "~/.subfleet", accept_empty=True)` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/doctor.py:116` | `"""&#96;~/.claude/settings.json&#96; matches what &#96;daemon install --hooks&#96; writes."""` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/doctor.py:121` | `return row("hook entries in ~/.claude/settings.json", UNKNOWN,` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/doctor.py:128` | `return row("hook entries in ~/.claude/settings.json", PASS,` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/doctor.py:131` | `return row("hook entries in ~/.claude/settings.json", FAIL,` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/doctor.py:190` | `if (Path(item).expanduser() / "subfleet" / "__init__.py").exists()]` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/doctor.py:356` | `"SUBFLEET_CLAUDE_SETTINGS", lambda: "~/.claude/settings.json", test_only=True)).expanduser())` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/doctor.py:360` | `return row("never-rules hook in ~/.claude/settings.json", UNKNOWN,` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/doctor.py:365` | `return row("never-rules hook in ~/.claude/settings.json", PASS,` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/doctor.py:367` | `return row("never-rules hook in ~/.claude/settings.json", FAIL,` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/doctor.py:512` | `plist = Path(PLIST_PATH).expanduser()` | Daemon launch-agent plist, outside session/account stores. Installer tests redirect PLIST_PATH; doctor tests supply plists. This job also uses fake HOME. |
| `subfleet/folders.py:38` | `passed the C-26.10 refusal of a workspace that contains &#96;~/.claude&#96; (review of` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/folders.py:179` | `real = os.path.realpath(os.path.expanduser(os.fspath(path)))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/cli.py:105` | `payload["workdir"] = str(Path(args.workdir or Path.cwd()).expanduser().resolve())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/cli.py:109` | `payload[key] = str(Path(payload[key]).expanduser().resolve())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/revision.py:21` | `resolved = Path(path).expanduser().resolve(strict=True)` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/service.py:39` | `body = read_regular(Path(path).expanduser(), 65536)` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/service.py:97` | `cwd = Path(args.workdir or Path.cwd()).expanduser().resolve()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/service.py:99` | `source = Path(args.target).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/service.py:189` | `cwd = Path(args.workdir or Path.cwd()).expanduser().resolve()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/gate/service.py:193` | `source = Path(args.target).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/guard/preflight.py:181` | `directory = Path(state_root if state_root is not None else _state_root()).expanduser() / "guard"` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/guard/preflight.py:297` | `path = Path(os.path.expanduser(override))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/guard/preflight.py:700` | `return os.path.expanduser(str(home_path("SUBFLEET_HOME", lambda: "~/.subfleet",` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/home_paths.py:51` | `return Path(override).expanduser() if expand_override else override` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/home_paths.py:60` | `return Path(home_path("SUBFLEET_CODEX_APP_HOME", lambda: Path.home() / ".codex",` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/home_paths.py:66` | `return Path(home_path("SUBFLEET_DESKTOP_LOGIN", lambda: Path.home() / ".claude.json",` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/hooks.py:64` | `Nothing here writes to &#96;~/.claude/settings.json&#96; unless` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/hooks.py:88` | `#: still resolves (&#96;~/.claude/settings.json&#96; points at v1 today).` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/hooks.py:166` | `return Path(home_path(SETTINGS_ENV, lambda: "~/.claude/settings.json")).expanduser()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/hooks.py:849` | `that keeps &#96;~/.claude/settings.json&#96; out of accidental rewrites).` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/host_push.py:48` | `#: worktree root (where Codex starts): &#96;mkdir -p .subfleet && git bundle create` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/host_push.py:49` | `#: .subfleet/push.bundle HEAD&#96;.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/host_push.py:50` | `BUNDLE_PATH = (".subfleet", "push.bundle")` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/host_push.py:546` | `"""Copy &#96;<root>/.subfleet/push.bundle&#96; into the daemon-owned &#96;target&#96;.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/ids.py:77` | `"workdir": str(Path(workdir).expanduser().resolve()),` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/ids.py:82` | `"out_path": str(Path(out_path).expanduser().resolve()) if out_path is not None else None,` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/importer.py:36` | `SUBFLEET_HOME=~/.subfleet uv run python -m subfleet.importer --dry-run` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:84` | `V1_STATE = Path("~/chief-of-staff/state/subfleet").expanduser()` | Captured release default; expansion performs no file access. Every omitted legacy argument is guarded before a read (including CLI and cockpit). |
| `subfleet/importer.py:85` | `DELEGATE_STATE = Path("~/.local/state/delegate").expanduser()` | Captured release default; expansion performs no file access. Every omitted legacy argument is guarded before a read (including CLI and cockpit). |
| `subfleet/importer.py:86` | `V1_ROSTER_DIR = Path("~/chief-of-staff/subfleet").expanduser()` | Captured release default; expansion performs no file access. Every omitted legacy argument is guarded before a read (including CLI and cockpit). |
| `subfleet/importer.py:538` | `"""&#96;~/.codex-<n>&#96; lanes; &#96;~/.codex&#96; is observed and never a lane (C-10.3)."""` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:540` | `for candidate in sorted(home.glob(".codex-*")):` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/importer.py:541` | `if candidate.name == ".codex" or not candidate.is_dir():` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/importer.py:550` | `suffix = path.name[len(".codex-"):] or path.name` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/importer.py:566` | `&#96;desktop&#96; set from &#96;~/.claude.json&#96; &#96;oauthAccount&#96;; each Codex home's account` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:624` | `claude_config = _read_json(home / ".claude.json")` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/importer.py:630` | `report.note("no ~/.claude.json oauthAccount: no lane is marked desktop (C-10.3)")` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/importer.py:707` | `# ~/.codex-N name or an alias that another current lane now owns.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:740` | `str(Path(name).expanduser()).lower()):` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/importer.py:1639` | `is the &#96;~/.claude&#96; whose &#96;sessions/&#96; registry names live Claude processes` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:1640` | `(default &#96;projects&#96;' parent, else &#96;~/.claude&#96;). A &#96;v1_state&#96; that holds none` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:2032` | `&#96;~/.claude/projects&#96;, or &#96;$SUBFLEET_CLAUDE_DIR/projects&#96;).` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/importer.py:2034` | `state_root = Path(state_root).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/importer.py:2036` | `if v1_state is V1_STATE else v1_state).expanduser()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/importer.py:2038` | `if delegate_state is DELEGATE_STATE else delegate_state).expanduser()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/importer.py:2040` | `if roster_dir is V1_ROSTER_DIR else roster_dir).expanduser()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/importer.py:2042` | `home = Path(home).expanduser() if home is not None else provider_home()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/importer.py:2081` | `projects = Path(claude_projects).expanduser() if claude_projects is not None else None` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/importer.py:2167` | `state_root = Path(state_root).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/importer.py:2169` | `if v1_state is V1_STATE else v1_state).expanduser()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/importer.py:2170` | `projects = Path(claude_projects).expanduser() if claude_projects is not None else None` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/importer.py:2224` | `help="where ~/.codex-<n> and ~/.claude.json live (default: $HOME)")` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/importer.py:2237` | `help="the ~/.claude whose projects/ holds the Claude transcripts "` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/importer.py:2238` | `"(default: $SUBFLEET_CLAUDE_DIR or ~/.claude)")` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/importer.py:2243` | `args.state_root = home_path("SUBFLEET_HOME", lambda: "~/.subfleet")` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/importer.py:2244` | `projects = Path(args.claude_dir).expanduser() / "projects" if args.claude_dir else None` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:38` | `file. &#96;paths.codex_homes()&#96; globs &#96;~/.codex-1&#96; … &#96;~/.codex-9&#96; and honours only` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:72` | `V1_ROSTER_DIR = Path("~/chief-of-staff/subfleet").expanduser()` | Captured release default; expansion performs no file access. Every omitted legacy argument is guarded before a read (including CLI and cockpit). |
| `subfleet/lanes_transfer.py:77` | `#: Where v1's scheduled work is defined. &#96;paths.codex_homes()&#96; reads` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:80` | `LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()` | Captured release default; expansion performs no file access. Every omitted legacy argument is guarded before a read (including CLI and cockpit). |
| `subfleet/lanes_transfer.py:82` | `#: &#96;paths.codex_homes()&#96;: &#96;[HOME / f".codex-{i}" for i in range(1, 10)]&#96;.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:85` | `V1_STATE_DIR = Path("~/chief-of-staff/state/subfleet").expanduser()` | Captured release default; expansion performs no file access. Every omitted legacy argument is guarded before a read (including CLI and cockpit). |
| `subfleet/lanes_transfer.py:276` | `Read from v1's own source on 2026-09-05: &#96;paths.codex_homes()&#96; returns` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:277` | `&#96;[~/.codex-1 … ~/.codex-9]&#96; that exist, unless &#96;SUBFLEET_CODEX_HOMES&#96;` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:285` | `discovered = [str(home / f".codex-{index}") for index in CODEX_HOME_RANGE` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/lanes_transfer.py:286` | `if (home / f".codex-{index}").is_dir()]` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/lanes_transfer.py:388` | `&#96;paths.codex_homes()&#96; globs &#96;~/.codex-1&#96; … &#96;~/.codex-9&#96; and reads no roster` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:406` | `# A parked row names the lane and its original &#96;~/.codex-<n>&#96; path; after the` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:421` | `for candidate in (home / f".codex-{index}" for index in CODEX_HOME_RANGE)` | Derived from explicit caller home/configuration or guarded provider/Claude home; no implicit personal fallback. |
| `subfleet/lanes_transfer.py:424` | `"v1 discovers Codex homes by globbing ~/.codex-1..9 (paths.codex_homes); "` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/lanes_transfer.py:433` | `runs = Path(v1_state).expanduser() / "runs"` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:451` | `target = Path(home).expanduser().resolve()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:455` | `and candidate and Path(candidate).expanduser().resolve() == target]` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:473` | `"""The original &#96;~/.codex-<n>&#96; path the v1 roster recorded for a transferred lane."""` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:539` | `# Codex homes by globbing ~/.codex-1..9 from every caller (CLI, gates,` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:542` | `src = Path(lane_home).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:576` | `current = Path(lane_home).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:577` | `if not plan.blocker and original and str(current) != str(Path(original).expanduser()):` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:578` | `back = Path(original).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/lanes_transfer.py:595` | `# and ~/.codex-<n>, the directory is still parked under the state root.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/lanes_transfer.py:599` | `current = Path(lane_home).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/notify_push.py:12` | `registers &#96;~/.claude/sessions/<pid>.json&#96; (&#96;sessionId&#96;, &#96;messagingSocketPath&#96;,` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/notify_push.py:61` | `"""&#96;~/.claude&#96;, overridable exactly as v1 overrides it (&#96;paths.claude_dir&#96;)."""` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/notify_push.py:63` | `return Path(home_path("SUBFLEET_CLAUDE_DIR", lambda: Path.home() / ".claude", expand_override=True))` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/offline.py:95` | `self.root = Path(root).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/cli.py:662` | `out_path=(str(Path(args.o).expanduser().absolute())` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/desktop.py:81` | `return Path(home_path(LOG_ENV, lambda: Path.home() / "Library" / "Logs"` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/handoff.py:956` | `chosen = (Path(override).expanduser() if override is not None` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/handoff.py:957` | `else Path(source_cwd).expanduser() if source_cwd else None)` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/mirror.py:4` | `&#96;~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json&#96;,` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/mirror.py:7` | `The transcript itself lives in &#96;~/.claude/projects/<slug(cwd)>/<cli>.jsonl&#96; and is` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/mirror.py:86` | `The merge base moves out of &#96;~/.claude/cc-mirror-state.json&#96; and into the state` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/mirror.py:123` | `#: Where transcripts live; shared with &#96;transcripts.claude_dir&#96;.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/mirror.py:314` | `return Path(home_path(STORE_ENV, lambda: Path.home() / "Library" / "Application Support"` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/mirror.py:323` | `"""The &#96;~/.claude/projects&#96; folder name for a session cwd.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/mirror.py:603` | `"""v1's &#96;~/.claude/cc-mirror.json&#96;, read and never written.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/mirror.py:613` | `return transcripts.claude_dir()` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/mirror.py:1501` | `for name in globbing.iglob(os.path.expanduser(pattern), recursive=True):` | Explicit archive glob loaded from mirror configuration through guarded Claude home; the operator-configured path is not a default. |
| `subfleet/sessions/registry.py:3` | `Every interactive Claude Code session registers &#96;~/.claude/sessions/<pid>.json&#96;` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/registry.py:67` | `return transcripts.claude_dir() / "sessions"` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/registry.py:136` | `"""One &#96;~/.claude/sessions/<pid>.json&#96;, as read."""` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/revive.py:152` | `return Path(home_path("SUBFLEET_SESSION_STORE", lambda: Path.home() / "Library"` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/revive.py:153` | `/ "Application Support" / "Claude" / "claude-code-sessions", expand_override=True))` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/transcripts.py:200` | `"""&#96;~/.claude&#96;, overridden by &#96;SUBFLEET_CLAUDE_DIR&#96; exactly as v1 overrides it.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/transcripts.py:205` | `return Path(home_path("SUBFLEET_CLAUDE_DIR", lambda: Path.home() / ".claude", expand_override=True))` | Guarded personal default (or guarded derivative); session override wins, missing override refuses during configure/collection/test/teardown. |
| `subfleet/sessions/transcripts.py:227` | `"""&#96;~/.claude/projects/<encoded cwd>/<session id>.jsonl&#96;, newest if several.` | Documentation/comment only; no path is read, written or resolved here. |
| `subfleet/sessions/transcripts.py:450` | `path = Path(transcript).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/transcripts.py:564` | `path = Path(transcript).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/transcripts.py:614` | `path = Path(transcript).expanduser()` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/transcripts.py:642` | `for line in lines_reversed(Path(transcript).expanduser()):` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/sessions/transcripts.py:661` | `for line in lines_reversed(Path(transcript).expanduser(), chunk=256 * 1024):` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/timers.py:410` | `from .adapters.claude import ENROLL_MODEL, ENROLL_PROMPT, home_login` | Module/symbol or provider executable reference; not a personal store path. |
| `subfleet/timers.py:451` | `self.store.add_event('timer.claude-cards', data=summary)` | Literal label, error text, or name component; no implicit personal path access at this hit. |
| `subfleet/timers.py:478` | `raw = json.loads(read_regular(Path(lane.home or lane.credential.ref).expanduser() / 'auth.json'))` | Explicit argument, enrolled lane/credential, workspace/output, or policy/configuration value; no implicit personal default. |
| `subfleet/timers.py:506` | `from .adapters.codex import _identity, _read_auth` | Module/symbol or provider executable reference; not a personal store path. |


## Round 2 access decisions

Both launchd plist accesses now use `cli.launchd_plist_path()` and the registered
`SUBFLEET_LAUNCHD_PLIST` test seam. `cmd_daemon_install` can write the plist, so
read-only justification would be insufficient. Doctor's default read uses the
same resolver; an explicitly supplied plist still wins.

The optional `~/bin/agent-secret` lookup now uses the registered
`SUBFLEET_AGENT_SECRET` seam before `is_file()`. A discovered helper is an
executable credential reader, so this is guarded too. Configure removes an
inherited `CLAUDE_LANE_AGENT_SECRET` and restores it at unconfigure. A test can
explicitly install its own helper, executable or runner stand-in.

The four actual keychain readers are registered in `KEYCHAIN_READERS`:
`_keychain_blob`, `ClaudeAdapter._resolve_keychain_token`,
`ClaudeAdapter._plan_from_keychain`, and `resolve_credential`. All construct
commands through `credentials.keychain_command`. Under either pytest marker,
native `security` and `/usr/bin/security` commands require an explicit runner
stand-in; the captured native runner does not grant permission. Fixture undo
cannot remove this check. Desktop/home probes report unavailable and optional
plan reads return no plan when blocked; mandatory credential reads refuse.

Command: `git grep -n "security\b\|keychain\|find-generic-password" -- subfleet/`

85 current hits, individually classified below. No other native keychain execution site was found.

| Hit | Source | Classification |
| --- | --- | --- |
| `subfleet/adapters/claude.py:70` | `#: The keychain item name pattern v1 established and v2 keeps (C-10.1).` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:96` | `#: The keychain item the Claude desktop app keeps its own login in — the same item` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:97` | `#: v1's &#96;claude.keychain_credentials&#96; reads, and read-only here (C-10.3).` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:371` | `#: Claude Code on macOS keeps a config directory's login in the keychain, not in` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:377` | `def keychain_service_for_home(home: str \| Path) -> str:` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:382` | `def _keychain_blob(service: str, *, security_bin: str = "security", runner=None) -> str \| None:` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:383` | `"""One targeted keychain read of a login blob; None when absent or unreadable.` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:385` | `from ..credentials import keychain_command` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:387` | `done = (runner or subprocess.run)(keychain_command(service, security_bin, runner=runner),` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:399` | `&#96;.credentials.json&#96; or, on macOS, its keychain item. None when there is none."""` | Explicit home credential file; fallback calls the guarded _keychain_blob reader. |
| `subfleet/adapters/claude.py:407` | `text = _keychain_blob(keychain_service_for_home(path))` | Explicit home credential file; fallback calls the guarded _keychain_blob reader. |
| `subfleet/adapters/claude.py:532` | `security_bin: str = "security",` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:558` | `def _resolve_keychain_token(self, ref: str) -> str:` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:559` | `"""One targeted keychain item read. The value is returned to the caller and` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:561` | `from ..credentials import keychain_command` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:564` | `keychain_command(ref, self._security_bin, runner=self._credential_runner),` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:569` | `f"claude: could not read the keychain item {ref}",` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:575` | `f"claude: no keychain token for {ref}",` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:579` | `f"it as the keychain item {ref}"` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:585` | `f"claude: the keychain item {ref} is empty", code=5,` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:600` | `def _plan_from_keychain(self, ref: str) -> str \| None:` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:603` | `from ..credentials import keychain_command` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:606` | `keychain_command(ref, self._security_bin, runner=self._credential_runner),` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/adapters/claude.py:634` | `if credential.kind == "keychain-token":` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:635` | `return {"CLAUDE_CODE_OAUTH_TOKEN": self._resolve_keychain_token(credential.ref)}` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:639` | `fix="a Claude lane is either kind 'keychain-token' or kind 'home'",` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:648` | `A keychain or environment lane already has it under` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:650` | `own &#96;.credentials.json&#96; or, on macOS, in that directory's keychain item` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:651` | `(&#96;keychain_service_for_home&#96;), both the provider CLI's store and only ever` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:777` | `kind="keychain-token")` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:782` | `The keychain item is read, never written, and a missing or unreadable one` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:789` | `return ProfileResult(PROFILE_UNAVAILABLE, detail=f"keychain: {error.code}")` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:795` | `if credential.kind == "keychain-token" and credential.ref.startswith(KEYCHAIN_PREFIX):` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:804` | `file to find the desktop login). A keychain-token lane has no such file of` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/claude.py:881` | `f"as the keychain item {credential.ref}"` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:908` | `"name the keychain item claude-quota-<email>, or point the lane at a "` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:914` | `self._plan_from_keychain(credential.ref)` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:915` | `if credential.kind == "keychain-token" else None` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:920` | `# the keychain item's name are not. That is the whole lesson of the` | Dispatches to a registered guarded credential reader; does not execute an additional keychain command. |
| `subfleet/adapters/claude.py:2118` | `"keychain_service_for_home",` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/adapters/codex.py:56` | `# The provider's safety refusals. "This content was flagged for possible cybersecurity risk` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/capacity.py:139` | `app's own keychain credential — never from &#96;~/.claude.json&#96;, which is carried` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/cli.py:2857` | `profile endpoint, asked with the desktop app's own keychain item, says who` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/contracts.py:319` | `ref: str                # keychain item, home directory, or environment variable name` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/contracts.py:320` | `kind: str               # "keychain-token" \| "home" \| "env"` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/credentials.py:17` | `def keychain_command(reference: str, security_bin: str = "security", *,` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:19` | `"""Keep v1's dedicated agent keychain when its helper is installed.` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:22` | `in the login keychain. Hosts without the helper retain the native backend.` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:24` | `# Provider-owned desktop/home OAuth blobs stay in their native keychain.` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:35` | `# A reference or fake HOME does not isolate macOS's login keychain. Fixture` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:39` | `and security_bin in ("security", "/usr/bin/security")):` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:40` | `raise AdapterError("refusing personal keychain read under pytest", code=7,` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:42` | `return [security_bin, "find-generic-password", "-s", reference, "-w"]` | Central command builder; helper path is isolated and native keychain fallback is refused under pytest. |
| `subfleet/credentials.py:56` | `if credential.kind != "keychain-token" or credential.provider != "claude":` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/credentials.py:59` | `result = (runner or subprocess.run)(keychain_command(credential.ref, runner=runner),` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/credentials.py:62` | `raise AdapterError("could not resolve lane keychain credential", code=7,` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/credentials.py:65` | `raise AdapterError("could not resolve lane keychain credential", code=7,` | Registered credential reader; command construction uses the pytest keychain guard. |
| `subfleet/daemon.py:629` | `# means the Claude adapter's keychain reader; a harness that must not` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/daemon.py:970` | `or a &#96;claude-quota-<email>&#96; keychain item. The adapter's enrol turn decides` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/daemon.py:979` | `"claude-quota-<email> keychain item)", Exit.INVALID_INPUT)` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/daemon.py:986` | `credential = Credential("claude", text, "keychain-token")` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/daemon.py:1641` | `keychain item it has no reason to read. &#96;~/.claude.json&#96; rides along as` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/daemon.py:3593` | `# Provider/keychain errors can contain secrets; log the error` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/daemon.py:8288` | `def _keychain_read(argv) -> bool:` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8289` | `"""Whether &#96;argv&#96; is one of the two reads &#96;credentials.keychain_command&#96; builds:` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8290` | `&#96;<agent-secret> get <reference>&#96; or &#96;security find-generic-password -s <reference> -w&#96;."""` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8292` | `return len(argv) >= 2 and ((argv[1] == "get" and len(argv) == 3) or argv[1] == "find-generic-password")` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8296` | `"""C-10.2: the adapter's runner during a re-enrollment. The keychain reads that` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8297` | `resolve a keychain-token credential and its plan stay with &#96;original&#96;; every other` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8300` | `keychain-token re-enrollment raise before its turn ("_enrollment_turn() missing 2` | Classifies/routes commands already built by guarded readers; no independent native credential command. |
| `subfleet/daemon.py:8305` | `if _keychain_read(argv):` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/home_paths.py:10` | `# An inherited helper may query the person's dedicated keychain. Tests must` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/home_paths.py:35` | `# Every credential command execution must go through credentials.keychain_command.` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/home_paths.py:37` | `# permission to query the person's login keychain.` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/home_paths.py:39` | `"subfleet.adapters.claude._keychain_blob",` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/home_paths.py:40` | `"subfleet.adapters.claude.ClaudeAdapter._resolve_keychain_token",` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/home_paths.py:41` | `"subfleet.adapters.claude.ClaudeAdapter._plan_from_keychain",` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/importer.py:655` | `# C-10.1: the credential reference is a keychain item name, &#96;claude-quota-<email>&#96;` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/importer.py:659` | `enrol(next_claude_id(), "claude", f"claude:{key}", str(reference), "keychain-token",` | Passes an explicit legacy credential reference to enrollment; reads use the guarded adapter. |
| `subfleet/sessions/handoff.py:13` | `a credential-reading pattern — &#96;agent-secret get&#96;, a keychain read, &#96;env&#96; or` | Credential-output suppression or redaction; no keychain command executes. |
| `subfleet/sessions/handoff.py:133` | `#: Suppression beats redaction here because the value a keychain read returns has` | Credential-output suppression or redaction; no keychain command executes. |
| `subfleet/sessions/handoff.py:148` | `r"\bsecurity\s+(?:dump-keychain\|find-generic-password\|find-internet-password)\b",` | Credential-output suppression or redaction; no keychain command executes. |
| `subfleet/sessions/handoff.py:647` | `if "agent-secret" in name.casefold() or "keychain" in name.casefold():` | Credential-output suppression or redaction; no keychain command executes. |
| `subfleet/store_schema.sql:18` | `credential_kind TEXT NOT NULL CHECK (credential_kind IN ('keychain-token','home','env')),` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
| `subfleet/timers.py:649` | `# C-23.47 for Claude homes: the CLI refreshes its own keychain` | Reference, registry entry, declaration, comment, error text or documentation; no additional credential execution. |
