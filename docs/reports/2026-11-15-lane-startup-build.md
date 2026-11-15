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
the startup keys. #310's own writable-argv tests will need `without_job_startup` after a merge,
as the older ones here did.

The contract doesn't overlap: #310 edits C-12.4 and C-26.11, and this build edits C-12.9 and the
change log.

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
