# D-LANE-STARTUP1: writable Claude lanes stop loading Max's personal startup files (design)

Design job on `design/lane-startup-217`, built from release/217 at `adf66f2e5`, 2026-11-14.
No change to Subfleet code; the build is a separate job.

## Verdict

Recommend **Design A**. Writable job launches keep today's setting sources, so the
never-rules guard stays exactly where it is now. The launch's existing single
`--settings` gains four job-only keys:

- `claudeMdExcludes`: every instruction file outside the job's repository root;
- `autoMemoryEnabled: false`, plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in its `env`;
- `disableAllHooks: false`.

Read-only launches and conversation turns keep their argv byte for byte.

Three findings shape that choice:

1. Dropping the `user` setting source would not be enough by itself. Claude Code's
   startup walk treats every directory above the working directory as a project
   directory, and it reads `<dir>/.claude/CLAUDE.md` in each. For a workdir under
   `~`, that path *is* `~/.claude/CLAUDE.md`. With `user` off, nothing marks it as
   already loaded, so by the code it comes back as a "Project" file.
2. `~/AGENTS.md` comes from a built-in plugin, `cc-plugin-agents-md`. By default it
   loads ancestor `AGENTS.md` files wherever no `CLAUDE.md` is found.
3. Writable lanes also load Max's auto-memory: `MEMORY.md` from
   `~/.claude/projects/<repo>/memory/`, together with a system-prompt section telling
   the lane to write memories into `~/.claude`. This job's own context shows it.

`claudeMdExcludes` exists in 2.1.318, `--settings` can carry it, and it covers all
three paths (user memory, the ancestor walk and the AGENTS.md plugin). The guard can
ride in `--settings` (Design B), but moving it also drops every other user-settings
hook and permission rule that lanes get today, so that design needs an inventory first.

## Scope, version and method

- CLI: `claude --version` gives `2.1.318 (Claude Code)`. `claude` resolves to
  `~/.local/share/claude/versions/2.1.318`, a Mach-O Bun build whose JavaScript sits
  in the file as text. Help text was saved with `claude --help`. No model call, no
  network, and no read of `~/.claude`, `~/.claude.json` or live state.
- Quotes come from the bundle through two committed read-only tools:
  - `tools/claude_bundle_quotes.py BINARY PATTERN` prints matches with context.
  - `tools/claude_bundle_resolve.py BINARY ANCHOR NAME` follows a module's
    `import{...}from"chunk"` statements to the one module that exports that name.
    This is needed because minified names repeat across chunks: there are three
    unrelated `function Ct(){` definitions.

  `@N` below is a byte offset in the executable. Every anchor quoted was checked to
  match exactly once (`--count`).
- `docs/reference/claude-help.txt` is a 2.1.260 capture, and the `--bare` and
  `--restricted` entries changed since then. The quotes below are from 2.1.318's own
  help.

## Findings

### F1. `--setting-sources` controls instruction files as well as settings

The help text talks only about settings: `--setting-sources <sources>  Comma-separated
list of setting sources to load (user, project, local).` The code uses the same
enabled-source list to gate memory files.

- Enabled sources (`function Wo(){let e=$et()` @~258138000):
  `let t=new Set(e);t.add("flagSettings"),t.add("policySettings");let o=ri.filter((i)=>t.has(i));`
  … `function qt(e){return Wo().includes(e)}`. `$et()` returns `allowedSettingSources()`.
- Project gate (@268853840):
  `function $O(e){if(qt(e))return!0;let n=j2n();return n!==null&&n.sparesInstructionFiles&&LRe().includes(e)}`.
  `j2n()` is the workspace-restriction state, which is null on our launches.
- Startup walk (`async function WWo(`, near @268868435):
  ```
  He=qt("userSettings")?Yu():null,Ae=He===null?null:{configHome:He,claudeMd:gee("User"),rulesDir:Oht()},
  qe=$O("projectSettings"),Ke=$O("localSettings"),pt=(qe||Ke)&&!q()?ke():void 0,
  ...for(let yo=pt;yo!==void 0&&yo!==wWo(yo).root;yo=Cx(yo))bt.unshift(yo);
  ...projectFiles:bo?[Wy(yo,"CLAUDE.md"),Wy(yo,".claude","CLAUDE.md"),...gXt(yo)].filter((xo)=>!Ee(xo,"Project")):[],
     rulesDir:bo?Wy(yo,".claude","rules"):void 0,localFile:Ke&&!Ee(xn,"Local")?xn:void 0
  ```

  - `user` gates `~/.claude/CLAUDE.md` and `~/.claude/rules/`
    (`gee("User")` is `rt($e(),"CLAUDE.md")`, and `$e()` is the config home).
  - `project` gates the walk from the working directory up to, but not including,
    `/`: `CLAUDE.md`, `.claude/CLAUDE.md` and `.claude/rules/` in every ancestor.
  - `local` gates `CLAUDE.local.md`.

  Nested loads, for directories a tool touches (`TQr`, `B8t`), check
  `$O("projectSettings")`. User conditional rules (`F8t`) check `qt("userSettings")`.
- The AGENTS.md plugin's ancestor search, `$.fs.ancestors` (`async function IQr(`),
  starts with
  `if(jP()||!$O("projectSettings"))return t(\`$.fs.ancestors (${n}): project memory is off\`),[];`.

### F2. Which flag gave the read-only lane its NONE

Either flag alone gives NONE for the instruction files (CLAUDE.md and AGENTS.md).
Only `--safe-mode` also stops auto-memory.

- `--setting-sources ""` empties the enabled list, so `qt("userSettings")` and
  `$O("projectSettings")` are both false (F1). No user memory loads, there is no
  ancestor walk, and `IQr` returns `[]`. **Auto-memory is not gated by setting
  sources:** the walk adds it under `if(Jf()&&!goe()){…n6e(bo,"AutoMem")…}`, and
  `Jf()` depends only on `Lh()` and `hI()` (F8).
- `--safe-mode`:
  - The startup code sets
    `if(Yr()){if(process.env.CLAUDE_CODE_SAFE_MODE="1",process.env.CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",…`
    (@~278276783), with `function Yr(){return De(process.env.CLAUDE_CODE_SAFE_MODE)||i("--safe-mode")}`
    (@257814483).
  - Session context is built behind
    `let h=jP(),…;try{if(!h)b=mwe(await sk(e,!1,n))` (`async function t1o(`), where
    `function jP(){return Boolean(i.CLAUDE_CODE_DISABLE_CLAUDE_MDS||lr("claudeMd",{explicitlyRequested:Vl().length>0}))}`
    (@263033415).
  - So no memory file of any type loads, auto-memory included. `Lh()` also returns
    `"off"` under `Yr()`.
  - Help text: "Start with all customizations (CLAUDE.md, skills, installed plugins,
    hooks, MCP servers, …) disabled … Admin-managed (policy) settings still apply.
    Auth, model selection, built-in tools and plugins, and permissions work
    normally. Sets CLAUDE_CODE_SAFE_MODE=1."

### F3. Dropping `user` alone brings `~/.claude/CLAUDE.md` back as a Project file (for workdirs under `~`)

- `WWo` builds its ancestor list from `ke()`, the original working directory. For
  each ancestor it adds `Wy(yo,".claude","CLAUDE.md")` and `Wy(yo,".claude","rules")`.
  When `yo` is `/Users/maxghenis`, those paths are the user memory file and the user
  rules folder.
- Today `user` is on. The user file loads first as `"User"` and goes into the
  processed-path set, so the later Project copy is skipped
  (`function P8t(e,n,r,s,h){let y=cf(e);if(r.has(y)||s>=CQr)return;…`).
- With `user` off, `Ae` is null and nothing pre-seeds that set. `DN()`, the only
  other path filter, rejects only network, UNC and device paths (`rr`, `hl`, `kO`,
  `Bk`, `N2`, `ib`, `CS`, resolved in chunk-qedh1vmm). So by the code
  `~/.claude/CLAUDE.md` and `~/.claude/rules/*.md` load as "Project".
- The plugin's `IQr` *does* pre-seed both files
  (`Qe=new Set([gee("Managed"),gee("User")].map(cf))`). That is why today
  `~/.claude/CLAUDE.md` doesn't count as a project CLAUDE.md and `~/AGENTS.md`
  loads (F4).
- Second-order effect: with `user` off, the masquerading file makes the plugin judge
  "this project has a CLAUDE.md". A repository that has only `AGENTS.md` would then
  lose its own guidance (`pGr` counts a `.claude/CLAUDE.md` of kind project in any
  ancestor).
- This is read from source, not observed. Probe 2 checks it.

### F4. `~/AGENTS.md` comes from the built-in plugin `cc-plugin-agents-md`

- Registered with `fue("cc-plugin-agents-md",…)`. Gated by
  `var _1o=()=>an("tengu_agents_md_mod",y1o)` with `var y1o=!0`, so it is on by default.
- Its description: `"AGENTS.md as project instructions: by default loaded where the
  project has no CLAUDE.md; by its instructionFiles option, loaded beside CLAUDE.md,
  left out, or with the project instructions dropped"`.
- Its option: `"claude-md": CLAUDE.md only … "claude-md-or-agents-md" (default): a
  project with no CLAUDE.md of its own gets its AGENTS.md files instead, loaded
  exactly where and how CLAUDE.md would be. … "managed-only": the project's and your
  own instruction files are dropped`.
- In fallback mode it searches ancestors for `["AGENTS.md",".claude/AGENTS.md"]` with
  `fs.ancestors` (`IQr`). That search walks every ancestor of the working directory
  and reads each candidate through `yV(…,"Project",…)`.
- `var M5e=[];function gXt(e){return M5e.map(…)}` is empty and never written (no
  `M5e.push`/`splice`/`length=` in the bundle). So the core walk itself never loads
  AGENTS.md.
- Options come from `var V4=["userSettings","flagSettings","policySettings"];function dTe(t){…be(s)?.pluginConfigs…}`,
  so `--settings` can set them as
  `{"pluginConfigs":{"<plugin id>":{"options":{"instructionFiles":…}}}}`. The keys
  tried are the id and its aliases (`H_e`); the alias table maps `agents-md` to
  `cc-plugin-agents-md`.
  Neither mode helps on its own: `claude-md` also drops a repository's own AGENTS.md,
  and `managed-only` drops all project guidance (constraint 2).

### F5. Flags and variables that turn memory off

None of them turns off *only* user memory or *only* ancestor memory.

| Mechanism | What the source says it does | Usable? |
|---|---|---|
| `CLAUDE_CODE_DISABLE_CLAUDE_MDS` | `jP()` is true, so every CLAUDE.md of every type, repo included, is skipped; `TQr`, `F8t`, `fIn` and `EOn` also return `[]` | No: drops repo guidance (2) |
| `--safe-mode` | Sets the variable above plus safe mode; hooks become policy-only (F7) | No: (1), (2) |
| `--bare` (`CLAUDE_CODE_SIMPLE`) | Help: "skip hooks … CLAUDE.md auto-discovery … Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain are never read)" | No: (1), (2), (3) |
| `--restricted` | Help: "ignores user, project and local settings files … refuses bypassPermissions" | No: writable lanes need bypass |
| `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` | Adds CLAUDE.md from `--add-dir` folders (`function WZ(){return N1s()??De(i.CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD)?Vl():[]}`); this loop is not gated by `projectSettings` | Adds files; removed today only on read-only launches |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `Lh()`: truthy gives `"off"`, explicitly falsy gives `"forced_on"` | Yes, for auto-memory (F8) |
| `claudeMdExcludes` setting | F6 | Yes |

### F6. `claudeMdExcludes` exists and works through `--settings`

- Schema (@258562093): `'Glob patterns or absolute paths of CLAUDE.md files to
  exclude from loading. Patterns are matched against absolute file paths using
  picomatch. Only applies to User, Project, and Local memory types (Managed/policy
  files cannot be excluded).'`
- Matcher (@268860168):
  `function v8t(){let e=gt().claudeMdExcludes;…let r=p8t.default(n,{dot:!0});return(s,h)=>(h==="User"||h==="Project"||h==="Local")&&r(s.replaceAll("\\","/"))}`.
  `gt()` is the merged settings of the enabled sources, and `flagSettings` is always
  among them (F1).
- Coverage. `function x8t(e,n,r){if(n!=="User"&&n!=="Project"&&n!=="Local")return!1;return(r??v8t())(e,n)}`
  runs in `P8t` for every `yV` load, whether or not the caller passed a matcher. It
  therefore applies to:
  - user memory and user rules (`gke` checks each entry with `x8t(_n,n,Y)`);
  - the ancestor walk;
  - nested loads;
  - `@`-imports, because an excluded parent's imports are never read and `yV`
    returns before them;
  - the AGENTS.md plugin's `IQr`, which calls `yV(VGr(xt,mt),"Project",Qe,…)` with no
    matcher, so `v8t()` applies.

  It does not apply to `AutoMem` or `Managed`.
- `--settings` keeps the key. `fA()` strips launch-folder keys, `claudeMdExcludes`
  among them, only when `launchFolderKeysFrom(source)` isn't `"as_read"`. That
  happens only for `projectSettings`/`localSettings` in a no-terminal serve mode
  (`function hwo(e){return e!=="globalConfig"&&df.has(e)&&Ree()}` with
  `df=new Set(["projectSettings","localSettings"])`).
- Negation works. The bundled picomatch has
  `ns.compileRe=…;if(e&&e.negated===!0)w=\`^(?!${w}).*$\`` (@264294945), and `v8t`
  passes no `nonegate`. So `!<root>/**` matches every path outside `<root>`. Patterns
  in a list are ORed, so two negations, `!A/**` and `!B/**`, together match
  everything. Two spellings of one root therefore need braces: `!{A,B}/**`.

### F7. How hooks from `--settings` merge, and the `disableAllHooks` gap

- Snapshot (`function r(){let o=be("policySettings");if(o?.disableAllHooks===!0)return{};` @148801855):
  ```
  if(o?.allowManagedHooksOnly===!0||Yr())return o?.hooks??{};   // safe mode: policy hooks only
  if(ad("hooks"))return o?.hooks??{};
  let t=Sr();                                                    // merged settings of enabled sources
  if(t.disableAllHooks===!0){if(!SEr())return o?.hooks??{};return S()}
  return a(t.hooks??{})
  ...var f=["userSettings","flagSettings","policySettings"];function S(){return m(f.filter(qt).map((o)=>be(o)?.hooks))}
  ```
  `flagSettings` is always enabled (F1). So a `--settings` hook loads whatever
  `--setting-sources` names, and hook lists concatenate across levels. The hooks
  page quoted in `docs/reference/claude-hooks.md` says the same: "Hook entries merge
  across settings levels rather than replacing each other." This agrees with the
  claim in `review_publish.claude_settings_args`'s docstring.
- **The gap.** `SEr()` is false unless
  `function xy(){return i.CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG===!0||…}` is set. So if
  the merged settings say `disableAllHooks: true`, only policy hooks run. A
  repository's `.claude/settings.json` or `settings.local.json` can therefore switch
  off the user guard on any launch that loads `project`/`local`, which today means
  every writable job.
- Merge order is in source. The disk load (`function dko(e){`, @258765630) does
  `for(let k of MBn(e)){…s=jI(s,Sn(k,Y,e),MB)}` with
  `function MBn(e){let n=new Set(e.allowedSources);return n.add("flagSettings"),n.add("policySettings"),ri.filter((r)=>n.has(r))}`.
  That is user, project, local, flag, policy, each merged over the last. The
  customizer is
  `function MB(e,n,r){…if(Array.isArray(e)&&Array.isArray(n)){if(r==="fallbackModel")return n;return L([...e,...n])}…}`,
  so arrays (hook lists, `claudeMdExcludes`) concatenate and scalars take the later
  source. So `"disableAllHooks": false` in `--settings` beats a project or local
  `true`; only policy outranks it. Claude Code's own notice names the flag source
  (`'the settings file Claude Code was started with (--settings) sets "disableAllHooks"'`).
  Conversation turns already do this (`claude_turn.argv`:
  `settings["disableAllHooks"] = False`, C-26.11); job launches don't.
- `review_publish`'s docstring says the inline `--settings` publish hook loading "is
  shown by those references, not by a Claude run". Probe 5 is the first live check
  of that mechanism.

### F8. Auto-memory reaches writable lanes

- `function Lh(){if(Yr())return"off";if(wC())return"off";if(q())return"off";let e=process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY;if(De(e))return"off";if(Qo(e))return"forced_on";return Hh()?"off":"unset"}`
  and `function hI(){let e=gt();if(e.autoMemoryEnabled!==void 0)return e.autoMemoryEnabled;return!0}`.
  The default is on, and setting sources don't gate it (F2).
- Evidence from this run: this job is a writable lane. Its context carried the
  "persistent file-based memory at `~/.claude/projects/-Users-maxghenis-subfleet-v2/memory/`"
  section and that folder's `MEMORY.md` (the canonical repo root, not the worktree).
  The section tells the lane to write files into `~/.claude`, and its entries include
  "Delegation goes to subfleet". Both conflict with lane briefs. Probe 1 confirms
  this on a haiku lane.
- `--settings {"autoMemoryEnabled":false}` turns it off. So does
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in that `env`, unless something sets the
  variable falsy.

### F9. What else writable lanes get from user settings today (matters for Design B)

- Subfleet's own `subfleet hook` entries: SessionStart, UserPromptSubmit,
  PostToolUse, and the PreToolUse attached-runner deny (`hooks.pre_tool_use`).
- The never-rules guard.
- User `permissions.deny` rules, which still bind under `--dangerously-skip-permissions`.
- User `env`, effort (D-EFFORT-ENV1), enabled plugins, skills, agents and commands.

I didn't read `~/.claude/settings.json`, so the exact inventory is the hub's to take.
Also, the repo's guard copy (`private/guard/never-rules-hook.sh`) is the Codex port.
Its header says the Claude hook moved its unscoped-search rule to
`~/.claude/hooks/unscoped_search.py` on 2026-10-20, so the copy cannot stand in for
the Claude entry.

## Candidate designs against constraints 1–5

Constraints, from the brief:

1. guard on every writable launch, never absent;
2. repo's own CLAUDE.md/AGENTS.md kept;
3. credentials path unchanged;
4. user memory, rules and imports dropped;
5. ancestors above the repo root dropped.

I add **4b**: auto-memory dropped (F8). It is the same kind of file, but the hub
should accept or reject it.

### Design A (recommended): keep sources, exclude in `--settings`

Applies to workspace-write job launches, `build_launch` and `resume_launch`. Writable
review launches are included and keep their `--setting-sources user`. A job-only
layer, not `lane_settings()`, adds these keys to the one `--settings`:

```json
{"env": {"CLAUDE_CODE_RETRY_WATCHDOG": "0", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
 "autoMemoryEnabled": false,
 "disableAllHooks": false,
 "claudeMdExcludes": ["<C>/CLAUDE.md", "<C>/rules/**",
                      "<A>/CLAUDE.md", "<A>/CLAUDE.local.md", "<A>/.claude/CLAUDE.md",
                      "<A>/AGENTS.md", "<A>/.claude/AGENTS.md", "<A>/.claude/rules/**",
                      "…one group of six per A…",
                      "!{<R>,<R_real>}/**"]}
```

Definitions:

- `R` is `git rev-parse --show-toplevel` run in `job.workdir` (its spelling, which
  follows `getcwd`), or the workdir itself outside git. `R_real` is `realpath(R)`.
  Write `!<R>/**` when the two are equal.
- `C` is the launch's config home, `_launch_home(env)/.claude`, which is `~/.claude`
  for token lanes.
- `A` ranges over every ancestor of `R` and `R_real` except `/`. `$HOME` is added
  whether or not it is an ancestor.
- The build also applies `claude_env_remove()` (`MEMORY_ENV`) to writable launches.

| | Holds? | Why |
|---|---|---|
| 1 | Yes, strengthened | `user` stays enabled, so the guard entry loads exactly as today. `disableAllHooks:false` closes F7's repo-file gap. No ordering window: nothing about the guard moves. |
| 2 | Yes | `project`/`local` stay on. Excludes name only paths above `R`, plus the negation, which spares `R/**`. Repo CLAUDE.md, nested CLAUDE.md, `.claude/rules` and AGENTS.md (plugin fallback) all keep loading. |
| 3 | Yes | No env or config-home change; `CLAUDE_CODE_OAUTH_TOKEN` as today. |
| 4 | Yes | `<C>/CLAUDE.md` and `<C>/rules/**` are excluded, so their imports are never read (F6). |
| 4b | Yes | `autoMemoryEnabled:false` plus the env variable. |
| 5 | Yes | Explicit ancestor list, and negation as a backstop for anything else outside `R` (nested loads from sibling folders, imports). |

Residue: personal skills, agents, commands, plugins, user `env` and user effort still
load. Constraints 1–5 don't name them. D-EFFORT-ENV1 stays as it is.

### Design B: drop `user`, carry the guard in `--settings`

`--setting-sources project,local`. The `--settings` carries:

- the never-rules PreToolUse groups, copied JSON-equal from user settings at every
  launch, with the launch refused when they are absent or unreadable;
- Subfleet's own PreToolUse entry (`hooks.hook_command()`);
- copies of user `permissions.deny`;
- plus all of Design A's keys. The excludes are still required because of F3.

| | Holds? | Why |
|---|---|---|
| 1 | Only with care | The guard moves into flag settings (F7 says it loads). Risks: a copy that drifts from user settings mid-rollout; fail-closed refusals; reading user settings on every launch. It must land in one commit with the source change. |
| 2 | Yes, with excludes | Without excludes, F3's masquerade hides AGENTS.md-only repos' guidance. |
| 3 | Yes | No credential change. |
| 4, 4b, 5 | Yes | As Design A. |

It gains broader isolation (skills, plugins, user env, effort). It costs the loss of
every user hook and deny rule it doesn't copy. Prerequisites: the hub's inventory of
user settings (names only), probe 5, and probe 2.

### Rejected

- **`--safe-mode`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `--bare`**: fail 2; safe mode
  and bare also fail 1, and bare fails 3 (F5).
- **Per-launch `CLAUDE_CONFIG_DIR`**: barred by constraint 3. It would move
  `~/.claude.json`, the keychain service name (`keychain_service_for_home`) and the
  transcript path (`_config_projects_dir`).
- **`--setting-sources ""` with `--add-dir R` and
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`**: loads only R's own
  `CLAUDE.md`, `.claude/CLAUDE.md` and rules, but fails 2 for AGENTS.md-only repos
  and nested CLAUDE.md (both need `projectSettings`). It also inherits Design B's
  guard move.
- **Plugin `instructionFiles` alone**: fails 2 or 5 (F4).

## Recommendation for the build

Build Design A as described. Keep `lane_settings()` and `claude_turn.argv` unchanged,
add a job-only `job_startup_settings(workdir, env)`, and merge it in both places that
call `claude_settings_args(..., settings=…)` for `workspace-write`.

Rollout: a resumed attempt still carries whatever its first turn loaded in its
transcript. Only fresh launches are clean.

Optional and not proposed now: `CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG=1` also protects
user hooks from a checkout's `disableAllHooks` and makes personal hooks fail closed.
It changes many other behaviours (personal skill grants, agent permission modes,
WorktreeCreate), so it would need its own probe.

### Invariants the build must test

| # | Invariant |
|---|---|
| I1 | Every workspace-write Claude launch argv (`build_launch`, `resume_launch`, review and non-review): no `--safe-mode`, `--bare`, `--restricted`; no `--setting-sources` value lacking `user`; the one `--settings` has `"disableAllHooks": false`. `doctor`'s C-14.3 check is unchanged. |
| I2 | For fixture workdirs (repo root; a subdirectory of a repo; a symlinked path; a non-git folder; `$HOME` itself; a linked worktree), `claudeMdExcludes` has `<C>/CLAUDE.md`, `<C>/rules/**`, the six names for every ancestor of `R`/`R_real` above `/` and for `$HOME`, and exactly one negated pattern spelling `R` and `R_real`. |
| I3 | Property: no non-negated entry names a path under `R`. A picomatch-equivalent check says every `R/CLAUDE.md`, `R/sub/CLAUDE.md`, `R/AGENTS.md` and `R/.claude/rules/x.md` is kept, and every listed outside path is excluded. |
| I4 | `autoMemoryEnabled` is false and `env.CLAUDE_CODE_DISABLE_AUTO_MEMORY` is `"1"` on writable launches; `MEMORY_ENV` names are in writable `env_remove`. |
| I5 | Read-only argv (job launch, isolated review, read-only conversation turn) is byte-identical to `adf66f2e5`. |
| I6 | `claude_turn.argv` output is unchanged for every permission (conversation turns are out of scope). |
| I7 | `env_add` gains no `CLAUDE_CONFIG_DIR`; `expected_transcript_path` is unchanged. |
| I8 | Exactly one `--settings`, valid JSON, still carrying `CLAUDE_CODE_RETRY_WATCHDOG=0` (D-WATCHDOG1). |
| I9 | The `--settings` payload of `resume_launch` equals `build_launch`'s for the same job and workdir. |

### Mutants that must each fail a test

| Mutant | Breaks |
|---|---|
| M1: writable argv gains `--setting-sources project,local` (guard dropped) | I1 |
| M2: `disableAllHooks:false` omitted | I1 |
| M3: `--safe-mode` added to writable argv | I1 |
| M4: `claudeMdExcludes` omitted | I2 |
| M5: ancestors taken from `job.workdir` instead of `R` (subdirectory fixture: repo-root CLAUDE.md excluded) | I3 |
| M6: ancestor walk off by one (includes `R`, or stops below `$HOME`) | I2, I3 |
| M7: `R_real` spelling dropped (symlink fixture) | I2, I3 |
| M8: two separate negations `!R/**`, `!R_real/**` (ORed, so they exclude everything) | I3 |
| M9: config-home or `$HOME` entries dropped, relying on negation (`$HOME` fixture) | I2 |
| M10: `autoMemoryEnabled` or the env variable dropped | I4 |
| M11: the job-startup settings also applied to read-only launches | I5 |
| M12: the keys put into `lane_settings()` (reaches conversation turns) | I6 |
| M13: `CLAUDE_CODE_OAUTH_TOKEN` replaced by `CLAUDE_CONFIG_DIR` | I7 |
| M14: a second `--settings` added instead of merging | I8 |
| M15: only `build_launch` updated | I9 |

## Probes for the hub (`docs/reports/lane-startup-probes/`)

| Probe | Launch | What it settles |
|---|---|---|
| 1 | Today's writable haiku lane | F8 (auto-memory reaches lanes) and the F4 path for `~/AGENTS.md`. |
| 2 | Nested `claude -p`, `--setting-sources project,local`, no excludes | F3: does `~/.claude/CLAUDE.md` return as a Project file, and does an AGENTS.md-only repo lose its guidance? |
| 3 | Nested, Design A settings | Constraints 2, 4, 4b, 5 end to end. |
| 4 | Nested, negation-only vs explicit-only excludes | Which half of the exclude list does the work. |
| 5 | Nested, `--setting-sources project,local` plus a marker PreToolUse hook in `--settings`; repo `disableAllHooks:true` with and without flag `disableAllHooks:false` | F7: flag hooks fire with `user` dropped; the flag override closes the repo gap; the gap is real without it. |
| 6 | Nested, Design A settings, `--debug hooks` | The user guard still runs on a Design A launch. |

The probes are harmless if a hypothesis is wrong: their only tool call is `echo`.
Nested runs use `--no-session-persistence` and the lane's own credential.

## Open questions for the hub

- Accept 4b (auto-memory) as part of D-LANE-STARTUP1?
- Writable reviews: they keep `--setting-sources user` (C-12.4, #221 r2). With
  Design A they load no instruction file at all; today they load only the personal
  ones. Should they get project guidance back? That would bring the project source,
  and with it project hooks, into reviews. My recommendation: no change now.
- Design B later? It needs the user-settings inventory (hook events and commands by
  basename, `permissions.deny` count, `env` names, `enabledPlugins`) before a build.

## Housekeeping

- Commits: `dab2face9` adds the two bundle tools; the next commit adds this note and
  the probe briefs.
- Folders created outside the worktree: none. Files I created in the assigned temp
  folder were deleted file by file with a guarded unlink: the saved help text, the
  quote dumps, the extracted module files and a 205 MB `bundle.js` copy. Nothing
  else was touched.
- The harness, not I, saved one oversized tool output under
  `~/.claude/projects/-Users-maxghenis-subfleet-v2-lanes-lane-startup/…/tool-results/`.
  I didn't read or remove it, because the brief keeps `~/.claude` hands-off.
- Two foreground commands (a `git status` and a `grep -a` loop) were moved to the
  background by the harness after a timeout. I stopped both with TaskStop; nothing
  outlives the session.
- Not run: any `claude` prompt, `subfleet`, `security`, any `gh` write, `git push`.
