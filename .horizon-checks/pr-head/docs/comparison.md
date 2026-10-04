# Subfleet rebuild: plan A vs plan B

Re-evaluation written 2026-09-05 08:25 EDT against plan B revision 4 (500 lines, modified 07:42). The first version of this memo (07:00) scored revision 3 (434 lines); Max said B was not finished, so everything below is re-scored on the finished text. Plan A is unchanged: `~/.local/share/.ef86c144b2f4b5530930898314c86734/design.md`, 468 lines, modified 06:38.

## Bottom line

Plan B wins, 90.5 to 79.75 on a 100-point rubric. On revision 3 the margin was 84.0 to 79.75. Revision 4 gained 6.5 points because it absorbed an Astra adversarial review (20 findings, all accepted with dispositions) and because a live experiment overturned its own central premise: headless Claude runs on setup-token lanes already stream a `rate_limit_event` with both windows' utilization and reset clocks, so the Claude capacity sensor exists today and the lane-homes pivot is now optional.

A+B: about 95. A now adds about 4.5 points on top of B alone, about 5% of B's value, down from 8 points (10%) against revision 3. Most of what A had over revision 3 (an uncertain-outcome state, a crash protocol per boundary, transactional admission, corrected salvage rules, a real rollback order) is in revision 4. What A still adds is an effort estimate and first tickets, migration by account, an explicit cancel-versus-complete ordering, quantified release gates with a soak, and a general external-action state machine.

Three scorers have now graded the plans against the same rubric. All three agree on the shape: A leads on architecture, migration, and testability; B leads on capacity truth, constraint fit, invariants, and decision-readiness. They disagree on magnitudes along model-family lines. Details below.

## How B's authorship was established

Plan B's own header says "Written 2026-09-05 by the Fable session Max asked." The memory note that session left says the same. I then checked the session transcript (`4f43e3a5-2453-4a64-86ac-b210c108beb0.jsonl`): all 325 assistant messages carry the model id `claude-fable-5-1`, from 06:21 to 07:42 EDT. So the self-description is confirmed. The first memo relied on the header and the memory note only. Plan A's authorship is unstated in the file, and A is not the Astra clean-room report B cites (checked byte for byte against that lane's output).

## What changed in B between revision 3 and revision 4

| Area | Revision 3 | Revision 4 |
|---|---|---|
| Claude capacity sensor | None for setup-token lanes; pivot to full-login homes gated on a three-day experiment | Experiment 0 (07:2x): `claude -p ... --output-format stream-json --verbose` under a setup token emits `rate_limit_event` with `five_hour` and `seven_day` utilization as fractions plus `resetsAt`, on three lanes; a rejected request carries `status: rejected` and no windows. Every allowed attempt is a provider reading at no extra cost. Homes become an optional milestone 5 item. |
| Evidence vocabulary | live, inferred, unknown | Six labels: provider, stale-provider, admission-observed, local-backoff, plus unknown states; a classification precedence (authentication, then admission, then quota) with the evidence recorded per answer |
| Attempt lifecycle | Job states plus `lost` | Persisted attempt states `reserved`, `starting`, `running`, `finalizing`, each with a defined recovery when the daemon dies at that boundary; `quarantined` state; terminal state and notice written in one transaction; idempotent digest-checked export |
| Process ownership | Guardian receipt; pgid re-adoption | Tree containment verified before any release: enumerate the pgid, match by boot id and start time, kill survivors, confirm empty, else quarantine |
| Admission | Policy caps | Numeric defaults (4 active attempts fleet-wide, 2 per lane, 1 while unmeasured, 6 h wall time, 3 attempts, 8 child jobs, a token overshoot cap); concurrency on an unmeasured lane is an atomic lease in the store; request id persisted before submission with a payload-mismatch error |
| Lane identity | A home directory | Immutable binding of account, credential reference, and credential epoch; setup tokens first class; rebinding invalidates readings and native-resume bindings |
| Salvage | "Refuse if the workdir's branch is main or master" | Corrected: private refs from any branch; a writable job whose workdir is on main is refused at admission; writable jobs run in an allocated worktree or under an exclusive reservation; baseline ref at attempt start |
| Routing comparators | One ordering for both providers | Per provider: Codex orders by weekly reset ascending as the primary key; Claude by worst-window headroom; desktop reservation is a Claude rule, Codex keeps its app-home semantics |
| Agent contract | "Stays valid"; aliases for three verbs | Compatibility parser for every v1 invocation through milestone 8, with a deliberate-break list; the five-line core still renames two verbs (see residuals) |
| Notices | `asyncRewake` replaces the socket | Leased waiters re-armed by any tool call, explicit timeout, `offered` to `acknowledged`; the socket stays until tickle, muster, and ping have tested replacements |
| Session revival | Automatic, behind a store lease | Off by default for desktop-owned sessions, because no store lease can exclude a desktop restart; handoff is the default recovery; tickle stays automatic |
| Migration | Symlink cutover and rollback | Idempotent importer from a written manifest; one owner per capability with a transfer order; rollback fences v2, reconciles writers, then revives v1 |
| Tests | One test per invariant row | All 220 rows adjudicated keep, replace, or drop with an acceptance owner in milestone 0; a named real-fixture corpus; opt-in real acceptance before an adapter is promoted |
| Milestone 0 | Experiments | Experiments plus a binding acceptance contract that milestone 1 is built against |
| Appendix B | Capacity audit "see below" with nothing below | Capacity audit done (reproduces the four percentages to the token), adversarial review done, experiment 0 filed |

## Peer scorings, round 1 (both scored revision 3)

Two independent lanes scored the plans against the same rubric before revision 4 landed: GPT-6 Astra on codex-4 and Claude Opus 5 pinned to a blind account. My revision 3 scores are beside them.

| # | Dimension (weight) | Astra A | Astra B | Opus A | Opus B | Fable A | Fable B |
|---|---|---|---|---|---|---|---|
| 1 | Diagnosis (15) | 8.5 | 8.0 | 7 | 9 | 8 | 9 |
| 2 | Architecture (20) | 9.5 | 6.5 | 9 | 7 | 9 | 7.5 |
| 3 | Capacity and routing (15) | 7.0 | 8.0 | 7 | 9 | 7.5 | 9 |
| 4 | Invariants as tests (10) | 8.0 | 8.0 | 6 | 10 | 7 | 9 |
| 5 | Migration and effort (10) | 9.0 | 5.0 | 9 | 6 | 9 | 7 |
| 6 | Testability (10) | 9.0 | 7.5 | 9 | 8 | 9 | 7.5 |
| 7 | Fit to constraints (10) | 6.5 | 7.5 | 5 | 9 | 6.5 | 9.5 |
| 8 | Decision-readiness (10) | 8.5 | 8.5 | 6 | 10 | 7 | 9 |
| | Weighted total | 83.25 | 73.5 | 74.0 | 84.0 | 79.75 | 84.0 |
| | Winner | A by 9.75 | | B by 10.0 | | B by 4.25 | |
| | A+B | 12% on A | | 35% on B | | 10% on B | |

Mean across the three: A 79.0, B 80.5. The direction of every dimension is shared; the magnitudes split by family, with Astra hardest on B and Opus hardest on A. Astra had also just written the adversarial review of revision 3 for B's author, and its low B architecture score names the same defects that review names; revision 4 fixes them.

### Peer disputes, checked against the code

| Raised by | Claim | Check | Status in revision 4 |
|---|---|---|---|
| Astra | B's explanation of the 06:33 failure ("model choice reads family-level telemetry") is wrong; the cause is that caller exclusions are not applied at model selection and a no-lane result never promotes | Confirmed. `_model_capacity_state` calls `_capacity_candidates(..., model_family=...)` at `delegate.py:664` without the caller's `-x`; exclusions join at line 1204; result 3 at line 1223 never promotes, since promotion fires only on result 4 at line 1278 | Wording unchanged at line 37. The remedy (lane pick and model choice as one evaluation) is unaffected |
| Astra | B mistranslated the main/master salvage guard | Confirmed. The Codex runner at lines 138 to 146 refuses `-b main` or `-b master`, the salvage push destination, not the workdir's branch | Fixed (review finding 3) |
| Astra | B's supporting reports were unavailable | My omission: I did not copy the reports directory for the round-1 lanes, to keep the hidden directory hidden | Supplied to round 2 |
| Opus | README line 108 does not say "10-point handicap"; the number is `INTERACTIVE_HANDICAP = 10.0` in `capacity.py:27` | Confirmed verbatim | Unchanged at line 53. The substantive point, that the code is stricter than the doc, stands |
| Opus | README line 380 says "auth/fleet conditions", so most of the five omitted recovery keys are not README violations | Confirmed verbatim; `codex-fleet-low` is the one that counts | Unchanged |
| Opus | B's binary-string evidence is not reproducible because `~/.local/bin/claude` does not exist | Rejected. On this machine `~/.local/bin/claude` is a symlink to `~/.local/share/claude/versions/2.1.260`; `strings` on it finds `CLAUDE_CONFIG_DIR` 40 times, `asyncRewake` 8, `rate_limit_event` 11. The Opus lane's PATH lacked the symlink | n/a |
| Opus | The core contract renames `runs --mine` to `jobs --mine` and `runs show` to `show` while claiming "CLAUDE.md unchanged" | Confirmed. Lines 287 to 288 still carry both renames; `cli.py:1297` registers `runs` today | Partly fixed: a compatibility parser accepts every v1 invocation through milestone 8, and deliberate breaks are removed only after consumers are updated. The sentence "keeps working unchanged" remains, and after milestone 8 the CLAUDE.md lines change |

Round 2 is running on revision 4 with the reports supplied: Astra run `20260905-082003-plan-compare-astra-r2` and Opus run `20260905-082005-plan-compare-opus-r2`, outputs in `~/.cache/subfleet-plan-compare/`.

## What was verified

Every mechanism claim that could be checked was checked against the tree at HEAD 57a851a.

| Claim set | Result |
|---|---|
| A: 12 code citations (`delegate.py:1231`, `:59`; `bin/subfleet-claude:864`, `:757`; `run_ledger.py:542`, `:515`, `:647`; `notify.py:632`; `consensus.py:268`, `:793`; public `pyproject.toml:26`, `cli.py:886`) | 12 of 12 land on the mechanism described. Finalize holds the ledger lock across artifact copies, a git call, and pruning. The notice push runs before the notice row is appended and `delivered` is set inside `push_to_session`. The public wheel packages only `subfleet/` while `cli.py` resolves runners from a sibling `bin/`. Both peers independently confirmed the same set. |
| A: 4 external doc citations | 3 confirmed verbatim: Codex `exec --json` with thread and turn events and saved auth; Claude `--bare` uses an API key, not the subscription; the Agent SDK page says "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits". 1 not confirmed: the App Server page documents stdio transport but no account rate-limit or reset-redemption methods, and the installed codex 0.153.3 binary shows no such method names. |
| A: public and installed trees differ | Confirmed. Public-only: `config.py`, `inbox.py`, `secret_store.py`. Installed-only: `consensus`, `handoff`, `lanes`, `reset_policy`, `keepalive`, `capacity_expiry`, `resume_codex`. |
| B: inventory table | Matches: 23 modules, 15,084 lines; bin totals; README 699 lines; 10 worktrees; 3.0 GB state, 45 top-level entries, 107 gates, 500 runs. Test files are 30 on disk; B says 31. |
| B: 30 named symbols | All present in the named modules; Opus confirmed each with a line number. |
| B: README-vs-code gaps (`watchdog.py:271-281`, `:732-736`; `capacity.py:1315-1322`) | Code citations confirmed. Two README attributions are loose (lines 108 and 380, above). |
| B: experiment 0 | The report shows four `rate_limit_event` payloads with session ids; the event name appears 11 times in the installed claude binary. I did not re-run the probes. |
| B: the 06:33 case at `delegate.py:1215` | Confirmed that a failed lane pick sets result 3 and never promotes; B's causal wording is imprecise (above). |
| B: lane reports | A-invariants, B-capacity, D-surface, F-design, G-review, and experiment 0 all exist under `~/.plans-eca6b8/reports/`. |
| B: `docs/integration-events.md` | Exists on a branch (commit 87064da), not on HEAD. |
| Both: CLI surfaces relied on | claude 2.1.260 has `--output-format`, `--session-id`, `--resume`; codex 0.153.3 has `exec --json`, `-o`, `resume`. |

## Rubric

| # | Dimension | Weight | What it asks |
|---|---|---|---|
| 1 | Diagnosis grounded in the real system | 15 | Are the problems real, located in the code, and the important ones? |
| 2 | Architecture soundness | 20 | Job ownership, durability, recovery, cancellation, races |
| 3 | Capacity truth and routing | 15 | The Claude sensor gap, unknown capacity, model-scoped limits, desktop-login protection, upward-only promotion |
| 4 | Invariants preserved as tests | 10 | Are ten weeks of incidents carried forward as named tests? |
| 5 | Migration, rollback, effort realism | 10 | Can v1 keep running, can it roll back, how long will it take? |
| 6 | Testability and acceptance criteria | 10 | Fake providers, crash suites, release gates |
| 7 | Fit to Max's constraints | 10 | Subscriptions only, desktop login never, task and tier routing, stable agent contract, Python and uv, one maintainer |
| 8 | Decision-readiness | 10 | Open decisions, experiments that resolve risks, first tickets |

## Scores against revision 4

Half points allowed. Weighted total = score x weight / 10. The B column shows the revision 3 score in brackets.

| # | Dimension (weight) | A | B | Why |
|---|---|---|---|---|
| 1 | Diagnosis (15) | 8 | 9.5 [9] | A: nine precise findings, all verified, with an honest limits section; misses the sensor question entirely. B: timestamped live observations tied to code, a measured inventory, an Astra capacity audit that reproduces the runaway percentages to the token, and an experiment that overturned the plan's own premise. Two loose attributions remain (the 06:33 causal wording, README line 108). |
| 2 | Architecture (20) | 9 | 8.5 [7.5] | A: attempt state machine with `indeterminate`, lifecycle invariants, admission as one transaction, lease generations, cancellation serialized against completion, PID-reuse guard, atomic artifact publication, fencing limits stated. B now has a persisted attempt state machine with per-boundary recovery, verified tree containment, quarantine, request-id semantics, immutable lane bindings with epochs, one transaction for terminal state and notice, and a non-blocking control loop with an offline CLI. A keeps the edge on cancel-versus-complete ordering, parent-to-child cancellation, and the fencing statement. |
| 3 | Capacity and routing (15) | 7.5 | 9.5 [9] | A: general and correct, but never names the Claude sensor question and leaves desktop protection to policy. B: a verified sensor on every attempt, six evidence labels, a classification precedence, per-provider comparators, an atomic lease for unmeasured lanes (A's own critique, adopted), numeric admission defaults, probes with the model the job wants. |
| 4 | Invariants as tests (10) | 7 | 9.5 [9] | A: principled but no enumeration. B: 220 rows, each to be adjudicated keep, replace, or drop with an acceptance owner before milestone 1, plus a named real-fixture corpus. |
| 5 | Migration and effort (10) | 9 | 8 [7] | A: migration unit is the account, per-account rollback, 8 to 12 engineer-weeks with exit evidence. B: import manifest, one owner per capability with a transfer order, a rollback that fences v2 first and never leaves two schedulers alive. Still no effort estimate anywhere in the document. |
| 6 | Testability (10) | 9 | 8.5 [7.5] | A: nine suites, crash matrix per boundary, disk-full, numeric release gates, a seven-day soak. B: per-boundary recovery is now specified, a real-fixture corpus is named per case, and an opt-in real acceptance matrix gates adapter promotion. No numeric release gates and no soak. |
| 7 | Fit to constraints (10) | 6.5 | 9.5 [9.5] | A: Go by default, desktop protection by policy, sessions kit postponed. B: Python stdlib and uv, setup tokens kept so no new logins are needed, desktop login never, compatibility parser for every v1 verb. Loses half a point for renaming two of the five contract lines while calling the contract unchanged. |
| 8 | Decision-readiness (10) | 7 | 9.5 [9] | A: four decisions to revisit, ten first tickets. B: eight decisions with recommendations, nine costed experiments with one resolved, a 20-finding review with dispositions and ten answered questions, milestone 0 as a binding acceptance contract. No ticket list, no time estimate. |
| | **Weighted total** | **79.75** | **90.5 [84.0]** | |

## Where they still disagree

| Question | A | B revision 4 | Read |
|---|---|---|---|
| Language | Go, decided by a two-day spike | Python stdlib | B. |
| Uncertain outcomes | `indeterminate` plus quarantine | `quarantined` after failed tree verification; `lost` only after a verified-empty group | Now equivalent. |
| Admission | Transactional reservations for concurrency, workspace, export | Atomic lease per unmeasured lane, request id, allocated worktree or exclusive reservation, numeric caps | Now close; A's output-path reservation is the residual. |
| Cancellation | Serialized against completion; propagates to children | Verified tree kill; child-job cap | A. |
| Claude capacity | Generalized buckets, estimates kept distinct | Verified stream sensor, six labels, precedence | B, decisively. |
| Desktop login | Operator policy | Never unless `--allow-desktop`, defined per provider | B. |
| Sessions kit | Postpone | Keep; automatic revive off by default for desktop-owned sessions | B, and now closer to A's caution. |
| Migration unit | The account | The capability, one owner at a time | A's is finer-grained; B's now prevents the two-scheduler race. |
| Effort | 8 to 12 engineer-weeks | None | A. |
| Release gates | Numeric, plus a soak | Per-milestone acceptance, real acceptance matrix | A. |

## A+B: what the runner-up still adds to the winner

| # | From A | Into B's section | Value |
|---|---|---|---|
| 1 | Effort estimate and first ten tickets | Build sequence | High: the only dimension where B has nothing |
| 2 | Migration by account: a canary account owned only by v2, per-account rollback | Migration and cutover, beside the per-capability owner rule | Medium |
| 3 | Cancellation serialized against result acceptance; parent cancel propagates to children | `kill` verb; attempt state machine | Medium |
| 4 | Quantified release gates (zero lost acknowledged jobs, zero stale publications, reconcile within 30 s) and a seven-day canary soak | Milestone 4 acceptance | Medium |
| 5 | External-action state machine `pending`, `executing`, `confirmed`, `failed`, `unknown`, with the operation key persisted before the call | Gates; reset credits | Medium |
| 6 | Output path reserved at admission and published by atomic rename; export failure separate from job success | Adapter `output()` row | Low: B's digest-checked export covers most of it |
| 7 | Fencing limits stated: a stale process keeps its filesystem and network powers | Design principles | Low |
| 8 | Anthropic's third-party login approval requirement | Open decision 4, public repo | Low now; blocking for any public product offering claude.ai login |
| 9 | Fairness: interactive allowance, per-parent bounds, ageing | Admission | Low for one user |

Combined score: about 95 of 100. Dimensions 2, 5, 6, and 8 rise to 9.5, 9, 9.5, and 10.

| | Alone | Combined | Marginal value of the other plan |
|---|---|---|---|
| B as base | 90.5 | 95 | A adds 4.5 points, about 5% of B |
| A as base | 79.75 | 95 | B adds 15.25 points, about 19% of A |

## Residual issues in revision 4

1. No effort estimate. Eight milestones and nine experiments have costs in hours or days for the experiments only.
2. The 06:33 row still says model choice "reads family-level telemetry"; the code shows lane-aware selection that ignores the caller's exclusions.
3. "README line 108 describes a 10-point handicap" attributes a code constant to the README.
4. The core contract block renames `runs --mine` and `runs show` while the paragraph above it says the CLAUDE.md lines keep working unchanged. Either keep `runs` as a permanent first-class alias or say the lines change after milestone 8.
5. `docs/integration-events.md` is cited as if on HEAD; it is on the Traycer branch.
6. Test file count 31 versus 30 on disk.

## Recommendation

Adopt B revision 4 as the plan of record. Add A's effort estimate and first-ticket list to the build sequence, the cancel-versus-complete rule and child propagation to the attempt state machine, and numeric release gates plus a soak to milestone 4. Fix the four wording residuals above before it becomes the plan of record. Keep A on file as the reference for lifecycle and release rigor.

Pending: round-2 peer scores on revision 4, in `~/.cache/subfleet-plan-compare/astra-scoring-r2.md` and `opus-scoring-r2.md`.

## Peer scorings, round 2 (revision 4, reports supplied)

| # | Dimension (weight) | Astra A | Astra B | Opus A | Opus B | Fable A | Fable B |
|---|---|---|---|---|---|---|---|
| 1 | Diagnosis (15) | 8.5 | 9.0 | 7 | 10 | 8 | 9.5 |
| 2 | Architecture (20) | 9.5 | 7.0 | 9 | 9 | 9 | 8.5 |
| 3 | Capacity and routing (15) | 7.5 | 8.5 | 6 | 10 | 7.5 | 9.5 |
| 4 | Invariants as tests (10) | 8.0 | 8.5 | 6 | 10 | 7 | 9.5 |
| 5 | Migration and effort (10) | 8.5 | 7.0 | 8 | 7 | 9 | 8 |
| 6 | Testability (10) | 9.0 | 8.0 | 9 | 9 | 9 | 8.5 |
| 7 | Fit to constraints (10) | 6.5 | 8.0 | 5 | 10 | 6.5 | 9.5 |
| 8 | Decision-readiness (10) | 8.5 | 7.5 | 6 | 10 | 7 | 9.5 |
| | Weighted total | 83.5 | 79.25 | 71.5 | 94.0 | 79.75 | 90.5 |
| | Winner | A by 4.25 | | B by 22.5 | | B by 10.75 | |
| | A+B | 10% on A | | 18% on B | | 5% on B | |

Mean across the three scorers: A 78.25, B 87.9. Astra's margin for A narrowed from 9.75 to 4.25 between revisions; Opus's margin for B widened from 10 to 22.5. The family split persists, and every scorer names the same A grafts: effort estimate, migration by account, cancellation ordering with child propagation, numeric release gates, an external-action state machine.

New round-2 findings carried into the build:

- Astra: `delegate.py:576` sorts `not fable_stranded(row)` ahead of `active`, so an active Fable-stranded lane can outrank an idle lane for non-Fable work; B's "strictly last" overstates v1. Moot for v2, where the desktop account is never a candidate without `--allow-desktop`.
- Astra: an empty recorded process group does not prove every writer stopped, because a descendant can `setsid` into a new group. v2 containment enumerates by process group, by parent chain, and by an inherited environment marker, and quarantines when any of the three cannot be evaluated.
- Astra: v1 gates call `delegate_main(peer_argv)` directly (`consensus.py:1127`), so repointing the CLI does not move them; the shadow period needs A's account-unit ownership rule.
- Both peers, both rounds: `runs --mine` must be permanent. v2 keeps every v1 verb spelling; `jobs` is the alias.
- Opus: A's "broad keyword" preamble diagnosis is wrong; the trigger is the workspace-write sandbox (`delegate.py:1069-1071`). A's design conclusion stands.
- Opus: Appendix B says "five blockers and twelve majors"; the review has 5 and 15.

Decision (Max, 2026-09-05 11:20): build the merged plan with Astra's help. Plan of record and build live in `~/subfleet-v2`.
