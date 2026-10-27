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

## Policy revision: ~/.subfleet roots (D-LANE-SCOPE1, 2026-10-24; r2, r3 and r3b 2026-10-25; r3c 2026-10-26)

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
and anything deeper stays scoped; a `-maxdepth`/`--max-depth` exempts the search it bounds (since
r3c only that simple command's own, see below). The region
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
where its links lead (`rg x ~/link-to-home` is denied), except under `/Volumes`, `/Network`,
`/net` or `/home`: a written path under them is never resolved or stat'ed; a link leading into
one still is. A glob is judged component by
component, listing only a folder a root can be in (computed from the roots, as snapshot-5's
`holds_root` is), so `rg foo "/V*/Macintosh HD"` is denied, matched in `/Volumes`' listing,
while `rg x /*/*/x` and `rg foo /Volumes/*/x*` read nothing inside a mount.

Its r3c (2026-10-26) closes the walk-arounds of the hub's review r2. A glob that matches every
entry is the folder it sits in: trailing `/`, `/.` and `<glob>/..` are read first, and a trailing
run of components made only of wildcards (`?*`, `[!.]*`, `[0-9]*`, a `/` inside brackets
included) is that folder, listed or not (`rg --files ~/.subfleet/jobs/?*` and `rg --files /tmp/?*`
are denied); any glob directly in a folder of a `.subfleet` is that folder, so r3's allowed
`~/.subfleet/jobs/2026*` is denied now, while `jobs/<id>/a1/*` and `/tmp/sf-*` pass.
`/Volumes/<entry>/<rest>` is judged by name as `/<rest>` (`"/Volumes/Macintosh HD/Users/<me>/.subfleet"`,
an external disk's `Users`); nothing inside a mount entry is listed, and a glob there matches the
names a root can have plus any other name. Comma braces are expanded (64 words at most; a nested
group, a `{x..y}` sequence or more reads each group as `*`) and zsh's `(a|b)` is judged by its
alternatives. A searcher's `..` after a link is resolved where find and rg resolve it (a cd
target's stays logical). Searchers are found by basename (`/usr/bin/find`, `\rg`, `"rg"`,
`$(which rg)`, egrep, fgrep, rgrep, ugrep) and right after a quote. Backslash escapes, `***`,
`~user`, `$'…'` (read as `*`) and the two code points APFS folds to an ASCII letter here (U+017F to
`s`, U+212A to `k`) are read as the shell and the volume read them. A bare `cd` is `cd ~`, a
recursive searcher given no path is judged by the cwd rule (`rg foo` sitting in the home), a
`sh`/`bash`/`zsh`/`dash`/`ksh -c` or `eval` script holding a searcher is judged as its own line to
depth 4 (deeper is denied), and a depth bound exempts only the simple command it is an argument
of (`rg --files ~/.subfleet # --max-depth` is denied). The block message's root list is made from
the root table, so a `/usr` or `/Volumes/<entry>` denial names its root.

Its r4 (2026-10-26) closes the walk-arounds of the hub's review r3. A `..` after a glob is
folded: each `<component>/..` after a word's first glob component goes by name, and the word is
denied when the folded or the unfolded reading names a root (`find ~/.sub*/jobs/<id>/..`,
`rg --files ~/work/r*/../../.subfleet`); in the unfolded reading a candidate a later `..` may climb
out of is kept, so that `..` is applied physically (`rg --files ~/w*/jl/..`, with `jl` a link into
a job folder). The trailing-run rule also runs on the part before a `**/`, so
`rg foo ~/PolicyEngine/*/**/*.py` is `~/PolicyEngine`. Every script a shell or `eval` runs is judged
as its own lines, in that shell's reading, to depth 4: a `-c` script after options that take a value
(`-o pipefail`, `-euo pipefail`, `-O extglob`, `+o …`, `+O …`, `--rcfile x`) or after `--`, under a
quoted shell name; `eval`'s arguments, joined; for a shell given no script, the here-string it reads
or what an `echo`, `print`, `printf` or `cat <<<` pipes into it; and a `$'…'` script decoded as bash
decodes it (`\xHH`, `\NNN`, `\'`). A searcher is found in command position past shell keywords
(`if`, `then`, `else`, `elif`, `while`, `until`, `do`, `!`, `{`), a function body's `{`, assignments,
redirections and the wrappers, each matched by basename with snapshot-5's table of options that take a
value (`sudo -u root`, `timeout -s KILL 60`, `exec -a x`, `env -u X`, `xargs -I {}`, `doas`,
`gtimeout`, `/usr/bin/env`), and with quotes or backslashes inside its name (`r''g`, `r\g`, `"r"g`,
`$'rg'`, `f\ind`). A `${…}` is no brace group, and a word whose brace group is read as `*` and holds
a `/` or a `..` component is denied (`rg --files /{Users/{me,x},y}/.subfleet`); that fails closed on
a rare form. A path word with more than 64 `{` or `[`, more than 256 components, or over 4 KB is
denied without being scanned, and the brace, bracket and folder scans each take one pass. rg given
no path, with a device or a closed stdin (`</dev/null`, `<&-`), searches its cwd and is judged as
`.`; `rg --version`, `-V`, `--help`, `-h`, `--type-list`, `find --version` and grep's `--version`,
`-V` and `--help` walk nothing and pass from any cwd. `~$USER`, `~${USER}` and `~$(whoami)` are the
home, `$"…"` is read as `"…"`, and a searcher's `/Volumes/<entry>/..` is also read with the entry as
`/` (the boot volume's alias is a link to `/`).

**Against the Claude half** (`~/.claude/hooks/unscoped_search.py`, session 7f214082's snapshot-5):
the path words follow its roots, normalization, letter case, links, stall mounts, step-by-step
globs and computed list of folders a glob reads, and the command reading follows its keywords,
wrapper table, shell options, `eval`, stdin scripts, stdin model and print-and-exit forms. Their
shared limits are in the report's known limits. Known differences that remain after r4, read from
the code as it stands:

- **Forms this overlay lets through and snapshot-5 judges.**
  - Relative paths and the cwd: a relative path is never judged (`rg --files ..` from a worktree),
    nor is a `cd` before one (`cd ~/.subfleet/jobs/<id> && rg --files ../..`), and the cwd rule
    knows only its fixed list (`rg --files .` or `rg foo` sitting in `~/.subfleet` or in `/usr`);
    snapshot-5 follows the cwd. These are D-LANE-SCOPE2's.
  - Variables: a searcher named through a variable (`RG=rg; $RG …`), a root held in any variable but
    `$HOME`, `$SUBFLEET_ROOT` and `$SUBFLEET_HOME` (`d=~/.subfleet; rg --files $d`), and a parameter
    form other than `$NAME` and `${NAME}` (`${SUBFLEET_ROOT:-~/.subfleet}`, `${HOME%/}/.subfleet`);
    snapshot-5 tracks assignments and expands these forms.
  - Searchers snapshot-5 knows and this overlay does not: fd, fdfind, bfs, gfind and ggrep.
  - Wrappers that move or run something: `env -C <root> rg …` and `sudo -D <root> rg …` (the cwd
    they set is not followed) and `env -S '<command>'` (its string is not run); snapshot-5 follows
    the first two and runs the third.
  - A quoted for-loop list (`for d in "$HOME/.subfleet"; do find $d …; done`); the unquoted form is
    denied.
  - A `$'…'` inside a path word is read as `*`, which matches no leading dot
    (`rg --files ~/$'.subfleet'/jobs` passes), and a word that starts with one
    (`$'/Users/me/.subfleet'`) is no path word; snapshot-5 decodes both.
  - An unquoted depth-bound spelling anywhere in the searcher's simple command exempts it, also as
    another option's value (`rg -e --max-depth ~`); snapshot-5 reads the options.
  - rg reading its patterns from stdin (`cat p | rg -f -` sitting in the home) counts as fed;
    snapshot-5 counts `-f -` as leaving `.` searched.
- **Places this overlay is stricter than snapshot-5.**
  - A quoted glob is judged as if unquoted (`find . -path '/tmp/*'`).
  - r3's trailing-`/*` rule runs before the glob (`rg x ~/Pol*/*` is `~/PolicyEngine`).
  - A trailing component made only of wildcards is its folder even where that folder is never
    listed (`/tmp/?*`, `~/PolicyEngine/?*`), and so is one before a `**/`
    (`~/PolicyEngine/?*/**/*.py`, `[!.]*/**/`); snapshot-5 reads only a bare `*` so.
  - Any glob directly in a folder of a `.subfleet` is that folder (`jobs/2026*`, `jobs/link-u*`).
  - `/Volumes/<entry>/<rest>` is judged by name as `/<rest>`, a glob inside a mount entry by name,
    and a searcher's `/Volumes/<entry>/..` also as `/`.
  - Every comma group of a word is expanded, a nested group or a `{x..y}` reads as `*`, and a group
    read as `*` that holds a `/` or a `..` component denies the word; snapshot-5's `expand_braces`
    expands the first plain group only and leaves nested and sequence groups as written.
  - A `..` after a glob is folded and judged (`~/.sub*/jobs/<id>/..`, `~/*/policyengine-us/..`);
    snapshot-5 drops a candidate that is no root before its `..` applies.
  - `~$USER`, `~${USER}`, `~$(whoami)` and `$"…"` are judged; `~-` is read as a user named `-`
    (`rg --files ~-/.subfleet` is denied).
  - A word past the scan cap (more than 64 `{` or `[`, more than 256 components, or over 4 KB) is
    denied without being judged.
  - A searcher nested deeper than 4 shells is denied (snapshot-5 follows nesting to its `MAX_NEST`
    of 24).
  - A searcher's plain name is found anywhere in the command's text, not only as a command word
    (`echo find / -name x` is denied), and any word naming a shell, from the command word on, starts
    a script reading (`echo bash -c 'rg --files ~/.subfleet'` is denied); a name written with a
    path, a quote or a backslash is read only in command position, as snapshot-5 reads every name.
  - ugrep's `-NUM` is no depth bound here (`ugrep -r -3 x ~/` is denied).

| File | SHA-256 |
| --- | --- |
| `never-rules-hook.sh` | `d44867ea69410296dd1c90e9153491cf46c796d7b31c5d72b62c1661b2d0575f` |
| `TRUST` | `9a3ce01239a121594a33fddb57c66c99a1acbbe755428e11da567a99ab618f58` |

TRUST is the installed TRUST of 2026-09-29 (`b3193b97…`), changing only `hook_sha256` and
appending four dated provenance additions (D-LANE-SCOPE1's three sentences, r2's one, r3's one
and r3b's one, whose mount clause r3c corrected); `hooks_trust_hash` and
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
