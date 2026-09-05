# Lane brief: importer (v1 state import, lane ownership transfer, shadow compare)

You are building the migration tooling of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/importer`). v1 lives at `~/chief-of-staff/subfleet` with its state under `~/chief-of-staff/state/subfleet/` and `~/.local/state/delegate/`. v2 must import what `docs/migration.md` says to import, idempotently and incrementally, and must support moving one account at a time from v1 to v2 ownership. Nothing in this lane touches v1's files except to read them, and nothing runs against the real v1 state except one opt-in dry run at the end.

## Read first, in this order

1. `docs/migration.md`: the import manifest is the specification. Every row's disposition and destination is decided there; if a row is ambiguous, say so in the final message rather than guessing.
2. `docs/acceptance-contract.md` sections 1, 3, 9.1, 9.6, 10, 15, 19, and `docs/plan.md` amendments 8 and 12.
3. `subfleet/store.py` (public methods), `subfleet/store_schema.sql`, `subfleet/contracts.py`, `subfleet/policy.py`, `subfleet/cli.py` (where `lanes transfer` should hang), `subfleet/client.py`.
4. v1 formats, read-only, at the paths the manifest names. Read individual files; never run `grep -r` or `rg` over the state root. For `runs/`, read at most the newest 20 `meta.json` files to learn the schema, not all 500.

## Scope: files you own

- `subfleet/importer.py` with `import_v1(state_root, v1_state=..., delegate_state=..., roster_dir=..., dry_run=False) -> ImportReport`: one function per manifest row that is `import`, each idempotent (a second run changes nothing) and incremental (a cursor per store in `events` rows of kind `import.cursor`); a written `ImportReport` (per store: seen, imported, skipped, reasons) saved to `<state root>/import-report-<utc>.json`; anything not in the manifest reported and left alone. Rules that matter: v1 run ids keep their id, `request_id` is `v1:<id>`, states map as the manifest says, a live v1 run is imported as external and never adopted; learned percentages are never readings; Codex wham cache rows become `provider` or `stale-provider` by age; the desktop OAuth payload becomes readings for the desktop account; keepalive pings become `admission-observed`; reset redemptions become confirmed `actions`; delegate cooldowns become closures with scope, clock source, and `source_event: v1-cooldown`; the integration-events salt is copied; roster accounts become `lanes` rows with `owner: v1`.
- `subfleet lanes transfer <lane> --to v1|v2` (CLI verb and daemon op if the daemon must own it; keep the CLI thin): flips `owner`, writes an `events` row, and edits both rosters in one step: v2's `lanes.json` and v1's roster file the manifest names, so v1 stops touching the account (plan amendment 8). The v1 edit is the only write to a v1 file in this repo; it is done with a backup copy beside the file and refused unless `--i-understand-v1-edit` is passed, and `--dry-run` prints the diff.
- `tools/compare_decisions.py` (not shipped in the package): given a `SUBFLEET_HOME` and v1's `~/.local/state/delegate/decisions.jsonl`, replays each v1 decision's inputs through `subfleet why --task --tier --json` (or the routing engine directly if the CLI cannot express an input) and writes `docs/shadow-diffs/<date>.md` with every difference and a one-line explanation slot.
- `tests/unit/test_importer.py`: a synthetic v1 state tree built in a temp dir from the formats you read (a few runs, a notices file, an outbox database with the `messages` table, `cooldowns.json`, `reset-policy.json`, `keepalive.json`, `capacity-live-cache.json`, `claude-oauth-raw.json`, the two roster files); import twice and assert idempotence; assert every rule above; assert an unknown store is reported and untouched. `tests/unit/test_lanes_transfer.py`: transfer flips ownership, writes the event, edits both rosters, refuses without the flag, and `--dry-run` writes nothing.
- One opt-in dry run against the real v1 state, gated by `SUBFLEET_LIVE=1`, that produces an `ImportReport` without writing to any store; you do not set that variable; the integrator does.

## Out of scope

Cutover of `run`, hooks, notices delivery, the daemon's timers, sessions, gates. Do not edit `docs/migration.md`; if you disagree with a row, say so in the final message.

## Acceptance for this lane

`uv run pytest -q tests/unit/test_importer.py tests/unit/test_lanes_transfer.py` passes in under 20 s; every docstring cites a clause or a `migration.md` row; the dry-run path exists and is documented at the top of `subfleet/importer.py`.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q tests/unit/test_importer.py tests/unit/test_lanes_transfer.py
```

Standard library only at runtime. Commit after every coherent step and push after every commit: `git push -u origin lane/importer`. Never commit to `main`, never force-push. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built; Tests (command, count, time); Manifest rows implemented, and rows deferred with the reason; Seam changes; Open questions for the integrator. No preamble.
