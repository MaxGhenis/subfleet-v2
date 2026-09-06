# Shadow-week coordination (v1 and v2 in parallel)

Started 2026-09-06 07:20 EDT. During the shadow week v1 (`~/chief-of-staff/subfleet`, branch `master`) keeps running production and other sessions keep improving it, while v2 (`~/subfleet-v2`, branch `main`) runs its daemon beside it and owns the canary account. This file is the ledger both sides read. Integrator for v2: the Fable session named `maxghenis-02` (message it by that name); it merges v2 lanes and ports v1 changes that matter to v2.

## Rules while both run

- v2 changes go through `main` of `MaxGhenis/subfleet-v2` with a green full suite (`uv run pytest -q`, 3,400+ tests); lanes branch from `main` under `~/subfleet-v2-lanes/<lane>/` and the integrator merges.
- v1 changes stay on v1's own branches and `master`; v2 never edits v1 files except through `sf2 lanes transfer` (the roster record and the home rename).
- Machine state nobody else touches during the soak: `~/.subfleet/` (the v2 state root), `~/.subfleet/lanes/codex-3/` (the relocated canary home; `~/.codex-3` is gone on purpose and must not be re-created), launchd `com.subfleet.daemon` and `com.subfleet.soak-report`, and the `transferred_to_v2` entry in `~/chief-of-staff/subfleet/codex-accounts.json`.
- A v1 change to a file the v2 importer reads (`claude-oauth-raw.json`, `capacity-live-cache.json`, `claude-accounts.json`, `codex-accounts.json`, `runs/<id>/meta.json`, `keepalive.json`, `notices`, `outbox.sqlite3`; manifest in `docs/migration.md`) gets a row below before it merges, with the exact keys that change.
- A v1 change to a verb v2 delegates until milestone 6 or 7 (`sessions`, `revive`, `tickle`, `muster`, `handoff`, `mirror`, `gate`) stays live automatically, because v2's compat layer calls the v1 binary; v2's own port of that behaviour (`subfleet/sessions/`, `subfleet/gate/`) is updated from the row below.
- Capacity vocabulary: v2 labels every reading `provider`, `stale-provider`, `admission-observed`, `local-backoff`, or `unknown` (contract C-9.1). A v1 change that adds provenance to percentages should use words that map onto these, so the nightly shadow-diff compare can explain every difference.

## v1 changes in flight

| When | Session (branch) | What changes | Files | v2 impact | v2 action | Status |
|---|---|---|---|---|---|---|
| 2026-09-06 | "Make subfleet report real login usage, not inferred percentages" (`claude/nifty-rubin-204883`) | Every Claude percentage names its source; inferred figures say so | `subfleet/claude.py`, `render.py`, `snapshot.py`, `paths.py`, `bin/subfleet-statusline`, tests | If `capacity-live-cache.json` or `claude-oauth-raw.json` gain or rename keys, the importer's capacity rows and the shadow compare must read them; v2's sensor already labels readings by source | Awaiting the session's key list; align names with C-9.1 labels | open |
| 2026-09-06 | "Fix subfleet revive: persistent host, no template causes, independent liveness alert" (`claude/vibrant-hypatia-a0021e`) | Revive semantics in v1's sessions kit | v1 `sessions`/`revive` code (not yet committed) | Live in v2 through delegation until milestone 6; v2's `subfleet/sessions/` port must mirror it (C-23.30 to C-23.36); a liveness alert that reads `ps` must treat an inspection failure as unknown, never as dead (v2 defect fixed 2026-09-05, C-4.2) | Awaiting a summary of the behaviour change on merge | open |

## v2 state other sessions should know

- `main` at the commit that adds this file: all seven milestones merged, 3,482 tests green, release gates green except canary, soak, and shadow diff.
- Decisions on the four cutover prerequisites: `docs/decisions/2026-09-05-cutover-prerequisites.md` (peer-reviewed by Astra and a Fable peer; final round in progress).
- Canary: codex-3 (max@axiom.org) transfers to v2 today; 100 read-only isolated-review jobs wait behind the weekly closure until Monday 07:04; seven-day soak with a daily record under `docs/soak/`.
- Runbook: `tools/canary_runbook.sh`, one phase per invocation.
