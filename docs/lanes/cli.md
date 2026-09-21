# Lane brief: cli (command-line client, offline mode, exit codes)

You are building the command-line client of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/cli`). subfleet v2 is one supervised daemon (`subfleetd`) that dispatches delegated agent work across several Claude and Codex subscription accounts; the CLI is a thin client over a unix socket, with a read-only offline mode when the daemon is down. The agent-facing verbs are already embedded in many agents' instructions and must keep their v1 spellings.

## Read first, in this order

1. `docs/acceptance-contract.md` sections 1, 2, 5.8, 6.1, 7.1, 15.4, 16, 17. Cite clauses (`C-x.y`) in every test docstring.
2. `subfleet/protocol.py` (wire shapes; use `encode`, `decode_response`, the `*Args` dataclasses) and `subfleet/contracts.py` (`Exit`, `JobState`, defaults). Do not change names or semantics; if something blocks you, make the smallest additive change and list it under "Seam changes".
3. `subfleet/store_schema.sql`: offline mode reads these tables read-only.
4. v1, read-only, for the exact user-facing behaviour to preserve (never modify v1, never run its commands): `~/chief-of-staff/subfleet/subfleet/cli.py` (verb table, the `runs` table columns and formatting, the four-line detached hint, `--json` dispatch line), `~/chief-of-staff/subfleet/subfleet/delegate.py` lines 700 to 760 (how a Claude Code session is detected and why detached is the default inside one), `~/chief-of-staff/subfleet/README.md` lines 1 to 120 (the documented contract). Never run `grep -r` or `rg` over `~/chief-of-staff/state` or any broad root; read specific files.

## Scope: files you own

- `subfleet/client.py`: `Client(state_root)` connecting to `daemon.sock`, sending one request and reading one response line with a timeout, raising `DaemonUnavailable` (maps to exit 69) when the socket is absent, refused, or the lock file's recorded identity is dead (read `daemon.lock` JSON; identity check via `ps -p <pid> -o lstart=` compared with the recorded `proc_start`, and `sysctl -n kern.boottime` compared with `boot_id`; keep this small and local to the client).
- `subfleet/offline.py` (C-17.5): read-only SQLite access (`file:...?mode=ro` URI) for `runs`, `runs show`, `status`, and `kill`. Offline `kill` signals the recorded pgid only after the same identity check as above and prints what it did; anything it cannot verify it refuses with the reason.
- `subfleet/cli.py` with `main(argv=None) -> int` and console entry `subfleet` (C-17.1 to C-17.6):
  - Verbs: `subfleet` and `status`, `run`, `runs [--mine] [--running] [--last N] [--json]`, `runs show <id> [--out|--err|--json]`, `runs reap`, `wait <id>... | --mine | --last [--timeout S]`, `kill <id> [--wait] [--confirm-dead|--force-release]`, `resume <id> [PROMPT]`, `lanes [list|probe|enroll <credential>|hold <lane> --until|release <lane>|transfer <lane> --to v1|v2]`, `why <id> | --task T --tier X`, `daemon [start|stop|status|logs|install]`, `doctor [--live]`, `ping [--session ID] TEXT`. Aliases: `jobs` for `runs`, `show` for `runs show`, `capacity` for `status`, `notify` for `ping`, `resume-codex` for `resume`. Deprecated but accepted with a stderr note: `-t CLASS`, `--overflow`, `-m sol` remapped to `astra`.
  - `run` flags per C-17.2. Inside a Claude Code session (detect as v1 does) the default is detached and the four-line hint is printed to stderr (C-17.6); `--wait`/`--attach` block through `wait`; `--json` prints one JSON object. The request id is generated with UUID4 unless `--request-id` is given and is printed back.
  - `wait` loops on the daemon's long poll (each call at most 60 s) until terminal or `--timeout`, then returns the job's rc mapped per C-17.3 (exit 124 on timeout, 125 on `lost`, 130 on `cancelled`).
  - Exit codes exactly per C-17.3; stdout carries the contract and stderr the prose (C-17.4).
  - `daemon start` launches `subfleetd` detached in its own session (double fork or `start_new_session=True`), waits up to 10 s for the socket, and exits 69 with the log tail if it never appears; `daemon stop` sends SIGTERM to the lock holder after the identity check; `daemon status` prints the lock contents and `ping`, and is the verb that answers "is the daemon alive" — it names a holder that is alive, dead, unverifiable, or stopped, and a stopped one is diagnosed from the lock without waiting on the socket at all (C-5.11); `daemon install` writes `~/Library/LaunchAgents/com.subfleet.daemon.plist` (KeepAlive, RunAtLoad, the resolved `subfleetd` path and `SUBFLEET_HOME`) and loads it, and `daemon install --dry-run` prints the plist.
  - `doctor` (offline): `claude --version`, `codex --version`, `uv --version`, the state root layout, PATH shadows for `claude` and `codex`, whether `daemon.sock` and `daemon.lock` agree, and whether `~/.claude/settings.json` carries the never-rules hook; `--live` adds one `ping` against the daemon (C-16.2), sent with empty text so the probe leaves no service notice behind, and the row carries the stopped-holder diagnosis when there is one (this sentence replaces the brief's original "reserved … prints not implemented", which `subfleet/doctor.py` outgrew in milestone 4).
- `tests/unit/test_cli.py`: a fake daemon (a thread serving the protocol on a temp socket with canned responses) exercising every verb, alias, the detached default and hint, `--json`, exit-code mapping, and the malformed-response path. `tests/unit/test_offline.py`: a temp store built from `store_schema.sql` with a few rows; `runs`, `runs show`, `status` render; offline `kill` refuses when identity cannot be verified. `tests/unit/test_daemon_verbs.py`: `daemon start` against a stub `subfleetd` script that creates the socket; `daemon install --dry-run` plist content.

## Out of scope

`subfleet/daemon.py`, `subfleet/store.py`, `subfleet/procs.py`, `subfleet/guardian.py`, adapters, `docs/invariants.md`. Other lanes build them concurrently. Your tests must not import them; the CLI imports `subfleet.store` and `subfleet.procs` lazily only inside `runs reap` and `doctor` and degrades with a clear message when they are absent.

## Acceptance for this lane

- Every verb and alias in C-17.1 parses and reaches the right op with the right args.
- `run` inside a fake Claude session prints the hint and returns 0 with the job id on stdout; `run --wait` returns the job's mapped rc.
- `wait --timeout 1` on a never-finishing job exits 124; a `lost` job exits 125; `cancelled` exits 130.
- Daemon down: `runs`, `runs show`, `status` work from the store; `run` exits 69 and prints `subfleet daemon start`.
- `-m sol` is remapped to `astra` with a stderr note; `-t review` is accepted with a note.
- `uv run pytest -q tests/unit/test_cli.py tests/unit/test_offline.py tests/unit/test_daemon_verbs.py` passes in under 30 s.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step with a message naming the clauses implemented, and push after every commit: `git push -u origin lane/cli`. Never commit to `main`, never force-push, never rewrite history. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built (files with one line each); Tests (exact command, pass count, wall time); Clauses covered; Clauses not covered and why; Seam changes; Open questions for the integrator. No preamble.
