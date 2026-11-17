# Build: Design A, writable Claude lanes stop loading personal startup files (D-LANE-STARTUP1)

Built on `feat/lane-startup-217` from 669a2e608, in a detached worktree. The design is
`docs/reports/2026-11-14-lane-startup-design.md`; this build follows the hub's rulings: Design A
only; `autoMemoryEnabled: false`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and the memory variables in
writable `env_remove`; writable reviews keep `--setting-sources user` and get the keys too;
`CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG` stays out. No Claude CLI ran: how Claude Code reads these
keys still rests on its 2.1.318 source, as the design note quotes it.

## Result

- Every workspace-write `build_launch` and `resume_launch`, review or not, now merges
  `job_startup_settings(workdir, env)` into its one `--settings`. Read-only launches, isolated
  reviews, `lane_settings()` and `claude_turn.argv` are byte-for-byte what adf66f2e5 built (189
  recorded cases). The other 48 recorded cases are writable, and they differ only by the startup
  keys.
- I1-I9 are tests. All 17 mutants (M1-M15, with M6, M9 and M10 split in two) are killed, each
  run alone in a copy of the tree.
- Claude Code's own matcher (picomatch and `FWo` extracted from the 2.1.318 binary, run under
  node) agrees with the test oracle on every one of 27,487 generated paths: 1,313 in the
  committed fixture plus 26,174 from six more seeds. No path's verdict differs, and `FWo`'s
  output matches every time.
- Formal run 1 (2,228 passed and 3 xfailed; 542 passed) and run 2 (435 passed) finished green.
  A supplementary run of `tests/e2e/test_conversations.py` failed under load; see "Commands".

## Changes

`subfleet/adapters/claude.py`
- `:142-156`: constants. `JOB_STARTUP_ENV` = `{"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}` (`:145`),
  `STARTUP_FILE_NAMES` (the design's six names, `:149`), `JOB_STARTUP_GIT_TIMEOUT_S = 10.0`
  (`:152`), `_GLOB_ESCAPES` (`:154`).
- `:372` `glob_literal(path)`: writes a path as a pattern that matches only that path.
  - `*?[]{}()|+.` become one-character classes (`[*]`).
  - `,` and `"` become one-character ranges (`[,-,]`, `["-"]`).
  - Every other character stays as it is (see "Matcher evidence" for why).
- `:390` `job_startup_settings(workdir, env)`, job-only, returns `{"env": JOB_STARTUP_ENV,
  "autoMemoryEnabled": false, "disableAllHooks": false, "claudeMdExcludes": [...]}`:
  - R is `git rev-parse --show-toplevel` in the workdir, run through `salvage._git_bytes` with a
    10 s cap. It falls back to `abspath(workdir)` (`:408-412`). R_real is `realpath(R)`.
  - C is the config home: `$HOME/.claude`, and also `CLAUDE_CONFIG_DIR` when the launch env sets
    it. Each C gets two excludes: `<C>/CLAUDE.md` and `<C>/rules/**`.
  - A is every ancestor of R and of R_real except `/`, plus `$HOME`. Each A gets the six names.
  - It ends with one negation: `!{R,R_real}/**`, or `!R/**` when the two spellings are equal.
  - Entries are de-duplicated in order. `$HOME` comes from `_launch_home(env)`, and env is the
    launch's assembled environment (`self.provider_environment.assemble(credential_env)`).
- `build_launch` `:1508-1512` and `resume_launch` `:1560-1564`:
  - `startup = job_startup_settings(...) if sandbox == workspace-write else None`.
  - `claude_settings_args(..., settings=lane_settings(startup))`: one `--settings` still,
    merged by the existing `lane_settings`. `lane_settings(None) == lane_settings()`, so the
    read-only path is unchanged.
  - `env_remove` is now `(*ENV_REMOVE, *claude_env_remove(...))` for every sandbox. Before, only
    read-only launches removed the memory variables; their value is the same as before.
- `:1483-1485`: a docstring sentence. `:2262-2263`: `__all__` gains `glob_literal` and
  `job_startup_settings`.
- Untouched: the read-only `--setting-sources ""`, the guard, `lane_settings`,
  `lane_settings_args`, `claude_settings_args`, `claude_turn`, the effort pin, and
  `expected_transcript_path`.

`docs/acceptance-contract.md`
- `:603` C-12.9 gains one passage: the keys, R, C and A, the 10 s cap, the escapes, auto-memory,
  `disableAllHooks`, the memory-variable removal, and the reason. It says no Claude run has
  exercised these keys, that a resumed session keeps what its first turn loaded, and that
  read-only launches and turns are unchanged.
- `:9`: one change-log line at the top of "Changes in version 3". It has no PR number and no
  blank line inside the list. (24e1d3bf8 later added the double quote's range to the `:603`
  passage, which first named only the comma's.)

Tests and tools:

| File | What it holds |
|---|---|
| `tests/unit/test_lane_startup.py` (new) | I1-I9 against the adapter, with fake HOME and fake repos |
| `tests/unit/test_lane_startup_glob.py` (new) | Oracle against the bundle recording; `glob_literal` properties |
| `tests/claude_md_glob.py` (new) | The matcher oracle |
| `tests/fixtures/lane_startup/picomatch-2.1.318.json` (new) | Bundle verdicts: 18 sets, 1,313 paths |
| `tests/fixtures/lane_startup/argv-adf66f2e5.json` (new) | 237 argv cases built by adf66f2e5 |
| `tools/claude_bundle_picomatch.py` (new) | Extracts the bundle's picomatch and `FWo` |
| `tools/claude_md_glob_record.py` (new) | The recorder |
| `tests/lane_settings_cases.py`, `tests/unit/test_lane_settings.py`, `tests/unit/test_lane_settings_review_r1.py`, `tests/unit/test_claude_adapter.py`, `tests/unit/test_review_publish_guard.py` | Older tests that pin the writable `--settings` now read it through `without_job_startup`, which asserts the keys are there and removes them |

Evidence data is in `docs/reports/2026-11-15-lane-startup-build-data/`: `run_mutants.py` and
`mutants.json`, `runs.log` (every run with its start and end `uptime`), `matcher-seeds-7-12.log`,
and `run3-*-summary.txt`.

### What a git failure does

Any exception from `_git_bytes` makes R `abspath(workdir)`. That covers a non-zero exit, a timeout
past 10 s, a missing git or an OS error. An empty or relative answer does the same. The result
fails closed:

- Nothing outside the workdir is kept.
- A job started in a subfolder of its repository also excludes the repository's top-level
  `CLAUDE.md`/`AGENTS.md` and those of folders between the top and the workdir. Fewer of the
  repository's own files load; no personal file is added.
- The worst case adds 10 s to a writable launch.

Tests (stub git on `PATH`):
- `test_r_falls_back_to_the_workdir_when_git_fails[not-a-repository|plain-failure|relative-answer|killed]`
- `test_r_falls_back_to_the_workdir_when_git_passes_its_cap` (`sleep 30` against a 0.5 s cap)
- `test_r_outside_git_is_the_workdir`

### Judgments beyond the letter of the design

1. C is a superset of the design's `_launch_home(env)/.claude`. It always includes
   `$HOME/.claude` and also adds `CLAUDE_CONFIG_DIR` when the launch sets it, since Claude Code
   reads its config home from that variable when it is set. No lane sets it today (I7 checks
   `env_add` gains none).
2. Writable `env_remove` uses `claude_env_remove(...)`. That is `MEMORY_ENV` plus any
   `CLAUDE_COWORK_MEMORY_*` name, the set C-23.2 already removes from read-only launches. The
   brief named only `MEMORY_ENV`.
3. Escapes are brackets and ranges, not backslashes: `FWo` turns every `\` into `/`.
4. The ancestor list leaves out `/` itself, and `$HOME` is listed even when it is not an ancestor. A `/`-level
   `CLAUDE.md` is still excluded, because the negation spares only R.

## Matcher evidence

The quotes come from Claude Code 2.1.318, `~/.local/share/claude/versions/2.1.318`
(sha256 `677a2ea7bf576cedd20e191d973914ed8a41796f53a63b1923381ddc4d5b5e7d`), read with the
dab2face9 tools. Each quoted string occurs once in the binary.

```
function v8t(){let e=gt().claudeMdExcludes;if(!e||e.length===0)return()=>!1;let n=FWo(de(),e).filter((s)=>s.length>0);if(n.length===0)return()=>!1;let r=p8t.default(n,{dot:!0});return(s,h)=>(h==="User"||h==="Project"||h==="Local")&&r(s.replaceAll("\\","/"))}
function FWo(e,n){let r=n.map((s)=>s.replaceAll("\\","/"));for(let s of r){if(!s.startsWith("/"))continue;let h=s.search(/[*?{[]/),y=h===-1?s:s.slice(0,h),b=Cx(y);try{let w=No(e,b),{resolvedPath:H}=w;if(DN(H)||s2(b,w))continue;let z=H.replaceAll("\\","/");if(z!==b){let Y=z+s.slice(b.length);r.push(Y)}}catch{}}return r}
```

`p8t=Oe(DXt(),1)` is picomatch, with `{dot: true}`. These picomatch behaviours, all read from
the bundle, decided the escapes:

- An empty input never matches, and an input equal to the glob's own text does
  (`if(e==="")return{isMatch:!1`).
- Two raw dots inside braces make a range (`dots=!0`). So `.` is bracketed: a folder named
  `..!x` broke the first recording.
- Once glob syntax appears, `"` toggles quoting and is dropped. So `"` becomes `["-"]`.
- A bracket holding `!` compiles to `(?:\[!\]|[!])`, which also matches the bracket's own text.
  So `!`, `@`, `^`, `$`, `#`, `~` and every other character outside the bracketed set stay raw.
  picomatch reads them literally where `glob_literal` puts them: no leading `!`, and `(` is
  always bracketed. The recording confirms this; its alphabet holds each of them.
- `[,]` widens in the same way, so the comma is the range `[,-,]`.
- A negation compiles to `^(?!^(?:BODY)$).*$`, and JavaScript's `.` matches no line terminator.

`tools/claude_bundle_picomatch.py` copies picomatch's modules (`rp`, `sp`, `rN`, `lN`, `uN`,
`DXt`) and `FWo` out of the bundle's DXt module. It shims `Cx` (dirname), `No` (realpath or a
link map), `DN` and `s2`. `tools/claude_md_glob_record.py` builds pattern sets with this tree's
`job_startup_settings` for synthetic workdirs and homes under `/sf-lsb-glob`; nothing was
created there. The names use an alphabet holding every metacharacter, `"`, a backtick, `<>%;:`,
tab, space and non-ASCII letters. One set in three is reached through a symlinked spelling, and
one in four sets `CLAUDE_CONFIG_DIR`. The recorder asks node (v26.8.2) for the bundle's verdict
on each path and compares it with `tests/claude_md_glob.py`.

| Recording | Sets | Paths | Agree | `FWo` mismatches |
|---|---|---|---|---|
| Committed fixture, seed 20261115 | 18 | 1,313 | 1,313 | 0 |
| Seeds 7-12, 60 sets each (`matcher-seeds-7-12.log`) | 360 | 26,174 | 26,174 | 0 |
| Seeds 20261115 and 1-4, 60 sets each, before the last fixture | 300 | about 21,800 | all | 0 |

The last row's exact counts were not kept. All three rows ran against the escapes now
committed.

The bundle's verdicts by label in the fixture (label/excluded):

| Label | Excluded | Paths | Meaning |
|---|---|---|---|
| keep | False | 240 | R's own files in either spelling, including names with spaces and metacharacters |
| exclude | True | 936 | Every ancestor's six names, the homes, one-edit neighbours of R's leaf, `R` + `x`, `R` + space, `/CLAUDE.md`, relative paths |
| dot-segment | True | 48 | `R/./CLAUDE.md` and `R/../CLAUDE.md`; Claude never builds these |
| line-terminator | False | 71 | See residues |
| empty | False | 18 | picomatch's empty input |

Across seeds 7-12 every keep path was kept (800 per seed) and every exclude path excluded.

Residues, pinned in `test_i3_by_the_bundles_own_verdicts`:
1. A folder outside R whose name holds `\n`, `\r`, U+2028 or U+2029 is not caught by the
   negation, since JavaScript's `.` matches none of them. Ancestors and homes are still listed
   explicitly.
2. A path that folds onto R once `\` is read as `/` counts as R's (1-2 per seed).
3. Matching is case-sensitive on a case-insensitive filesystem.
4. If Claude Code spelled its cwd logically while git spells R physically, R's files would be
   excluded. This would fail closed; the design's probe 3 is meant to check it, and it has not
   run.
5. A resumed session keeps what its first turn loaded.

## Invariants and their tests

Unless the row says otherwise, tests are in `tests/unit/test_lane_startup.py`.

| Invariant | Tests (cases) |
|---|---|
| I1: writable argv has no `--safe-mode`/`--bare`/`--restricted`; `--setting-sources` keeps `user`; `disableAllHooks:false` | `test_i1_i4_i8_i9_every_writable_launch` (12: task × tmp × mcp, build and resume); `test_a_writable_job_changes_only_by_the_startup_keys` (48 recorded writable cases against adf66f2e5) |
| I2: fixture workdirs get the C, A and `$HOME` entries | `test_i2_i3_each_fixture_workdir` (6: repo-root, subdirectory, symlinked, non-git, home, linked-worktree); `test_i2_a_home_lane_also_excludes_its_own_config_home`; `test_lane_startup_glob.py::test_job_startup_settings_still_writes_the_recorded_patterns` (18) |
| I3: nothing under R excluded, every outside path excluded | `test_i2_i3_each_fixture_workdir`; `test_i3_a_git_spelling_through_a_symlink_spares_both`; `test_i3_a_workdir_with_spaces_non_ascii_and_glob_metacharacters` (workdir `we ird é名 *?[{!,}] (a\|b)+@ "q" $^#~ ..x` with decoy siblings); the three git-fallback tests; `test_lane_startup_glob.py::test_i3_by_the_bundles_own_verdicts` (18), `::test_the_oracle_gives_the_bundles_verdict_on_every_recorded_path` (18), `::test_glob_literal_matches_its_path_and_no_neighbour` (Hypothesis, 300 examples) |
| I4: `autoMemoryEnabled:false`, `env.CLAUDE_CODE_DISABLE_AUTO_MEMORY="1"`, `MEMORY_ENV` in writable `env_remove` | `test_i1_i4_i8_i9_every_writable_launch` |
| I5: read-only argv byte-identical to adf66f2e5 | `test_i5_i6_every_other_launch_is_byte_for_byte_adf66f2e5s` (189 recorded non-writable cases: read-only jobs, isolated reviews, turns at every permission, enroll, gate); `test_the_grid_is_the_recording` |
| I6: `claude_turn.argv` unchanged | the same 189 cases; `test_i6_turn_argv_takes_nothing_from_job_startup` |
| I7: no `CLAUDE_CONFIG_DIR` in `env_add`; transcript path unchanged | `test_i7_no_config_dir_joins_and_the_transcript_path_is_unchanged` (writable, read-only) |
| I8: one `--settings`, valid JSON, watchdog kept | `test_i1_i4_i8_i9_every_writable_launch`; `test_the_payload_is_valid_json_at_every_fixture` (6) |
| I9: resume payload equals build payload | `test_i1_i4_i8_i9_every_writable_launch`. The publish hook's path names the attempt folder, so the payloads are compared with `/attempt-1/` and `/attempt-2/` normalised. |

Other tests:
- The bundle-recording tests in `test_lane_startup_glob.py`.
- Escape strings, oracle refusals, picomatch `test` rules, `FWo`'s real-path spelling, and the six names.
- Payload size: `test_the_payload_size_for_a_workdir_twelve_levels_deep`. A workdir 12 levels
  below `/` gives 75 patterns and a 6,471-byte `--settings` payload. The same workdir 12 levels
  below the pytest temp root, 21 deep in all, gives 129 patterns and 16,695 bytes.

## Mutants and their killers

`docs/reports/2026-11-15-lane-startup-build-data/run_mutants.py` worked on a `git archive HEAD`
copy at `$TMPDIR/sf-lsb/mutants/tree`, never the worktree. For each mutant it:

1. replaced exactly the named text, with the expected occurrence count asserted;
2. ran both lane-startup modules with `-n 4`;
3. wrote the original bytes back and checked both the copy's and the worktree's sha256.

Every restore and worktree check passed. The unmutated copy (M0) gave 348 passed. Full failure
lists are in `mutants.json`.

| Mutant | Failed | Killing tests (count) |
|---|---|---|
| M1 `--setting-sources project,local` added | 60 | `test_i1_i4_i8_i9_every_writable_launch` (12), `test_a_writable_job_changes_only_by_the_startup_keys` (48) |
| M2 `disableAllHooks:false` dropped | 60 | same as M1 |
| M3 `--safe-mode` added | 60 | same as M1 |
| M4 `claudeMdExcludes` dropped | 83 | `test_i2_i3_each_fixture_workdir` (6), recorded patterns (18), writable differential (48), git fallbacks (4), and 7 more |
| M5 ancestors from `job.workdir`, not R | 8 | `test_i2_i3_each_fixture_workdir` (2: subdirectory, symlinked), recorded patterns (6) |
| M6a ancestor walk includes R | 32 | `test_i2_i3_each_fixture_workdir` (5), recorded patterns (18), git fallbacks (4), size (2), and 3 more |
| M6b ancestor walk stops before the top folder | 29 | `test_i2_i3_each_fixture_workdir` (6), recorded patterns (18), size (2), home lane (1), and 2 more |
| M7 R_real dropped | 8 | `test_i2_i3_each_fixture_workdir[symlinked]`, `test_i3_a_git_spelling_through_a_symlink_spares_both`, recorded patterns (6) |
| M8 two negations | 8 | same as M7 |
| M9a config-home entries dropped | 28 | `test_i2_i3_each_fixture_workdir` (6), `test_i2_a_home_lane_also_excludes_its_own_config_home`, recorded patterns (18), and more |
| M9b `$HOME` entries dropped | 28 | same tests as M9a |
| M10a `autoMemoryEnabled` dropped | 60 | `test_i1_i4_i8_i9_every_writable_launch` (12), writable differential (48) |
| M10b the env variable dropped | 60 | same as M10a |
| M11 startup keys on read-only launches | 88 | `test_i5_i6_every_other_launch_is_byte_for_byte_adf66f2e5s` (88) |
| M12 keys put into `lane_settings()` | 190 | `test_i5_i6_...` (189), `test_i6_turn_argv_takes_nothing_from_job_startup` |
| M13 OAuth token replaced by `CLAUDE_CONFIG_DIR` | 2 | `test_i7_no_config_dir_joins_and_the_transcript_path_is_unchanged` (2) |
| M14 a second `--settings` | 70 | `test_i1_i4_i8_i9_every_writable_launch` (12), writable differential (48), `test_i2_i3_each_fixture_workdir` (6), and 4 more |
| M15 only `build_launch` updated | 36 | `test_i1_i4_i8_i9_every_writable_launch` (12), writable differential (24) |

## Lines #310 (`fix/effort-env-217`, D-EFFORT-ENV1) also touches

This compares `fix/effort-env-217` at fdb7e8092 against the merge base 82d3dd819.

`subfleet/adapters/claude.py` has a textual conflict on two lines. In both, the base had
`argv += claude_settings_args(job, sandbox, attempt_dir, settings=lane_settings())  # C-12.4, D-WATCHDOG1`:

| Base line | Method | Here (now `:1510` / `:1562`) | In #310 |
|---|---|---|---|
| 1434 | `build_launch` | `settings=lane_settings(startup)` | `settings=lane_settings(effort=effort)  # C-12.4, D-EFFORT-ENV1` |
| 1484 | `resume_launch` | `settings=lane_settings(startup)` | `settings=lane_settings(effort=effort)  # C-12.4, D-EFFORT-ENV1` |

The reconciliation for both lines is `settings=lane_settings(startup, effort=effort)` with both
decision tags.

Near these lines:
- In `resume_launch`, #310 adds an `effort` parameter and `--effort` lines a few lines above the
  settings line, after the `--output-format` line. My `startup = ...` lines sit just above the
  settings line.
- #310 also changes the `lane_settings`/`lane_settings_args` signatures (base `:343-354`) and
  `reconstruct_v1_argv`. I touched neither.

The test helpers don't conflict:

| File | My lines | #310's lines | Gap |
|---|---|---|---|
| `tests/lane_settings_cases.py` | `without_job_startup` inserted after base `:357` | `:1`, `:35`, `:56`, `:58`, after `:122`, and `without_override` `:341-348` | 9 lines from #310's `without_override` |
| `tests/unit/test_lane_settings.py` | `:118`, `:136`, `:164`, `:226` | `:23`, `:120`, `:170`, `:191`, `:228` | one unchanged line (`:119`, `:227`) between two pairs |
| `tests/unit/test_claude_adapter.py` | import `:26`; `without_publish_hook` `:410-411` | 15-line test inserted after `:404` | none |

#310's new test in `test_claude_adapter.py` calls `without_publish_hook`, which now also drops
the startup keys.

Corrected in fix round 1. The two statements that stood here understated the merge: that #310's
writable-argv tests alone would need `without_job_startup`, and that the contract doesn't overlap.
Review r1 merged 2234e7196 with #310's head 4fd796b49 in a scratch repository, without committing,
and measured:

- Conflicts: `claude.py:1510` and `:1562` (the two settings lines above; `:1565` and `:1617`
  after fix round 1), plus the contract's change log, where both PRs add a line. #310 edits
  C-12.4 and C-26.11 and this PR C-12.9, so the clauses themselves don't overlap.
- Resolved to `lane_settings(startup, effort=effort)`: 181 failed, 1,828 passed.
  - 179 of the failures are this PR's own recorded cases: 154 in
    `test_i5_i6_every_other_launch_is_byte_for_byte_adf66f2e5s`, 24 in the writable differential
    (`test_a_writable_job_changes_only_by_the_startup_keys`) and `test_the_grid_is_the_recording`.
    The `argv-adf66f2e5.json` recording predates #310's effort env key and its 70 added cases.
  - The other 2 are #310's property tests in `test_effort_env.py`, which need
    `without_job_startup`.

The hub's ruling: #310 merges first. This PR then merges release/217, keeps both change-log lines,
takes `lane_settings(startup, effort=effort)` on both settings lines, adds `without_job_startup`
to #310's two property tests, and re-records `tests/fixtures/lane_startup/argv-adf66f2e5.json` from
a base that includes #310 (`HOME=<fake> python -I tests/lane_settings_cases.py <git archive of
that base> <scratch>`, as under "Commands"). The fixture must be re-recorded after #310; until
then it holds adf66f2e5's argv. Fix round 1 did not merge #310.

## Records: new keys

No event or store key was added. The new keys live only in the launch argv's `--settings` JSON
and in `env_remove`, and the daemon writes both into `launch.json` (`daemon.py:9900`). I grepped
`tests` for every reader of `launch.json`, `env_remove`, `--settings`, `claudeMdExcludes` or
`ENV_REMOVE`: 44 files.

- 24 of them are in run 1 (23 from the brief's list and the new glob module), which passed.
- `tests/fake/test_state_contract.py` is in run 2, which passed.
- `tests/unit/test_claude_v1_parity.py` passed in dev3 but was left out of the formal runs (see below).
- `tests/live/test_claude_live.py` did not run, since it needs a real CLI.
- Among the other 17 files, the exact compares are on conversation turns:
  - `tests/unit/test_claude_turn.py:59` and `tests/e2e/test_conversations.py:232` (its `ultracode` key).
- The rest check membership or absence:
  - `test_launch_env_round2.py:142`, `CLAUDE_MEMORY_STORES` in `env_remove`, which stays true.
  - `test_workspace_transient.py:489`, that `launch.json` is absent.
- No exact compare of a writable launch's settings or `env_remove` exists outside run 1.

## Commands and results

Every test command was `uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest ... -q -p
no:cacheprovider --basetemp=$TMPDIR/sf-lsb/pytest-<label>`, with
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache` and `PYTHONPATH`
unset. Runs used `-n 4` unless the row says serial. The UTC times and 1-minute load averages
come from `runs.log`, which records `uptime` at each start and end.

| Run | Files | Start → end (load) | Result |
|---|---|---|---|
| dev1 | the two lane-startup modules | 03:14:24 → 03:15:17 (183 → 194) | 1 failed, 344 passed (fixed before dev2) |
| dev2 | same | 03:15:31 → 03:15:54 | 348 passed |
| dev3-unit | run-1 unit files, including `test_claude_v1_parity.py` | 03:16:44 → 03:20:47 (175 → 186) | 104 failed, 2130 passed, 3 xfailed. Older tests pinned the writable `--settings`; fixed in add5ae3b4 and 728decba6 |
| dev4-unit | run-1 unit files without v1-parity | 03:23:19 → 03:29:07 (172 → 186) | 2228 passed, 3 xfailed |
| dev5-other | run-1 fake/e2e/process files | 03:29:18 → 03:35:58 (185 → 189) | 542 passed |
| mutants | 3 batches, M0-M15 | 03:38:10 → 03:49:03 (193 → 152) | M0 348 passed; every mutant killed |
| size | `-k payload_size -s`, serial | 03:49:29 → 03:49:32 | 2 passed (sizes above) |
| **Run 1, unit** | 17 unit files naming the brief's terms (without v1-parity) plus `test_lane_startup_glob.py` | 03:52:44 → 03:55:18 (138 → 149) | **finished: 2228 passed, 3 xfailed, 4 warnings, 149.7 s** |
| **Run 1, other** | the 18 e2e/fake/process files naming them | 03:55:25 → 03:59:22 (148 → 154) | **finished: 542 passed, 235.4 s** |
| **Run 2** | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | 03:59:29 → 04:01:36 (150 → 108) | **finished: 435 passed, 119.5 s** |
| run3-records (supplementary) | the 17 other `launch.json`/`env_remove` readers, without `tests/live` | 04:04:31 → stopped about 04:17 (150 → 204) | **did not finish.** It passed the tool's 600 s cap, the harness moved it to the background, and I stopped it at about 49%. It had 6 errors, 1 failure and 1 skip, all in `tests/e2e/test_conversations.py` (from the kept state roots) |
| run3a | the 11 small unit files of that set | 04:21:55 → stopped by the harness about 04:41 | **did not finish:** about 496 of about 514 passed, none failed, when the harness stopped it |
| run3-conversations | `tests/e2e/test_conversations.py` | 05:02:22 → 05:15:33 (274 → 123) | **finished, rc 1:** 6 failed, 15 passed, 13 skipped, 11 errors, 622 s. Every failure is a `condition timed out after 10s/30s/60s` at `tests/e2e/conftest.py:297`, plus one teardown `no such table: notices`. Every kept daemon log was empty. The 13 skips are person-only tests that refuse inside a Subfleet job |
| run3-serial | three of those tests, serial | 05:16:12 → 05:19:18 (118 → 114) | `test_a_job_a_turn_dispatched_reaches_the_next_turn_and_a_ping_does_not` and `test_a_claude_turn_on_the_wrong_model_fails_model_mismatch` passed. `test_a_codex_conversation_is_read_only_until_the_guard_is_proven_live` errored at setup (10 s timeout). That test runs a Codex conversation, which this change does not reach |

Reading of the supplementary failures: they are load timeouts in daemon start-up, not this
change. Evidence:
- Two of the three, rerun alone, pass, including the one that dispatches a job.
- The failing set includes a Codex conversation.
- I5/I6 prove the turn argv byte-identical.
- The daemon logs were empty: the daemon wrote nothing before the waits expired.

This is not proven, because the file was not run at the base commit for comparison.

Non-test commands:
- The recorder: `uv run python tools/claude_md_glob_record.py <claude 2.1.318> <work> <out>
  --sets 18 --seed 20261115` for the fixture, and `--sets 60 --seed 7..12` for the seed log.
- `HOME=<fake> python -I tests/lane_settings_cases.py <git archive adf66f2e5 subfleet>
  <scratch>`, which recorded the 237 argv cases.
- `python3 run_mutants.py <worktree> <copy> <results>`, in three batches.

## What did not run, and what went wrong

- **The whole suite** did not run, as the brief asked. No real-CLI probe ran, and no probe brief
  was written.
- **A live-state read.** dev3 ran `tests/unit/test_claude_v1_parity.py`, which the brief's run-1
  list includes because it names `build_launch`. At this base, that test reads
  `~/chief-of-staff/subfleet/bin/subfleet-claude` through the password-database home when the
  file exists, and in dev3 it ran without skipping. That breaks the hands-off rule. I excluded the
  file from dev4 and from both formal runs, so run 1 is the brief's list minus this one file.
  #310 replaces the read with a `SUBFLEET_V1_RUNNER` gate.
- **Background work.** Three commands, `run3-records`, `run3a` and a collect-only count, passed
  the Bash tool's cap and the harness moved them to the background:
  - I stopped two of them with TaskStop; the harness stopped `run3a` at its limit.
  - `run3-conversations` also passed the cap and finished in the background, inside the session.
  - So did the cleanup's `rmtree` of `$TMPDIR/sf-lsb`. The `/tmp/sf-failed` folders were
    removed in a separate foreground step.
  - Nothing was started with `&`, `nohup` or the like. After each stop I checked for processes
    with this worktree as their cwd and found none.
- **Commit order.** 87d8cb798 (the adapter) comes before the older tests' fixes in add5ae3b4 and
  728decba6. bf80765a6's glob tests fail until the escape fix a7bac0885. So the
  intermediate commits 87d8cb798..a7bac0885 do not pass the older launch tests. I did not rewrite
  history.
- **The harness's commit.** 728decba6 ("fixup! ...") was made by the harness's auto-commit
  before a context compaction. It holds `run_mutants.py` and `mutants.json`, and nothing else.
- **What was read.** I read the Claude Code binary at `~/.local/share/claude/versions/2.1.318`, as
  the brief directs (the dab2face9 tools). `~/.claude`, `~/.subfleet`, `~/.codex` and the store
  were not read.

## Cleanup

These were removed with a guarded `shutil.rmtree` that asserts `sf-lsb` or `sf-failed` is in the
path:

- `$TMPDIR/sf-lsb` and everything below it:
  - `base-adf66f2e5/`, the `git archive` the recordings came from;
  - `mutants/`, the tree copy and its results;
  - `node/`, the extracted bundle module;
  - `record/`, `smoke/`;
  - every `pytest-*` basetemp;
  - `out-*.txt`, `runs.log`, `run.sh` and the file lists.
- `/tmp/sf-failed/64342` (from run3-records).
- `/tmp/sf-failed/82407`, `/tmp/sf-failed/86302`, `/tmp/sf-failed/91960` and
  `/tmp/sf-failed/94450` (from run3-conversations).
- `/tmp/sf-failed/50586` (from run3-serial).

`/tmp/sf-failed` itself predates this job (created 2026-11-14 14:56 UTC), so it stays. The shared
`$TMPDIR/uv-cache` stays.

Status: done at 05:33:04 UTC. Afterwards `$TMPDIR/sf-lsb` no longer exists and `/tmp/sf-failed` is empty.

Left in place: `/tmp/sf-e2e-yege7njs`, an e2e harness state root created at 04:17:34 UTC, the moment
I stopped run3-records (`tests/e2e/conftest.py:372` makes these, and a killed test leaves one
behind). It is almost certainly mine, and no process has it open. Its name falls outside the
`sf-lsb`/`sf-failed` guard, so I did not remove it; it can be deleted. The `/tmp/sf-st-*`
folders come from `tests/frontend/test_core_store.py`, which I did not run, so they are not mine.

## Commits

| Commit | Content |
|---|---|
| 87d8cb798 | adapter |
| e7fbe9cb1 | contract |
| bf80765a6 | tests, oracle, tools and fixtures |
| a7bac0885 | escape fix |
| add5ae3b4, 728decba6 | older tests and the mutant data |
| 24e1d3bf8 | the contract's quote range |
| 08858cfd0, 740b241ad and this table's commit | report and evidence data |

## Fix round 1

This round fixes review r1's three P2s
(`~/reviews/subfleet-hub/lane-startup-2026-11-14/review-a-r1.md`, head 2234e7196) under the hub's
rulings. It was built from 2234e7196 in a detached worktree, starting at 19:37:56 UTC. No Claude
CLI ran. The Claude Code 2.1.318 binary was read (sha256 `677a2ea7…5e7d`, as before) only to
extract its matcher. Line numbers below are this round's head unless marked otherwise.

### P2 1: a repository's own git config could move R

**Cause.** `job_startup_settings` took `git rev-parse --show-toplevel` as R without checking it,
and a checkout's own config decides that answer:

- With `core.worktree /` in `.git/config`, git answered `/`. The negation became `!/**`, which
  spares every path.
- A `.git` file whose `gitdir:` names another repository hands git that repository's config. In a
  probe here, git 2.55.0 followed it and answered that repository's `core.worktree`, a folder
  outside the workdir.

**Change.** `_job_root` (`subfleet/adapters/claude.py:412`) now picks R:

- Git's answer is R only when it is not `/` and the workdir's real folder is that folder or lies
  under it. Otherwise R is `abspath(workdir)`, as on any git failure.
  - "Not `/`" is judged on the folder: `samestat` with `/`, so `//` or `/private/..` count too.
  - "Is or lies under" compares `realpath(workdir)` and each of its parents with `realpath(R)` by
    `samestat`, not by string.
- The one `rev-parse` runs with `core.fsmonitor=false` pinned:
  - `pin_git_config` (`:394`) appends `JOB_STARTUP_GIT_CONFIG` (`:156`) through
    `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`, after the entries an existing
    count names, and keeps those entries.
  - A count git would refuse is replaced by the pin's own count. Git 2.55.0 refused
    `GIT_CONFIG_COUNT=abc` ("bogus count") and a count with a missing key ("missing config key"),
    so the call failed either way.
  - The environment is `_git_env(None)`, the one the call had before, plus the pin;
    `_git_bytes` applies `_git_env` again, which changes nothing.
- `pin_git_config` joins `__all__` beside `glob_literal` and `job_startup_settings` (5c7c1de60).

**Why folders, not strings.** The ruling says "`realpath(workdir)` equals `realpath(R)` or lies
under it". Python's `realpath` keeps the case and Unicode form it is given (checked here: an
NFC-spelled path to an NFD folder comes back NFC, and an upper-case spelling stays upper-case).
Git, though, spells the folder as it is stored. A string comparison would make a workdir given in
another case, or in NFC for an NFD folder, fall back to the workdir. That would undo the review's
"lower case" row and half of P2 2. Comparing by `samestat` gives the ruling's meaning for every
spelling.

**Tests** (`tests/unit/test_lane_startup.py`):

| Test | Before (2234e7196) | After |
|---|---|---|
| `test_r_is_the_workdir_when_the_checkouts_config_moves_git_off_it[root]`: the review's `core.worktree /` case; git answers `/` | failed: `'!/**' in excludes` | passed |
| `…[sibling]`: `core.worktree` names a sibling folder | failed: the negation spared the sibling | passed |
| `test_r_is_the_workdir_when_a_gitdir_file_points_git_at_a_tree_elsewhere` | failed: the negation spared `elsewhere-tree` | passed |
| `test_the_git_call_pins_core_fsmonitor_off`: a stub git records the `GIT_CONFIG_*` names it was given and what the real git reads as `core.fsmonitor` there, against a checkout whose config names a marker hook | failed: no pin; `fsmonitor=<the hook>` | passed: `COUNT=1`, `KEY_0=core.fsmonitor`, `VALUE_0=false`, `fsmonitor=false`; the marker never ran |
| `test_the_pin_is_appended_after_config_the_environment_already_counts`: a count of 2 already set | failed: count stayed 2 | passed: entries 0 and 1 kept, the pin at 2, count 3 |
| `test_r_may_be_an_ancestor_that_git_names_and_that_holds_the_workdir` | passed | passed |
| `test_r_is_gits_answer_when_the_workdir_is_spelled_otherwise` | passed | passed |

The last two rows pin behaviour that should not change, so they pass on both sides. The first
pins the ruling's boundary: an ancestor that holds the workdir is accepted. The second guards
against a string comparison; it skips on a case-sensitive file system.

### P2 2: a repository stored in NFD lost its own guidance

**Cause.** Claude Code 2.1.318 normalizes its cwd to NFC (review: `Rn`, applied to
`originalCwd`) and builds every path of its startup walk from that. Git and `realpath` return the
name as stored. For a folder created as `café` in NFD, R and R_real were NFD, so the negation
spared only the NFD spelling. Every path Claude Code built (NFC) fell outside it, so the
repository's own `CLAUDE.md`, `AGENTS.md` and `sub/CLAUDE.md` were excluded. This fails closed:
no personal file was added.

**Change.** R's spellings are now `R`, `R_real`, `NFC(R)` and `NFC(R_real)`, de-duplicated in that
order (`claude.py:466-469`). Both the ancestor list and the negation use them, and the stored forms
stay. For an ASCII or already-NFC path the four collapse to the two there were, so the patterns
are unchanged. The re-recorded main fixture is byte-identical, below.

**Tests:**

| Test or check | Before | After |
|---|---|---|
| `test_i3_a_repository_stored_in_nfd_keeps_its_own_guidance[as-stored]`: a real repository folder `café` in NFD, workdir `café/sub`, through `job_startup_settings`, the I2/I3 checks and the oracle, with Claude Code's cwd taken as `NFC(os.getcwd())` | failed: the negation lacked the NFC spelling | passed |
| `…[nfc-spelled]`: the same folder given to the job in NFC | failed | passed (git answers NFD; R is accepted by `samestat`) |
| `test_lane_startup_glob.py::test_job_startup_settings_still_writes_the_recorded_patterns[n000…n011]` over the new NFD recording | 12 failed | 12 passed |
| `…::test_the_oracle_gives_the_bundles_verdict_on_every_recorded_path[n000…n011]`, `…::test_i3_by_the_bundles_own_verdicts[n000…n011]`, `…::test_the_nfd_recording_asks_for_each_roots_nfc_spelling_and_the_bundle_keeps_it` | passed | passed |
| `nfd_bundle_check.py`: the review's real-folder check, with the bundle's matcher | rc 1: the NFD repository kept 0 of 4 own files in each of its 4 cases; the ASCII control kept 4 of 4 | rc 0: 4 of 4 kept in all 6 cases; 7 of 7 outside and personal paths excluded in each |

The replay tests in the fourth row judge the recording, not the code, so they pass on both sides.

The bundle's matcher and the evidence:

- **Recorder.** `tools/claude_md_glob_record.py --nfd` puts one decomposed letter in every
  repository folder name: an accent (`e`+U+0301 and kin), the singleton U+212B, or conjoining
  Hangul jamo. It asks for each root's files, mutations and ancestors in the NFC spelling as well.
  Without `--nfd` it makes the same random calls as before.
- **NFD fixture.** `tests/fixtures/lane_startup/picomatch-2.1.318-nfd.json` (`--sets 12 --seed
  20261115 --nfd`) has 1,432 paths, and the oracle and the bundle agree on all of them.
  - With the old patterns, the bundle excluded 144 of the 320 root files: every NFC spelling.
  - With the new ones it keeps all 320.
- **Main fixture.** Re-recorded with this tree and the default mode (`--sets 18 --seed 20261115`),
  it is byte-identical to the committed `picomatch-2.1.318.json`: 1,313 paths, all agreeing.
- **Seeds 1-6.** With `--nfd` and 60 sets each, they give 42,728 paths, all agreeing between the
  oracle and the bundle, with no `FWo` mismatch.
  - Of 9,600 root-file paths the bundle kept 9,598.
  - The other two (seeds 2 and 3) are random subfolder names, `.\␉#^` and `#:[\.`. Once `\` is
    read as `/`, each holds a `.` segment, and picomatch's `/**` with `dot: true` never matches
    one. This is not about NFD; see residue 3.
- **Real folders.** `docs/reports/2026-11-15-lane-startup-build-data/nfd_bundle_check.py` makes a
  repository `café` in NFD (with `CLAUDE.md`, `AGENTS.md` and `sub/CLAUDE.md`) and an ASCII
  control `cafe`. It takes patterns from the tree's `job_startup_settings` for each workdir (the
  top and `sub`, given as stored and in NFC). Node then gives `process.cwd()`, which was NFD, and
  normalizes it to NFC. Paths built from that cwd go to the bundle's matcher, with `FWo` reading
  the real disk.

### P2 3: the #310 merge needs more than the report said

Not merged here, as ruled.

- **The report.** The statements at the old `:269` and `:272` under "Lines #310 also touches" are
  replaced with the review's measurements:
  - conflicts at `claude.py:1510` and `:1562` (now `:1565` and `:1617`), plus the contract's
    change log;
  - 181 failures, of which 179 are this PR's recorded cases (154 + 24 + 1), and 2 are #310's
    `test_effort_env.py` property tests, which need `without_job_startup`;
  - the hub's order: #310 first, then this PR merges release/217 and re-records
    `argv-adf66f2e5.json` from a base that includes #310.
- **The PR body.** Its corrected text is in `docs/reports/2026-11-15-lane-startup-build-data/
  pr-326-body-fix-r1.md`, ready for `gh pr edit 326 --body-file`. That replaces the one #310
  bullet with a "Merging with #310" section, and describes R's check, the pin and the NFC
  spellings. I did not edit the PR on GitHub: that writes outside this job's workspace (see
  "Deviations").

### C-12.9 and its change-log line

- **The passage (`docs/acceptance-contract.md:607`).** It now lists the ancestors and the negation
  over every spelling of R. R is git's answer, run with `core.fsmonitor=false` appended through
  `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` and capped at 10 s, only when it
  is not `/` and is the workdir's real folder or holds it, compared as folders. It names
  `core.worktree` and a `gitdir:` file as the reason. R's spellings are R, its real path and the
  NFC form of each.
- **The change-log line (`:9`).** It gains "#326 review r1" in its references, and its root clause
  now reads "which is git's top-level folder only when that folder holds the workdir and is not
  `/`, and is spared in its NFC spelling too".
- **Commit.** 99f6bca5b.

### Open residues

1. **Unverified, from the review.** The AGENTS.md plugin resolves `~` paths (`_7s`, @299541146).
   If personal settings configure extra instruction paths that it reads outside `yV`, the excludes
   won't apply. I did not read personal settings to settle it, as ruled.
2. **R may still be moved up.** A checkout's config can still make R a proper ancestor of the
   workdir (not `/`), as the ruling allows. The folders between it and the workdir then load as the
   checkout's; `$HOME`'s and `C`'s files stay excluded by name.
   `test_r_may_be_an_ancestor_that_git_names_and_that_holds_the_workdir` pins this.
3. **A `.` segment after the fold.** A repository subfolder whose name holds a `.` or `..` segment
   once `\` reads as `/` (`.\x`, `x\..`) loses its nested files: picomatch's `/**` refuses such a
   segment. This fails closed and predates this round; it is the mirror of the build's residue 2.
4. **`$HOME` and `CLAUDE_CONFIG_DIR` in NFD.** They get no NFC spelling, which is outside the
   ruling. When R lies under `$HOME`, the NFC ancestors of R cover `$HOME`'s six names. Otherwise,
   how Claude Code spells home paths was not checked.
5. **A workdir of `/` itself.** R is the workdir, so the negation is `!/**`, as before this round.
   A quick search of `subfleet/` found no submit-time refusal of `/`. It was not tested and is not
   in this round's rulings.
6. **File systems that tell NFC from NFD apart.** There, `NFC(R)/**` would also spare a separate
   folder spelled in NFC. The daemon runs on APFS, which doesn't tell them apart.
7. **A stalled git call, from the review (not run).** An `include.path` pointing at a FIFO still
   stalls the call to its 10 s cap. The pin does not change that.

### Commands and results

Unless the row says otherwise, every test command was `uv run --no-sync --with
'pytest-xdist>=3.6' python -m pytest … -q -p no:cacheprovider -n 4 --basetemp=$S/pytest-<label>`.
`TMPDIR` was `$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR` was `$TMPDIR/uv-cache`, and
`PYTHONPATH` was unset. `S` is `$TMPDIR/sf-lsa-fix1`. The times are UTC, and the loads are
`uptime`'s one-minute figure. Outputs are in
`docs/reports/2026-11-15-lane-startup-build-data/fix-r1-evidence.log`.

| Run | Command | Start → end (load) | Result |
|---|---|---|---|
| setup | `uv sync --group dev` | about 20:04 (45 s) | installed into the worktree's `.venv` |
| before-1 | the 9 new tests (`-k`), on the unfixed tree | 20:15:19 → 20:17:13 (121) | 7 failed, 2 passed (the two pinning tests), 77 s |
| before-2, before-3 | the same, with `--tb=line` (before-3 without the two pinning tests) | about 20:18 → 20:24 | the same 7 failures, each on the assertion named above |
| bundle before | `HOME=$S/fakehome .venv/bin/python -I nfd_bundle_check.py <worktree> <binary> $S/nfd-before2` (run twice; the second after the script learned to print whether node's raw cwd was NFD) | 20:27:35, 20:28 | rc 1 both times, as in the table above |
| record before | `.venv/bin/python -I tools/claude_md_glob_record.py <binary> … --sets 12 --seed 20261115 --nfd` | 20:30:44 → 20:32:35 | 1,432 agree; `keep/True=144` |
| after-1 | both lane-startup modules, fixed tree, before the glob changes | 20:38:02 → 20:45:55 (167) | 357 passed, 411 s |
| record fixture | the same recorder, writing `tests/fixtures/lane_startup/picomatch-2.1.318-nfd.json` | 20:46:23 → 20:46:53 | 1,432 agree; `keep/False=320` |
| record main | the recorder, `--sets 18 --seed 20261115`, to scratch | 20:47:25 → 20:48:01 | 1,313 agree; `cmp` byte-identical to the committed fixture |
| bundle after | `nfd_bundle_check.py` | 20:49 | rc 0, "all ok" |
| seeds | the recorder, `--sets 60 --seed 1..6 --nfd` | 20:50:22 → about 20:58 | 7,032 + 7,136 + 7,231 + 7,194 + 7,127 + 7,008 paths, all agree, 0 `FWo` mismatches |
| prefix | the new test files and NFD fixture copied over a `git archive 2234e7196 subfleet tests tools pyproject.toml`, run there (imports checked to resolve to the copy); the glob module and the 9 new tests | 21:05:35 → 21:09:32 | 19 failed (12 `[n0xx]` pattern cases and 7 lane-startup tests), 99 passed, 205 s |
| **A** | the new and changed tests: `tests/unit/test_lane_startup_glob.py` whole, and in `tests/unit/test_lane_startup.py` the 9 new tests plus the I2/I3/payload tests whose helpers changed (`-k`) | 21:14:26 → 21:17:05 (193 → 119) | **135 passed**, 113 s |
| **B** | `tests/unit/test_lane_startup.py tests/unit/test_lane_startup_glob.py tests/unit/test_claude_adapter.py` | 21:17:36 → 21:20:43 (129 → 125) | **566 passed**, 158 s |
| **C** | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | 21:21:01 → 21:25:36 (124 → 92) | **435 passed**, 260 s |
| D | after adding `pin_git_config` to `__all__`: an import check that every `__all__` name exists, then `-k "pin or module"` over `test_lane_startup.py test_test_module_names.py` | 21:27:39 → 21:28:24 | exports ok; 3 passed |

**Unchanged paths.** Run B holds the recorded fixture `argv-adf66f2e5.json`, which this round
does not touch:

- `test_i5_i6_every_other_launch_is_byte_for_byte_adf66f2e5s`: 189 read-only launches, isolated
  reviews, turns, probes and gates, byte for byte;
- `test_the_grid_is_the_recording`;
- `test_a_writable_job_changes_only_by_the_startup_keys`: 48 writable cases, equal once the keys
  are taken out.

All passed.

**Commits:** 08acfbaa3 (adapter and its tests), 6e080e03c (NFD recorder mode, fixture, glob tests
and the bundle check), 99f6bca5b (C-12.9), 5c7c1de60 (`__all__`), then this section and its
evidence.

### What did not run

- **Wider runs.** The whole suite (CI runs it), any Claude CLI, the #310 merge, and a re-run of the
  build's mutant runner against the new code.
- **Runs A-C** ran before 5c7c1de60, which only adds a name to `__all__`. Run D and the import
  check cover it.
- **The PR body** was not changed on GitHub.

### Deviations from the brief

1. **Folder comparison.** R is checked by folder identity (`samestat`), not by comparing
   `realpath` strings. The reason is under P2 1.
2. **The PR body** is delivered as a file, not edited: `gh pr edit` writes outside the job's
   workspace.
3. **One commit for two findings.** P2 1 and P2 2 share one adapter commit (08acfbaa3), since both
   change `job_startup_settings`'s R and its test helpers. The recording, the contract, the export
   and the report are separate commits.
4. **Run A** is the new and changed tests chosen by `-k`, as the table says, not two whole files.
   Run B then runs both lane-startup files whole.
5. **No delegation.** I did the work alone, with no in-session workflow or subagents: the standing
   rule sends delegation through Subfleet, and this brief forbids running `subfleet`.
6. **Git and the harness.** Plain `git status` hung on the untracked scan, so I used
   `--untracked-files=no`. Three commands passed the Bash tool's 120 s default and the harness
   moved them to the background; I stopped each with TaskStop:
   - the opening `date; git status` (the date had printed);
   - a recursive `grep` over `subfleet tests docs`, replaced by `git grep`;
   - a `git diff --stat` chained after the recorder edit (the edit had completed).

   No process from this job is left running. The first `git archive` for the prefix copy hit its
   100 s `timeout` and left a partial tree. I removed it with the guarded `rmtree` and archived
   only `subfleet tests tools pyproject.toml`.

### Housekeeping

- **Scratch.** Everything this round wrote outside the worktree is under `$TMPDIR/sf-lsa-fix1`:
  - the probe repositories;
  - the fake home;
  - the pytest basetemps `pytest-before1..3`, `pytest-after1`, `pytest-prefix`, `pytest-runA..D`
    and `pytest-collect`;
  - the prefix tree;
  - the NFD real-folder trees `nfd-before`, `nfd-before2` and `nfd-after`;
  - the recordings and seed outputs, with their extracted matcher modules (third-party code, not
    committed);
  - the evidence files.

  It held 87 MB. I removed it at 21:35:56 UTC with a `shutil.rmtree` that asserts the path's last
  part is `sf-lsa-fix1`, after checking that no process named it, and confirmed afterwards that it
  no longer exists.
- **Outside the scratch.** The worktree's `.venv`, from `uv sync`, is inside the workspace and git
  ignores it. I left it, since the guard can't name it.
- **Shared folders.** The shared `$TMPDIR/uv-cache` predates this round and stays. The temp root
  had 1,425 entries changed since my start, from other sessions (`.com.openai.codex.*`, `.tmp*`
  and more). I can't attribute any to this round, and I removed none.

## Fix round 2

This round fixes review r2's findings
(`~/reviews/subfleet-hub/lane-startup-2026-11-14/review-a-r2.md`, head d929336a8) under the hub's
rulings: two P1s, two P2s and the P3. It was built from d929336a8 in a detached worktree, starting
at 04:10:12 UTC. No Claude CLI ran. The Claude Code 2.1.318 binary (sha256 `677a2ea7…5e7d`) was
read only to extract its matcher and to read how its memory walk calls it. Line numbers are this
round's head.

### P1 1: ancestor exclusions could stop a launch with `E2BIG`

**Cause.** `job_startup_settings` listed the six names under every ancestor of each spelling of
R, each ancestor written in full. Ancestor k is about k folders long, so the patterns grew with the
square of R's depth. The review's repository, 400 one-character folders deep (an 839-byte path),
gave a 1,096,385-byte `--settings`, past macOS's 1,048,576-byte `ARG_MAX`.

**Change.**
- `_ancestor_glob` (`subfleet/adapters/claude.py:468`) writes each spelling's ancestors as one
  nested pattern that names each folder once: `/a/b/c/R` gives `/a{,/b{,/c}}`. The six names are
  appended to it, as before to each ancestor. picomatch compiles `{,x}` to `(|x)` and nests braces;
  `glob_literal` still brackets every brace and comma inside a name. The size is now linear in R's
  length.
- `$HOME` and C keep their explicit entries. The negation is unchanged and still last.
- `JOB_STARTUP_SETTINGS_MAX_BYTES` (`:164`) is 256 KiB. A payload past it refuses the launch with
  `AdapterError` reason `claude-startup-size` (`:637-643`, `subfleet/refusals.py:179`). No legal
  path alone reaches it (see the sizes below); a repository with many personal links could.

**Sizes**, each measured on the payload that goes to `--settings`
(`deep_bundle_check.py`, synthetic paths; the 400-deep case also on a real repository in
`test_the_payload_for_a_repository_400_folders_deep_runs`):

| Case | Path | Spellings of R | Before | After |
|---|---|---|---|---|
| review: 400 folders `x` | 839 bytes | 1 | 1,096,385 bytes (review) | 13,580 bytes, 15 patterns |
| 511 folders `x` at `PATH_MAX` | 1,022 bytes | 2 | not measured | 33,255 bytes |
| folders `"` (7 JSON bytes per escaped `"`) | 1,022 | 2 | not measured | 76,065 |
| folders `,` | 1,022 | 2 | not measured | 61,795 |
| folders `\x01` (JSON `\u0001`) | 1,022 | 2 | not measured | 68,930 |
| folders `e`+U+0301+`\x01` | 1,020 | 4 | not measured | 92,259 |
| folders `e`+U+0301+`"` | 1,020 | 4 | not measured | 97,947 |
| folders `{` | 1,022 | 2 | not measured | 47,525 |
| folders `名` | 1,020 | 2 | not measured | 34,693 |

The largest is 97,947 bytes, under a tenth of `ARG_MAX` and under the 128 KiB that
`test_the_payload_at_path_max_stays_far_under_arg_max` asserts.

**The bundle's verdicts at depth.** `docs/reports/2026-11-15-lane-startup-build-data/
deep_bundle_check.py` gives the nine cases above to the extracted picomatch and `FWo` under node.
For every spelling it judges every ancestor's six names (expected excluded), R's own files
(expected kept) and decoys beside R (expected excluded): 46,041 paths, 0 differ ("all ok"). V8
compiles the 511-deep nesting.

**Cost in the bundle.** Claude Code's memory walk builds the matcher once per walk
(`Ee=R8t()`, where `R8t` memoizes `v8t()`). Under load 110-150, building the matcher and judging
five paths cold took:

| Depth | New: build + first five | Old: build + first five |
|---|---|---|
| 12 (the first case run, so it also warms picomatch) | 297 + 14 ms | 82 + 1 ms |
| 100 | 1 + 271 ms | 685 + 1,600 ms |
| 200 | 2 + 747 ms | 1,337 + 1,396 ms |
| 400 | 82 + 1,582 ms | 4,533 + 6,305 ms |

Warm, five paths took under 40 ms in every new case. At depth 12 the first-case warm-up dominates.

**Tests** (`tests/unit/test_lane_startup.py`):

| Test | Before (d929336a8) | After |
|---|---|---|
| `test_the_payload_for_a_repository_400_folders_deep_runs` (`:708`): a real repository 400 folders below the test's temp folder; I2, I3, and the launch's own argv run with `/usr/bin/true` | failed: payload past 64 KiB | passed |
| `test_the_payload_grows_linearly_with_depth` (`:728`): 400 deep against 100 deep | failed: ratio about 16 | passed: under 4.5 |
| `test_the_payload_at_path_max_stays_far_under_arg_max[plain\|quote\|comma\|control\|nfd-control\|nfd-quote\|cjk]` (`:748`) | 7 failed: payload past 128 KiB | 7 passed |
| `test_a_payload_past_the_cap_is_refused` (`:780`) | failed: no cap | passed: `claude-startup-size` |

**Test tools.**
- `tests/claude_md_glob.py:108` reads nested braces with empty alternatives, as picomatch does;
  `expand` (`:184`) lists the brace-free patterns one names. Python's `re` cannot nest about 500
  groups, so the `PATH_MAX` test compares the paths the patterns name, and the bundle check
  above gives the matcher's verdicts there.
- `_chain` (`test_lane_startup.py:174`) builds the nested pattern independently of the adapter.
  `_check_i2` (`:188`) checks it, and checks through the oracle that the listed entries alone
  exclude every ancestor's six names.
- `tests/unit/test_lane_startup_glob.py:139` pins the oracle's reading of nested braces. The
  refusal cases trade the nested brace, now stated, for a nested brace with one alternative and an
  unclosed one.
- Both fixtures were re-recorded with this tree (commands below). They hold the same paths,
  expectations and bundle verdicts as before (1,313 and 1,432, all agreeing with the oracle, 0
  `FWo` mismatches). Only `patterns` and `fwo` changed: 718 → 316 patterns (32,293 → 17,780 bytes)
  and 678 → 282 (38,920 → 20,290). The verdict tallies by label are the build's.

### P1 2: personal instructions loaded through a link inside R

**Cause.** Claude Code matches an instruction file by the path it built, and its walk builds paths
from the workdir. `repo/CLAUDE.md -> ~/.claude/CLAUDE.md` is spelled under R, so the negation
spared it, and `<C>/CLAUDE.md` excluded only the personal spelling. The same held for a linked
folder and for any chain of links.

**Change.** `_linked_startup_files` (`:510`) finds the instruction files inside R whose real file
is personal. `job_startup_settings` excludes each one in every spelling of R, and in the NFC form
of its relative path (`:630-632`), before the negation. A linked folder is excluded as
`<path>/**`.
- A file is personal when its real path lies under a config home C, when it is `<C>/CLAUDE.md` or
  one of `$HOME`'s `CLAUDE.md`, `CLAUDE.local.md` and `AGENTS.md` by file identity (`samestat`, so
  a hard link counts), or when its real path lies outside R. Folders are compared by identity
  (`_below`, `_inside`, `:488`, `:505`).
- Where it looks:
  - the five single-file names in every folder from R to the workdir, in the workdir's real
    and logical spellings;
  - everything under each of those folders' `.claude/rules`, or the whole rules folder when the
    folder itself is personal;
  - when git named R, every symbolic link git tracks (`git ls-files -s -z`, mode `120000`), run
    through `_job_git` (`:421`) with the same pin and 10 s cap. These are the files Claude Code
    reads when a session touches a subfolder.
- `_job_root` (`:429`) now also returns whether git named R. A failed `ls-files` skips the
  tracked-link pass; the start-up files are still checked.

**Judgment.** The ruling says "a personal instruction file (or lies under the personal Claude
folder)". I read "personal" as "a file the excludes refuse by its real spelling": under C, one of
the named personal files, or anywhere outside R. So a link from R to another folder outside R is
excluded too, which fails closed. A file of R that is not a link is personal only when R overlaps
C or `$HOME`. A workdir that is itself a link to `~/.claude` makes R's own files personal.

**Tests** (`tests/unit/test_lane_startup.py`; each judges every spelling of R with the oracle):

| Test | Before | After |
|---|---|---|
| `test_a_direct_link_to_the_personal_file_is_excluded` (`:811`): the review's `repo/CLAUDE.md -> ~/.claude/CLAUDE.md`; exactly that path is added, once per spelling | failed: `CLAUDE.md` kept | passed |
| `test_a_linked_folder_into_the_personal_folder_is_excluded` (`:829`): `repo/.claude -> ~/.claude`, a tracked `repo/docs -> ~/.claude`, and `sub/.claude -> ~/.claude` on a subfolder workdir's chain | failed: 7 personal paths kept | passed |
| `test_a_chain_of_two_links_to_a_personal_file_is_excluded` (`:851`): `AGENTS.md -> ../hop-1 -> hop-2 -> ~/AGENTS.md`, and `CLAUDE.local.md -> notes/local.md -> ~/.claude/CLAUDE.md` | failed | passed |
| `test_a_link_out_of_r_and_a_hard_link_to_a_personal_file_are_excluded` (`:887`) | failed | passed |
| `test_a_workdir_that_is_a_link_to_the_personal_folder_excludes_its_files` (`:907`) | failed | passed |
| `test_links_that_stay_inside_r_keep_loading` (`:869`): `CLAUDE.md -> AGENTS.md`, rules linked within R | failed, but only on the new `_check_i2`'s nested-pattern check; its verdicts held | passed |

The bundle's matcher was not run on the link cases. These patterns are plain `glob_literal`
paths, a form the recordings already hold.

### P2: `tests/fake/test_prompt_seam.py` picks the guardian

**Cause.** The fixture's fake `Popen` records every subprocess. A writable Claude launch now runs
`git rev-parse` (and `git ls-files`) through `subprocess.run` beside the guardian, so
`command, = [...]` met two commands.

**Change** (`:80`). The test takes the one command whose `argv[1:3]` is `-m subfleet.guardian`,
and asserts that every other recorded command is git.

**Test.** The old test file against this round's adapter: 2 failed
(`[workspace-write-True-haiku]`, `[workspace-write-False-haiku]`: "too many values to unpack
(expected 1, got 2)"), 6 passed. The new file: 8 passed (run A).

### P2: an R whose real folder is `/` is refused

**Cause.** `_job_root` refused git's `/`, but its fallback, `abspath(workdir)`, was not checked.
A workdir of `/`, `//` or a link to `/` gave a negation that spared every path.

**Change.** `job_startup_settings` refuses any R whose real folder is `/` (`_is_root`, `:459`, by
`samestat`), whichever way R was chosen. The launch fails with `AdapterError` reason
`claude-startup-root` (`:616-619`): "a writable Claude launch keeps personal startup files out only
below /, and this job's repository root is / (C-12.9)", fix "run the job in a folder below /". The
reason and its text are in `subfleet/refusals.py:177`.

**Test.** `test_a_root_of_slash_is_refused[/|//|link]` (`:925`) checks `job_startup_settings` and
`build_launch`, and that `refusals.render` gives the same text. Before: 3 failed ("DID NOT RAISE").
After: 3 passed.

### P3: the PR body

`docs/reports/2026-11-15-lane-startup-build-data/pr-326-body-fix-r1.md` now describes the nested
ancestor pattern, the link rule, both refusals, the sizes, the bundle check and the limits. Its
test paragraph gains this round's counts. It is not applied on GitHub.

### C-12.9 and its change-log line (ad26853f6)

- **The passage** (`docs/acceptance-contract.md:607`) now says:
  - each spelling's ancestors are written as one pattern that names each folder once;
  - which files inside R are excluded as personal, and where they are looked for;
  - why listing ancestors in full grew past `ARG_MAX`, and that a `PATH_MAX` path gives under
    100 KB;
  - the `claude-startup-size` refusal past 256 KiB;
  - the `claude-startup-root` refusal for an R whose real folder is `/`.
- **The change-log line** (`:9`) cites "#326 reviews r1 and r2" and adds the personal files
  inside R, the linear payload and the `/` refusal.

### Open residues

1. **Untracked links below the workdir.** Claude Code reads a subfolder's instruction files only
   when a session touches that folder. Links there are found only when git tracks them; an
   untracked one (in an `--in-place` checkout, say) is not looked for. Links on the start-up
   chain are found whether tracked or not. A job worktree is a fresh checkout, so every link in it
   is tracked at launch.
2. **`@` imports.** A repository `CLAUDE.md` that imports `@~/.claude/CLAUDE.md` was not examined;
   whether Claude Code applies `claudeMdExcludes` to imports was not read.
3. **Claude Code's rules loader.** It checks a rules entry's logical path when that differs from
   its real one (`if(_n!==Nt&&x8t(_n,n,Y))continue`). The excludes name both spellings, so this
   round relies on neither.
4. **Cost at extreme depth.** With 400 folders, building and first matching took about 1.7 s
   under load 110-150, still less than the old patterns at that depth.
5. **Build round residues 1-5 and fix round 1 residues 1-4, 6 and 7 stand.** Fix round 1's
   residue 5 (a workdir of `/`) is fixed here.

### Commands and results

Unless the row says otherwise, every test command was `uv run --no-sync --with
'pytest-xdist>=3.6' python -m pytest … -q -p no:cacheprovider -n 4 --basetemp=$S/pytest-<label>`,
inside `timeout 570`. `TMPDIR` was `$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR` was
`$TMPDIR/uv-cache`, and `PYTHONPATH` was unset. `S` is `$TMPDIR/sf-lsa-fix2`. Times are UTC and
loads are `uptime`'s one-minute figure. The tails are in
`docs/reports/2026-11-15-lane-startup-build-data/fix-r2-evidence.log`.

| Run | Command | Start → end (load) | Result |
|---|---|---|---|
| setup | `uv sync --group dev` | about 04:20 | installed into the worktree's `.venv` |
| extract | `.venv/bin/python -I tools/claude_bundle_picomatch.py <binary> $S/node/pm.cjs`, then node: `makeRe` of nested braces | about 04:22 | `^(?:\/a(\|\/b(\|\/c))\/CLAUDE\.md)$`; verdicts as expected |
| sizes | the adapter on synthetic paths (realpath mapped, git not run) | about 04:40 | the table under P1 1 |
| dev1 | the new tests (`-k`), fixed tree | 04:45:02 → 04:53:25 (113) | 4 failed, 14 passed: `re`'s nesting limit at `PATH_MAX`, and one wrong expectation; both fixed in the tests |
| deep | `.venv/bin/python -I deep_bundle_check.py <worktree> <binary> $S/deep` | 04:55:44 → 05:00:08 | rc 0, "all ok": 46,041 paths, 0 differ |
| timing | node: compile and first-match time, old patterns against new | about 05:01-05:05 (110-175) | the cost table under P1 1 |
| dev2 | the new tests and `test_prompt_seam.py` (`-k`) | 05:06:43 → 05:10:24 (177) | 1 failed (an expectation about `rules/p.md` under R's spelling, which Claude Code never reads), 31 passed |
| record | `tools/claude_md_glob_record.py <binary> … --sets 18 --seed 20261115`, then `--sets 12 --seed 20261115 --nfd` | 05:12:44 → 05:13:33 | 1,313 agree; 1,432 agree; 0 `FWo` mismatches |
| before | the new tests (`-k "400 or linearly or path_max or past_the_cap or link or slash or c6_7"`) over `git archive d929336a8 subfleet tests pyproject.toml`, with the new test module, oracle and seam test copied in, run from that copy (imports checked to resolve there) | 05:21:08 → 05:23:31 (146) | **21 failed, 11 passed**, 97 s. The 21 are every new case above, plus `test_i2_i3_each_fixture_workdir[symlinked]` and `[linked-worktree]` (the new `_check_i2`). The 11 passed are the 8 seam cases (whose fix is in the test), `test_i3_a_git_spelling_through_a_symlink_spares_both`, and `test_the_payload_is_valid_json_at_every_fixture[symlinked\|linked-worktree]` |
| seam before | that copy with this round's `claude.py` and `refusals.py` and d929336a8's `test_prompt_seam.py` | 05:25:54 → 05:28:00 | **2 failed, 6 passed**: the two writable-haiku cases |
| **A** | `tests/unit/test_lane_startup.py tests/unit/test_lane_startup_glob.py tests/fake/test_prompt_seam.py` | 05:29:15 → 05:36:35 (105 → 94) | **423 passed**, 344.5 s |
| B1 | Run B files 1-12 (the e2e, fake and process files, then `test_agent_secret_credentials` … `test_claude_cards`) | 05:38:37 → 05:47:08 (100) | **513 passed**, 493.0 s |
| B2 | files 13-24 (`test_claude_conversation_mcp_scope` … `test_empty_provider_results`) | 05:47:21 → 05:55:35 (79) | **815 passed**, 484.9 s |
| B3 | files 25-36 (`test_enrollment_runner` … `test_launch_env_round7`, with `test_handler_refusals`) | 05:56:29 → 06:05:21 (79) | **1,881 passed**, 513.0 s |
| B4 | files 37-48 (`test_limit_from_model_text` … `test_round10_refusal_context`) | 06:06:58 → 06:16:33 (54) | **stopped by its 570 s `timeout`** at about 97%; no failure in the captured tail (36% on) |
| B5 | files 49-60 (`test_scoped_stream_capacity` … `test_timers_busy_read`) | 06:17:16 → 06:26:49 (118) | **stopped by its 570 s `timeout`** at about 55%; no failure in the output |
| **C** | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | 06:27:09 → 06:29:37 (19 → 52) | **439 passed**, 134.7 s. Round 1 had 435; the 4 more are two parametrized handler-refusal tests over the two new reasons |
| **B4 again** | the same 12 files, `timeout 560` | 06:31:03 → 06:33:19 (70 → 59) | **1,200 passed**, 134.0 s |
| **B5 again** | the same 12 files, `timeout 560` | 06:33:27 → 06:39:43 (56 → 32) | **860 passed**, 373.7 s |

Run B is every test file that imports a module this round changed: `git grep -lP` over `tests/*.py`
for imports of `subfleet.adapters.claude`, `subfleet.refusals` and `tests.claude_md_glob`. That
gave 70 files. Five are helpers, not test files: `tests/conftest.py`, `tests/fake/profile.py`,
`tests/fixtures/home_resolvers_release_217.py`, `tests/lane_settings_cases.py` and
`tests/task_hold_cas_legacy.py`. Run A held three. That leaves 62 test files, of which Run B ran
60; the other two are under "What did not run". Run B finished with **5,269 passed** (B1-B3 and
the second B4 and B5), and none failed in any run.

### What did not run

- **Two importers.** `tests/live/test_claude_live.py` needs a real Claude CLI.
  `tests/unit/test_claude_v1_parity.py` reads `~/chief-of-staff/subfleet/bin/subfleet-claude` at
  this base (build report, "What did not run"), which this brief's hands-off rule forbids.
- **The whole suite**, any Claude CLI, the #310 merge, and the build's mutant runner against this
  round's code.
- **The bundle on the link cases.** The bundle's matcher judged the deep and recorded cases, not
  the link tests; the oracle judges those.
- **Claude Code's loader.** I read the bundle's `v8t`, `R8t`, `x8t` and the rules loader's check,
  and did not run the loader. That a link inside R loaded the personal text is the review's finding,
  not reproduced here.

### Deviations from the brief

1. **The personal-file rule is wider than the ruling's words.** A file inside R whose real path
   lies anywhere outside R is excluded too, not only one under C or a named personal file. The
   reason is under P1 2. It fails closed.
2. **Two refusals, not one.** Besides the ruled `/` refusal, a 256 KiB cap refuses an oversized
   payload (`claude-startup-size`). This makes "well under the limit" hold even with many personal
   links, which the path-length bound alone does not cover.
3. **A second git call.** `git ls-files -s -z` finds tracked links below the workdir. It is
   capped and pinned like `rev-parse`, so a writable launch can now spend up to 20 s on git at
   worst.
4. **The order of runs.** Runs B4 and B5 first stopped at their caps, so Run C ran before they were
   re-run. The brief's order (new tests, importers, then the four files) holds for every run that
   finished, except that Run C finished before the B4 and B5 re-runs.
5. **The PR body** is a file, not edited on GitHub, as the brief says.
6. **No delegation, no advisor.** I worked alone. One advisor call was refused as rate-limited.
   Headless mode forbids background work, so I ran no workflow.
7. **Two commands passed the Bash tool's cap and were moved to the background by the harness:**
   - the opening `date -u; pwd; git status`, while plain `git status` scanned untracked files; it
     finished in the background;
   - a scratch-size `du` at cleanup; it finished before I could stop it.

   Nothing was started with `&`, `nohup` or the like, and no process from this job is left running.

### Housekeeping

- **Scratch.** Everything this round wrote outside the worktree was under `$TMPDIR/sf-lsa-fix2`
  (567 MB):
  - the extracted matcher modules (third-party code, not committed);
  - the smoke repository and fake home;
  - the base-tree copy;
  - every pytest basetemp;
  - the recordings;
  - the deep-check work folder;
  - the run outputs.

  I removed it at 06:42:48 UTC with a `shutil.rmtree` that asserts the path's last part is
  `sf-lsa-fix2`, and checked that it no longer exists.
- **Outside the scratch.** `/tmp/sf-importers.txt` held a first importer list for a moment. I
  deleted it with `rm -f` in the next command and wrote later lists under the scratch.
  `/tmp/sf-e2e-*` matched nothing afterwards. The worktree's `.venv` (from `uv sync`) is ignored
  and stays. The shared `$TMPDIR/uv-cache` predates this round and stays.

### Commits

| Commit | Content |
|---|---|
| effa0f2af | adapter: nested ancestor patterns, linked personal files, the `/` and size refusals, the refusal texts |
| 54317c028 | tests: oracle nested braces and `expand`, the new cases, `_check_i2`, the prompt seam, `deep_bundle_check.py` |
| 1eae2f944 | the re-recorded fixtures and the glob tests |
| ad26853f6 | C-12.9 and its change-log line |
| c0c5e57f0 and this section's commit | report, PR body and evidence log |
