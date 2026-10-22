A pytest process keeps refusing personal store defaults even if a test replaces
os.environ with an empty dictionary. This round also closes explicit-override
escapes: MCP's raw CLAUDE_CONFIG_DIR, literal/link/tilde live roots, tilde-spelled
HOME, the v1 PATH fallback, and each searched PYTHONPATH entry in doctor.

The shared guard checks 41 call sites covering 27 distinct override names.
Relative cache overrides are judged against their documented state-root base;
the other readers retain cwd semantics. A test may select an isolated path or an
explicit function argument; an override naming the account home or a live root
is refused. The process's own code/environment remains its own. Empty overrides
accepted by accept_empty retain release's working-directory meaning.

The property's set iteration is replaced with ordered draws from a fixed name
tuple. All seeds 0–7 pass in both the lane and minimal environments: 16 tests,
150 passing examples each. The final code also passes all focused regressions
and 17 mutation controls. Fresh children confirm that plain production resolves
as release (221 comparisons), imports no pytest, and that only test processes
get the one-line daemon refusal (exit 7).

Release/217 at `4f5983f4c` (#223, including #232/#238) is merged. The contract keeps
both parents' complete change-log histories and clause IDs, release's amended
bodies, and ST3's C-20.6 amendments.

**Validation is incomplete.** Current-head unique outcomes:

| Directory | Files | Collected | Passed | Failed | Skipped | Unrun | Complete / partial / unstarted files |
|---|---:|---:|---:|---:|---:|---:|---|
| tests/unit | 332 | 13,652 | 10,071 | 4 | 7 | 3,570 | 100 / 3 / 229 |
| tests/fake | 112 | 2,130 | 865 | 0 | 0 | 1,265 | 12 / 1 / 99 |

The 4 failures are one unresolved 0.5-second fake-version startup/report timing
case and three importer cases requiring process inspection denied by this
sandbox. Seven cases skipped for unavailable native ps/sysctl inspection.
H4 stopped inside its 20-minute bound; the continuation hold was interrupted
during collection after the cutoff and contributed no outcomes. There are
4,835 unrun cases in 332 partial/unstarted files. This is not a green full-suite
result.

The five invariants are covered by named tests: no personal live-root resolution
under pytest (with the caller-input and own-code boundaries above), exact plain
production behavior, a HOME-bounded waiver re-read on every call, unchanged
child-environment detection, and repeatable draws. Computed imports, aliased
import_module and exec on unexecuted paths remain static-analysis limitations.

The report contains each finding's cause/fix, every resolver and its treatment,
every seed's counts, all hold outcomes, and the per-file ledger:
[Round-1 report](docs/reports/2026-10-19-st3-isolation.md).
