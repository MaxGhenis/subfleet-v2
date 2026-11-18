A pytest process never resolves the account's live roots (`~/.subfleet`,
`~/.claude`, `~/.codex`, `~/.claude.json`), whatever `os.environ` holds (D-ST3),
even when a test replaces it with an empty dictionary. Tests use stand-in
accounts and temporary folders, never a live root.

**The rule.** For this process, every refusal takes a process rule (pytest's
own modules are loaded) as well as the environment rule. No module under
`subfleet/` imports pytest, so a production process cannot trip it into
loosening; a false positive only refuses. A child given an environment keeps the
environment rule alone. Tests of production behaviour waive the process rule
only through the `production_process` fixture, and only while HOME is neither the
account's home nor inside a live root, read again at every call.

**What is refused in a test process, before any access:**
- A missing override.
- A HOME that is the account's home (by any spelling, `~<name>` included) or
  inside a live root or under `/.vol/`.
- An override beginning `~` or `~<name>` that expands to such a home.
- An override that names the account's home, a live root or a path inside one,
  by any spelling of its name: after `~` expansion, lexically normalized against
  the consumer's base, and followed through links.
- Any override under `/.vol/`, macOS's path by inode, which no name can judge
  (review r2).
- An empty override whose working directory is inside a live root: `""` is
  judged as `.` (review r2). Elsewhere `""` is returned, as release returns it.

The shared check covers 41 call sites for 27 override names: `home_path` and the
raw reads routed through `checked_override`. This process's own code is its own,
even in a checkout under `~/.subfleet/worktrees`. The check judges names, not
inodes: a hard link to a live file is out of scope, as C-20.6 states, and a
`documented_limitation` test pins that.

**The daemon's entry points.** `python -m subfleet.daemon` and the `subfleetd`
console script both run `daemon.run_program` (review r2: the script had run
`daemon.main`, so a marked child printed a traceback). A test process's refusal
is exit 7 and one line, written through `daemon_log.write_scrubbed`, as C-25.7
asks of the daemon's stderr. Production is unchanged.

**Release.** `origin/release/217` at `c2e080e44` (#243, #247, #249, #245, #233) is
merged. The contract keeps both change logs, minus a stale pre-D-WALL1 entry
that the previous merge had restored beside release's revision: release's 215
entries appear as release has them, plus this branch's 8. All 257 clause IDs
survive, and only C-20.6 differs from release. #233's stream lint refused
`run_program`'s `print`, so the refusal now goes through `daemon_log`.

**Validation at head (`ff7ec9913`; the contract-reading files again at
`e570bd37f`):**

| Directory | Files | Cases | Passed | Failed | Skipped | Xfailed | Unrun |
|---|---:|---:|---:|---:|---:|---:|---:|
| `tests/unit` | 344 | 14,284 | 14,279 | 0 | 5 | 0 | 0 |
| `tests/fake` | 112 | 2,130 | 2,128 | 0 | 0 | 2 | 0 |
| `tests/e2e` | 18 | 109 | 93 | 0 | 16 | 0 | 0 |

- **Skips:** 4 retention cases need sortedcontainers in uv's offline index,
  which this host's cache lacks and the job could not fetch. One is an opt-in
  benchmark. The 16 e2e skips are person-only cases that cannot run inside a
  Subfleet job (CI runs them).
- **Xfails:** the two are pre-existing and documented.
- **Not run:** `tests/frontend`, `tests/process` and `tests/live`.
- **Regressions:** each of review r2's inputs failed at `c45c2e0ec`
  (31 failed, 5 passed, in an archive).
- **Mutations:** all 21 kill their named tests with passing controls, review
  r2's four new ones included (console script back to `main`, `/.vol/`
  accepted, `""` unjudged by either route).

**Invariants, each held by named tests:**
1. No guarded resolver offers or touches a live root under pytest.
2. Plain production resolves exactly as release does: 238 comparisons in a
   fresh child, including empty overrides from a working directory inside a
   stand-in store.
3. The waiver never holds for a personal HOME.
4. Children keep the environment rule.
5. The property's draws repeat across processes.

Report, with each finding's cause and fix, every resolver, every hold and the
per-file ledger: [the D-ST3 report](docs/reports/2026-10-19-st3-isolation.md).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
