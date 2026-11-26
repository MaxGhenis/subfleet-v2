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

## Policy revision: ~/.subfleet roots (D-LANE-SCOPE1, 2026-10-24; r2, r3 and r3b 2026-10-25; r3c and r4 2026-10-26; r5 2026-10-27; r6 and r7 2026-10-28; r8 2026-10-29)

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

Its r5 (2026-10-27) closes the walk-arounds of the hub's review r4. A long word with no slash costs
what a short one does (awk's `base()` is one split, and the command prep reads runs of plain
characters at once). A heredoc's body (`<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`, several on a
line in order) is data: the prep leaves it out of its quote and separator tracking, so an
apostrophe in it hides nothing after it, and its text is read by the text rules as written; a
heredoc that feeds a shell given no script (`bash <<EOF`, `sh -s <<'EOF'`) is that shell's script,
and one fed to `cat` is what cat writes to a pipe. A depth bound covers only its own command's walk:
a bounded `find X -maxdepth N … | xargs <searcher>` is judged with X as the consumer's roots, and the
command `-exec`, `-execdir`, `-ok` or `-okdir` runs is judged on its own with `{}` as find's roots
(`find . -maxdepth 1 -exec rg foo ~ \;` is denied). A root right before a closing backtick ends
there, and a command substitution inside double quotes is judged on its own
(`echo "$(rg --files ~/.subfleet)"`). A glob component bash 3.2 can match against `..` (`.*`, `.?`,
`.[.]`, `.[!a]`) is read as its folder and as that folder's parent in every context, and more than
three such components in one word are denied. No step through the data volume's alias into a mount
that may stall is stat'ed (`/System/Volumes/Data/Volumes/<entry>/x/..`). A command over 64 KB that
names a searcher is denied as too long to judge. `~`, `~name`, `$HOME` and `$SUBFLEET_*` right
before a brace group are path words (`~{,/.subfleet}`); `~` followed by `$`, `${`, `$(` or a
backtick is the home (`~$LOGNAME`, `~$(id -un)`); a glob word climbing out of a `/Volumes` entry
with `..` is also read from `/`. zsh's `=rg` and `=find` are the searcher; a pipe carries what every
earlier stage writes into a shell given no script (`… | cat | bash`, `… | tee /dev/null | bash`,
printf's format with its arguments in order); a `-c` script that only runs its arguments (`"$@"`,
`$@`, `"$1"`, `$*`) runs the words after its name, and under xargs what xargs reads; what a command
substitution in `eval`'s arguments writes is a script; and `<&0` leaves rg's stdin as it was.

Its r6 (2026-10-28) closes the findings of the hub's review r5. Under a heredoc delimiter word with
no quote and no backslash (`<<EOF`, `<<-EOF`) the shell runs the body's command substitutions, so each
`$(…)` and backtick span in the body is judged as a command; a quoted or escaped delimiter keeps the
whole body data. xargs running `sh`, `bash`, `zsh` or `dash` with a `-c` script that names a searcher
puts the whole command under the xargs rule, a bounded producer's roots and a root right before the
xargs included. Any producer's heredoc or here-string piped into a shell given no script is its script
(`tee <<'EOF' | bash`). A `<<` inside `$((…))` or `((…))` is a shift, and a `<<` whose body never ends
disables only itself. A `$(` inside double quotes ends where a heredoc-aware `parend` says, so the
commit idiom `"$(cat <<'EOF' … EOF)"` with a lone `"` in its body hides nothing after it. zsh's `=rg`
and `=find` right after a backtick reach the rule. A command that names a searcher and holds more
than 4,096 of one of `$`, `#` or `\` is denied as too complex to judge, and so is one whose `$(…)`,
`$((…))`, `<(…)` and heredoc forms take the prep past its shared budget of 20,000 steps. What a
`<(…)` writes is the script of the shell or `source` that reads it, and a `for` loop variable (its
words) or a piped `while read` variable (the producer's roots) that is a searcher's whole operand is
read as what it stands for.

Its r7 (2026-10-28) closes the findings of the hub's review r6. A `<<` inside `$((…))`, `((…))`, `$[…]`
or `let '…'` is a shift even when the span crosses lines, in `parend` as in the prep, while a `$(` or
a backtick inside it is still a command whose heredoc is read; a `((` is arithmetic when its inner `(`
closes right before its outer one, and since r8 (decision 3) a `$((` is also arithmetic whenever its span
ends in `))`, as bash reads `$((1) + (1<<X…))`. A heredoc body is read a line at a time with
no copy of the rest of the command, and each line read, by `parend` and by the prep, is a step of the
shared 20,000-step budget. What a `<(…)` writes, the `cat`, `tee`, `sort` or other filter it is a word
of passes down the pipe after it, into a shell given no script (`cat <(echo 'rg --files ~/.subfleet')
| bash`). A plain assignment after a loop's `done` that lasts (at the top of the command, with no loop,
`if`, `case` or brace group open, after `;`, a newline, `&` or nothing and before `;`, a newline, `&&`
or `||`) ends the loop variable's reading, and a literal value is read in its place: `for d in
~/.subfleet/*; do …; done; d=src; rg foo "$d"` passes, and `…; d=~/.subfleet; rg foo "$d"` is denied.
(r8 narrows where that reset applies and what replaces the words, below.)

Its r8 (2026-10-29) closes the findings of the hub's review r7. A loop variable keeps its root wherever
the shells keep it (decision 1): a newline right after `&&`, `||`, `|` or `|&` continues the line, so
the operator still decides whether an assignment after it lasts, and a pipe carries on to the command
on the next line (`echo 'rg --files ~/.subfleet' |⏎bash` is a pipe into bash); `time` or `time -p`
before a compound command is a keyword, so the command it times is counted as open, and zsh's `repeat`
and `foreach` open one too; a command that defines a function (a `name()` or the word `function`
anywhere in it) never ends a loop variable's reading, since a function body runs out of textual order;
and only a plain word (no `$`, backtick, quote, backslash or blank) replaces the loop's words, while any
other value joins them, so `…; d='src /'; rg foo $d` keeps the loop's root and `for d in src; do :;
done; d='/'; rg foo "$d"` reads `/`. A command that names a searcher and holds more than 4,096 of `'` or
of a backtick is too complex to judge, as for `$`, `#` and `\` (decision 2). A `$((` whose span ends in
`))` is arithmetic (decision 3). What a filter passes on from its `<(…)` is also the roots a piped
`while read` reads (decision 4: `cat <(find ~/.subfleet -maxdepth 1) | while read d; do rg foo "$d";
done` is denied).

Its r9 (2026-10-29) closes the findings of the hub's review r8. Wherever a plain `name=value` at the
top of the command does not end a loop variable's reading (a compound command open, a keyword before
it, an assignment that may not last, a command that defines a function, or after zsh's `repeat` or
`foreach`), its value now joins the loop's words, as a value that is not a plain word already did
(decision 1; r10 carries the join into substitutions and nested scripts, below). r8 dropped the value
there, so after a scoped loop it let through a root assigned in a command holding `()` or the word
`function`, after a `repeat` or a `foreach`, inside an `if` or a brace group, after `&&`, or in the
loop's own body: `for d in src; do :; done; d=~/.subfleet; rg foo "$d"; echo '()'` and `for d in src;
do d=/; done; rg foo "$d"` are now denied, and both shells walk the root. A command that names a
searcher and holds more than 4,096 `"` is too complex to judge too (decision 2).

Its r10 (2026-10-30) closes the findings of the hub's review r9. The value joins inside a substitution
and a nested script too (decision 1): r9 read a loop there but dropped a later assignment, so after a
scoped loop it let through a root assigned in a quoted `$(…)`, a `bash -c` or `eval` script or a
heredoc script, and `eval d=/` itself: `echo "$(for d in src; do :; done; d=/; rg foo "$d")"` and
`for d in src; do :; done; eval d=/; rg foo "$d"` are now denied, and both shells walk `/`. An
assignment inside a quoted `$(…)` that names no searcher is still not read (`for d in src; do :;
done; echo "$(d=/)"; rg foo "$d"` passes, and no shell walks a root). In a command that defines a
function, a value assigned where the reset does not apply, to a name no loop has read yet, joins the
words of a later loop over that name (decision 2), since a function body written before the loop may
run after it: `f() { d=/; }; for d in src; do :; done; f; rg foo "$d"` is denied, and both shells walk
`/`, while `f() { d=lib; }; …; f; rg foo "$d"` and `d=/; f() { :; }; …; rg foo "$d"` pass. The
over-blocks of both are listed under "stricter" below.

Its r11 (2026-10-30) closes the findings of the hub's review r10. Each word a searcher's operand is
read as through a loop variable is a step of the shared 20,000-step budget, and so is each 16 bytes of
them (decision 1): r10 counted none of them, so a 7.9 KB command whose loop variable stood for 420
values in each of 420 searches ran past the hook's 60 s timeout, and a count of words alone would let
one 4,000-byte word read 2,000 times cost 25 s. Such a command is now too complex to judge, and the
message names loop variables. The path-word judgment judges each distinct word once, however many
places hold it. A function body's value joins a piped `while read` loop's words too (decision 2), so
`f() { d=/; }; echo src | while read d; do :; done; f; rg foo "$d"` is denied, and both shells walk
`/`. A `for` loop read where the reset would not apply (a keyword before it, another compound command
open, not where an assignment lasts, or a command that defines a function), and every `read`, joins the
words an earlier loop over its name left instead of replacing them (decision 3), since the loop may run
in a scope of its own: after `for d in ~/.subfleet/*; do :; done`, a loop over `src` in a `bash -c`
script, a `$(…)`, a `<(…)`, a function body, a subshell, a quoted `$(…)` or a piped `while read` no
longer hides the root from the `rg foo "$d"` after it, and the shells walk it. A `for` loop at the top of
the command still replaces the words; the top is where a plain assignment would last (r7 decision 5: outside
a subshell, a substitution and a nested script, before any `case` pattern, and after `;`, a newline,
`&` or nothing), with no other compound command open, no keyword before the loop and no function
defined in the command. Anywhere else, at the top of a nested script or substitution included, it joins
them. The over-blocks of all three are listed under "stricter" below, and what stays open under "let
through".

Its r12 (2026-10-30) closes the findings of the hub's review r11. What find's `-exec` `{}` stands for
costs steps of the shared 20,000-step budget as a loop variable's words do, a step for each word and
for each 16 bytes (decision 1): r11 charged nothing for it, so 300 roots and 300 `{}` (2.3 KB) cost
26 s of CPU and 400 of each ran past 120 s, and both are now too complex to judge in 0.10–0.20 s;
100 of each are still read. The same charge now applies to each round of a `printf` format after the
first, to the script `eval` or `source` runs, and to the script a shell runs from a pipe, a
substitution or its arguments (decision 2). Before it, a 16 KB format repeated for 64 words and piped
into bash, and 8 KB echoed through 1,000 shells, each ran past 40 s (the hub's runs at `82df89ded`);
both are now too complex to judge in under 1 s. A shell's own here-string or heredoc is text of the
command itself, read once already (a heredoc's lines cost steps), so it is not charged again. The
message names find's `{}`, printf's repeated format and text piped into a shell. This round looked for
no other place that turns one word into many.

Its r12d (2026-10-31) charges the one place r12 named and left uncharged: what xargs reads, when the
shell xargs runs has `-c` and no script, or a `-c` script that only runs its arguments, such as `bash
-c '"$@"' _`. Before it, the hook took 51 s to read 8 KB echoed through 1,000 `| xargs -0 bash -c`
stages and ran past 75 s on 32 KB through 1,500, where its timeout is 60 s (the hub's runs at
`307e2373c`); it now denies those rows, and 1 KB through 100 such stages, as too complex to judge in
under 1 s of CPU (0.37–0.79 s across r12d's and review r12's runs). It still reads 1 KB through 10,
and still allows `echo 'rg -n foo src' | xargs -0 bash -c`. The message's "piped into a shell"
covers this text. The hub read the seven places in `simple()` and `execs` where the prep reads a
script, and review r12 read the other four, in `spans()` and `prep()`. Each one reads the command's
own text, already spends, or reads what a substitution's commands write (`subout`). `subout` reads
the substitution with no script door open, so the substitution's own text and words that already
spend (printf's later rounds and the words a loop variable stands for) bound what it writes. At the
hook's `:2168` the quote reader has already put a loop variable's words into a shell's `-c` word,
and they spent there (`:2038`).

**Against the Claude half** (`~/.claude/hooks/unscoped_search.py`, session 7f214082's snapshot-5):
the path words follow its roots, normalization, letter case, links, stall mounts, step-by-step
globs and computed list of folders a glob reads, and the command reading follows its keywords,
wrapper table, shell options, `eval`, stdin scripts, stdin model and print-and-exit forms. Their
shared limits are in the report's known limits. Known differences that remain after r12d, read from
the code as it stands, with review r5's check 8 forms marked (snapshot-5 read from its frozen
`.ref`: `for` at `:2179-2187`, `read` at `:2192-2197`, shells at `:2227-2252`):

- **Forms this overlay lets through and snapshot-5 judges.**
  - Relative paths and the cwd: a relative path is never judged (`rg --files ..` from a worktree),
    nor is a `cd` before one (`cd ~/.subfleet/jobs/<id> && rg --files ../..`), and the cwd rule
    knows only its fixed list (`rg --files .` or `rg foo` sitting in `~/.subfleet` or in `/usr`);
    snapshot-5 follows the cwd. These are D-LANE-SCOPE2's.
  - Variables: a searcher named through a variable (`RG=rg; $RG …`), a root held in any variable but
    `$HOME`, `$SUBFLEET_ROOT`, `$SUBFLEET_HOME` and a loop variable read as below (its loop's words,
    or a plain word assigned to it after its loop, r7 decision 5, any other value joining the loop's
    words, r8 decision 1, and any value assigned where that reset does not apply joining them too, r9
    decision 1, inside a substitution or a nested script too, r10 decision 1, and from a function body
    written before the loop, r10 decision 2, for a piped `while read` loop too, r11 decision 2, with an
    inner-scope loop joining an earlier loop's words, r11 decision 3) (`d=~/.subfleet; rg --files $d`),
    and so a `read` after a
    plain assignment (`for d in ~/.subfleet/*; do :; done; d=src; read d <<< ~/.subfleet; rg foo "$d"`:
    `d=src` ends the loop's reading, and a `read` from a here-string assigns nothing the prep reads,
    though both shells walk the root), an assignment by `+=` or by a builtin (`export`, `declare`,
    `typeset`, `readonly`, `local`, `eval`, `printf -v`, `read`), and a value a command substitution
    writes (after `for d in src; do :; done`, each of `d=; d+=/`, `export d=/`, `printf -v d /`, `read d
    <<< /`, `d=/ eval 'rg foo "$d"'` and `d=$(echo /)` before `rg foo "$d"` passes, and both shells walk
    the root: the prep reads only a plain `name=value`, with its value as written; of `eval`, a prefix
    assignment, a builtin it runs, or a `+=` or substitution value it assigns passes, since r10 decision
    1 reads `eval d=/` as a nested script: `eval 'd=$(echo /)'`, ``eval 'd=`echo /`'`` and `d=; eval
    'd+=/'` pass too, and both shells walk the root, `R11_KNOWN_OPEN`; modeling the builtins is a
    follow-up, `R10_KNOWN_OPEN`), and a parameter form other than
    `$NAME` and `${NAME}`
    (`${SUBFLEET_ROOT:-~/.subfleet}`, `${HOME%/}/.subfleet`); snapshot-5 tracks assignments and
    expands these forms.
  - A `for` loop variable inside a word or after a `cd` (`for d in ~/.subfleet/*; do rg foo
    "$d/x"; done`, `…; do cd "$d" && rg foo; done`), and zsh's `for d (…)`; snapshot-5 expands the
    variable wherever it is used. (Its whole-operand form is judged, r6 decision 9.)
  - Searchers snapshot-5 knows and this overlay does not: fd, fdfind, bfs, gfind and ggrep.
  - Wrappers that move or run something: `env -C <root> rg …` and `sudo -D <root> rg …` (the cwd
    they set is not followed) and `env -S '<command>'` (its string is not run); snapshot-5 follows
    the first two and runs the third.
  - A quoted for-loop list feeding find through a variable inside a word (`for d in
    "$HOME/.subfleet"; do find $d/x …; done`); the whole-operand form is judged (r6 decision 9).
  - A `$'…'` inside a path word is read as `*`, which matches no leading dot
    (`rg --files ~/$'.subfleet'/jobs` passes), and a word that starts with one
    (`$'/Users/me/.subfleet'`) is no path word; snapshot-5 decodes both.
  - An unquoted depth-bound spelling anywhere in the searcher's simple command exempts it, also as
    another option's value (`rg -e --max-depth ~`); snapshot-5 reads the options.
  - rg reading its patterns from stdin (`cat p | rg -f -` sitting in the home) counts as fed;
    snapshot-5 counts `-f -` as leaving `.` searched.
  - A heredoc body read by something other than a shell given no script is data for the prep:
    `source /dev/stdin <<EOF`, `. /dev/stdin`, or a `while read` loop that runs its lines, gets no
    prep reading of its body (its text is still read by the text rules).
  - What a command substitution in `eval`'s arguments writes is modeled for `echo`, `print`,
    `printf` and `cat`; any other command contributes only its words (`eval "$(sed … file)"`).
  - *Fixed at this head:* the substitutions of an unquoted heredoc body (review r5's P2-1, r6
    decision 1): snapshot-5 runs them (`read_heredocs` and `expansions`), and so does this overlay now.
  - *Fixed at this head:* a root assigned after a scoped loop where the reset does not apply (review
    r8's P3-1, r9 decision 1). r8 dropped such a value and kept only the loop's words, so it let
    through `for d in src; do :; done; d=~/.subfleet; rg foo "$d"` beside a `()` or the word
    `function` anywhere in the command, after a zsh `repeat` or `foreach`, inside an `if`, a brace
    group or a timed group, after `&&`, or in the loop's own body (`for d in src; do d=/; done; rg foo
    "$d"`), all of which the shells walk. The value now joins the loop's words.
  - *Fixed at this head:* a root assigned after a scoped loop inside a quoted `$(…)`, a `bash -c` or
    `eval` script or a heredoc script, and one a function body assigns before its loop (review r9's
    P3-1 and P3-2, r10 decisions 1 and 2). r9 dropped such a value, so it let through `bash -c 'for d
    in src; do :; done; d=/; rg foo "$d"'` and `f() { d=/; }; for d in src; do :; done; f; rg foo
    "$d"`, both of which the shells walk. The value now joins the loop's words.
  - *Fixed at this head:* a loop in an inner scope over an earlier root loop's name, and a function
    body's value before a piped `while read` loop (review r10's P3-4 and P3-1, r11 decisions 3 and 2).
    r10 replaced the loop's words there, so it let through `for d in ~/.subfleet/*; do :; done; (for d
    in src; do :; done); rg foo "$d"` and `f() { d=/; }; echo src | while read d; do :; done; f; rg foo
    "$d"`, both of which the shells walk. The words now join.
- **Forms both let through, or not checked against snapshot-5.**
  - A loop variable searched in a function body written before its loop (review r10's P3-5, r11
    decision 4): `f() { rg foo "$d"; }; for d in ~/.subfleet/*; do f; done` and `f() { rg foo "$d"; };
    for d in src; do :; done; d=/; f` pass, and both shells walk the root. The prep reads in textual
    order, so it reads the search before the loop or the value; modeling it needs a second reading of
    a command that defines a function, a follow-up (`R11_KNOWN_OPEN`).
  - A loop a pipe or `&` runs in a subshell, over an earlier root loop's name (found in r11): `for d in
    ~/.subfleet/*; do :; done; for d in src; do :; done | cat; rg foo "$d"`, and the same with `& wait`
    for `| cat`, pass, and both shells walk the root. The loop sits at the top of the command, where its
    reading replaces the words (`R11_KNOWN_OPEN`). Review r11 found more spellings of it, each passing:
    `|& cat` and `&| cat`, which only zsh walks, and `&!`, `2>&1 | cat`, `& rg foo "$d"` (the loop run in
    the background before the search), a pipe continued on a new line and `| while read x; do :; done`,
    which both shells walk (all but `&|` pinned in `REVIEW_R11_OPEN`). None is modeled.
  - A loop whose list expands to nothing, after a root loop over its name (review r11; since r6): `for d
    in ~/.subfleet/*; do :; done; for d in $nothing; do :; done; rg foo "$d"` passes, and both shells
    walk the root, since a loop over nothing leaves the earlier value. The prep reads the second loop's
    words as written (`$nothing`) and replaces the root's with them (`REVIEW_R11_OPEN`). The same after
    `for d in $(true)` is denied, and both shells walk it (`REVIEW_R11_DENIED`).
  - A redirection, a heredoc operator or a `$((…))` between a searcher and its root (review r11):
    `find 2>&1 / -name x`, `rg --files 2>/dev/null ~/.subfleet`, `rg --files &>/dev/null ~/.subfleet`,
    `rg --files <<EOF ~/.subfleet⏎x⏎EOF` and `rg --files $((1<<2)) ~/.subfleet` pass, and both shells
    walk the root; the installed hook lets them through too (`REVIEW_R11_OPEN`).
  - A raw 0x01 byte in the text a pipe gives a shell (review r12; older than r12): `echo '<0x01>; rg
    --files ~/.subfleet' | bash`, the same through `| xargs -0 bash -c`, and `printf '<0x01>; %s\n'
    'rg --files ~/.subfleet' | bash` pass, and both shells walk the root. The same byte in `eval '…'`,
    in `bash -c '…'` and at the top of the command is denied. No command pinned in the test file
    holds this byte.
  - Loop shapes other than a `for` list or a piped `while read` whose variable is a searcher's whole
    operand: `while read d; do …; done < list`, `until`, `select`, a loop variable in a nested
    script, `for d in $(…)` (the prep splits the word at its `(`), and `xargs -a <(…)` or a `>(…)`;
    unjudged here, and snapshot-5's `read` variable is unknown (`:2192-2197`). Two of them by name,
    both walked by the real shells (r7 decision 4, `test_r7_known_differences_name_the_open_forms`):
    - the common bash form of a fed loop, `while read d; do rg foo "$d"; done < <(find ~/.subfleet
      -maxdepth 1)`: the prep reads left to right, and the `< <(…)` that feeds the `read` comes
      after the loop's `done`, so `$d` stands for nothing when `rg` is read;
    - nested loops, `find ~/.subfleet -maxdepth 1 | while read d; do for f in "$d"/*; do rg foo
      "$f"; done; done`: the inner `for` list is taken as written (`"$d"/*`), not as the outer
      loop's roots.
  - Productions a pipe joins, which both shells walk (r8 decision 6). `cat <(printf 'rg --files ')
    <(echo ~/.subfleet) | bash` and `paste -d' ' <(echo 'rg --files') <(echo ~/.subfleet) | bash`: the
    prep joins what the two `<(…)` write with a line break, and each line is judged on its own, so the
    searcher and its root are never read together. A pipe after a brace group or a subshell, `{ printf
    'rg --files '; echo ~/.subfleet; } | bash`, and so `{ echo 'rg --files ~/.subfleet'; } | bash` and
    `(echo 'rg --files ~/.subfleet') | bash` (found beside review r7's three): a pipe carries what the
    simple command right before it writes, and right before it stands only the group's closing `}` or
    `)`, so it carries nothing the group's commands write. Open at this head
    (`test_r8_known_differences_name_the_stricter_and_open_forms`).
  - zsh's `=(…)`, `source =(echo 'rg --files ~/.subfleet')` and `zsh =(…)`: zsh writes what the
    substitution prints to a temporary file and passes its name, and no script reading follows a
    file name (only a `<(…)` read by a shell, `source` or `.` is a script here, r6 decision 9);
    snapshot-5 runs no script from a shell's or `source`'s file argument either (`:2245-2252`). Open
    at this head (r7 decision 4).
  - *Fixed at this head:* the filter-heredoc pipe (`tee <<'EOF' | bash`, review r5's P3-1, decision 3;
    snapshot-5 models only echo, printf and cat there, so this overlay is now the stricter);
    arithmetic `<<` (P3-2, decision 4; across lines since r7 decision 1, review r6's P3-1 and P3-2); the lone `"` in `"$(cat <<'EOF' … EOF)"` (P3-3, decision 5);
    `` `=cmd` `` (P3-4, decision 6); xargs into a shell `-c` script (P2-2, decision 2); `<(…)` scripts
    and `for`/piped `while read` loops feeding a searcher (P3-7, decision 9: snapshot-5 runs no
    script from a shell given a `<(…)` file, `:2247-2252`, and leaves a `read` variable unknown, so
    those two readings are the stricter here; its `for` reading is shared).
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
  - A heredoc body no shell runs is still read by the text rules, as the installed hook reads it
    (`cat > notes.md <<'EOF'⏎find / -name x⏎EOF` is denied); snapshot-5 reads it as data.
  - A searcher in a markdown code span in such a body is denied, its root ending at the closing
    backtick (review r5's P3-5; r6 decision 7 keeps it as an intended over-block:
    ``cat > notes.md <<'EOF'⏎Run `cd repo && rg --files ~/.subfleet` to see⏎EOF``). Review r5's
    suggested rescoping, dropping the backticks, does not rescope it (the text rules still read
    `rg --files ~/.subfleet` in the body), nor does quoting the path; naming the root outside the
    searcher's span does (`` Run `rg --files` in `~/.subfleet` ``), as does naming a scoped path.
  - A command over 64 KB that names a searcher anywhere, prose included, is denied unread, and so
    is one that holds more than 4,096 of one of `$`, `#` or `\` (r6 decision 8) or of `'` or a backtick
    (r8 decision 2: a large heredoc that names a searcher and carries more than 4,096 quotes or
    backticks, such as a file of prose with its apostrophes or code spans, is denied) or of `"` (r9
    decision 2: so is a heredoc that names a searcher and carries more than 4,096 double quotes, such as
    a file of prose with its quotations or a JSON document), or whose `$(…)`,
    `$((…))`, `((…))`, `<(…)` and heredoc forms take the prep past its 20,000-step budget (r6). Since
    r7 decision 2 each heredoc body line read is a step: a body inside `"$(…)"` is read three times, so
    6,661 lines are past the budget, and one at the top of the command once, so 20,000 lines are. Since
    r11 decision 1 each word a searcher's operand is read as through a loop variable is a step, and so
    is each 16 bytes of them, so a `bash <<'EOF2'` script with `for d in src; do :; done` and 162 lines
    of `d=src/mN; rg -n foo "$d"` is too complex to judge, though no shell walks a root (each value
    joins the loop's words in a nested script, so each search reads all of them; 161 lines are read;
    `R11_STRICTER`). Since r12 decisions 1 and 2 the same charge, a step for each word and each 16
    bytes, applies to what find's `{}` stands for, to each round of a `printf` format after the first,
    to the script `eval` or `source` runs, and to the script a shell runs from a pipe, a substitution or
    its arguments, and since r12d to what xargs reads when a shell under xargs runs it; r12 looked for no
    other place that turns one word into many, and r12d charged the one it named. So `cat <<'EOF' |
    bash` with 4,000 lines of `rg -n foo src/x` (64 KB) is too complex to judge, though no shell walks a
    root, because the pipe's text costs steps on top of the heredoc's lines (2,500 lines, 40 KB, are
    read, and so is `bash <<'EOF'` with the same 4,000 lines; `R12_PIPED_MOVED`). Under the budget, cost
    still grows with the line: a 64 KB command of short words, distinct path words or globs costs
    2.5–11 s of CPU (installed 0.3 s; review r11).
  - A component bash can match against `..` is read as its parent in zsh's reading too, and a word
    with more than three such components is denied unread.
  - A pipe into a shell given no script reads every earlier stage's words as its script, also for a
    filter whose output differs from its words (and a filter's heredoc, here-string or `<(…)`; the
    `<(…)` since r7 decision 3: review r6's P3-4 found r6's pass-on never reached a pipe), and a `{}`
    in what find's `-exec` runs stands for all of find's roots.
  - xargs running a shell whose `-c` script names a searcher is judged by the xargs rule: a root
    anywhere in the command denies, also one the script never reads (r6 decision 2).
  - A loop variable that is a searcher's whole operand stands for all its words or roots at once,
    and a piped `while read` takes any producer's words as roots, a filter's `<(…)` pass-on included
    (r8 decision 4) (`ls ~/.subfleet | while read d; do rg foo "$d"; done` is denied, though ls writes
    names relative to `~/.subfleet`).
  - What any filter passes on from its `<(…)` goes down the pipe (r7 decision 3), tee's file operands
    included, so `tee <(echo 'rg --files ~/.subfleet') | bash` is denied though neither shell walks a
    root: tee writes into the substitution, and nothing reaches bash (r8 decision 5).
  - An unterminated heredoc leaves the rest read as commands; the shells read it all as the body.
  - A loop variable's reading ends only at an assignment that lasts (r7 decision 5): one inside a
    compound command (`if`, a brace group) or a nested script, one after a `case` pattern written
    without its optional leading `(` anywhere earlier in the command (the prep reads its `)` as a
    close), or one whose value is not a plain word (r8 decision 1: a value with `$`, a backtick, a
    quote, a backslash or a blank joins the loop's words) leaves the loop's words in place, so `for d in
    ~/.subfleet/*; do :; done; { d=src; }; rg foo "$d"` is denied though no shell walks a root
    (`R7_LOOPVAR_STRICTER`). Since r8 decision 1 no assignment ends the reading in a command that holds a
    `name()` (any empty `()`, `$()` included) or the word `function` anywhere, prose included, or after a
    zsh `repeat` without `do … done` or a `foreach`, which the prep counts as open to the end of the
    command. Since r9 decision 1 the value of every such assignment joins the loop's words, inside a
    substitution or a nested script too since r10 decision 1, so a root assigned where the shells never
    run the assignment, run it only in a subshell, a substitution or a nested script, or replace it later
    in the same command is denied too: `for d in src; do :; done; false && d=/; rg foo "$d"` walks no
    root in either shell (`R9_STRICTER`), and neither do review r9's five (`REVIEW_R9_STRICTER`, with
    `…` for `for d in src; do :; done`): `…; (d=/); rg foo "$d"`, `…; ( d=/ ) && rg foo "$d"`, `…; if
    true; then d=/ d=src; fi; rg foo "$d"`, `f() { :; }; for d in src; do :; done; d=/; d=src; rg foo
    "$d"` and `…; f() { d=/; }; rg foo "$d"`, nor r10 decision 1's `for d in src; do :; done; bash -c
    'd=/'; rg foo "$d"` and `for d in src; do :; done; echo "$(d=/; rg foo src)"; rg foo "$d"`
    (`R10_STRICTER`). Since r10 decision 2, in a command that defines a function, a value assigned where
    the reset does not apply joins the words of a later loop over its name even when it comes before
    that loop, so beside the never-run assignment a function body never called is denied, `f() { d=/; };
    for d in src; do :; done; rg foo "$d"`, and so is a root the loop replaces, `f() { :; }; if true;
    then d=/; fi; for d in src; do :; done; rg foo "$d"`; neither shell walks a root on either
    (`R10_STRICTER`). Since r11 decision 2 that holds for a piped `while read` loop too, so `f() { d=/;
    }; echo src | while read d; do f; done; rg foo "$d"` is denied, though bash runs the loop in a
    subshell and zsh's `read` replaces the value at the end of its input. Since r11 decision 3 a loop read
    where the reset would not apply, and every `read`, joins the words an earlier loop over its name
    left, so a search in an inner scope that reuses a root loop's name is denied, though it searches only
    the inner loop's words: `for d in ~/.subfleet/*; do echo "$d"; done; ls src | while read d; do rg
    foo "$d"; done`, and the same with the inner loop in a `bash -c` script, a subshell, a `$(…)`, a
    quoted `$(…)`, a function body or a `<(…)` (review r10 counted five such; r11's probes found
    seven). So is a search after a loop the shells run in place where the prep reads it as joining:
    inside an `if`, a `case` arm or a brace group, after `!`, `time`, `&&` or `||`, in an `eval` script,
    nested in another loop, in a command that defines a function, or at the top of a nested script or
    substitution after a root loop over the same name there (`for d in ~/.subfleet/*; do :; done; if
    true; then for d in src; do :; done; fi; rg foo "$d"`, and review r11's thirteen, among them `bash -c
    'for d in ~/*; do du -sh "$d"; done; for d in src tests; do rg -n foo "$d"; done'`). Neither shell
    walks a root on any (`R11_STRICTER`, `REVIEW_R11_STRICTER`).

### Relative roots: the embedded parser (D-SEARCH-REL1, 2026-11-05)

The regex judgment above reads the command's words. It resolves an absolute root, and `.` when
the cwd itself is a broad root, but it never resolved a relative root against the cwd: from a job
worktree (`~/.subfleet/worktrees/<job>`), `find ..` walks every job worktree, and the rule let it
run. On 11/04 one lane's `find ..` did that for 38 minutes. The Claude session's hook has no such
gap: it calls `unscoped_search.py`, a parser that splits the command as the shell does, follows
`cd` and resolves every root against the cwd.

The hook now carries that parser byte for byte (SHA-256
`0ec2f7aa2cbdc1718e1424c18c8f46dac2bdafdc70faa16c9e03c7d079fdb207`, the hub's copy read on
2026-11-05) as the body of a quoted heredoc in the region's `unscoped-search: parser` block, so
TRUST's `hook_sha256` covers the parser's bytes and nothing new is staged. What judges the rule
now:

- **The regex judgment** runs first, as before. Every block it makes stays, with its message.
- **The parser** runs next, when the rule's prefilter (`pw_named`) found a searcher in the command
  and the regex judgment did not block. A hit (searcher, root as written, root resolved) becomes
  the rule's block message, which names the root as written and the root resolved:
  `Matched broad root: '.. (/Users/<user>/.subfleet/worktrees)'`. So the hook blocks when either
  judgment blocks.
- **The interpreter** is the session hook's: `/opt/homebrew/bin/python3` when it is executable,
  else `/usr/bin/python3`, isolated (`-I -S`), with the command on stdin and the cwd, `$HOME` and
  `$SHELL` as its arguments.
- **The fallback:** with no interpreter, a nonzero exit, or no answer within 5 s (the loader arms
  an alarm whose signal ends the parser), the regex judgment decides alone, as it did before this
  revision.

The parser is not run as `python3 -I -S /dev/fd/3`. Apple's Python 3.9 (`/usr/bin/python3`) run
that way, with descriptor 3 a file (bash 3.2 writes a heredoc to a temporary file), ran nothing and
exited 0, so every command would have passed in silence: on macOS an open of `/dev/fd/3` shares the
descriptor's offset, and Python 3.9's zip-archive probe of the path leaves it at the end of the
file (observed 2026-11-05; Homebrew's Python 3.14 is not affected). A loader given to `-c` reads
descriptor 3 itself, arms the alarm and runs the bytes as `__main__`.

The parser starts Python only for a command that names a searcher, and only when the regex
judgment let it pass. Bash parses the heredoc, under 1 ms of CPU, for every command that reaches
the region; a command the prefilter turns away never reaches it. One test seam, which the daemon's
launch environment never carries to a lane: `CODEX_GUARD_TEST_PYTHON` names the interpreter
instead (a path that is not executable is no interpreter). `tests/unit/test_guard_search_rel.py`
holds the hub's 17 rows, the corpus comparison, the fallback, the alarm, the drift check against
`~/.claude/hooks/unscoped_search.py` (skipped where that file is absent) and the cost;
`docs/reports/2026-11-05-search-rel-parser.md` has the measurements. A change to the session
parser comes here in one reviewed step, between the heredoc's lines, with `hook_sha256` moved.
The rollback is the previous pair: hook `e869dd4cac5402112c3a135531de861c84089290ee31779f52145474f678dfed` with TRUST
`36267abbee6e414805defd0672138f0be9b8dd2fd8675f739c77df90332bafc0`. Once D-CODEX-0169 (below) is installed,
the previous pair becomes hook `e869dd4c…` with TRUST `0224cb3f…`. TRUST `36267abb…` pins codex-cli 0.159.0,
so preflight refuses it beside a 0.169.1 CLI.

### The guard sync (2026-11-11) and its r2 (2026-11-14)

The guard sync (2026-11-11, the hub's instruction audit) changed one sentence outside the
unscoped-search region: the `[keychain-dump]` block's last sentence now reads "Read one secret
with 'agent-secret get <service>'; 'agent-secret search <query>' finds its name. A bare 'security
find-generic-password' on the agent-secrets keychain raises a password dialog while that keychain
stays locked." The old sentence advised that bare read, which the agent-secrets skill bars. The
rule's logic did not move, and the hub edits the Claude hook's block to the same sentence at
install, so the block stays byte-identical on both sides (`tests/unit/test_guard_sync.py`).

The guard sync r2 (2026-11-14, the hub's brief of 2026-11-11) ported two rules the Claude hook
enforced and the overlay let through. `[axiom-brief-regime]` denies a `subfleet run` into
`TheAxiomFoundation` whose brief names RuleSpec or encoding but carries no `ENCODING REGIME`
block. `[hand-authored-rulespec]` denies a `git commit` in a `TheAxiomFoundation/rulespec-*`
checkout that stages rule YAML with no encoder apply manifest. The hub cut both blocks from the
Claude hook; they sit in the overlay byte for byte, between trust-root and corpus-squash as in
the Claude hook, and the first prefilter literal is again the Claude hook's prefilter line, whose
`subfleet run` and `git [-C <dir>] commit` terms let them run. `tests/unit/test_guard_sync.py`
pins the blocks and the line (`tests/fixtures/guard/claude-blocks/`) and checks that both hooks
reach the same decisions. Before the merge below, its rollback was the guard sync's pair (hook
`34682488…3983`, TRUST `680f864e…12d9`). That hook has no D-SEARCH-REL1 parser, so it is no rollback
after the merge; the merged pair's and the fix r1's sections below give the rollbacks since.

The overlay now denies eleven rules, as its header lists them: nine Bash rule blocks copied byte
for byte from the Claude hook, in its order (tg-getupdates, keychain-dump, trust-root,
axiom-brief-regime, hand-authored-rulespec, corpus-squash, corpus-push, stash-shared and
local-main), the unscoped-search region with its parser, and the hf-dest adapter.

### The merged pair (2026-11-14)

D-SEARCH-REL1 (on release/217) and the guard sync (#303) were each built on hook `e869dd4c…`, and
release/217 at `82d3dd819` was merged into the guard sync on 2026-11-14. The merged hook is that
hook with both lines' edits and nothing else: removing the parser and undoing its other edits gives
#303's hook (`6c1beb64…`), and undoing the guard sync's two steps gives release/217's (`af5e7239…`)
(`tests/unit/test_guard_subfleet_root.py`, the `test_gs_undoing_…` and `test_merge_undoing_…` tests).
The two lines touch no common state:

- The two Axiom blocks run before the region. They read `$command`, `$cwd` and `$HOME`, assign
  their own names (at the merge `seg`, `wd`, `pf`, `gdir`, `origin_url`, `missing`, `f`, `m` and the
  `_abs` forms; since the fix r1 `ab_list`, `pf_abs`, `hr_list`, `hr_d`, `hr_gd`, `gdir`,
  `origin_url`, `hr_files` and `missing`; since the fix r2 also `ab_many`, `ab_n`, `ab_i`, `ab_p`,
  `ab_dir`, `ab_files`, `hr_t0`, `hr_late`, `hr_n`, `hr_i`, `hr_dirs` and `hr_gds`), and never change `$command` or `$cwd`. Release/217 changed only header comments before
  them, so they run on the code they ran on in #303. `corpus-push` assigns `seg` and `origin_url`
  before it reads them.
- The parser block reads `$command`, `$cwd`, `$HOME`, `$SHELL` and the region's own `us_*` and
  `pw_*` names. Every region function that uses `f` or `m` declares it `local`.
- The parser runs only when `pw_named` sets `us_ask`. `pw_named`'s four terms are the prefilter's
  third to sixth terms, which neither line changed, so a command that reaches the parser passes
  the prefilter under the old first literal and the Claude line alike. The wider first literal
  lets more commands (`git commit`, `pgrep`, `subfleet run`, `fd`) reach the Bash branch. Each
  such command the rule blocks allow pays for the region's walk as well as the heredoc's parse,
  though it starts no Python unless `pw_named` names a searcher. Review r1 of #303 measured the
  hook's CPU per call, base (`af5e7239…`) to merged, 8 runs each at a load of about 120:
  `pgrep -f codex` 70 to 137 ms, `fd -H x /` 75 to 143 ms and `git commit -m x` 68 to 182 ms,
  while `echo hello`, which the prefilter still stops, stayed at 73 and 69 ms. A command an Axiom
  block denies never reaches the parser, as with every earlier rule block.

`test_guard_search_rel.py`'s "r12d hook" (the hook before the parser) is #303's hook on this line, so
its fallback comparison holds the guard sync's denials constant. Rolling back the guard sync alone
restores release/217's pair (hook `af5e7239…`, TRUST `4ff9cb86…`). Rolling back the parser alone
restores #303's pair (hook `6c1beb64…`, TRUST `2955f02b…`). Either also drops the fix r1 below.

### The guard sync fix r1 (2026-11-15)

Review r1 of #303 found that the `[hand-authored-rulespec]` block read the index of whatever folder
the command named, and reading an index makes git run that repository's `core.fsmonitor` command,
here on the host, outside the lane sandbox. Both hooks now export `GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false` right after reading their input, outside
the byte-identical blocks (`tests/fixtures/guard/claude-blocks/git-config-pin.txt`). Command-scope
config outranks the system, global and repository files; only `GIT_CONFIG_PARAMETERS`, which the
launcher sets and a lane's commands cannot change, is read after it. No other git call in the hook
reads an index or runs a configured command: in a scratch repository whose fsmonitor, hooks path,
external diff, pager and textconv each wrote a marker, `remote get-url`, `branch --show-current`,
`worktree list` and `show-ref` ran none of them, and the hook never gives git a terminal.

The block also read two manifests per staged rule file, about 0.1 s each at load, so 700 staged
files passed the 60 s hook timeout, which allows. One `git cat-file --batch-check` now reads every
manifest, so the block's git processes per checkout stay at three, whatever the index holds. Both
Axiom blocks now walk the command's words: every `git … commit`, through git's global options
(`-c`, `--no-pager`, `-C`, `--git-dir` and the rest) and a `GIT_DIR=` before git, and every
`subfleet run`, with every `-C` and `-p`, attached or not. Each is judged from every folder the
shell can be in there: the payload cwd and each `cd` or `pushd` target before it, a relative one
resolved against the folder before it. The fix r1 refused a command with more than 16 folders or
briefs to check, to keep the work under the timeout; review r2 found that the 16 counted path
strings, not folders, and did not bound the time (the fix r2 below). The first prefilter literal's git term widened to
`git[[:space:]].*(commit|\\$)` so those forms reach the block. The hub makes the same three edits
in the Claude hook; the fixtures hold the text (`<rule>.sh`, `prefilter.txt` and
`git-config-pin.txt`; the hub's 2026-11-11 cut stays as `<rule>.before.sh` and
`prefilter.before.txt`). Its pair is hook `324d2a84…` with TRUST `cc26c421…`, and its rollback
the merged pair (hook `ed31bcac…`, TRUST `990dcbb8…`): undoing its step gives that hook byte for
byte (`test_fix_r1_undoing_its_step_gives_the_merged_hook`).

### The guard sync fix r2 (2026-11-17)

Review r2 of #303 found two gaps in the `[hand-authored-rulespec]` block, on both sides. The fix
r1's bound of 16 places counted path strings: `cd .; cd ./.; …` gave 16 keys for one checkout,
each costing three git processes and a pass over the whole index, and with 3,000 staged files the
review's command took 128 to 150 s, past the 60 s hook timeout, which allows. Both Axiom blocks
now drop `.` and empty components from each path and resolve each folder (`cd -P`, `pwd -P`)
before they use it, comparing the resolved keys whole, so every spelling that resolves to one
folder or brief is checked once, and a folder that is not there starts no git process. That holds
per folder, not per checkout: a subfolder of a checkout is a folder of its own, with its own git
processes. (Review r3 found that a `cd` target holding `..` after a link resolved where no shell
lands; since the fix r3 below such a target names two folders, and both are checked.) Either block
refuses more than 64 paths to resolve, and the brief block more than 16 distinct briefs. The hand
block checks its first two folders in full: the first two distinct (resolved folder, `--git-dir`)
keys that exist, in the order its awk meets them. The awk takes each `git … commit` in command
order and, for each, every folder met before it (the payload cwd, then each `cd` or `pushd` target
in order) with that commit's `-C` words applied; a folder that is not there is skipped and not
counted. These are not always the payload cwd and the folder a command names: in `cd a && cd b &&
git commit` the folders are the cwd, `a` and `b`, so `b` is the third, and under load the budget
can refuse that commit from one checkout (review r3's case 1). Before each git call for a later
folder the block denies once 15 s have passed since it began, and it never allows a folder it did
not check. The bound is when the block starts its last git call, not how long the first two
folders or that call take: one or two checkouts with a large index under heavy load can still
carry the hook past 60 s. At a load of 250 to 285, one `cat-file --batch-check` over 6,000
manifests took 7 to 31 s for 0.4 s of CPU, and the whole hook took 44.5 s for one 3,000-file
checkout. At a load of about 320, a budget that also covered the first folder refused a commit
from one checkout with one staged file, so the first two are exempt. The fix r2 bounded neither
the parse nor the brief block's reads: review r3 found that its `cd` walk cost the square of the
chain's length (the fix r3 below).

The block also read git's quoted names. `git diff --cached --name-only` quotes a name holding a
non-ASCII byte, a double quote, a backslash or a control character, the anchor never matched the
quote, and such a rule file got through. The diff now runs with `-z` and `--no-relative`, and a
`tr` turns its NULs into newlines and a newline inside a name into `\001`. Each name under the
anchor is looked up with its own bytes, a name holding `\001` (a newline) is denied whatever is
staged, and a checkout's `diff.relative` no longer hides files from a subfolder. The hub makes the
same block edits in the Claude hook (`<rule>.sh`); the fix r1's text stays as
`claude-blocks/fix-r1/<rule>.sh`. Its rollback is the fix r1 pair: undoing its step gives that
hook and TRUST byte for byte (`test_fix_r2_undoing_its_step_gives_the_fix_r1_pair`).

### The guard sync fix r3 (2026-11-26)

Review r3 of #303 found that the fix r2's `join` split and rebuilt the whole path at every `cd`, so
a relative `cd` chain cost the square of its length: 13 s of CPU at 4,000 `cd a; ` on the overlay,
against the fix r1's 0.24 s, and the brief block alone did not finish 3,000 in 200 s. A hook
killed at its 60 s timeout allows, and nothing bounded that work: the 64-path refusal counts keys,
and the budget covers only git calls. Both Axiom blocks now keep every stored folder (the payload
cwd, `$HOME`, each `cd` or `pushd` target and the one before it) in `norm`'s form as they read the
words, and `join` splits only the new word and appends it, so a `cd` costs its own words and one
copy of its folder, and no stored folder is split again. On review r3's rows (4,000 and 8,000
`cd a; `, and 4,000 `cd a/..; `, before a commit or a `subfleet run`) the hook now takes 0.11 to
0.47 s of CPU on either side; `test_a_long_cd_chain_costs_cpu_near_linear_in_its_length` bounds
each row at 10 times the fix r1's CPU, never under 1 s.

Each `git … commit` and each `subfleet run` is still judged from every folder met before it, so
2,000 `cd` and then 2,000 commits that each name one absolute `-C` cost four million path joins
while giving one key, which the 64 never counts: without a bound that took 13.8 to 30 s of CPU and
was allowed. Each block now counts every (folder, command) pair it examines, once more for each
`-C` (and, in the brief block, `-p`) word applied to it, and refuses past 4,096, the 64-folder
bound squared, so 64 folders can still each meet 64 commands; it never allows past the cap
(`test_too_many_folder_and_command_pairs_are_refused`). At the cap, with 64 folders of about
20 KB each, either block took 0.06 to 0.15 s.

Review r3 also found that the blocks resolved a `cd` target physically (`cd -P`), while bash and
zsh resolve it logically by default: from `a/`, with `a/link` → `x/y`, `cd link/../rs` lands in
`a/rs`, and the block checked `x/rs`, which was not there, and allowed. Each block now takes a `cd`
or `pushd` target's `..` off by name, as a logical `cd` does, before `cd -P`. Where the target's
physical spelling (`..` kept) differs, that folder is checked too, so a shell that resolves links
first (`cd -P`, bash's `set -P`, zsh's CHASE_LINKS or CHASE_DOTS, none of which the blocks parse)
is still judged where it lands; both spellings count toward the 64. `-C`, `--git-dir`, `GIT_DIR=`
and `subfleet run`'s `-C` and `-p` keep `..` as written, since git and subfleet resolve them
physically (`subfleet/cli.py` opens the brief, then calls `resolve()`).

What the fix r3 leaves open:

- The parse's bound: a `cd` costs its own words and one copy of its folder, and a command's
  (folder, command) pairs stop at 4,096. A folder grows with the chain, so a long relative chain
  still costs more than linear time in bytes copied, as the fix r1's did: on the overlay, 1.8 s of
  CPU at 16,000 `cd a; ` (96 KB) and 5.5 s at 32,000 (192 KB), against the fix r1's 1.1 and 4.2 s,
  and 23.1 s at 64,000 (384 KB), refused. A long folder held while short `cd`s repeat costs each
  `cd` a copy of it: with a 100 KB folder, 3.7 s at 10,000 `cd .` and 6.7 s at 20,000, allowed
  because no checkout is there. Extrapolated from these, not measured, about 700 KB of either shape
  in one command reaches the 60 s timeout, and less under load, since the timeout counts wall time;
  the timeout allows. A running count of the folder bytes each `cd` copies, refused through the same
  `TOOMANY`, would bound it; that is left to the hub.
- One or two checkouts whose index alone outlasts the timeout, and one git call that runs long
  after the last budget check, as in the fix r2.
- Up to 64 `cd -P` forks in each block (each a fork with no exec) before git runs or a brief is
  read; not measured.
- The brief block's reads: up to 16 regular files, each read by up to two `grep`s, with no time
  bound; not measured.

The hub makes the same block edits in the Claude hook (`<rule>.sh`); the fix r2's text stays as
`claude-blocks/fix-r2/<rule>.sh`. Its rollback is the pair after the merge of release/217 (hook
`7c96811e…`, TRUST `2e16c035…`): undoing its step gives both byte for byte
(`test_fix_r3_undoing_its_step_gives_the_merged_pair`).

| File | SHA-256 |
| --- | --- |
| `never-rules-hook.sh` | `899b43ec41dcbdb673298e6c22d14caea88f450ff33405789d8513a013657038` |
| `TRUST` | `fa0df26c35087bf9116b9c6793490d70116ec13dac9a46576bb67287da35ad85` |

TRUST is the installed TRUST of 2026-09-29 (`b3193b97…`), changing only `hook_sha256` and
appending eight dated provenance additions (D-LANE-SCOPE1's three sentences, r2's one, r3's one,
r3b's one, whose mount clause r3c corrected, and the guard sync's four); `hooks_trust_hash` and
`override` hash the hook's identity, not its bytes, and do not move. The staging helper above
refuses to replace a different overlay in place, so this revision is staged into a fresh state
root and swapped in; `tests/unit/test_guard_subfleet_root.py` states and checks the rule's
invariants against the original hook, and `docs/reports/2026-10-24-guard-subfleet-root.md`
holds the install steps, the live checks, the known limits and the rollback, which restores
hook `a60d1c51…` with TRUST `b3193b97…`. D-CODEX-0169 (below) then moved `codex_version` to
`codex-cli 0.169.1` and appended one more dated sentence, and the guard sync fix r3 appended one
after it.

### Codex pin 0.169.1 (D-CODEX-0169, 2026-11-17)

On a ChatGPT login, codex-cli 0.159.0 answers `model_not_found` for GPT-6.2 Sol and 0.169.0 serves
it; the hub did not try the versions between. 0.169.1, which this revision pins, also lists GPT-6.2 Sol
in Codex's bundled model catalog. Preflight refuses any Codex whose `--version` differs
from TRUST's `codex_version`. D-CODEX-0169 (#333, merged into release/217 at `024b23f80`) moved
`codex_version` to `codex-cli 0.169.1` and appended one dated provenance sentence. The hook bytes,
`hooks_trust_hash` and `override` stayed as they were. On release/217 it replaced TRUST
`4ff9cb8603f9e84e3fe1870a07c71a7616b08384909b2bc4900035054607cf14` with #333's TRUST,
`9fb9bcf7beecb09d79e2ce0729c807ef08577fa827121d8281a5ce14017cb7d2` (hook `af5e7239…`). The guard
sync's fix round 3 merged release/217 and made the same two edits in the guard sync's TRUST, after its
four sentences; the table above gives that merged TRUST, not #333's.

The live guard keeps hook `e869dd4c…` until D-SEARCH-REL1's `af5e7239…` gets its own install decision,
so the hub installs a TRUST, `0224cb3f…`, that differs from #333's TRUST in `hook_sha256` only. It
differs from this file's TRUST in that and in lacking the guard sync's sentences. `tools/guard_pin_0169.py`
derives it from the installed TRUST (`36267abb…`) with the same two edits. It refuses unless the result
differs from the TRUST of the checkout it runs from only in `hook_sha256` and is the pinned `0224cb3f…`;
its docstring says to run it from a checkout of #333's commit, whose TRUST is `9fb9bcf7…`. At this
head the repository's TRUST also carries the guard sync's sentences, so `stage` refuses here
(`tests/unit/test_guard_pin_0169.py` checks that refusal, and runs the kit's other checks on #333's
TRUST rebuilt from this one). From #333's checkout it stages the result in a fresh folder, preflights
it on every Codex lane it is given and swaps it in, keeping the previous TRUST as
`TRUST.pre-0.169.1.bak`. Its rollback refuses once a later guard change replaces the hook or the TRUST.
`docs/reports/2026-11-17-codex-0169-pin.md` has the evidence, the install steps and the rollback.

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
