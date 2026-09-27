# Plan of record

Adopted 2026-09-05 (Max: "take the best of both and lets build it, w help from astra"). The plan of record is `plan-b-rev4.md` as amended below. Each amendment names the section of plan B it changes and where it came from: plan A (`plan-a.md`), the round-1 and round-2 peer reviews (`reports/scoring-*.md`), or the comparison memo (`comparison.md`). `acceptance-contract.md` turns this plan into the clauses the code is built against; where the two disagree, the contract wins and this file is corrected.

## Amendments

| # | Section of plan B | Amendment | Source |
|---|---|---|---|
| 1 | The agent contract; Surface, Verbs; Build sequence 8 | Every v1 verb spelling that appears in the README or in any agent's CLAUDE.md is permanent, not transitional: `run`, `runs [--mine\|--running\|--last N]`, `runs show <id> [--out\|--err]`, `runs reap`, `wait`, `kill`, `status`, `capacity`, `resume-codex`, `handoff`, `gate`, `notify`, `enroll`. `jobs` and `show` are aliases of `runs` and `runs show`, not replacements. Milestone 8 "compatibility removal" applies only to the `DELEGATE_*`, `CARPOOL_*`, `CLAUDE_LANE_*` env families, the `-t` classes, `--overflow`, Sol aliases, and the public `codex`/`claude` verbs. The sentence "keeps working unchanged" becomes literally true. | Opus r1 dispute 3, Astra r2 dispute 3, comparison memo residual 4 |
| 2 | Processes, tree containment; Survival matrix; Experiment 5 | An empty recorded process group does not prove every writer stopped: a descendant can `setsid` into a new group. Containment enumerates three ways before any workspace release: members of the recorded pgid, descendants of the guardian by parent chain, and processes carrying the inherited marker `SUBFLEET_ATTEMPT=<attempt id>` in their environment. "Verified empty" means all three enumerations return no live process. If any enumeration cannot run, the workspace is `quarantined`. Experiment 5 is rewritten to spawn a `setsid` descendant and assert quarantine, not release. | Astra r2 dispute 2 |
| 3 | Processes | The daemon holds an exclusive advisory lock on `~/.subfleet/daemon.lock` for its lifetime and records pid, boot id, and process start time in it. A second daemon exits with code 69 and names the holder. SQLite write serialization is not the singleton mechanism. A CLI that finds the lock held by a dead process (boot id or start time mismatch) may start a daemon; one that finds a live holder never does. | Plan A, Lifecycle; Opus r2 graft 2 |
| 4 | Core model; Verbs, `kill` | Cancellation is a durable request row, not a signal. Ordering is defined in both directions: if the cancel commits before result acceptance, a later successful attempt keeps its artifacts but the job stays `cancelled`; if acceptance commits first, the cancel reports "already finished" (exit 0 with a note). Cancelling a parent cancels its child jobs by default; a child submitted with `--independent` outlives its parent. Child concurrency and attempt budgets aggregate under the parent. | Plan A, Lifecycle; all three scorers |
| 5 | Gates; Reset credits and keepalive | Every external side effect (a GitHub merge, a reset-credit redemption, later a message send) is a typed action with states `pending`, `executing`, `confirmed`, `failed`, `unknown`. The intent and a stable operation key are persisted before the remote call. A timeout after submission is `unknown` until reconciled by reading the remote state, never retried blind. `status`, `why`, and `--dry-run` never perform an action. | Plan A, Approvals; Opus r2 graft 1 |
| 6 | Provider adapter contract, `output()`; Storage | Artifact publication is: write to a temporary file in the same directory, fsync the file, rename atomically, fsync the directory, then record path, size, and SHA-256 in the store. The caller's `-o` path is reserved at admission (a lease keyed on the resolved path) and published by rename after acceptance; a failed export leaves the job `succeeded` and records the export failure separately. | Plan A, Workspaces; Opus r2 graft 3 |
| 7 | Core model, request id | A job's payload digest is the SHA-256 over: prompt bytes as sent, resolved workdir and its git head if any, task, tier or pinned model, sandbox, exclusions, `-o` path, `allow_desktop`, and the policy hash. A repeated request id with the same digest returns the existing job; with a different digest it is exit 2. The digest and the inputs it covers are recorded on the job row. | Plan A, Domain model; Opus r2 graft 5 |
| 8 | Migration and cutover | Ownership during the shadow period is per account as well as per capability. `lanes.json` marks every account `owner: v1` or `owner: v2`; v2 never dispatches on a v1-owned account and v1's roster loses an account the moment v2 takes it. The first v2-owned account is one Codex lane (the canary). v1 gates call `delegate_main` directly, so they follow their accounts, not the CLI symlink. Rollback transfers accounts back one at a time and never leaves two schedulers or two redeemers on one account. | Plan A, Migration; Astra r2 winner note |
| 9 | Build sequence | Effort: plan A's 8 to 12 engineer-weeks for one implementer is adopted as the size of the work. Delivery is parallel Astra and Opus lanes with a Fable integrator, so the calendar target is milestone 1 within two working days of the contract landing, milestones 2 and 3 within the following three, cutover of `run` (milestone 4) after a seven-day shadow week, and milestones 5 to 8 in the two weeks after that. Every milestone row in the contract carries an acceptance test name, not a prose criterion alone. | Plan A, Delivery plan; all three scorers |
| 10 | Test strategy; Build sequence 4 | Release gates before the `run` cutover, measured on this Mac against the fake providers and one real canary: zero lost acknowledged jobs in the crash suite; zero duplicate accepted results; zero results accepted from a stale attempt; zero workspace reuse after an unverified termination; cached `status` p95 under 100 ms; `submit` p95 under 250 ms excluding provider probes; recovery after a daemon SIGKILL under 30 s; 100 representative canary jobs plus a seven-day soak with no unresolved ownership, loss, or duplicate-action defect. | Plan A, Release criteria |
| 11 | Routing as data, admission | Anti-starvation: queued jobs age in FIFO order within a tier, a parent's descendants share one concurrency bound, and a `waiting` job records a reason and a next-check time. No interactive allowance is needed for one user. | Plan A, Routing |
| 12 | Storage | The store has a schema version. Upgrade is: stop admission, drain or re-adopt attempts, `VACUUM INTO` a backup, migrate additively, run `PRAGMA integrity_check`, resume. A CLI newer than the daemon refuses to mutate the store and reports both versions. | Plan A, Configuration |
| 13 | Design principles | Add: fencing protects subfleet's accepted state and publication path; it does not revoke a running provider's filesystem or network powers. A stale process with credentials can still write files and call services. Prompts and worktrees are not isolation, and the plan does not claim hostile-agent isolation. | Plan A, Workspaces |
| 14 | What I watched it do this morning; Where the README and the code disagree; Appendix B | Wording corrections: the 06:33 row's cause is lane-aware model selection that ignores the caller's exclusions plus no promotion on a no-lane result, not "family-level telemetry" (`delegate.py:664`, `:1204`, `:1223`, `:1278`). README line 108 describes "active-app protection"; the 10-point value is `INTERACTIVE_HANDICAP` in code, and the delegate's sort puts `not fable_stranded` before `active` (`delegate.py:576`), so "strictly last" overstates v1. README line 380 scopes recovery notices to auth and fleet conditions, so only `codex-fleet-low` is a documented omission. The adversarial review had 5 blockers and 15 majors. Thirty test files, not 31. `docs/integration-events.md` is on the Traycer branch. | Astra r1 and r2, Opus r1 and r2, comparison memo |
| 15 | Build sequence 2 | Milestone 2 acceptance: the table shows no percentage without a `provider` reading. Admission-observed and local-backoff evidence render as words, never as a percentage. | Astra r2 |
| 16 | Open decisions 4 | Anthropic's Agent SDK page: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products." A public core that offers claude.ai login is blocked on that approval; the public extraction ships API-key or bring-your-own-login only. | Plan A, Provider adapters |
| 17 | Build sequence | Plan A's first ten tickets map onto the lanes in `docs/lanes/`: contracts and fake provider (Fable, done in milestone 0), store and idempotent submission, admission reservations and launch intent, execution identity and cancellation, immutable artifacts and one accepted result, first real adapter with fixtures, fault injection at every transition, inspect/wait/explain and transactional notices, first canary. | Plan A, Delivery plan |

## What is not adopted from plan A

Go as the implementation language (Python and uv are the operator's stack). A coordinator-surviving execution supervisor with lease generations (the guardian plus receipts plus verified containment is one mechanism fewer). Deferring reset-credit automation and postponing the sessions kit (both are in daily use). Temporal or any workflow engine. An interactive-capacity allowance (one user).

## Open decisions still with Max

Numbered as in plan B: 1 (Claude sensor: stream event, decided by experiment 0), 2 (desktop login never: standing order), 3 (sessions and gates as separate entry points), 4 (freeze public repo; see amendment 16), 5 (Traycer events only), 6 (keep the name), 7 (automatic revive off by default for desktop-owned sessions), 8 (sidebar mirror kept). The build proceeds on the recommendations; any of them can be reversed as a policy change.

## Execution decision, 2026-09-19

After reviewing the rebuild and the proposed seven-day soak, Max requested:
"lets just do a clean cutover". For this installation, direct cutover replaces
the staged canary/shadow schedule in amendments 8–10. Automated correctness and
performance checks still apply. Preserve running v1 jobs, fence old scheduled
services, transfer accounts only when idle, keep rollback evidence, and verify
the installed daemon, CLI, and retained native menu bar app. A waived rollout
observation remains unperformed; it is not recorded as a passed gate.

## Operator reserve authorization, 2026-09-20

Max requested using otherwise unused Opus capacity, including on accounts whose
Fable capacity is exhausted. The Microcosm task explicitly requested a per-job
authorization path when usage telemetry cannot establish reserve slack. This
amends C-11.7 with an opt-in exception for **unmeasured** reserve only: a new
dispatch must name an exact enrolled lane and model, record its operator reason
and evidence, and pass a fresh same-model admission probe. Missing usage is never
represented as exhaustion or available quota. Measured reserve restrictions and
all known closures, identity, ownership, desktop, and concurrency checks remain
in force. Authorization does not become a policy default or transfer to a new
job or resumed session.

## Scoped Fable capacity correction, 2026-09-20

Max requested using spare Opus capacity after weekly Fable exhaustion as the
first policy-encoding test. Actual provider streams and the installed Claude
client establish that `seven_day_overage_included` is the Fable bucket. C-9.8
therefore scopes that reading and rejection to Fable, not the whole account;
named Opus and Sonnet rejections likewise retain their scopes. C-11.7 accepts
paired shared/scoped weekly readings from the same provider stream event, and
an active reported model-only exhaustion permits a supervised probe of another
model when reserve measurement is unavailable. Fresh measured restrictions
still apply. See the [evidence and regression record](reports/2026-09-20-fable-scoped-capacity.md).

## Refresh follow-up, 2026-09-20

Live menu verification confirmed that the account list and reload feedback work,
but exposed stale usage between scheduled probes. C-18.1 now defaults to a
60-second wait after probe-cycle completion. Previously its 300-second interval
exceeded the 120-second reading TTL, and measuring the next cycle from its start
could make the per-lane cooldown skip that cycle. Explicit configured intervals
are respected; the change does not extend reading freshness or probe busy lanes.

The desktop sidebar mirror also needs reuse of unchanged input across passes
and cooperative shutdown at this installation's session-store size. Its health
record must distinguish progress from completion; an incomplete pass is never
reported as successful.

## Handoff fan-out, 2026-09-20

A session tried to hand five stalled threads to parallel writable lanes and got
one. Max asked: "should we fix subfleet to do this right", and later granted
"feel free to make any changes to subfleet without my approval ... only
potential worry is loss of user data". C-6.5 is corrected to what it always
said: the refusal is for a second live *instance* of a session (the 2026-09-04
twin), not for a second job. The hold moves to where a job writes, keyed on the
checkout so a subdirectory cannot slip past it. Identity that cannot be
established still refuses. The instance and the write target are recorded in
the `job.submitted` event rather than in new columns, so the installed release
can still open the store after a rollback. C-6.8 (same day) stops a transient
git timeout during workspace preparation from failing a job without a trace.

## A stalled queue that said nothing, 2026-09-20

The same afternoon, fifteen jobs sat queued for more than three hours. C-6.9
(above, another session's fix) is why nothing was admitted. Three further
faults made it invisible and expensive, and are corrected together. C-6.10: a
capacity wait was rechecked one second later for ever, and every recheck wrote
a full decision row, so three unplaceable jobs cost a core and 681 MB of a
709 MB store; the recheck now backs off to 30 s while the verdict repeats, a
repeat adds no row, and freed capacity is still seen on the next pass. C-6.11:
`why` printed `null` for a queued job, `status` counted the whole store as
running, and `daemon.log` was silent; every unplaced job now has a stated
reason, and a fleet that places nothing for a minute says so. C-5.10: a worker
that raises is retried with backoff rather than on every 50 ms tick. No schema
change: the new state is in memory, so rollback stays possible.

## Fable retired from dispatch, 2026-09-27

Max: "opus 5.5 is strictly better than fable" and "we dont use fable anymore".
The installed policy already routed the writing tasks to Opus; the shipped
default still named Fable, so a fresh install or a reset would have brought it
back. Plan B's routing-as-data example is amended: `authored-prose`, `strategy`,
and `adjudication` are Opus at every tier; `fable` leaves `models`; `fable`,
`claude-fable-5`, and `claude-fable-5-1` are `retired` aliases of `opus`; and
`reserve.models` is empty, because a reserve held for a model nothing routes to
only stops Opus work. Retirement follows Sol's path (C-17.2): `run -m` and `-t`,
`why -m`, `sessions handoff --to`, `sessions revive --model`, and `gate --peer`
accept the old name, say so on stderr, and use the successor even under a
`policy.json` that still lists Fable. `gate --peer opus` is the Claude-family
peer that `fable` was; a gate opened before the retirement runs its next round
on Opus and keeps its earlier rounds as recorded. The reserve rule (C-11.7) and
stranded-capacity ordering (C-23.37) are unchanged and keep their tests against
an explicit policy that still reserves Fable. Claude accounts still report
Fable's weekly window; it is recorded under its own id, and a `retired` alias
never re-labels it as Opus's (the v1 importer used to, through the alias map).
