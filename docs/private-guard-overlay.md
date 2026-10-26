# Private guard overlay and portable package

This completes the packaging boundary from plan B decision 9. The repository
remains private. No repository publication, visibility change, licensing
decision, or historical-data sanitization is implied. The wheel contains the
portable `subfleet` core; the source distribution contains that core, README,
project metadata, and lockfile. Neither contains the private overlay, tests,
operator reports, or staging tools.

The reviewed operator pair moved unchanged to `private/guard/`:

| File | SHA-256 |
| --- | --- |
| `never-rules-hook.sh` | `a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db` |
| `TRUST` | `0b9d688e6b2ca3adefdb8ad465c0fad5e6e18dfa884bc2309ff4f506c2287bab` |

No rule, reference-path pin, version pin, or provenance was regenerated. The
reference path in TRUST is historical input to the parity check. The actual
hook command and hooks/list identity are derived from the resolved installed
overlay path.

## First installation over the existing private release

The deployment owner performs these steps; packaging tests never touch installed
state. Use the actual daemon state root, including a custom `SUBFLEET_HOME` if
configured. In the private reviewed checkout, before changing the release pointer:

```sh
uv run python -m tools.stage_guard_overlay --source private/guard --state-root "$HOME/.subfleet"
```

The helper verifies the source pins, copies exact bytes into a fresh directory,
validates the copied pair, then renames the directory into `<state root>/guard`.
Its directory is mode 700, hook 755, and TRUST 600. It is idempotent only for an
already-valid, byte-identical pair and refuses a different or damaged existing
overlay. It does not generate TRUST, call Codex, alter releases, restart the
daemon, or rewrite active launches. A refused stage is an installation blocker;
do not deploy a core-only release and assume it will fall back to the old hook.

After staging, compare both installed files with the reviewed source and run
offline validation using the new source or staged release:

```sh
cmp private/guard/never-rules-hook.sh "$HOME/.subfleet/guard/never-rules-hook.sh"
cmp private/guard/TRUST "$HOME/.subfleet/guard/TRUST"
uv run python -c 'from pathlib import Path; from subfleet.doctor import check_guard_preflight; import os; r=check_guard_preflight(Path(os.environ.get("SUBFLEET_HOME", "~/.subfleet")).expanduser()); print(r); raise SystemExit(0 if r["status"] == "pass" else 7)'
```

Use the normal backed-up release installation and controlled daemon restart.
Preserve its configured state root, HOME/PATH, guard timeout/cache/TRUST
overrides, and provider CLI wrappers (including `current/bin/codex` when the
front-door shim points there). Staging at the state-root default requires no new
environment variable. A shell-only override does not configure launchd: existing
`SUBFLEET_GUARD_TRUST`, `SUBFLEET_CODEX_GUARD_CACHE`, and
`CODEX_GUARD_PREFLIGHT_TIMEOUT` overrides must be in its environment to survive a
restart. Explicit `preflight(hook_path=..., trust_path=...)` still wins over
defaults; a TRUST environment override does not change the default hook path.

**Retain all old release directories used by active launches.** Their recorded
`-c hooks=...` arguments still name the old bundled hook. The staged overlay and
new daemon do not rewrite those commands. Do not remove those releases until
the corresponding guardians and provider descendants have finished and normal
containment has confirmed this. Preserve guardian adoption through the daemon
restart using the existing deployment procedure. This migration changes no job,
attempt, lane ownership, or credential record. The native app is independent;
keep its installed version and do not infer it from the Python package version.

Offline doctor verifies the effective pair and reports its paths. It does not
claim runtime Codex trust: use the normal preflight (or explicitly authorized
`doctor --live`) to verify hooks/list before admitting executable Codex work.
The new absolute path and overlay pins produce a new cache key; existing cache
markers cannot stand in for this verification. A later approved hook replacement
at the same path also forces re-verification. Malformed or mismatched files are
checked on every launch, even when a cached verdict exists.

If deployment must roll back, restore the previous release pointer through the
normal rollback procedure and retain both old releases and the staged overlay.
Old releases keep their bundled resolution; new in-flight launches may already
name the external overlay. Removing either can break future tool calls in an
otherwise live provider session. Do not edit policy in place while sessions use
it; future policy revisions need their own reviewed rollout.

Claude's separately installed global hook and isolated read-only review behavior
are unchanged. The test fixture in `tests/fake/guard.py` is deliberately synthetic
and must never be staged as operator security policy. It is absent from both
prepared distributions, and the runtime has no test-fixture fallback.

## Policy revision: ~/.subfleet roots (D-LANE-SCOPE1, 2026-10-24; r2, r3 and r3b 2026-10-25)

The table above is the original pair of 2026-09-05. It is not the rollback: its TRUST
(`0b9d688e…`) pins `codex-cli 0.153.3`, and staging it would make preflight refuse every
Codex lane with VERSION (`subfleet/guard/preflight.py`, the `codex_version` check). Never
stage that TRUST. The rollback is hook `a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db`
(the table's hook) with the installed TRUST of 2026-09-29,
`b3193b97d712bda6a5cc0f2447907e674f7ed90911a82d7709de8298b42aaa06` (the `codex-cli 0.159.0`
pin and the provenance of the 0.158.0 and 0.159.0 moves, which the table predates).

On the hub's decision D-LANE-SCOPE1, after a lane's `rg --files -g AGENTS.md ~/.subfleet`
walked 14,121 job folders and 1,765 job worktrees, the `unscoped-search` rule treats
`~/.subfleet` and any one path component directly under it (`~/.subfleet/jobs`,
`~/.subfleet/worktrees`, …) as broad roots, spelled `~`, `$HOME`, `${HOME}` or
`/Users/<user>`, with or without a trailing slash. Its r2 adds the names lanes have for that
folder: `$SUBFLEET_ROOT` (the daemon sets it on every attempt) and `$SUBFLEET_HOME` (the
launch allowlist passes it through), each also as `${…}`, are spellings of `$HOME/.subfleet`
under the same depth rule. One job's folder (`~/.subfleet/jobs/<id>`, `$SUBFLEET_ROOT/jobs/<id>`)
and anything deeper stays scoped; `-maxdepth`/`--max-depth` still disarms the rule. The region
is edited in this overlay itself now: the Claude hook no longer carries it (its rule moved to
`unscoped_search.py` on 2026-10-20), and the same roots go into that analyzer in one reviewed
step.

Its r3 (2026-10-25) judges path words on disk before the text rules: a root by name that names
an existing non-directory (a file, or a link to one) is not broad, so `grep -rn x
~/.subfleet/policy.json` passes; a root followed by a trailing `/*` (or a trailing run of `*` /
`.*`, or a `**/`) is that root, for old roots and new (`rg foo ~/.subfleet/jobs/*`, `rg foo
~/*`); and a glob whose literal folder can hold a root by name (`/`, `/Users`, `/private`, a
home, its `Library` or `.subfleet`) is expanded by bash and judged by what it matches
(`rg foo ~/Pol*` is denied, `grep -rn x ~/.subfleet/*.json` over files and a glob that matches
nothing pass). Every direct child of `~/.subfleet` stays a root.

Its r3b (2026-10-25) judges those path words with the roots, globs and mounts of the Claude
half's snapshot-5. Every path word is resolved, normalized (`//`, `.` and `..` by name, the data
volume folded to `/`, `/private/tmp`, `/private/var` and `/private/etc` to `/tmp`, `/var` and
`/etc`) and matched in any letter case against snapshot-5's roots, which adds `/Volumes` and each
entry in it, `/System`, `/System/Volumes`, `/private`, `/var`, `/var/folders`, `/opt`,
`/opt/homebrew`, `/Library`, `/Applications`, `/usr` and `~/Library`. A written path is judged
where its links lead (`rg x ~/link-to-home` is denied), and nothing under `/Volumes`,
`/Network`, `/net` or `/home` is ever resolved or stat'ed. A glob is judged component by
component, listing only a folder a root can be in (computed from the roots, as snapshot-5's
`holds_root` is), so `rg foo "/V*/Macintosh HD"` is denied, matched in `/Volumes`' listing,
while `rg x /*/*/x` and `rg foo /Volumes/*/x*` read nothing inside a mount.

**Against the Claude half** (`~/.claude/hooks/unscoped_search.py`, session 7f214082's): after
r3b the path words follow its snapshot-5 (roots, normalization, letter case, links, stall
mounts, step-by-step globs and the computed list of folders a glob reads), and its bare-`/*`
rule is r3's decision 2. Its intended limits are this overlay's too: a glob in a folder that
cannot hold a root is not listed, so a link inside it to a root is not followed
(`rg x src/link-u*`); zsh qualifiers (`*(/)`), `<1-9>` and extendedglob forms are not judged;
and a link inside a listed folder that points into a mount that may stall is still resolved.
Known differences that remain: this overlay judges only words spelled from `~`, `$HOME`,
`$SUBFLEET_*` or `/` (relative paths and the cwd rule's list are D-LANE-SCOPE2's), never sees a
searcher right after a quote, judges a quoted glob as if unquoted, and runs r3's trailing-`/*`
rule before the glob (`rg x ~/Pol*/*` is denied here, allowed there). The report's known limits
list the rest.

| File | SHA-256 |
| --- | --- |
| `never-rules-hook.sh` | `f952fd3e9648df1783cec96327b66a84a21082ba63469ef56d41749d23a5131e` |
| `TRUST` | `0d5dac0fc9290eac8cfd1483b7f44cde9bb27e817657dd7fe4dc7b2ab07054d8` |

TRUST is the installed TRUST of 2026-09-29 (`b3193b97…`), changing only `hook_sha256` and
appending four dated provenance additions (D-LANE-SCOPE1's three sentences, r2's one, r3's one
and r3b's one); `hooks_trust_hash` and
`override` hash the hook's identity, not its bytes, and do not move. The staging helper above
refuses to replace a different overlay in place, so this revision is staged into a fresh state
root and swapped in; `tests/unit/test_guard_subfleet_root.py` states and checks the rule's
invariants against the original hook, and `docs/reports/2026-10-24-guard-subfleet-root.md`
holds the install steps, the live checks, the known limits and the rollback, which restores
hook `a60d1c51…` with TRUST `b3193b97…`.

## Verification

Run the full fake-provider suite, build both distributions with `uv build`, and
inspect their members and bytes before installing. Overlay regressions cover
missing and malformed pins, mismatch, executability, explicit/root resolution,
doctor/preflight agreement, same-path cache invalidation, exact idempotent staging,
refusal to overwrite different policy, and preservation of an old launch path.
Private source tests pin both files of the reviewed pair, including TRUST provenance. Package
inspection must show no `private/`, `tests/`, bundled hook, or TRUST member and
no original private hook/TRUST payload. No provider or installed-state changes
are needed for this verification.
