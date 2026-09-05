# Lane brief: sessions (milestone 6: tickle, muster, revive, mirror, handoff as clients of the job store)

You are building the sessions kit of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/sessions`). In v1 the tools that keep Claude Code sessions alive across the desktop app's account switches (tickle, muster, revive, mirror, handoff) lived inside the dispatcher and multiplied its state; on 2026-09-04 a headless revive produced a second live instance of a session ("the twin"), and on 2026-09-05 the same thing happened to the integrator of this very rebuild. In v2 they are a separate entry point, `subfleet-sessions`, reached through the permanent verb `subfleet sessions`, that reads Claude Code's transcripts and session registry and submits jobs through the daemon like any other client.

## Read first, in this order

1. `docs/acceptance-contract.md`: C-17.1 (the `sessions` and `handoff` verbs), C-15 (notices), C-6.5 (the refusal of a second writable job for a session id running from another instance), and the carried-forward clauses C-23.14 (what a handoff may carry), C-23.20 (revive measures the lane it is about to use), C-23.28 (mirror health is a state file), C-23.30 to C-23.36 (registry rows, headless lane runs are not sessions, the importer, when a session may be nudged, the worker decides against the transcript, which sessions revive admits, a handoff is bounded), C-23.39 (revive keeps the session's tier), C-23.54 (one dispatch path), C-23.55 (one live revive per session). Cite clauses in every test docstring.
2. `docs/plan.md` open decisions 7 and 8 and `docs/plan-b-rev4.md` "Session continuity, kept beside the fleet": automatic revival is off by default for sessions the desktop app owns; explicit handoff is the default recovery of a cold session; tickle stays automatic; the mirror is a 60 s file-copy timer.
3. `subfleet/cli.py`, `subfleet/compat.py` (which currently delegates `sessions`, `tickle`, `muster`, `revive`, `handoff` to the v1 binary; your work replaces that delegation for these verbs), `subfleet/client.py`, `subfleet/hooks.py` (SessionStart and UserPromptSubmit already read notices), `subfleet/notify_push.py` (the v1 session-registry push, ported), `subfleet/daemon.py` (submit; the `session:<id>` lease), `subfleet/store.py`, `subfleet/contracts.py`.
4. v1, read-only (never modify, never run its commands): `~/chief-of-staff/subfleet/subfleet/tickle.py` (`turn_state`, interruption detection, the nudge rules), `~/chief-of-staff/subfleet/subfleet/handoff.py` (`_build_brief`, the scrub list), `~/chief-of-staff/subfleet/bin/subfleet-mirror` (the sidebar index copy and its state sidecar), `~/chief-of-staff/subfleet/subfleet/notify.py` (registry lookup, `find_session`), the `sessions`, `tickle`, `muster`, `revive`, `handoff` verbs in `~/chief-of-staff/subfleet/subfleet/cli.py`, and `~/chief-of-staff/subfleet/tests/test_tickle.py`, `test_handoff.py`, `test_session_mirror.py` for the behaviour to preserve. Read `~/.claude/sessions/` and `~/.claude/projects/` structure only through the fixtures you build from a few redacted real files; tests never touch the real directories.

## Scope

- `subfleet/sessions/` package with console entry `subfleet-sessions` and the `subfleet sessions <verb>` and `subfleet handoff` verbs wired into `cli.py` (remove those verbs from `compat.py`'s delegation list and add compat cases that map v1's `tickle`, `muster`, `revive` spellings to `sessions continue --scope interrupted|idle|cold` while keeping `sessions tickle|muster|revive` as aliases; every spelling in `~/.claude/CLAUDE.md` and the `tickle` and `muster` skills keeps working).
  - `registry.py` (C-23.30, C-23.31): read the session registry; rank duplicate rows by live pid, then present socket, then newest start; recognise a headless lane run by its recorded lane session marker and never treat it as a session; detect two live instances of one session id and report both.
  - `transcripts.py` (C-23.33, C-23.34): locate a session's transcript, classify the last turn (interrupted, idle, cold, running) as v1 `turn_state` does, recognise the app's synthetic resume stub, and re-check quiet time immediately before acting.
  - `nudge.py` (tickle and muster, C-23.33): nudge only interrupted sessions younger than eight hours, once per interruption point, with a per-session cooldown recorded in `events`; muster nudges idle sessions the operator lists; delivery through the daemon's `ping` op (notice rows), never a direct socket write from this package; a duplicate live instance is nudged at most once and the report names both.
  - `revive.py` (C-23.20, C-23.35, C-23.39, C-23.55): explicit `sessions revive <id>` submits a job of kind `revive` on the session's own tier unless `--model` is given, after a live probe of the lane; the daemon's `session:<id>:revive` lease (add it as a lease key in the admission path if absent, additive) is taken in the admission transaction; a revive is refused for a headless lane run, for a session the operator retired, for a session whose recorded permission mode is not `bypassPermissions`, and, by default, for a session the desktop app owns (policy `sessions.auto_revive_desktop_owned: false`); `sessions continue --scope cold` therefore defaults to handoff, and `--revive` opts in per call.
  - `handoff.py` (C-23.14, C-23.36, C-23.54): build the brief from the transcript as v1 does, scrub credentials and encoded binary while keeping ordinary code and commands, suppress the results of credential-reading tool calls, bound every section by character caps, point at the source transcript, and submit through `subfleet run` (the daemon's submit op) with the caller session recorded so the notice comes back to the right place.
  - `mirror.py` (C-23.28): the 60 s desktop sidebar index copy as a daemon timer (register it with the timers component if `subfleet/timers.py` exists on `main`; otherwise a `sessions mirror --once` verb plus a timer stub the timers lane can adopt), with a per-pass state sidecar that `doctor` reads; it tolerates a long in-flight pass and never calls a provider.
- `policy.json` keys (additive, defaults in `default_policy.json`): `sessions.nudge_max_age_h` 8, `sessions.nudge_cooldown_min`, `sessions.auto_revive_desktop_owned` false, `sessions.handoff_caps` (per-section character caps), `sessions.mirror_interval_s` 60.
- Tests under `tests/unit/test_sessions_*.py` from redacted fixtures (a registry directory with a duplicate pair, transcripts in each turn state including the synthetic resume stub and a headless lane run, a retired-session marker), and `tests/fake/test_sessions_end_to_end.py` running `subfleet sessions continue --scope interrupted` against the fake daemon and asserting one notice per interrupted session, none for the duplicate's second instance, and a refused revive for a desktop-owned session unless `--revive` is passed.

## Out of scope

Gates, the importer, the timers' other duties, the daemon's core. Do not change existing clause meanings; if one must change, say exactly what and why in your final message.

## Acceptance for this lane

- Every v1 spelling for these tools parses and reaches the new code (compat cases added and green).
- A fixture session interrupted 30 minutes ago is nudged once; the same fixture is not nudged again within the cooldown; a fixture interrupted nine hours ago is not nudged; a headless lane run is never nudged or revived; a duplicate pair yields one nudge and a report naming both pids.
- `sessions revive` of a desktop-owned session is refused with the fix line naming `--revive` and `handoff`; with `--revive` it submits a job whose decision shows the live lane probe; a second revive of the same session while the first is live is refused by the lease.
- A handoff brief from a fixture transcript containing a fake secret contains no secret, keeps the code blocks, respects the caps, and is submitted as a job with the caller session recorded.
- `uv run pytest -q tests/unit/test_sessions_*.py tests/fake/test_sessions_end_to_end.py` passes in under 30 s; the full suite stays green.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step and push after every commit: `git push -u origin lane/sessions`. Never commit to `main`, never force-push. Never read a real credential value into a file, a log, or a fixture; never write under `~/.claude`. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built; Tests (command, count, time); Clauses covered; Compat cases added; Seam changes; Contract questions; Open questions for the integrator. No preamble.
