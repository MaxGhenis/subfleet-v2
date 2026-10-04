# Sessions lane report (milestone 6)

Branch `lane/sessions`. Brief: `docs/lanes/sessions.md`.

## Built

`subfleet/sessions/`, the `subfleet-sessions` console entry, and the permanent
verbs `subfleet sessions <verb>` and `subfleet handoff` (C-17.1).

| module | what it owns |
|---|---|
| `transcripts.py` | the turn classifier (interrupted / completed / stopped / tickled / empty), the app's synthetic resume stub, the dedupe key, the headless-lane heuristic, the cold scan (C-23.31, C-23.33, C-23.34) |
| `registry.py` | `~/.claude/sessions` rows, C-23.30's ranking, duplicate live instances, C-23.31's two lane signals |
| `nudge.py` | tickle and muster: eligibility, the delay, the re-check, the manual quiet window, the `SUBFLEET_TICKLE` switch, the detached worker the hook spawns |
| `revive.py` | the candidate filters, the tier rule, and the `revive` job |
| `handoff.py` | the scrub list, the suppression list, the section caps, the brief, the submission |
| `mirror.py` | a full port of `bin/subfleet-mirror` v5.0, plus the per-pass health sidecar |
| `client.py` | the kit's one connection to the daemon |
| `cli.py` | the verbs, shared by both entry points |

Verbs: `sessions [list|continue|tickle|muster|revive|mirror|retire|unretire|
handoff]` and `handoff`. `continue --scope interrupted|idle|cold` is v1's
tickle, muster and revive; `retire`/`unretire` are C-23.35's durable flag, which
v1 had as a function and no verb.

## The three decisions worth reading

**A revive is a job whose `caller_session` is the session being revived.** Not
the operator's. That is what makes C-6.5 — "a writable job for a session id that
already has one running from another instance" — refuse exactly the 2026-09-04
twin, and it puts the completion notice where the continued work is. The cost is
that the revive does not appear in the operator's `runs --mine`; the CLI prints
the job id instead. See "Open questions".

**A cold sweep decides nothing.** Plan decision 7 makes an explicit handoff the
default recovery of a cold session, so a bare `--scope cold` lists what is
recoverable and names the two recoveries. `--revive` launches headless
continuations; `--handoff --to <model>` dispatches one continuity brief per
candidate. Neither is automatic: both spend a lane and both write somebody
else's worktree.

**Mirror health is the sidecar and only the sidecar.** v1 inferred an in-flight
pass from `pgrep -f bin/subfleet-mirror` — which a rename would have silently
broken — and judged staleness from log recency, which fired a false "stalled" on
2026-08-19 07:08 because `--quiet` keeps the log silent on a no-op pass. v2's
pass records itself before the work starts and again when it ends, so an
in-flight pass is *visible* rather than guessed at, and C-23.28's thirty-minute
cutoff is applied to a start time the mirror wrote down.

## Tests

```
uv run pytest -q tests/unit/test_sessions_*.py tests/fake/test_sessions_end_to_end.py
```
202 passed in 6.9 s (the brief's cap is 30 s).

Full suite: `uv run pytest -q` — 3035 passed, 5 skipped in 148 s.
Unit + fake, run repeatedly during the lane: 2976 passed in 85 s.

`tests/sessions_fixtures.py` builds a `~/.claude` and a desktop session store
under `tmp_path` and reaches them through `SUBFLEET_CLAUDE_DIR` and
`SUBFLEET_SESSION_STORE`. No test reads or writes the operator's own
`~/.claude`, `~/Library/Application Support/Claude`, or `~/chief-of-staff`.
`FAKE_SECRET` is a token shaped like the ones the scrub list catches and is not,
and has never been, a key.

## Bugs the tests found

1. **The store's audit event shadowed the record it audited.**
   `store.transaction(kind, ...)` writes its own event under the kind it is
   given, and `_session_events` reads the newest row of each kind — so a
   transaction named `session.nudged` buried the nudge record under a summary
   carrying no dedupe key, and every second nudge fell through to the cooldown
   message instead of the dedupe one. The audit kinds are now distinct
   (`session.nudge.recorded`).
2. **`load_policy` refused `sessions.mirror_interval_s: 0`**, which is the
   documented way to switch the mirror timer off. The `sessions` block is now
   validated on its own: zero is *off* for every cap, window and interval there,
   except the two mirror health windows, where zero is not off but broken.
3. **`session_event` called `wake_worker` unguarded**, so an exception raised
   before `wake_worker`'s own `try` would have exited non-zero on a
   `SessionStart` — which blocks the session from starting.
4. **A skipped revive was `cancelled`**, and `exit_for_job` reports a cancelled
   job as 130 whatever its rc, hiding the refusal C-17.3 numbers 7. It is now
   `failed` with rc 7.

## Two fixture facts worth carrying forward

* A provider-limit banner sits **below** the narration it interrupted. The
  classifier stops at the first real assistant turn reading backwards, so a
  banner above the text is never seen and the turn reads `completed`. v1 behaves
  identically; the branch only fires when the banner is the last entry.
* A long unbroken alphanumeric run is caught as **encoded binary** before any
  section cap applies, so a caps test has to use prose.

## Compat cases added

`sessions`, `handoff`, `tickle`, `muster` and `revive` left `DELEGATED`, and
`mirror` left `REFUSED` — v2 owns the mirror now (C-23.28). All six are
`PERMANENT` spellings and carry no note, because plan amendment 1 makes every v1
spelling permanent and a note on a spelling the contract promises to keep trains
agents to change commands that work.

| v1 spelling | v2 argv |
|---|---|
| `sessions [--all] [--json]` | `sessions` (the parent carries the listing's flags, as `runs` does) |
| `handoff <id>\|--last --to M [-C DIR]` | `handoff` |
| `tickle [--session\|--transcript\|--all\|--dry-run\|--force\|--json]` | `sessions continue --scope interrupted` |
| `muster [--dry-run]` | `sessions continue --scope idle` |
| `revive [--model\|--max\|--dry-run]` | `sessions continue --scope cold` |
| `mirror [--list\|--dry-run\|--quiet\|--prune\|--dead-home\|--exclude\|--no-restore\|--no-flag-sync\|--archive]` | `sessions mirror` |

`tests/fixtures/compat/cases.json`: 29 existing rows re-recorded, 4 added
(`tickle-dry-run` from the `/tickle` skill, and `mirror-no-flag-sync`,
`mirror-prune-dead-home`, `mirror-version` from `bin/subfleet-mirror`'s own
argparse). 393 cases total; every other row is byte-identical.

Two `V1_ONLY_FLAGS` rows:

* `revive --no-fallback` — **dropped** with one note. v1 walked a model chain
  (`SUBFLEET_REVIVE_MODELS`) and this stopped it after the first; v2 pins never
  fall back (C-11.2), so `--model M` already means M or nothing.
* `mirror --version` — **refused**. Dropping it would perform the sidebar pass
  the caller asked to identify; the fix names `subfleet -V`.

Three hard-coded sets in `tests/unit/test_compat.py` had to move with the
tables, and would have silently stopped grading these verbs otherwise:
`PERMANENT_HEADS`, the `pairs` map in `test_no_v1_flag_is_unaccounted_for`, and
the `["codex", "claude", "mirror"]` parametrize on the provider-verb refusal.
A new `test_no_v1_mirror_flag_is_unaccounted_for` harvests
`bin/subfleet-mirror`'s `add_argument` lines, because v1's `subfleet mirror` is
an `add_help=False` REMAINDER passthrough and its flags are not in v1's argparse
tree for the existing diff to see.

## Seam changes

All additive. Nothing existing changed meaning.

**`subfleet/protocol.py`** — `OPS` gains `sessions`; `SessionsArgs` is new.

**`subfleet/daemon.py`**

* `sessions` op: `state` (retirement, last nudge, revive-lease holder, plus the
  ledger's own lane session ids), `nudged` (the reservation, which re-checks the
  dedupe key and the cooldown inside the transaction that records the nudge),
  `retire`, `unretire`. Records live in `events`; the audit kinds are distinct
  from the record kinds (`audit_kind`), for the reason in "Bugs" above.
* `revive_lease_key(session)` → `session:<id>:revive`, taken in the
  `attempt.reserved` transaction, held by the job id so every existing
  holder-keyed release site frees it (C-23.55).
* `_skip_revive`: a revive-lease conflict at admission is **terminal**, not a
  wait. Admission's ordinary conflict branch sets `waiting`/`capacity` and
  retries forever, and a revive that waits for its twin would launch the twin
  the moment the twin ended.
* `_validate_conflicts` gains a revive branch, so the ordinary second revive is
  refused at submit with exit 7 naming the holder, and the lease is the durable
  second half of the same rule (C-6.5's shape).
* `_launch` uses `adapter.resume_launch` for a job of kind `revive`, so it
  continues the named session instead of starting a new one. `build_launch`'s
  path is untouched for every other kind, including `resume` — which the daemon
  still launches as a fresh session, unchanged from before this lane.

**`subfleet/scheduler.py`** — `probe_required` is unconditionally true for a job
of kind `revive` (C-23.20). `_prepare_route` converges after one probe because
the approved pair short-circuits its loop.

**`subfleet/adapters/base.py`, `subfleet/adapters/codex.py`** —
`resume_launch` gains `model_id: str | None = None`, which the Claude adapter
already had. Codex accepts and ignores it: a Codex thread already carries its
resolved model.

**`subfleet/policy.py`, `subfleet/default_policy.json`** — an additive
`sessions` block: `nudge_max_age_h` 8, `nudge_cooldown_min` 1.5,
`nudge_delay_s` 8, `nudge_sample_s` 3, `sweep_quiet_s` 120, `muster_max_age_h` 2,
`muster_quiet_s` 120, `revive_min_age_s` 120, `revive_max_batch` 8,
`auto_revive_desktop_owned` false, `mirror_interval_s` 60, `mirror_stall_min` 10,
`mirror_hang_min` 30, `mirror_ultracode_default` true, and `handoff_caps`
(v1's nine section constants).

**`subfleet/timers.py`** — a `mirror` cycle on its own single-worker pool, so an
8-minute pass cannot hold a probe or a keepalive behind it in the two-slot cycle
pool. `sessions.mirror_interval_s: 0` removes it from `intervals`.

**`subfleet/doctor.py`** — one `desktop sidebar mirror` row, plus
`subfleet.sessions.mirror` in the module list.

**`subfleet/hooks.py`** — `SessionStart` calls `wake_worker`, which records the
session, the source and the transcript and spawns
`python -m subfleet.sessions.cli continue --scope interrupted --session … --source …`
detached. It decides nothing and never raises.

**`subfleet/cli.py`** — `cmd_sessions` and `cmd_handoff`, both thin.
**`subfleet/compat.py`** — the tables above, plus `_verb_of` learning
`sessions_command` and `_target_path` stopping at the first flag (otherwise
`sessions continue --scope interrupted` reads as a verb path
`sessions.continue.interrupted`).

**`pyproject.toml`** — `subfleet-sessions = "subfleet.sessions.cli:main"`.

**`tests/fake_adapter.py`** — `resume_launch` builds a launch recording which
session it continued, so a revive's `--resume` target is observable in the
attempt row.

## Contract questions

1. **C-23.20 says "a `provider` reading taken in the same pass".** v2's probe
   writes whatever readings the adapter returns; `measured` in the recorded
   decision is `fresh_provider` within `caps.reading_ttl_s` (120 s), which is a
   *stored* reading by the clause's own wording. The lane implements the clause
   as "a revive always probes in the pass that admits it" —
   `probe_required` is unconditionally true for kind `revive`, so the lane is
   measured in that pass whatever the ledger holds. If the intent was stricter
   (the reading object itself must have been produced by *that* probe), the
   scheduler needs a way to say so and the clause should say which.
2. **C-23.31 says a request naming a headless lane run "is refused with the
   reason".** The lane reads that as: a sweep that merely passes over a lane
   reports 0 and lists the reason; a request that *names* one exits 7. Both
   `sessions continue --session <lane>` and `handoff <lane>` now do the latter.
   Confirm that reading.
3. **Ledger row 212 is marked `replace`** and the replacement text says the
   prompt "is written once and retained as the immutable record rather than
   unlinked". The test keeps the row's prescribed name
   (`test_handoff_prompt_created_private_and_unlinked_after_dispatch`) while
   asserting the replacement behaviour, which reads oddly. Renaming it is a
   ledger edit, so it was left alone.

## Open questions for the integrator

1. **The revive job's `caller_session`.** It is the session being revived, for
   the C-6.5 reason above. The consequence is that `subfleet runs --mine` from
   the operator's session does not show the revive, and the terminal notice
   lands in the revived session's inbox rather than the operator's. If you would
   rather the operator own the job, the twin protection has to move somewhere
   else — an extra `session:<target>` lease in the admission transaction — and
   `_notice` needs a second target.
2. **`docs/invariants.json` still records `subfleet/sessions/tickle.py`** as the
   `v2_module` for rows 179–184 and 186–192. The brief names `nudge.py` and
   `revive.py`, which is what was built. Row 159 names `mirror.py` and row
   208–212 name `handoff.py`, which match. The prescribed **test names** are all
   used verbatim, so a coverage test keying on them works either way; only the
   module column is stale.
3. **`subfleet mirror` no longer delegates to v1.** Both mirrors can run at once
   during the shadow period — v2's merge base is
   `$SUBFLEET_HOME/sessions/mirror-flags.json` and it never touches v1's
   `~/.claude/cc-mirror-state.json` — but they will both write the desktop
   store, and v1's launchd job is still installed. Decide whether the cutover
   unloads `com.maxghenis.cos.subfleet-mirror` before or after the daemon's
   timer is enabled.
4. **`sessions.handoff_caps` is validated but nothing consumes `recent_records`
   as a character cap** — it is a count of main-chain entries that bounds the
   scan. It lives in `handoff_caps` because that is where v1 kept it; move it if
   you would rather the section read as characters only.
