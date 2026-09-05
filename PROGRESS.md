# Lane progress: cutover-compat

Brief: `docs/lanes/cutover-compat.md`. Branch `lane/cutover-compat`.

## State

| Step | State |
|---|---|
| Read the contract, plan, v2 seams, v1 surface | done |
| `docs/reference/claude-hooks.md` (fetched + binary check) | done |
| Compatibility case harvest → `tests/fixtures/compat/cases.json` | done (389 cases) |
| `subfleet/compat.py` | done |
| `subfleet/hooks.py` + the `subfleet hook <event>` verb | done |
| `daemon install --hooks [--dry-run]` | done |
| `subfleet/notify_push.py` (layer 4) | done |
| `subfleet/doctor.py` + `cmd_doctor` rewired, `--live` | done |
| Entry points (`pyproject.toml`, `subfleet/__main__.py`) through compat | done |
| `tests/unit/test_compat.py` (1461) | done |
| `tests/unit/test_hooks.py` (44) | done |
| `tests/unit/test_doctor.py` (31) | done |
| `tests/unit/test_notify_push.py` (31, beyond the brief) | done |

`uv run pytest -q tests/ --ignore=tests/live` — 2356 passed, 62 s. The three
files the lane's acceptance names run in about 4 s together.

## What is here

- **`subfleet/compat.py`** — the front door in front of `cli.main`. Four
  dispositions: `map` (a permanent spelling, no note), `note` (deprecated, one
  stderr line), `delegate` (v1 still owns the verb; its exit code comes back
  unchanged), `refuse` (the direct provider verbs, exit 7). Reads and maps the
  `CARPOOL_*` → `SUBFLEET_*` aliasing v1 does in its bash launcher, and reports
  the `DELEGATE_*` / `CLAUDE_LANE_*` names without obeying them.
- **`subfleet/hooks.py`** — the three Claude Code entry points. SessionStart and
  UserPromptSubmit exit 0 and print v1's `render_pending` shape; PostToolUse
  takes a `flock` lease, long-polls, and exits 2 with the notice on stderr.
  `plan`/`apply`/`installed` write `~/.claude/settings.json` only through
  `daemon install --hooks`, always after printing the diff, and never touch v1's
  entries.
- **`subfleet/notify_push.py`** — layer 4, ported from v1 `notify.py`. Records
  `offered` with transport `socket`; never `acknowledged`.
- **`subfleet/doctor.py`** — one table, `pass`/`fail`/`unknown` with a fix line
  on every row. `cli.doctor_checks` is now a call into it, not a second table.

## Findings that shaped the build

1. `asyncRewake` hooks "cannot add context or influence decisions on successful
   completion", so layer 2 exits **2** with the notice on **stderr** and exits 0
   silently when it has nothing (`docs/reference/claude-hooks.md` §1, §3).
2. Exit 2 on `SessionStart` blocks session startup and on `UserPromptSubmit`
   blocks the prompt **and erases it**, so those two always exit 0.
3. The installed 2.1.260 binary has `tool_result` and `tool_response` but not
   `session_start_reason`; the hooks read both spellings of each field.
4. v1's `subfleet run` is a pass-through to `delegate.main()`, so the real v1
   `run` flag set is `delegate.py:_parser()`, not `cli.py`.
5. **v1's `bin/subfleet` is a bash launcher that aliases `CARPOOL_*` →
   `SUBFLEET_*`** before exec'ing Python (`bin/subfleet:8-12`). v2's entry point
   is a console script with no launcher, so the aliasing had to move into
   `compat.map_env` or it would stop happening at the cutover.
6. **There is no `subfleet-gate` binary.** `gate` is `subfleet/consensus.py`
   reached through v1's own `subfleet`, so the pass-through target is the v1
   binary (`SUBFLEET_V1_BIN`, else `~/chief-of-staff/subfleet/bin/subfleet`).
7. **v1's runners call subfleet's hidden verbs back through
   `$SUBFLEET_RUN_SUBFLEET` / `$DELEGATE_SUBFLEET`** on every record they write.
   A symlink flip that broke `_record-run` would break every v1 run already in
   flight, which is why `delegate` exists as a disposition and why the hidden
   verbs are delegated silently.
8. **`--independent` means two different things.** It was an argparse
   abbreviation of v1's hidden `--independent-review` and is a real, unrelated
   v2 flag (C-7.3). v2's meaning wins; the abbreviation matcher is barred from
   touching any option v2 itself defines.
9. **`jobs` and top-level `show` are not v1 spellings.** Neither string appears
   anywhere in the v1 tree. They are aliases C-17.1 introduces, so they cannot
   break a v1 command — only add one.
10. **The console script pointed at `cli.main`, and `python -m subfleet` did
    not exist at all** — so the compatibility layer was unreachable at runtime
    and the hook command `daemon install --hooks` writes would not have run.
    Both are wired and pinned by tests.
11. **`PYTHONPATH` on this machine points at the v1 checkout**, which outranks
    site-packages and an editable install's `.pth`, so a v2 console script in a
    v2 virtualenv imports v1's package and prints v1's verb table.
    `doctor.check_pythonpath` reports it; `check_symlink` recognises v1 by its
    output as well as by its path.

## Next

Nothing outstanding in this lane's scope. Open questions for the integrator are
in the final report.
