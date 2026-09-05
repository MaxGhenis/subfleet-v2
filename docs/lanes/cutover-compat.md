# Lane brief: cutover-compat (v1 invocation compatibility, hook-based notice delivery, doctor)

You are building the milestone-4 surface of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/cutover-compat`). When the `subfleet` symlink is repointed from v1 to v2, every command any agent types today must keep working, and completion notices must reach the calling Claude Code session by documented means. This lane builds the compatibility parser, the hook entry points, the notice delivery layers 2 and 3, and the `doctor` checks that gate the cutover.

## Read first, in this order

1. `docs/acceptance-contract.md` sections 15 (notices), 17 (CLI), and 6.7; `docs/plan.md` amendment 1 (every v1 verb spelling is permanent).
2. `subfleet/cli.py`, `subfleet/client.py`, `subfleet/offline.py` (the CLI as the cli lane built it); `subfleet/daemon.py` handlers for `notice.pending` and `notice.ack`; `subfleet/store.py`.
3. The v1 surface, read-only: `~/chief-of-staff/subfleet/README.md` (all of it; every command line in it is a compatibility case), `~/chief-of-staff/subfleet/subfleet/cli.py` (the verb table and the hidden verbs), `~/chief-of-staff/subfleet/subfleet/hooks.py` and `~/chief-of-staff/subfleet/bin/subfleet-hook` (what the three Claude Code hooks do today), `~/chief-of-staff/subfleet/subfleet/notify.py` (`render_pending`, `push_to_session`, the session registry lookup), `~/.claude/settings.json` (the installed hooks, read-only), `~/.claude/CLAUDE.md` section "Model routing" (the agent contract as agents see it). `docs/reports/D-surface.md` section 4 lists the contract lines and the gotchas.
4. Claude Code hooks reference: fetch `https://code.claude.com/docs/en/hooks` once and save the relevant sections to `docs/reference/claude-hooks.md` (command hook fields, `asyncRewake`, `if`, PostToolUse, SessionStart, UserPromptSubmit). Cite it; do not rely on memory.

## Scope: files you own

- `subfleet/compat.py`: a parser layer in front of `subfleet/cli.py` that accepts every v1 invocation and maps it to a v2 verb with a one-line stderr note when the spelling is deprecated (never for the permanent verbs). Build the case list from the README and v1 `cli.py`: `runs`, `runs show`, `runs reap`, `status`, `capacity`, `wait`, `kill`, `resume-codex`, `handoff --to`, `gate pr|plan|continue` (pass-through to `subfleet-gate` until milestone 7, with its 0 to 5 codes), `notify`, `sessions`, `enroll`, `login`, `pick`, `reset`, `hooks status`, `errors`, `-t fable|review|build|sweep`, `--overflow`, `-m sol`, `-a`/`-H` pins, `codex`/`claude` direct verbs (refused with the front-door message), `DELEGATE_*`/`CARPOOL_*`/`CLAUDE_LANE_*` environment variables (read, mapped, noted). `tests/unit/test_compat.py` runs every case and asserts the mapped op and arguments; the case table lives in `tests/fixtures/compat/cases.json` and the README lines it came from are cited.
- `subfleet/hooks.py` and the `subfleet hook <event>` verb: `SessionStart` and `UserPromptSubmit` print unacknowledged notices for the current session in v1's `render_pending` shape and mark them `surfaced` (C-15.3); `PostToolUse` (Bash) reads the tool input, and when the command ran `subfleet run`, asks the daemon for the job that submission created (by request id printed on stdout), long-polls it with a lease so two hooks never wait on one job, and exits 2 with the notice text when it finishes within the hook timeout, else exits 0 silently (the composition plan B rev 4 describes under "Notices and waiting", layer 2). `subfleet daemon install` writes the hook entries into `~/.claude/settings.json` only with `--hooks` and only after printing the diff; `--dry-run` prints without writing.
- `subfleet/notify_push.py`: layer 4, the best-effort push through the v1 session registry (`~/.claude/sessions/<pid>.json`, socket, peer token, envelope), ported from v1 `notify.py` as an adapter that records `offered` with transport `socket` and never claims acknowledgement.
- `doctor` additions (in `subfleet/cli.py` or a `subfleet/doctor.py` it calls): the compat case table loads; the hook entries in `~/.claude/settings.json` match what `daemon install --hooks` would write; the `subfleet` symlink target and version; PATH shadows for `claude`, `codex`, `subfleet`; the state root layout; whether `daemon.lock` names a live process; `--live` runs `ping` against the daemon.
- `tests/unit/test_hooks.py`: each hook event against a temp store with notice rows; PostToolUse against a fake daemon that finishes a job after a delay; the lease prevents a second waiter; timeout exits 0. `tests/unit/test_doctor.py`.

## Out of scope

The importer and `lanes transfer` (another lane), timers, sessions kit, gates internals, routing. Do not change exit codes or verbs in `subfleet/cli.py`; add in front of it.

## Acceptance for this lane

- Every command line in v1's README and in `~/.claude/CLAUDE.md` "Model routing" parses through `compat.py` to a v2 op, with the permanent verbs producing no note.
- `subfleet hook SessionStart` prints pending notices in v1's shape and marks them surfaced; `UserPromptSubmit` the same; `PostToolUse` delivers a finished job's notice with exit 2 and stays silent on timeout.
- `doctor` reports each check with pass, fail, or unknown and a fix line.
- `uv run pytest -q tests/unit/test_compat.py tests/unit/test_hooks.py tests/unit/test_doctor.py` passes in under 20 s; every docstring cites a clause.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q tests/unit/test_compat.py tests/unit/test_hooks.py tests/unit/test_doctor.py
```

Standard library only at runtime. Commit after every coherent step and push after every commit: `git push -u origin lane/cutover-compat`. Never commit to `main`, never force-push, never edit `~/.claude/settings.json` or any v1 file. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built; Tests (command, count, time); Compatibility cases (count, and any v1 command that could not be mapped); Seam changes; Open questions for the integrator. No preamble.
