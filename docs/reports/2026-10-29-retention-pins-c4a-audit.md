# Retention dependency pins: continuation 4a contract audit, 2026-10-29

Base audited: `2f1c81ab982d030eef62e2db0e21fcc773b7585f` (#272). Documentation correction commits: `4ac3e781cc920dfb3408fdea161d030c33bf2da6` and `e8152e0063f9c781cf8894e8c169ece877eb3286`. Only the contract, the two named decision notes and this report are changed; the main report, tests and product code are untouched. Shared Git metadata refused `index.lock`; the ordered commits are delivered using private scratch metadata and the verified `rdp217c4a.bundle`.

Resolution at continuation 5: **D1 fixed in `42bdf10798ba43551adb98fdf9cf81a1ed0eed98`**, following red regression commit `c03ab23cf6a040ef8d365d882c9b4d9816db0c79`. Dependency attribution now uses the blocking query's shared `dependencies.key`; legacy `folders.retiring_folded` is unchanged. [Continuation 5 evidence](2026-10-29-retention-pins-c5-build.md) records the green regression, generated alias property and targeted mutants. Continuation 4b completed all its named verification. The original red replay below remains historical evidence.

Resolution after review r1 and fix round r1: the three allocated-worktree gaps found at `3893c7120` are fixed: Git C-quoted alternates decode on bytes; complete alternates files stream with conservative incomplete-discovery pins; and terminal legacy exporters retain their actual job-row output path. The separate job-directory dependency boundary already present in release/217 is fixed at the census, fence and final commit rechecks. The desktop timing sentence now cites the completed continuation 4b comparisons. [Fix round r1](2026-10-29-retention-pins-fix-r1.md) records red/green, normal properties, Git comparisons and named mutant results. These resolutions supersede the quoted historical status sentences below. Review r2 at `dd0e4ea8d` confirmed the original fixes but found two incomplete-discovery gaps: ordinary alternates file changes during discovery and terminal legacy exporters. [Fix round r2](2026-10-29-retention-pins-fix-r2.md) resolves both, with six red/green regression inputs, descriptor/pathname stability checks and legacy incomplete evidence preserved through terminal export rechecks. Earlier audit counts and timings remain dated evidence.

The audit reads the audited source, not the PR description. **True** means supported by the cited source; it does not certify an unfinished mutant or property. **Changed** identifies a false current-implementation description corrected in the documentation commits. **D1** identifies the defect found at the audited base and fixed in continuation 5; the contract promise was retained. Historical observations about old heads remain historical; no historical host probe is claimed as a new execution.

Started 2026-10-29 00:19:33 UTC; the three-hour stop point is 03:19:33 UTC. Environment setup used the required locked Python 3.14.7 dev sync once, with private TMPDIR, uv cache and virtualenv. No network beyond that sync, provider, live store, production CLI, fetch or push was used. The lane rules prohibit sub-agents; none was started.

## Audit overview

| Target | Verdict | Evidence / correction |
|---|---|---|
| C-6.5 | Changed | Read-only jobs hold reader rows; no source exclusive lease. [a.reader], [a.transaction]. |
| C-6.11 | True after D1 fix in c5 | Dependency roles and rendering include the Unicode alias in late reservation. [a.attribute], [render.needs]. |
| C-8.3 | True | Pending output lease pins through restart/export; written export releases the pin. [a.export], [d.live]. |
| C-8.4 | Changed; D1 fixed in c5 | Folded dependency lookups replace the exact-query gap; canonical nested row/lease checks remain. [d.fences], [r.select], [ar.commit]. |
| C-13.4 | True | Allocated-tree ownership, atomic row/fence checks, cwd holds and archive verification remain. [ar.owned], [r.select], [r.holders], [ar.commit]. |
| C-24.5 | True | Shared turn rows, writer exclusion and retention-only ancestor blockers remain. [a.transaction], [f.retiring], [c.previous]. |
| C-26.3 | True | Native/conversation serialization and canonical native keys remain. [a.native], [native.holds]. |
| Turn-folder case-alias decision | Changed | Replace current SQL/ASCII-only and firmlink-gap claims with folded dependency behavior; retain exact TURN/READER row checks. [d.key], [d.legacy], [f.turns]. |
| Desktop retention archive | Changed | Record merged nested fences, folded dependency admission and available dormant mode; retain the exact own-tree SQL join. [r.select], [ar.commit], [a.retention]. |

## Verification owed at audit, completed in continuation 4b

These identifiers are used in the sentence table. Every item was unrun or capped in continuation 3. This audit does not run them or infer the parallel lane's results. Source inspection supports the affected sentences, but their independent verification still depends on the named checks. These checks subsequently passed in [continuation 4b](2026-10-29-retention-pins-c4b-evidence.md); D1 was independently fixed and tested in continuation 5.

| Key | Exact outstanding check | Affected promise |
|---|---|---|
| M1 | `transaction-own-not-filtered` | One own-target fence is contested once, not duplicated as a dependency blocker. |
| M2 | `transaction-hold-names-no-place` | Reservation hold identifies the needed place and its role; D1 exposes a Unicode case outside the existing examples. |
| M3 | `git-storage-no-commondir` | Common directory is recorded. |
| M4 | `git-storage-no-worktrees-fallback` | Missing commondir falls back from worktrees/admin to the repository. |
| M5 | `git-storage-no-alternates` | Borrowed object directories are recorded. |
| M6 | `git-storage-alternates-one-level` | Alternates are followed five levels. |
| M7 | `git-storage-gitfile-relative-to-cwd` | Gitfile paths are relative to their containing folder. |
| M8 | `git-storage-nearest-ignores-gitfiles` | Gitfiles above a checkout are included. |
| M9 | `record-raw-spelling` | Dependencies are canonically spelled at recording. |
| M10 | `fences-not-retention-only` | Ordinary writer leases above a place do not become retention fences. |
| M11 | `render-ignores-needs` | `why` names the dependency role. |
| M12 | `turn-reason-ignores-needs` | Conversation reasons name git-storage dependencies. |
| M13 | `collate-nocase-restored` | Unicode folded pins replace ASCII-only SQL matching. |
| M14 | `record-no-output` | Output dependency is recorded. |
| M15 | `record-no-git` | Git storage dependency is recorded. |
| M16 | `admission-no-needs-role` | Early dependency hold contains its role. |
| P1 | `test_python_pins_and_transaction_agree_with_independent_reference` | Pin census and transaction agree with the independent reference. |
| P2 | `test_live_dependents_are_monotone_and_the_last_ending_allows_retirement` | Adding live dependents cannot release a pin; last ending permits retirement. |
| P3 | `test_early_fences_equal_reservation_before_and_after_quarantine` | Early and transaction fence/hold behavior agrees across quarantine. |

The 3,000 jobs / 100 live and 6,000 jobs / 600 live timing comparisons, final measurement-tool revalidation, and any query plans required by those results also belong to 4b. No timing comparison is performed here.

## Contract sentence table

Sentence IDs refer to the text at the audited base, in source order, splitting at sentence-ending punctuation followed by a capital or backtick. Each complete original sentence is included so the mapping remains stable after the corrections. Clause IDs retain merge_contract.py's `- **C-x.y**` single-line format; all **255** are unique. The only change-log addition names #272.

| Clause / sentence | Sentence at audited base | Verdict | Code site or change |
|---|---|---|---|
| C-6.5 S1 | Refusals are exit 7 with the fix named: API-key home, workdir under `/tmp`, writable job that would write in a checkout on `main` or `master` (C-13.2), `-o` path held by a live job of another request id, a symlink loop in the requested `-o` path, a writable job for a session id that already has one running from another instance, a second writable job on the same worktree (a detached in-place job where a writable conversation turn is live, queued or running, or its attempt quarantined and its `worktree-turn:` row still held, is such a second writer; turns among themselves are not, C-24.5), C-8.5 host-push metadata above its config cap or sparse objects and objects above its cumulative intake cap, a writable job beyond `caps.max_writable_per_session` when a policy sets it (none by default, C-6.4), a child beyond `caps.max_child_jobs` when a policy sets it (none by default), an isolated review (`-I`) that would inherit `CLAUDE_CODE_MANAGED_SETTINGS_PATH` or a remote managed-settings variable, and an isolated Codex review whose home carries managed requirements or a non-empty system or project config layer (C-23.3). | True | [a.home], [a.submit], [a.writable], [a.conflicts], [isolation], [isolation.codex]; refusal taxonomy retained. |
| C-6.5 S2 | This list is the index of refusals: a new refusal is added here, not in a clause of its own. | True | The refusal sites [a.home], [a.writable], [a.conflicts], [isolation] implement the indexed refusal rules. |
| C-6.5 S3 | What holds a writable job is where it writes. | True | [a.submit], [a.transaction]. |
| C-6.5 S4 | The write target of an in-place job is the real path of the checkout that contains its workdir (`git rev-parse --show-toplevel`), or for a conversation's turn outside git its folder (C-26.10), so `/repo` and `/repo/sub` are one target and two linked worktrees of one repository are two; a target is spelled one way (`folders.canonical`: `~`, symlinks, `.` and `..` resolved, and each name as the kernel stores it, whatever case or Unicode form it was typed in), so on a case-insensitive volume `~/Scratch` and `~/scratch` are one target, as a conversation's workspace is (review of 5e9f2fbd, P3-4); the target of any other writable job is the worktree the daemon allocates for it (C-6.6), which no other job shares. | True | [a.folder], [f.canonical], [a.transaction]. |
| C-6.5 S5 | Such a job holds that worktree, never the checkout it is cut from, and a read-only job holds no folder: neither waits for an ordinary writer's lease on its source folder, but each waits while retention retires a tree that folder is or is in (C-8.4). | Changed | CHANGED: a read-only job holds a reader row on its resolved run folder; the source checkout has no exclusive lease. [a.reader], [a.transaction]. |
| C-6.5 S6 | Submit records that folder, the workdir spelled as a target is, as `folder` in the `job.submitted` event, so admission reads the fence by string operations alone. | True | [a.submit], [a.submitted], [a.early]. |
| C-6.5 S7 | One instance of a session may therefore hold any number of writable jobs on distinct targets, with no count unless a policy sets `caps.max_writable_per_session`, a backstop against a runaway caller and not a throttle (C-6.4: no count bounds what runs by default). | True | [a.writable], [a.conflicts]. |
| C-6.5 S8 | An instance is a process: the caller's pid with its boot id and start time (C-5.3), read by the daemon at submit and recorded with the target in the `job.submitted` event, so a recycled pid is never mistaken for it and no schema change is needed. | True | [a.instance], [a.submitted]. |
| C-6.5 S9 | A submission is refused when the session has a live writable job whose recorded instance is another process that is still running (the twin), and also whenever the two cannot be told apart: the submission carries no pid, either process cannot be inspected, or the live job recorded no instance. | True | [a.writable], [a.instance_alive]. |
| C-6.5 S10 | An instance that is provably gone (no such pid, or the pid now belongs to a later process) is not a second instance: a session resumed after a restart or an account switch is one instance whose detached jobs survived, and it may dispatch again. | True | [a.instance_alive], [a.writable]. |
| C-6.5 S11 | A revive is always another instance of the session it continues (C-23.54), so a revive and any other writable job of that session refuse each other as before. | True | [a.refuse_revive], [a.writable]. |
| C-6.5 S12 | Both questions need `ps` or `git`, so they are answered under the submit lock before the inserting transaction, which re-checks with SQL alone and refuses any live job the submission was not cleared against (C-3.3) (incident: 2026-09-20, a session handing five stalled threads to parallel lanes was refused every writable job after the first with "caller_session is held by a live job", whichever worktree it named; the code compared session ids and never instances, so it refused the one instance this clause never meant to stop). | True | [a.writable], [a.conflicts], [a.submitted]. |
| C-6.5 S13 | Milestone 9 adds: a socket `submit` of `kind: "turn"` or a `turn:` request id (C-26.1); a person-only operation from an agent (C-25.6); a writable turn whose workspace contains the state root, `~/.claude`, `~/.codex`, a lane home, or the guard's directory, and a turn on `main` or `master` without `allow_main` (C-26.10). | True | [a.submit], [c.dispatch]; retained turn boundary and workspace refusals. |
| C-6.5 S14 | Output paths, including their parents and absent destinations, compare by the shared folder identity at conflict checks, reservation, export and recovery: resolve the kernel spelling, then NFC on a sensitive volume; on an insensitive volume strip Unicode default-ignorables, equate dotless i with i, and use NFD(casefold(NFD(path))). | True | [f.identity], [out.prepare], [out.holds]. |
| C-6.5 S15 | Display spelling stays as supplied and old raw lease aliases still exclude another owner. | True | [out.prepare], [out.holds], [a.export]. |
| C-6.5 S16 | Known conservative gaps lean toward over-collision: full casefold expansions, dotless i on APFS, default-ignorables on APFS, and canonical normalization on sensitive volumes can refuse distinct files. | True | [f.identity] explicitly keeps these conservative equivalences. |
| C-6.5 S17 | An OSError, including PermissionError, ELOOP, EIO or TimeoutError, falls back to the exact supplied string and is logged once per path rather than stopping unrelated jobs. | True | [f.identity] catches OSError and calls the logged exact-string fallback. |
| C-6.5 S18 | This may under-collide aliases while the filesystem is unreachable, matching 2.1.11. | True | [f.identity] exact-string fallback on an inaccessible path. |
| C-6.5 S19 | An identical stored output path remains refused even if a quarantined owner holds only its canonical lease. | True | [a.conflicts], [out.holds]. |
| C-6.5 S20 | External path replacement or directory renaming during a live lease remains an under-collision limit of path-based ownership (audit L4). | True | Path keys [out.prepare] are not inode locks; no live external-renaming guarantee is made. |
| C-6.5 S21 | Identity answers are memoized only for one submit, admission pass or export, outside the main-store transaction, with no stored column or back-fill. | True | [out.prepare], [a.pass], [a.export]; each builds an operation-local identity cache outside its writer. |
| C-6.5 S22 | For legacy alias lease pairs, the oldest acquired_at wins, breaking ties by holder then lease key; other holders persist export_error: superseded by <job>, append that reason to their notices, and release all their job and accepted-attempt leases at export. | True | [out.holds], [a.export], [a.export_error]. |
| C-6.5 S23 | Every output export either records an export artifact or export_error, including missing ownership or deliverables, and releases those leases. | True | [a.export], [a.export_error]. |
| C-6.5 S24 | Beside it, submit records the other places the job needs while it has not ended, spelled the same way, as `depends`: its review root, its `-o` path and the git storage its checkout uses; admission reads the fence on those too (C-8.4). | True | [d.record], [a.submitted], [a.needs], [a.early], [a.needs_blockers]. 4b: M3–M9, M14–M15. |
| C-6.11 S1 | A job that is not placed always has a stated reason, and a fleet that places nothing says so. | True | [a.admit_kind], [a.note], [a.why]. |
| C-6.11 S2 | Each admission pass records, per job it leaves unplaced, why: `behind-older-job` with the older job's id and the tier (C-6.9), and `for_good` (below) when its capacity clock runs and every lane refuses it for a standing reason (C-11.8); `fleet-full` with `max_active_attempts`; `slot-kept` with `kept_for`, `live`, and `max_active_attempts`, when a lane would take the job and the fleet is one short of its cap because C-6.9 keeps the last slot for an older waiting job of the tier (it is not reported as a full fleet, which an operator can see it is not); `parent-cap` (these four, and `behind-older-job`, only in a pool with a count cap, C-6.9; in an uncapped pool `no-slot` means a probe, a latched credential or, with other lanes held otherwise, a pilot holds the lane); `lane-proving`, when every lane that could take a detached job has room only but for its pilot (C-6.14), each rejection's `slot_block` naming the pilot (`proving:<attempt id>`); `disk` with `free_gb`, `reserved_gb`, `floor_gb`, `resume_margin_gb` and `placement_reserve_gb` (C-6.17); `ramp` with `used`, `limit`, `window_s` and `next_slot_at` (C-6.18); `machine-busy` with `class` and the reading that held it (`cpu_idle` and `cpu_idle_min`, `disk_busy` and `disk_busy_max`, `load_per_cpu` and `load_threshold`, `memory_pressure` and `memory_threshold`, plus CPU/disk release progress, C-6.13); `lease-held` with the lease keys another job holds (output keys use C-6.5's canonical comparison identity even for a legacy raw guard; the job's `out_path` keeps its display spelling), among them a key the job needs free but does not take (a turn's `worktree:<target>`, the `worktree-turn:` rows of the turns in a detached writer's folder, C-24.5, and retention's fence on a detached job's source folder or on a folder above any job's, C-8.4), which is never queued for, on a look on the job's clock as at a full one; a hold with a retention fence carries `retiring`, those keys, and the checked `folder`; when that is another dependency place, the hold also names its `needs` role (`review-root`, `output`, `git-storage`), and `why` describes retention removing that finished job's tree (C-8.4) (and, for a turn, `queued`: free keys kept for an older turn, C-26.9); `probe-pending` with the `lane` and `model` of the probe, and `behind` when that probe is an older job's turn (C-6.9); `route-moved` with `tries`, when the job's route could not be settled in C-6.3's `ROUTE_TRIES` reservations in a row (a commit before each check moved the lane its pin names, or removed a reading the decision rested on) and it keeps its place for the next pass; `attempt-live` (an earlier attempt of the job is still live or quarantined, found before the job is looked at or again inside its reserving transaction); `conversation-blocked` with `conversation_id`, `blocked_by` and `legacy_hold` (a turn whose conversation is blocked, C-24.5), or with `error_type` and `error` when its turn manifest is missing, cannot be read, or does not name its conversation, message and provider, or its conversation could not be checked for a reason other than a store error, which holds that job alone (a store error is the pass's, C-6.12); `message-settled` with `conversation_id` and `state` (its message was withdrawn after the job was made; the job is cancelled, C-24.7); `approval`, `uncertain`, `workspace` (C-4.1); `route` with `error_type`, `error`, and `deferrals` (C-6.12), which is reported as that whatever else holds the job while its clock runs; `pin-unadmittable` with `lane_id`, `reasons`, `closures`, `since` and `fail_at` (C-11.8: its pinned lane can never admit it), which is reported as that whatever else would hold the job, except a wait on `approval` or `uncertain`; otherwise the commonest first reason lanes rejected it for (`reserve:fable:unmeasured`, `closed:account`, `no-slot`, ...), with the `next_check_at` of any wait that has one, and `for_good` with the standing reasons when every lane refuses it for one (C-11.8), in which case `why` adds that it holds no other job back; a pass that asks it again on the job's clock replaces the last look's `for_good` with its own answer. | True after D1 fix in c5 | [a.early] and [a.attribute] name the role using the same dependency comparison in `42bdf10798ba`. Other hold construction remains in [a.pass]; rendering is [render.needs], [render.why], [c.note], [c.describe]. 4b: M2, M11–M12, M16, P3. |
| C-6.11 S3 | A wait that is a person's or a retry's to end (`approval`, `uncertain`, `workspace`) is reported as that even when the fleet is also full or an older job is also ahead of it. | True | [a.pass] checks operator/workspace waits before ordinary capacity ordering. |
| C-6.11 S4 | `hold` is the last pass's answer and `recheck` describes the last look (`recheck.label` is what that look found): the reason is the latest look's, not the first's, whether or not the verdict repeated; a look that ends without changing a verdict or a clock still refreshes it; and a pass that does not look at the job repeats the last hold in full (`leases`, `max_active_attempts`), so `why` never degrades between looks. | True | [a.capacity], [a.admit_kind], [a.why]. |
| C-6.11 S5 | A `behind-older-job` hold whose named predecessor no longer holds back later jobs is instead rechecked on this pass (C-6.10), so a stored hold never keeps a job behind a predecessor that has started or ended or now holds nobody back. | True | [a.pass] revalidates its named predecessor before retaining a hold. |
| C-6.11 S6 | A pass that raises publishes nothing: the holds and the `admission` snapshot stay as the last whole pass left them, because half a hold set would read as nothing left pending, and C-5.10 logs and paces the failure. | True | [a.admit_kind] publishes only after the pass returns. |
| C-6.11 S7 | When every lane has a standing reason the label is the commonest of those and not `fleet-full`: a probe's reservation counts toward the fleet cap, so such a job would otherwise read `fleet-full` for the second each probe runs. | True | [a.pass] chooses dominant standing rejection before the fleet-full fallback. |
| C-6.11 S8 | The record is in memory and replaced whole each pass; C-26.9's turn pass and the detached pass each replace their own jobs' part, and `hold` is read from the two together. | True | [a.admit_kind], [a.why]. |
| C-6.11 S9 | `subfleet why <job>` returns it as `job` (`state`, `tier`, `wait_reason`, `next_check_at`, `hold`, `recheck`, `decision_source`, `decided_at`) and as `queue`, the same in sentences, ahead of the decision; for a turn it also returns `lanes`, its lane ids by reason, the first reset among the closed ones and the capacity blocks (`conversations.waits.lane_summary`), from which its message says why it waits (C-24.4). | True | [a.why], [render.why], [c.describe]. 4b: M11–M12. |
| C-6.11 S10 | A queued or waiting job with no decision row (admission held it back, or no pass has reached it) is evaluated for the answer and marked `decision_source: evaluated-now`; no decision is recorded and no job changes, because answering is not admission. | True | [a.why] calls _pick only for a pending job without a decision and sets evaluated-now. |
| C-6.11 S11 | The evaluation asks who the desktop login is, as every evaluation does (C-10.3), and a newly verified identity is recorded as the `desktop.identity` event it would have been on the next pass. | True | [a.desktop], [a.why]. |
| C-6.11 S12 | A job that ended without a decision prints its state and `No decision recorded.`, unless admission refused it (C-6.12), which prints `Refused at admission: <message>` in its place The CLI never prints `null`, also against a daemon that predates `queue`. | True | [a.why], [cli.why]. |
| C-6.11 S13 | `daemon.status` carries `admission`: `pending` (unplaced jobs other than those held as `approval`, `uncertain`, `workspace`, `attempt-live`, `conversation-blocked` or `message-settled`, which are a person's or a retry's to end), `placed_at`, `idle_for_s` and `idle_since` (null unless jobs are pending and none has been placed), `reasons` (count per reason), and `open_lanes`; it is one snapshot, replaced whole at the end of a pass, so a status request never reads half of an update. | True | [a.note], [a.status]. |
| C-6.11 S14 | `open_lanes` is the lanes owned by v2, enabled, not the desktop login while Claude Code uses it (C-10.3), identity not mismatched, under no account-wide closure, with a slot free (always, while no per-lane cap is set) and no probe or pilot (C-6.14) holding it. | True | [capacity.open]. |
| C-6.11 S15 | Open means capacity exists, not that it fits: a job can still be refused an open lane by a model-scoped closure, the reserve, or its own exclusions, and the reasons say which. | True | [capacity.open], [a.pass] still evaluates job-specific rejections. |
| C-6.11 S16 | `daemon.status` also carries `machine_guard`: its enabled state and, when enabled, C-6.13's values, units, ages, readability and last read cost; `subfleet status --json` preserves that object. | True | [a.response_status], [cli.status]; machine_guard is included in the daemon status response. |
| C-6.11 S17 | `subfleet status` prints one `admission:` line while `idle_for_s` is set, and its `running jobs:` count and table cover live jobs only. | True | [cli.status]. |
| C-6.11 S18 | When jobs have been pending with none placed for 60 s, `daemon.log` gets one line (count, seconds idle, open lanes by id, reasons by count, and the first job in admission order), and one line when placing resumes or nothing is left pending. | True | [a.note]. |
| C-6.11 S19 | The line is a warning when a lane is open and some job is held for a reason other than ordinary queueing (`fleet-full`, `slot-kept`, `parent-cap`, `no-slot`, `lane-proving`, `lease-held`, `probe-pending`, `behind-older-job`, `route-moved`, `machine-busy`, `ramp`); otherwise it is information, because with no lane open or a fleet at its cap waiting is the expected answer. | True | [a.note]. |
| C-6.11 S20 | The fleet is looked at again every 600 s while the stretch lasts: a warning is written each time, information at most every 3600 s, so a wait that was expected when it was first logged becomes a warning within ten minutes of becoming one (incident: 2026-09-20, fifteen jobs from six sessions sat queued for more than three hours beside open lanes; `subfleet why` printed `null` for every one of them, because a job admission skips gets no decision row and the CLI printed the missing value instead of the daemon's text; `subfleet status` said `running jobs: 519`, the size of the store, because `daemon.status` carries every job as `jobs`; and `daemon.log` had not gained a line since the daemon started). | True | [a.note]. |
| C-8.3 S1 | The `-o` export is a copy of the accepted deliverable published by C-8.1 after acceptance. | True | [a.export] reads the accepted deliverable and publishes its bytes. |
| C-8.3 S2 | A failed export sets `export_error` on the job and emits an event; the job stays `succeeded`. | True | [a.export], [a.export_error]; export failure does not rewrite succeeded. |
| C-8.3 S3 | The job holds `out:<path>` from its reservation until its export is written, or until it ends without one; an export not yet written, also one a restart finishes, keeps a finished job's allocated worktree that path is in (C-8.4). | True | [a.native] takes the output key; [a.export] releases it only after export/error handling; [d.live] reads outstanding output leases regardless of job state. 4b: P1, M14. |
| C-8.3 S4 | Once written, the export owes nothing to the folder it was written into: when that folder is in a finished job's allocated worktree, retention may archive that tree with the exported file in it (C-8.4: the file is then in `$SUBFLEET_HOME/archive/<that job>/`, no longer at the path), while the accepted deliverable stays in the exporting job's own directory until that job is retired. | True | [a.export], [d.live], [ar.file], [ar.verify], [ar.commit]; there is no dependency pin after the lease is released. |
| C-8.4 S1 | Retention: newest 500 jobs or 2 GiB (policy `retention.jobs`, `retention.bytes`), whichever binds, counted over jobs that are not conversation turns (turn jobs have their own budget, C-26.12), computed outside transactions in a maintenance pass; jobs that are active, `quarantined`, have unread notices addressed to a session, hold salvage refs referenced nowhere else, or are a gate's evidence are never pruned. | True | [r.budget], [r.pass], [r.pins], [r.queries]. |
| C-8.4 S2 | A notice with no session can never be read, so it pins nothing (review IR-17). | True | [r.queries] restricts unread-notice pins to nonempty session ids. |
| C-8.4 S3 | Probe and keepalive results live in `readings` and `events`, not in `jobs`. | True | Readings/events are separate store tables; probe paths do not submit retained jobs ([a.pass]). |
| C-8.4 S4 | I5: a job with its own allocated worktree (writable, not in place) is protected as `turn-folder` by the archive driver's `_pin_reasons`, at selection and again before committing, while a live `worktree-turn:` or `worktree-read:` row names its recorded worktree or a folder inside it (a conversation in a repository nested there keys its row on that repository); a row on the in-place workspace keeps no in-place job, whose workspace retention never removes. | True | [r.pins], [r.select], [ar.commit], [f.turns]. |
| C-8.4 S5 | A row on `jobs/<job>` or a folder inside it keeps that job, including an in-place job, because retirement moves the job directory too. | True | [ar.job_fence], [f.turns]. |
| C-8.4 S6 | Before moving either tree, `Retirement._fence_job_folder` checks turn rows and queued or live turn workdirs and fences the canonical `jobs/<job>` directory atomically, including on recovery. | True | [ar.job_fence], [ar.quarantine]. |
| C-8.4 S7 | Quarantine cleanup keeps live turn cwds and reader or turn rows, including detached jobs and quarantined attempts, and fences its location before reclaiming it. | True | [ar.q_fence], [a.reader], [ar.reclaim]. |
| C-8.4 S8 | Its retirement transaction checks `folders.turn_holds` on the canonical worktree and every folder inside it before taking `retire:<job>` and `worktree:<folder>` together, and the commit transaction checks them again on the journal's canonical spelling, so a recorded worktree spelled in another case, or none recorded, does not hide a row; turn and detached-job reservation check that fence in the same store transaction, on the folder and on every folder above it (`folders.retiring`: string operations on the canonical spelling, no filesystem work), and hold writable turns, readers and detached jobs there `lease-held` until retirement releases it, also one in a repository nested inside the tree, whose row or `worktree:` lease is keyed on that repository; a retention hold for either a turn or a detached job names the fence keys as `retiring` and the folder checked, and `why` says retention is removing the finished job's tree (C-6.11). | True after D1 fix in c5 | Nested-row/fence guarantees are true: [r.select], [ar.commit], [f.turns], [f.retiring], [a.transaction]. The hold-folder subclaim is fixed at [a.attribute] in `42bdf10798ba`; [render.why] receives the complete dependency hold. 4b: M2, M11–M12, M16, P3. |
| C-8.4 S9 | Its `leases` list names all held blockers; `retiring` names only the retention-held `worktree:` keys among them, in that order, and is omitted when none exist. | True | [a.early], [a.attribute] construct retiring from retention-owned worktree blockers only; [render.needs] preserves other leases. 4b: M1, M10. |
| C-8.4 S10 | The early look names retention's fences in both lists, while a reserving hold can additionally name ordinary writer or other held leases in `leases`. | True | [a.early], [a.attribute]. |
| C-8.4 S11 | Every reservation fences its canonical resolved run folder in the same transaction that takes its leases. | True | [a.reader], [a.transaction]. |
| C-8.4 S12 | A turn has a reader row there when it differs from its Git hold; every detached job has a job-held reader row there for the whole lease lifetime, including retries and quarantined attempts. | True | [a.reader], [f.turn_key]. |
| C-8.4 S13 | Detached launch uses that reservation's recorded resolved folder, so retargeting the submitted symlink after reservation does not redirect the provider away from its pin. | True | [a.resolved_launch], [a.launch]. |
| C-8.4 S14 | These reader rows exclude no writer and do not change C-6.5's exclusive contention on the write target. | True | [a.transaction] contests TURN rows for writers, never READER rows. |
| C-8.4 S15 | Admission spells that submitted target again before reservation (`folders.present`, with the detached in-place answer taken before `_workspace`); if its pathname now resolves through a replacement symlink, the exclusive lease names the resolved folder. | True | [a.detached], [a.pass], [f.present]. |
| C-8.4 S16 | A read-only detached job's folder, and an allocated writer's source folder, is its workdir spelled once at submission (`folder` in `job.submitted`). | True | [a.submit], [a.submitted]. |
| C-8.4 S17 | Every detached admission look also spells where the workdir resolves now (`_detached_folders`, outside transactions), so a workdir replaced by a symlink into another fenced tree is checked there as well as on its recorded folder; Each modern recorded/current source carries its own absence answer to the early and reserving checks, using the same absent-name folding as row subjects independently of the row answer; no spelling is cached between looks. | True | [a.detached], [a.early], [a.transaction]. |
| C-8.4 S18 | A legacy detached job with no recorded folder or write target uses that current spelling with NFC normalization and casefolding (`folders.retiring_folded`), including when quarantine has made its original names unspellable. | True | [a.detached], [f.retiring]; legacy dispatch selects retiring_folded rather than the present-name exact lookup. |
| C-8.4 S19 | This can conservatively hold a legacy job on a case-equivalent name on a volume that distinguishes case. | True | [a.detached], [f.folded]; insensitive comparison is deliberately conservative on a sensitive volume. |
| C-8.4 S20 | An admission look that reaches the early fence check reads these folders before `_workspace`; a fence still present in the transaction recording the wait skips workspace preparation and runs no workspace Git. | True | [a.detached], [a.early], [a.pass]. |
| C-8.4 S21 | An `unspelled` in-place job can first run `_rediscovered`'s read-only lookup. | True | [a.rediscover]. |
| C-8.4 S22 | Operator holds, standing pin refusals, disk and machine checks and pool ordering can hold a job earlier. | True | [a.pass] performs those checks before reaching _fence_hold. |
| C-8.4 S23 | A fence acquired after the early read is still checked in the reserving transaction, though workspace preparation may already have run. | True | [a.transaction], [a.needs_blockers]. 4b: P3. |
| C-8.4 S24 | Fences found after the pass's lease census count as seen, so release brings the next look forward (C-6.10). | True | [a.seen], [a.early], [a.attribute]. |
| C-8.4 S25 | An early fenced priority job keeps its priority place across tiers under a pool cap (C-6.16). | True | [a.early] appends its waiter to priority_waiters. |
| C-8.4 S26 | Admission also checks the fences on both a turn's Git folder and its canonical actual run folder, and on every folder above either, before workspace preparation, using the same `folders.present` spelling and folded comparison for absent names as the reserving transaction, with a plain read followed, when a fence is found, by a transaction that checks the job's state and re-reads the fences. | True | [a.early], [a.pass], [f.present], [f.retiring]. |
| C-8.4 S27 | The Git folder this look reads is the one the reserving transaction names: for a turn submit could not spell in full (`unspelled`, below), the folder found again before the look, whose one answer the transaction uses too (`_rediscovered`), so a persisted `core.worktree` naming a fenced top outside the turn's folder holds the turn before its workspace is prepared (review of #141 at 2cbf6fca, P2-A). | True | [a.rediscover], [a.pass]. |
| C-8.4 S28 | An eligible turn whose fence is still held in that transaction skips workspace preparation and runs no git in the tree being retired but, for an `unspelled` turn, that lookup: `git rev-parse --show-toplevel` in its folder (a second time when git finds no checkout under a `.git`), which only reads, run without git's diagnostic trace variables (`GIT_TRACE*`, dropped from every git Subfleet's salvage helpers run, and Trace2's three targets set to `0`, which turns off a `trace2.*Target` in the global or system config too) (measured on APFS: no content, mode, mtime or ctime of the tree, its nested `.git` and index, a linked worktree's admin directory or an external top moved; a read may move an atime, which retention does not sign). | True | [a.rediscover], [a.folder], [g.env]. |
| C-8.4 S29 | When that lookup cannot finish (C-6.8), a turn whose recorded folder is fenced still waits on that fence, and any other waits for its workspace. | True | [a.pass] retains the recorded-folder fence path on rediscovery failure. |
| C-8.4 S30 | The turn receives the hold the reserving transaction records when retention's fences are the only blockers it finds: `lease-held` on the same fence keys in the same order, the same folder named (its actual run folder when that is fenced, otherwise its Git folder), a capacity wait under the same signature (so the same backoff), and the same `waiting` row and clock. | True | [a.early], [a.transaction], [a.capacity] agree on folder-fence waits. Dependency-only attribution is fixed in `42bdf10798ba`. 4b: P3. |
| C-8.4 S31 | Where the transaction would also find a key retention does not hold (a detached writer's `worktree:` on a writable turn's own folder, a lease another job holds or is queued for), its hold would list that key too and its signature would cover it; the look's lists retention's fences alone, and the first look after they are let go finds the rest. | True | [a.early], [a.transaction]. |
| C-8.4 S32 | This hold precedes the placement checks and refusals that follow workspace preparation. | True | [a.pass] calls the early fence path before _workspace and later placement validation. |
| C-8.4 S33 | For a still eligible turn, a fence released before that transaction allows the look to proceed; a job ended or cancelled meanwhile is neither held nor prepared. | True | [a.early] re-reads job state/cancellation and the fences inside job.fenced. |
| C-8.4 S34 | The reserving transaction still checks the fences and holds a turn if retention acquired one after the early read; that look may already have prepared its workspace in the tree. | True | [a.transaction], [a.needs_blockers]. 4b: P3. |
| C-8.4 S35 | An in-place writer's folder is its write target, its checkout's top level recorded at submit and spelled again at reservation (C-6.5), and a fence on that folder itself is its own `worktree:` key, contested as before; an allocated writer's write target is the worktree admission allocates for it (C-6.6). | True | [a.folder], [a.pass], [a.transaction]. |
| C-8.4 S36 | Such a job waits through the retirement. | True | [a.early], [a.transaction]. |
| C-8.4 S37 | A queued job whose recorded folder, row path or dependency folds within the allocated worktree keeps that tree at the commit (`worktree-in-use`, while the job has not ended), which rolls the retirement back; submission records a job's workdir in its one spelling (`folders.canonical`), so a workdir typed in another case or Unicode form than its volume stores, or through the Data volume's firmlink (`/System/Volumes/Data/…`), still names the tree it is in, as long as every name in it can be looked up then; a name that cannot (gone, or under a directory that cannot be searched at that moment) stays as typed. | True | [a.workdir], [f.canonical], [d.key], [d.live], [r.select], [ar.commit]. 4b: M13, P1. |
| C-8.4 S38 | Of the leases on a folder above a turn's or a detached job's, only retention's fence holds it, never a detached writer's (C-6.5). | True | [f.retiring], [d.fences] filter retention holders. 4b: M10. |
| C-8.4 S39 | Read-only detached jobs and allocated writers take no exclusive lease on their source folder and wait there only for retention; writable turns and in-place writers retain C-6.5's contention on their exact write target. | True | [a.transaction], [a.reader], [a.needs_blockers]; READER rows add protection without source exclusivity. 4b: M1, M10. |
| C-8.4 S40 | Admission spells the folder a row will name again off the store lock before its reserving transaction; detached in-place writers take this answer before `_workspace` and reuse it at reservation. | True | [a.pass], [a.detached], [f.present]. |
| C-8.4 S41 | It reserves a row (a turn's, or an in-place writer's `worktree:`) only on a folder whose every name it looked up (`folders.present`), so a row names its folder as the volume stores it, whatever submit recorded: a native session's cwd typed `…/jOB/vendor/lib` for the tree `Job`, spelled at submit while the tree was in quarantine, kept the names that could not be looked up as typed, and a row on it matched none of the checks above once its job had ended (review of 8a112986, finding 1); and a folder submit could not spell in full (`unspelled` in `job.submitted`, judged on the spelling submit records) is found again at admission as submit finds it, from its checkout's top level, once per look and before the look for retention's fence, whose answer the reserving transaction uses (never for a push job, C-8.5); where git finds no checkout though a `.git` is at or above the folder (its tree away for git's look), submit and admission ask git once more, so a turn submitted from a subdirectory while its tree was away still holds its checkout, not the subdirectory (C-6.5; review of 3410b4f0, P1; review of fb674463, Q1, Q1b and Q1c). | True | [a.folder], [a.rediscover], [a.pass], [f.present]. |
| C-8.4 S42 | A folder with a name that is not there reserves nothing: it waits `lease-held` on a retention fence it is in, compared case- and normalization-insensitively (`folders.retiring(..., folded=True)`), and its queued job keeps the tree at the commit when any of its recorded places folds within the allocated tree (`worktree-in-use`); with no such fence it waits for its workspace (C-6.8). | True | [f.retiring], [a.early], [d.live], [ar.commit]. 4b: P1, P3. |
| C-8.4 S43 | A `worktree:` lease held by anything but the job's own retirement (`retention:<job>`) keeps the job as `worktree-lease`: one on its recorded worktree keeps any job, and one on a folder inside it keeps a job with its own allocated worktree, as a turn row does (a detached in-place writer in a repository nested there holds `worktree:<that repository>`, and its quarantined attempt leaves its job `lost` with the lease kept); the retirement transaction reads those leases on the canonical worktree and every folder inside it (`folders.exclusive_inside`, one key range) before taking the fence, and the commit transaction reads them again on the journal's canonical spelling. | True | [r.queries], [r.pins], [r.select], [ar.commit], [f.inside]. |
| C-8.4 S44 | A job may need other places than its folder (`dependencies`): an isolated review's root (`-I -D`; `gate pr` sets it to the pull request's checkout), its `-o` path, which the daemon writes after the job ends (C-8.3), and the git storage its folder's checkout uses: for each `.git` at or above the folder, the directory it is or names (`gitdir:`), that directory's common directory (`commondir`, or the repository two levels above `worktrees/` when that file is gone) and the object directories its alternates name, five levels deep, read from those files without running git, so a `.git` naming a directory in quarantine still names it. | True | [d.record], [d.git], [d.storage], [d.alternates]; file parsing has no Git subprocess. 4b: M3–M8, M14–M15. |
| C-8.4 S45 | A checkout outside a tree can keep its repository inside one (`git -C <tree>/vendor/lib worktree add <outside>`), where every git command run in the checkout reads and writes, and where a worktree writer submitted from there registers its new worktree (N4). | True | [d.git], [d.storage], [a.submit]. 4b: M3–M8, M15. |
| C-8.4 S46 | Submit records these, spelled once (`folders.canonical`), as `depends` in `job.submitted`, beside the folder. | True | [d.record], [a.submitted]. 4b: M9, M14–M15. |
| C-8.4 S47 | `worktree-in-use` keeps a job's allocated worktree (recorded, or allocated with none recorded) while another job that has not ended needs a place that is the tree or inside it: its workdir, worktree, review root or `-o` path as its row holds them, its recorded folder or write target (or, for a job queued before the folder was recorded, the folder admission spelled, `job.folder_spelled`), or a place in `depends`; and while an export to a path there is pending (some job holds `out:<path>`). | True | [d.live], [d.users], [d.dependents], [r.pins], [r.select], [ar.commit]; own live/export evidence is protected by earlier reasons. 4b: P1–P2, M13. |
| C-8.4 S48 | Places compare folded (`folders.fold`: Unicode's canonical caseless form, under which APFS finds a name), so a row's workdir typed in another case or Unicode form than the volume stores, which `Path.resolve` keeps, names the tree. | True | [d.key], [f.fold], [d.live]. 4b: P1, M13. |
| C-8.4 S49 | The pass reads this, and so do the selecting transaction and the commit transaction, again on the fence's own spelling, by database reads alone: each job's first `job.submitted`, by job id (`events_job`), and the `out:` leases, by a range of their key (validation status in `docs/reports/2026-10-27-retention-dependency-pins.md`; new timings are pending). | True | [d.queries], [d.live], [r.pins], [r.select], [ar.commit]; query shape is inspectable, timing remains 4b work. 4b: P1. |
| C-8.4 S50 | Admission reads retention's fence on each place in `depends` by its recorded spelling, before preparing the workspace and in the reserving transaction; a matching dependency fence holds the job `lease-held` until the retirement lets go (C-6.11 names the place). | Changed lookup; D1 fixed in c5 | CHANGED: dependency fences compare normalized paths folded in both reads ([d.key], [d.fences], [a.early], [a.needs_blockers]); the parenthetical promise that C-6.11 names the place is true after `42bdf10798ba`. 4b: M1–M2, M10–M12, M16, P3. |
| C-8.4 S51 | Known limit of dependency admission: unlike the pin query, this lookup does not fold an unspellable tail, so a case or Unicode alias retained while its parent is in quarantine can miss that dependency fence. | Changed | CHANGED: an unspellable case/Unicode tail still finds the folded dependency fence ([d.key], [d.fences]). 4b: P3. |
| C-8.4 S52 | Folded dependency admission remains unfinished in this integration. | Changed | CHANGED: dependency fences are retention-held worktree keys read in one indexed range, nearest first per place ([d.fences]). 4b: M10, P3. |
| C-8.4 S53 | A job a daemon queued before `depends` was recorded has those places read for it outside any transaction, from its row: by admission when it looks at the job, and by retention once a pass (no such job is newer than a pass). | True | [d.fenced], [d.legacy], [a.needs], [r.pass]. |
| C-8.4 S54 | Git-storage discovery does not interpret `GIT_DIR`, `GIT_COMMON_DIR` or `core.worktree`. | True | [d.git] reads gitfiles/commondir/alternates only; no environment/config interpretation. |
| C-8.4 S55 | A written export keeps nothing (C-8.3): retention may archive a tree a finished job exported into. | True | [a.export], [d.live]. |
| C-8.4 S56 | Folder strings are compared as recorded, with canonical spelling established outside transactions (`folders.canonical`), never filesystem inspection under the store lock. | Changed | CHANGED: distinguish exact folder-row checks from normalized folded dependency checks ([f.turns], [f.inside], [d.key], [d.fences], [d.dependents]). |
| C-8.4 S57 | Pruning archives first (d635, `docs/desktop/retention-archive.md`): a job's files (worktree and job directory, byte for byte as clones or copies, except a tracked file whose raw bytes are a blob a network remote's refs reach in a repository that is not scratch, read back whole, and except regenerable output: inside the entries a tool writes at the top of a worktree directory whose structure says it is a virtualenv, `node_modules`, `__pycache__` (all of it) or a tool cache carrying `CACHEDIR.TAG`, which git tracks nothing in and ignores entirely (names compared NFC-normalized and casefolded) and no nested repository holds, none of them holding a `.git`, the files a rule proves regenerable (an installed distribution's file whose sha256 its RECORD names, from a distribution uv installed from an index or a network URL; bytecode with a magic number beside its source; a tool cache's own files; nothing of `node_modules`), which are deleted with the tree and not restored, while everything else in that directory is archived), its worktree's admin directory and every object that directory names, its salvage commits and baseline (parents of one anchor commit, the only head of a bundle of every commit no network remote holds; a salvage ref whose commit the verified anchor reaches is referenced elsewhere), and its rows (`rows.json`) are written to `$SUBFLEET_HOME/archive/<job>/` and read back before the rows are deleted; after that, an entry is deleted only if its signature (ctime included) is unchanged right before the unlink, or, for a file that had other links when archived and whose ctime alone moved, only if its bytes still hash to the archived sha256, and anything new or changed goes to `$SUBFLEET_HOME/retention-conflicts/<job>/`. | True | [ar.file], [ar.git], [ar.verify], [ar.commit], [fs.signature], [fs.delete], [r.regen]. |
| C-8.4 S58 | A salvage snapshot's skipped paths are protected unless the archive itself holds their bytes and reads them back; a caller's `salvage_referenced_elsewhere` vouch releases only the ref pin, never the file-preservation check. | True | [ar.bytes], [ar.verify], [ar.final], [r.pins]. |
| C-8.4 S59 | A regular file with neither stored bytes nor a verified omission or regenerable proof keeps the job as `protected`, with `unarchived path` and its path in the result's `deferred` reasons, never `pruned`; this check also runs before committing a resumed archive, whose journal bypasses the builder. | True | [ar.bytes], [ar.final], [r.finish]; Defer becomes protected/deferred rather than pruned. |
| C-8.4 S60 | A job whose tree another tool holds aside, or whose tree left or came back while it was being retired, is kept. | True | [ar.begin], [ar.identity], [ar.quarantine], [ar.final]. |
| C-8.4 S61 | A pass retires the oldest eligible jobs in a bounded batch, without sizing every job first, parks a job whose archive needs more than its slice with its progress (a slice always makes new progress, and parked jobs take turns), defers a busy or failing one (keeping what it archived; a repeated error waits longer), counts a job it cannot measure as decided, and never runs a repository-wide `git worktree prune`; the daemon retains measured sizes and archive progress between passes. | True | [r.pass], [r.measure], [r.rollback], [ar.archive]; no global worktree-prune call. |
| C-8.4 S62 | While work remains, a completed batch that advances (including a newly cached size, a parked archive slice, publication, partial verified deletion, or a prune) is followed 5 s later; an advancing batch that reaches its deadline warns and raises `TimeoutError`, on C-5.10’s worker clock. | True | [a.retention] selects RETENTION_CATCH_UP_S or raises after advancing timeout. |
| C-8.4 S63 | A deadline with neither pruning nor advancement marks `TimeoutError`, warns once with the store’s job count, rearms the hour and does not raise. | True | [a.retention] non-progressing interrupted branch. |
| C-8.4 S64 | A cancelled pass marks `CancelledError` and rearms the hour even after progress. | True | [a.retention] cancellation branch. |
| C-8.4 S65 | A completed pass with nothing more, or a batch with no advancement, marks and rearms the hour from its end. | True | [a.retention] completed fallthrough. |
| C-8.4 S66 | In every rearming outcome `_last_maintenance` is set last, after status bookkeeping, so a bookkeeping failure leaves retention due. | True | [a.retention] writes _last_maintenance after timers.mark in every rearming branch. |
| C-8.4 S67 | A deadline limits new starts, never commits of verified archives; even slow pre-selection work permits one job to start and receive its archive slice. | True | [r.pass], [r.measure], [ar.archive]; slice/checkpoint handling permits one start after pre-selection work. |
| C-8.4 S68 | A failed batch holder scan marks `ScanFailed` and rearms the hour, even after quarantine, rollback or archive work, to avoid cycling the store against an unavailable scanner. | True | [r.holders], [a.retention]. |
| C-8.4 S69 | Delivered service notices older than 14 days are pruned first, once per retention call (one bounded batch); a failure there is logged and never stops job retention. | True | [a.notices], [a.retention]. |
| C-8.4 S70 | Recovery finishes journaled retirements before new selection and prioritizes a job left with a journal-less `retention:<job>` lease. | True | [r.recover], [r.pass]. |
| C-8.4 S71 | Its events, each pool in its result and the daemon's log lines report the bytes freed (tracked files a remote holds and regenerable output, deleted without a copy) apart from the bytes moved into the archive and the bytes the archive adds (its bundle, manifest, summary and rows, and byte copies), with the net. | True | [ar.account], [ar.commit], [a.retention]. |
| C-8.4 S72 | A job whose baseline no network remote holds, and whose bundle would carry more than `retention.remote_less_history_bytes` (default 64 MiB) of history, is kept; a job registered in a repository inside another job's tree pins that job while it has rows. | True | [ar.history], [ar.max_bundle], [r.hosts], [r.pins], [ar.commit]. |
| C-8.4 S73 | Retention's children run under the `utility` QoS clamp (C-5.1). | True | [q.argv]; retention_git.run wraps child commands with this QoS argv. |
| C-8.4 S74 | Archives are kept until a person removes them. | True | [ar.publish], [r.no_delete]; no published-archive expiry is scheduled. |
| C-13.4 S1 | Lane worktrees the daemon allocates live under `$SUBFLEET_HOME/worktrees/`, never `/tmp`; a job's own is `jobs.worktree`, or, while that is NULL, the `worktrees/<job id>` admission allocated before recording it. | True | [ar.owned], [a.pass]. |
| C-13.4 S2 | A path left out of salvage keeps its worktree unless retention's archive itself holds and verifies that path's bytes, even when the salvage ref is referenced elsewhere. | True | [ar.bytes], [ar.verify], [ar.final]. |
| C-13.4 S3 | They are removed only after the job is terminal, no live conversation turn or reader holds their folder (I5: `folders.TURN` and `folders.READER`, C-8.4), retention has checked those rows and taken its canonical `worktree:<folder>` fence atomically against new turn reservations, no live process has its canonical cwd in the owned tree, and retention has archived and verified them (C-8.4); their registration is removed with them, after every object it names is in the archive's bundle. | True | [r.pins], [r.select], [r.holders], [ar.commit], [ar.reclaim]. |
| C-13.4 S4 | The cwd check is hold-only, uses the same in-process read as C-5.5 before moving the tree and at holder checks on the moved tree, including a retirement resumed from quarantined, and names pids with "cwd in worktree", never commands. | True | [a.observers], [r.select], [r.holders]. |
| C-13.4 S5 | It includes observers and excludes in-place workdirs. | True | [ar.owned], [a.observers]. |
| C-13.4 S6 | A worktree allocation that failed, and a conversation worktree rebuilt after an interrupted add, remove only their own unlocked registration, never others by a repository-wide prune. | True | [a.discard]; conversation worktree rebuild calls discard_registration on its own target. |
| C-13.4 S7 | A cwd observer holds only the worktree (its lease until the job ends, then, however it ends, a reader row that excludes no writer, C-5.5, C-5.9) and its retention, never the attempt's TMPDIR (C-12.10) or its verdict (C-5.9), and never authorizes a signal. | True | [a.release_ending], [a.keep_cwd], [r.pins], [a.observers]; reader conversion does not authorize a signal. |
| C-24.5 S1 | (one turn at a time per conversation) A conversation's next message gets a turn job only when the previous turn job is terminal and has released its `conversation:` and `native:` leases, and the conversation is not blocked. | True | [c.previous], [c.dispatch]. |
| C-24.5 S2 | A conversation is blocked while its `blocked_by` (the service's own: `unfinished-turn`, `delivery-unknown`, `quarantined-turn`) or its `legacy_hold` (the legacy import's, C-30.4) is set. | True | [c.dispatch], [c.next]. |
| C-24.5 S3 | While it is, no message is dispatched or re-admitted, and a turn job created before the block places nothing at admission (hold `conversation-blocked`, C-6.11). | True | [c.dispatch], [a.pass]. |
| C-24.5 S4 | Different conversations run concurrently under admission, also when they share a folder (one write target, C-6.5): each writable turn holds a row of its own on the folder, `worktree-turn:<target>:<job id>`, and none holds `worktree:<target>`, so no conversation waits for another's turn there (the owner's ruling of 2026-09-28, "nothing should be queued"; the Claude app lets several sessions share a folder). | True | [a.transaction], [f.turn_key]. |
| C-24.5 S5 | A read-only turn holds `worktree-read:<folder>:<job id>` (its checkout's top, or the directory outside git), which excludes no writer; of the leases on folders, only retention's fence on that folder or on one above it holds it (C-8.4). | True | [a.transaction], [f.retiring], [a.reader]; [c.describe] renders the hold. 4b: M10, M12. |
| C-24.5 S6 | Two turns of one conversation, or on one native session, still never run at once (`conversation:` and `native:`, C-26.3). | True | [a.native], [native.holds], [c.previous]. |
| C-24.5 S7 | A turn and a detached writer in one folder still exclude each other: a writable turn waits `lease-held` on `worktree:<target>` while a detached writer (or retention's fence, C-8.4) holds it, a detached writer waits at admission while any `worktree-turn:` row is on its folder, and one submitted then is refused (C-6.5). | True | [a.transaction], [a.conflicts], [a.writable]. |
| C-24.5 S8 | A turn, writable or read-only, also waits `lease-held` on `worktree:<folder>` of a folder above its own while retention's fence holds it (a repository nested in a job's tree being retired, C-8.4), never for a detached writer's lease there: a repository nested in a checkout is a hold of its own (C-6.5). | True | [f.retiring], [a.transaction], [a.early]. 4b: M10. |
| C-24.5 S9 | Why they still exclude each other: C-6.5 promises a detached in-place job that it writes its checkout alone, so the salvage ref taken at its end (C-13.1) is its own work, and salvage never snapshots a folder while a conversation writes in it (I5); detached writers normally run in worktrees of their own (C-6.6), so this meets a conversation only for an explicit `--in-place` job or one whose worktree a conversation continues, and the wait names the job that holds the folder (C-24.4). | True | [a.transaction], [a.writable]; writer exclusivity is unchanged. |
| C-24.5 S10 | What another conversation changed in the folder during a turn is labelled on that turn's changes (C-26.14). | True | [c.diff]. |
| C-24.5 S11 | A detached writer's edits are not: none is made from the turn's reservation to its end, but admission takes the turn's start snapshot before its reserving transaction, so a detached writer that held the folder after that snapshot began and released it before the reservation can leave edits in the turn's diff that nothing names (review of 5e9f2fbd, P3-1; a known limit). | True | [a.pass] prepares its workspace/start snapshot before the reserving transaction, so the stated residual gap remains. |
| C-24.5 S12 | `steering` is live and holds the queue until its host settles; `steered` is terminal and never gets its own job. | True | [c.next], [c.steers]; steering is included among live message states, steered among terminal states. |
| C-24.5 S13 | A missed steer keeps its original sequence and runs next: the dispatcher takes it ahead of every queued message that was not steered, behind only a repair message, and in sequence among missed steers (C-24.9); its reason keeps the `steer-missed:` mark the queue orders by when a submit of its turn is deferred, and when a handoff that cancelled its turn job is rolled back (C-30.3). | True | [c.next], [c.missed], [c.steers]. |
| C-24.5 S14 | Handoff refuses a live steer. [milestone 9; owner fake; module `subfleet/conversations/dispatch.py`; design §4] | True | [c.handoff] refuses unsettled steers. |
| C-26.3 S1 | (one writer per native session) A turn holds `conversation:<id>` and `native:<provider>:<native session id>` from reservation through finalization or quarantine. | True | [a.native], [native.key], [a.release_ending]; quarantine retains the job-held leases. |
| C-26.3 S2 | Resume, revive and handoff reservations wait while either is held, and `resume` refuses a `turn` source. | True | [a.native], [native.continuations], [native.holds], [a.resume], [c.handoff]. |
| C-26.3 S3 | A live process outside Subfleet holding the session (Claude: a pid in `~/.claude/sessions/*.json` naming it, in whichever case either spells its UUID, without Subfleet markers and not an owned identity; Codex: the thread reports an active turn) makes the turn wait `external-writer`, shown to the person, not a refusal: a Claude message is not dispatched while the process lives (a dispatch wait, with no job made), and an attempt that finds either writer when it starts (the Claude app may take the session while a job waits for admission) ends before its message is written and re-admits it, however often (C-24.6). | True | [c.writer], [c.dispatch], [c.claude], [c.codex]; external writer discovery is not run against host data in this audit. |
| C-26.3 S4 | Admission holds nothing for a writer. | True | [c.dispatch], [c.writer]; the writer reason is handled at dispatch/driver start rather than taking an admission lease for it. |
| C-26.3 S5 | Conversation-bound sessions are never listed, nudged, revived, cold-swept or handed off by the sessions kit (C-26.13), whichever case a UUID is spelled in, and the daemon refuses to record a nudge of one. | True | [a.bound], [a.refuse_bound]; the sessions kit consumes conversation_sessions as an exclusion. |
| C-26.3 S6 | Native UUIDs compare in canonical lowercase form in conflict checks and all native-session lease keys, including old raw keys; opaque native ids and display spellings remain as supplied. [milestone 9; owner fake; module `subfleet/conversations/dispatch.py`, `subfleet/daemon.py`, `subfleet/sessions/`] | True | [c.native], [native.key], [native.holds]. |

## Decision-note sentences

The case-alias rows audit the current SQL, firmlink and linked spelling statements and every corrected sentence. The archive rows cover its merged pin implementation (§10), I16, nested-fence/dormant statements (§15), and the associated implementation-test descriptions (§16). The 2026-10-05 observed-before narrative and unrelated archive-format design are retained as history/design, not represented as newly executed evidence.

| Document / sentence | Sentence at audited base | Verdict | Code site or change |
|---|---|---|---|
| Decision / option 2 | Option 2, with option 1's delay moved to where the row is taken. | True | [a.folder], [a.rediscover], [a.early], [a.transaction]. |
| Decision / option 3 | Option 3 is applied only to the comparisons that read a name not looked up. | Changed | Dependency pins and fences always compare normalized paths folded, including fully spelled recorded paths: [d.key], [d.fences], [d.live]. 4b: P1, P3. |
| Decision / SQL pin | `worktree-in-use` compares the tree itself without ASCII case (`COLLATE NOCASE`), as its LIKE already compared a folder inside it. | Changed | The current pin uses [d.key], [d.users], [r.pins]; no dependency SQL LIKE or NOCASE self-join. 4b: M13, P1. |
| Decision / queued alias | A turn waiting on the fence for `jOB` then keeps `Job` at the commit, and the retirement rolls back. | True | [d.live], [d.dependents], [ar.commit], [r.finish]. 4b: P1. |
| Decision / current rows | Retention's row checks stay exact. | True | [f.turns], [f.inside], [r.select], [ar.commit]; folded dependency evidence is separate. |
| Decision / row comparisons | Every check that reads a row compares strings exactly. | Changed | Narrow to TURN/READER folder rows. Job-row dependency places are folded in [d.live]; the exact folder-row sites are [f.turns], [a.transaction]. |
| Decision / fold scope | Why fold only where a name was not looked up. | Changed | Narrow this rationale to row-folder fences; [f.retiring] uses absent-name folding, whereas [d.fences] folds all dependency places. 4b: P3. |
| Decision / spelled folder | A spelled folder is the kernel's spelling, and so is retention's fence: exact comparison is right and over-matches nothing. | True in its row-folder scope | [f.canonical], [f.present], [f.retiring], [r.select]. |
| Decision / folding definition | `fold` is Unicode's canonical caseless form, NFD(casefold(NFD(s))) (D145). | True | [f.fold], [d.key]. |
| Decision / row legacy | Earlier builds can have rows keyed on incomplete spellings; this change does not migrate those rows. | True | [f.turns] compares existing rows as stored; [a.rediscover] and [f.present] establish new row subjects. |
| Decision / SQL rationale | Its LIKE (a folder inside the tree) already ignored ASCII case, but its `=` (the tree itself) did not. | Changed current-operator implication | Mark these as the former SQL operators; the current pin is [d.live], [d.users], [r.pins]. |
| Decision / SQL semantics 1 | NOCASE and LIKE match an alias of a tree's own name in other ASCII capitals. | True; preserved verbatim | A statement about the historical SQL operators, not a claim that [r.pins] still uses them. A preservation-only follow-up restores it verbatim. |
| Decision / SQL semantics 2 | They do not match one spelled with letters outside ASCII that APFS folds to its letters (`ﬁ` for `fi`, `ſ` for `s`, the Kelvin sign for `k`), since SQLite folds only ASCII. | True; preserved verbatim | The historical SQL distinction remains; current Unicode behavior is [f.fold], [d.key], [r.pins]. 4b: M13, P1. |
| Decision / obsolete SQL outcome | The folded fence still holds such a turn, and it takes no row; see the known limits. | Changed stale-gap implication | The folded dependency pin now keeps the needed tree and the commit rolls back: [d.fences], [d.live], [ar.commit]. A quarantined folder still receives no row. 4b: M13, P1, P3. |
| Decision / bounded 1 | A turn whose folder never comes back fails after `caps.workspace_retry_max` tries. | Changed | Qualify this workspace retry bound to the absence of a retention fence. [a.early] can wait on retention without consuming workspace retries; [a.wait] handles the workspace bound. |
| Decision / bounded 2 | It never waits forever and never takes a row. | Changed | A fenced job waits for retirement; neither wait takes a row on a missing folder. [a.early], [a.transaction]. |
| Decision / differential guarantee | For ASCII names without LIKE's wildcards, the folded fence holds a turn exactly when `worktree-in-use` keeps the tree for its job, and exactly when the folder is within the tree up to ASCII case (differential property). | Changed | With multiple dependency places, a folder-only equivalence is incomplete. State the shared normalized folded dependency-containment comparison instead: [d.key], [d.fences], [d.dependents]. 4b: P1, P3. |
| Decision / non-ASCII gap | A name above `worktrees/` typed in another case or normal form, outside ASCII, is not matched by `worktree-in-use`. | Changed | The former ASCII-only pin gap is closed by [d.key], [d.live], [r.pins]. Folding may conservatively match a distinct tree on a sensitive volume. 4b: M13, P1. |
| Decision / SQLite fact | SQLite folds only ASCII. | True; preserved verbatim | Preserved as historical SQL semantics, separated from the current Python comparison [d.key]. |
| Decision / no missing-folder row | The turn still waits on the fence and takes no row. | True; preserved verbatim | [a.early], [a.transaction] keep a fenced/missing-folder job unreserved. |
| Decision / retired non-ASCII outcome | If the retirement commits, the turn fails after C-6.8's retries, and no row ever names the removed tree. | Changed obsolete failure outcome | A matching live dependency now prevents that commit: [d.dependents], [ar.commit]. The corrected note records the remaining conservative over-match instead of claiming the removed pin gap remains. |
| Decision / renamed tree | A tree renamed to another case while a row names it (`mv Job JOB`) gives the tree a new one spelling that the row does not have. | True | The retained folder-row keys [f.turns] do not track external directory renames. |
| Decision / firmlink fence | A queued turn whose workdir is typed through a firmlink (`/System/Volumes/Data/Users/…`) is held by the folded fence, since its folder is spelled through the kernel. | True | [f.canonical], [a.workdir], [a.folder], [a.early]. |
| Decision / firmlink pin | `worktree-in-use` compares the job's `workdir` string, so on this branch it does not keep the tree. | Changed | [d.live] includes canonical recorded folder/workdir/depends and [d.legacy] canonicalizes a legacy workdir outside transactions. The pin uses [d.key]. 4b: M9, M13, P1. |
| Decision / firmlink outcome | The turn takes no row, and fails after C-6.8's retries if the retirement commits (review of 3410b4f0, P3). | Changed obsolete gap outcome | Recorded-folder or legacy-canonical-path evidence keeps the queued job and rolls retirement back: [d.live], [d.legacy], [ar.commit]. |
| Decision / canonical submit | MaxGhenis/subfleet-v2#140 records `jobs.workdir` canonically at submit, for writers and turns alike, which keeps the tree for a workdir that exists at submit. | True | [a.workdir], [a.submitted], [d.live], [r.pins]. |
| Decision / remaining tail | What is left is a non-ASCII name in a tail submit could not look up (above). | Changed | Both dependency pin and fence now fold an unspellable tail: [d.key], [d.fences], [d.dependents]. Role attribution's separate D1 defect is fixed in `42bdf10798ba`. 4b: M13, P1, P3. |
| Decision / old rows | Rows reserved before this change by a build of integrate/2111-features are compared as before. | True | [f.turns] does not rewrite old lease keys; [a.rediscover] handles admission-time spelling. |
| Archive §10 S1 | A job is kept while any of these holds (C-8.4, unchanged from `release/217` except salvage): it is not terminal; it is a gate review; an attempt is live or quarantined; an unread notice addressed to a session; it is a parent of any job; it or an attempt holds a lease; another holder has its worktree lease, or, for a job with its own allocated tree, a worktree lease on a folder inside it; a resume holds its `retire:` fence; a salvage ref of an in-place job (or one that cannot be resolved, or whose commit the verified anchor does not reach); gate or merge evidence names it; the conversation service names it (asked again inside the commit transaction); a job not yet ended needs a place in its allocated tree (`worktree-in-use`, the tree recorded in `jobs.worktree` or, while that is NULL, the `worktrees/<job id>` admission allocated, revision 5), or an export to a path there is pending (below); a turn job within `turn_keep_days`; an explicit reference; another job's worktree is registered in a repository inside its tree and that job still has rows (`nested-host: <those jobs>`: which job each owned worktree's gitfile names is read once per pass, and a job whose tree is gone counts as hosted by the tree its source directory is in; asked again inside the commit transaction, so a host already in flight is put back). | Changed stale integration label | Replace "unchanged from release/217 except salvage" with release pins plus the dependency places and pending exports. [r.pins], [d.live], [d.dependents]. |
| Archive §10 S2 | The hosted job then retires first, while its registration is readable, with its own anchor and bundle, and the host in a later pass. | True | [r.hosts], [r.pins], [ar.begin], [ar.commit]. |
| Archive §10 S3 | `worktree-in-use` (C-8.4, `subfleet/dependencies.py`, 2026-10-06). | True | [r.pins], [r.queries], [r.select], [ar.commit]. |
| Archive §10 S4 | The places a job that has not ended needs are its workdir, worktree, review root (`-I -D`) and `-o` path as its row holds them; its folder or write target as submit recorded it (or, for a job queued before that, as admission spelled it, `job.folder_spelled`); and what submit records as `depends`, spelled once: the review root, the `-o` path, and the git storage its folder's checkout uses (for each `.git` at or above the folder, the directory it is or names, that directory's common directory and the alternates of its objects, read from the files without running git). | True | [d.legacy], [r.pass]. |
| Archive §10 S5 | A checkout outside the tree can keep its repository in it (`git -C <tree>/vendor/lib worktree add <outside>`); every git command in the checkout then works in the tree, and a worktree writer submitted from there registers its worktree there. | True | [d.git], [d.storage], [d.live]. 4b: M3–M8, M15. |
| Archive §10 S6 | A job holding `out:<path>` has an export pending (it is written after the job ends, C-8.3), and that path is a place too, whatever the job's state. | True | [d.live], [a.export]. 4b: M14, P1. |
| Archive §10 S7 | A tree is kept while any such place is the tree or inside it, comparing places folded (`folders.fold`), so a row's workdir typed in another case names the tree. | True | [d.key], [d.live], [d.users], [r.pins]. 4b: M13, P1–P2. |
| Archive §10 S8 | The pass reads this once, and the selecting and commit transactions read it again for their job, also on the fence's spelling, with database reads alone: each job's first `job.submitted` by job id (`events_job`) and the `out:` leases by a range of their key. | True | [d.queries], [d.live], [r.pins], [r.select], [ar.commit]. 4b: P1. |
| Archive §10 S9 | The self-join it replaces compared every finished job's tree with every job not yet ended by `LIKE`; new timing measurements remain pending; validation status is in `docs/reports/2026-10-27-retention-dependency-pins.md`. | True | [r.pins] now uses live/users; the old timings are not asserted and new comparisons remain 4b work. |
| Archive §10 S10 | A job queued before `depends` was recorded has those places read once a pass, before any transaction (`dependencies.legacy`). | True | [d.legacy], [r.pass]. |
| Archive §10 S11 | Admission reads retention's fence on each place in `depends` as on a job's folder (C-6.5, C-6.11). | Changed | Dependencies use folded normalized comparisons before _workspace and in reservation; only retention worktree holders count, one range, nearest first. [d.fences], [a.early], [a.needs_blockers]. D1 concerns attribution, not the blocker. 4b: M10, P3. |
| Archive §10 S12 | A written export keeps nothing: retention may archive a tree a finished export wrote into, the file with it. | True | [a.export], [d.live], [ar.file]. |
| Archive I16 S1 | A finished job's tree is kept, at the pass, the selection and the commit, exactly while a job that has not ended needs a place in it (its folder, workdir, worktree, review root, `-o` path or git storage, compared folded) or an export to a path in it is pending; spelling a place in another case or Unicode form changes nothing, and a job that has not ended never releases a pin. | Changed over-exhaustive wording | Remove "exactly": dependency evidence is a sufficient protection, but other pins also keep a finished tree. [r.pins], [d.live], [r.select], [ar.commit]. 4b: M13, P1–P2. |
| Archive I16 S2 | Admission starts no job while retention holds the fence above any such place. | True | [a.early], [a.needs_blockers]; blocking itself is unaffected by D1. 4b: M10, P3. |
| Archive §15 / fence scope | Retention's fences compare a job's own tree exactly. | Changed | The merged checks include descendant folders, in [f.turns], [f.inside], [r.select], [ar.commit]. |
| Archive §15 / row pin | The `turn-folder` pin and the selecting transaction compare a turn row's folder with the tree. | Changed incomplete scope | Census compares within; selection and commit use the canonical tree and inside=True. [r.pins], [r.select], [ar.commit], [f.turns]. |
| Archive §15 / exact lease join | The `worktree-lease` pin joins `worktree:<tree>`. | True; preserved verbatim | [r.queries] still has that own-tree SQL join; [r.pins], [f.inside], [r.select], [ar.commit] separately cover nested leases. |
| Archive §15 / nested gap | So a turn row or a `worktree:` lease on a folder inside a finished job's tree, such as a repository nested in it, does not keep the tree. | Changed | It does keep an owned tree. [r.pins], [f.turns], [f.inside], [r.select], [ar.commit]. |
| Archive §15 / live job pin | `worktree-in-use` covers such a folder only while the job working there has not ended. | True in this paragraph's workdir/folder scope | [d.live] limits job-row/folder evidence to nonterminal jobs; pending exports are separately documented in §10 and I16. |
| Archive §15 / old review findings | Reviews of #113 and #134 reported this and related gaps: a turn admitted while the fence sits on a folder above its own, and a tree whose name is spelled in another case. | Historical; preserved | The current corrective implementation is [f.retiring], [r.select], [ar.commit]. This audit does not rerun those old reviews. |
| Archive §15 / integration state | Their fixes are the stack on #134 (#137 and the PRs above it). | Changed | State that the fixes are merged here with #272 dependency pins/fences. [f.turns], [f.inside], [r.select], [ar.commit], [d.fences]. |
| Archive §15 / dormant condition | Until it lands, the 2.1.11 installer sets `SUBFLEET_RETENTION_DORMANT=1` in the daemon's launchd plist. | Changed stale pre-merge condition | The merged implementation still offers dormant mode through this environment switch. [a.retention] returns before maintenance. The installer is outside this source tree and was not inspected; no live launchd state was read. |
| Archive §15 / dormant behavior | A pass then selects, archives and deletes nothing (C-8.4). | True | [a.retention] checks the switch before maintenance; its service-notice/TMPDIR housekeeping is outside job retirement. |
| Archive §16 / I16 tests | Generated layouts of repositories and linked checkouts check that the git directory and common directory git itself uses are among the recorded git storage; generated stores check `worktree-in-use` (pass and `only`) and the transactions' `dependents` against a direct oracle, under re-spelling and under adding or ending a job; `fences` agrees with `folders.retiring`; the reads' plans use `events_job` and a lease range. | Changed exact-fence equivalence | Qualify the equality to the folded folders.retiring(..., folded=True) comparison for normalized paths. Existing simple-spelling unit examples still compare the exact helper; the added property/fake files cover aliases. Source: [d.fences], [f.retiring], [d.queries]. 4b: M3–M8, M13, P1–P3; the report does not claim their capped cases completed. |
| Archive §16 / fake coverage | `tests/fake/test_retention_dependencies.py` runs the four gaps through the daemon's own submit and admission against the archive driver's own retirement: a queued job keeps the tree; a job submitted under the fence waits, unprepared, named by `why`; the commit keeps the tree for a job submitted after selection; a pending export keeps the tree and a written one goes into the archive; jobs queued before `depends`, and turns, are covered. | True as a statement of existing test coverage | The named test bodies exercise [a.early], [a.needs_blockers], [ar.commit], [a.export]. Only guards and the explicit D1 replay are executed by this continuation; the later continuation 5 regression closes D1. 4b: M1–M2, M11–M12, M16, P3. |

## Executed code finding D1: fixed in continuation 5 (`42bdf10798ba`)

At the audited base, C-6.11's dependency `folder`/`needs` promise and C-8.4's hold-description subclaims were false for this input. **The job is still held safely.** Only its dependency attribution is wrong; this replay does not demonstrate an admission or retirement safety breach. The binding sentences were retained; continuation 5 makes them true by sharing `dependencies.key` between blocking and dependency attribution.

The blocking query [d.fences] compares `dependencies.key` paths: normalized, then NFD(casefold(NFD(path))). At the audited base, the reserving hold's attribution at [a.attribute] instead called [f.fences] with `fold=True`, which uses [f.folded]: NFC(path).casefold(). These differ for a component spelled `\u0390` (Greek small iota with dialytika and tonos) and `\u03aa\u0301` (capital iota with dialytika, followed by combining acute). Their dependency keys agree, but the attribution helper does not match them. The early hold takes its role/path directly from `dependencies.fences`; the late transaction can lose both fields.

The fake replay below uses a normal ASCII owner id and an absolute recorded dependency alias in the first `job.submitted`, the same evidence-replay method as `record_alias` in the existing spelling fake. It runs the real in-process fake admission, without starting a provider. A late fence is installed by the workspace stand-in between the early look and the reserving transaction. The early case is the positive control. Both must remain waiting with the same retention key; the contract then requires `folder` and `needs: review-root` in both holds.

Observed holds, with temporary roots abbreviated but code points preserved:

```json
{"late": false, "tree": "<root>/\u0390/state/worktrees/20261029-000000-owner", "alias": "<root>/\u03aa\u0301/state/worktrees/20261029-000000-owner/vendor/lib", "hold": {"reason": "lease-held", "leases": ["worktree:<root>/\u0390/state/worktrees/20261029-000000-owner"], "retiring": ["worktree:<root>/\u0390/state/worktrees/20261029-000000-owner"], "folder": "<root>/\u03aa\u0301/state/worktrees/20261029-000000-owner/vendor/lib", "needs": "review-root"}}
{"late": true, "tree": "<root>/\u0390/state/worktrees/20261029-000000-owner", "alias": "<root>/\u03aa\u0301/state/worktrees/20261029-000000-owner/vendor/lib", "hold": {"reason": "lease-held", "leases": ["worktree:<root>/\u0390/state/worktrees/20261029-000000-owner"], "retiring": ["worktree:<root>/\u0390/state/worktrees/20261029-000000-owner"]}}
```

The early case passes. The transaction case fails the final contract assertion: `folder` and `needs` are both absent. All preceding safety, fence-key and workspace-count assertions pass. This is an observed failure at the audited base, not a mutant result or a host-timeout exception. The committed regression repeats that red result on `ce9e154eb`, then passes both cases after `42bdf10798ba`.

Continuation 5 delivered the product-code fix and this exact alias as a repository regression. It reuses the dependency comparison for hold attribution and preserves the contract. Existing 4b checks M2/M11/M12/M16/P3 completed separately; the new regression and generated property cover this additional input.

Original reproducible fake input (now committed as `test_c6_11_unicode_dependency_hold_names_its_place` in `tests/fake/test_retention_dependencies.py`):

```python
import json
from pathlib import Path
import pytest
from subfleet import dependencies, folders
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_admission_liveness import _live
from tests.fake.test_detached_retention_fence import Workspaces
from tests.fake.test_writer_retention_fence import look, measured

@pytest.mark.parametrize("late", [False, True], ids=["early", "transaction"])
def test_c6_11_unicode_dependency_hold_names_its_place(tmp_path, late):
    with fleet_daemon(tmp_path / "\u0390" / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        owner = "20261029-000000-owner"
        actual = daemon.root / "worktrees" / owner
        actual.mkdir(parents=True)
        tree = folders.canonical(actual)
        alias = str(Path(*("\u03aa\u0301" if folders.fold(part) == folders.fold("\u0390") else part
                           for part in Path(tree).parts)) / "vendor" / "lib")
        assert dependencies.key(alias).startswith(dependencies.key(tree) + "/")
        assert not folders.fences(folders.exclusive_key(tree), alias, fold=True)
        job = submit(daemon, harness, sandbox="read-only")
        # Replay valid absolute dependency evidence retained under another case.
        with daemon.store.transaction() as tx:
            event = tx.execute("SELECT event_id,data_json FROM events WHERE job_id=? AND kind='job.submitted'",
                               (job,)).fetchone()
            data = json.loads(event[1])
            data["depends"] = {"review_root": alias}
            tx.execute("UPDATE events SET data_json=? WHERE event_id=?", (json.dumps(data), event[0]))
            tx.execute("UPDATE jobs SET review_root=? WHERE job_id=?", (alias, job))
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=False)
        if late:
            def lands(row):
                assert daemon.store.acquire_lease(fence, "retention:" + owner)
                return workspaces(row)
            patch.setattr(daemon, "_workspace", lands)
        else:
            assert daemon.store.acquire_lease(fence, "retention:" + owner)
        hold = look(daemon, job)
        assert not _live(daemon, job), hold
        assert hold["retiring"] == [fence], hold
        assert workspaces.calls == ([job] if late else []), workspaces.calls
        print(json.dumps({"late": late, "tree": tree, "alias": alias, "hold": hold}, ensure_ascii=True))
        assert {key: hold.get(key) for key in ("folder", "needs")} == {
            "folder": alias, "needs": "review-root"}, hold
```

Run through the supplied `suite_hold.sh`, the same locked environment/cache and scratch TMPDIR/Hypothesis roots as the guards, with one pytest:

```sh
uv run --no-sync pytest -q -s -c pyproject.toml -p no:cacheprovider \
  -p tests.conftest <scratch>/test_c4a_unicode_hold.py
```

The explicit tests.conftest plugin installs the repository's safe homes, credential removal and keychain-exec guard for a test file outside the repository test directory.

## Guard and finding holds

One pytest per hold, serial (no xdist). Starts and all three loads are captured inside the admitted command with `datetime.now(UTC)` and `os.getloadavg()`, before exec to pytest. The wrapper's denied `sysctl` output is not a load measurement. Every completed case includes pytest setup, call and teardown. No completed prefix from an interrupted run is counted.

| Hold | Start UTC, 2026-10-29 | Load 1 / 5 / 15 min | Passed / failed / skipped | Pytest wall | Lock |
|---|---|---|---|---|---|
| guards | 00:31:54.617 | 60.7 / 48.0 / 43.5 | 63 / 0 / 0 | 120.25 s | locked slot B |
| finding | 00:45:00.108 | 37.7 / 54.1 / 53.7 | 1 / 1 / 0 | 12.60 s | locked slot A |
| guards-final | 00:45:22.964 | 38.0 / 53.2 / 53.4 | 63 / 0 / 0 | 42.41 s | locked slot B |

Each guard run comprises **37** `tests/fake/test_state_contract.py`, **1** `tests/unit/test_test_module_names.py`, and **25** `tests/unit/test_invariants_index.py` cases. The first run follows the contract/note corrections; the final run follows the preservation-only note follow-up. The finding hold is deliberately red against the unmodified product code and is reported separately from the **63/63 passing final guards**.

## Delivery and still open

The documentation correction and its preservation-only follow-up are separate forward commits; no commit was rewritten. This report is the next commit. Every commit has the requested co-author footer. Named files only are staged. The assigned `.git` file and detached base checkout metadata stay unchanged; the private branch is `fix/retention-dependency-pins-217`. The bundle carries only the ordered continuation-4a commits and requires the audited base.

All test processes and lock waiters are drained or explicitly stopped before delivery. Scratch stays below 1 GB and is removed with the lane rules' guarded cleanup after the bundle is pack-verified. Bytecode from the source-only diagnostic is removed; tests use PYTHONDONTWRITEBYTECODE=1. The final response confirms completion of that cleanup.

**Resolved in continuations 4b and 5:** D1 is fixed in `42bdf10798ba43551adb98fdf9cf81a1ed0eed98` with red/green regression and generated alias coverage. Continuation 4b completed all 16 listed mutants, the three properties, final measurement-tool revalidation and both timing comparisons (neither required query tuning). No retention-safety defect was observed in those continuation runs. Review r1 subsequently established three allocated-worktree counterexamples and the pre-existing job-directory boundary; fix round r1 closes them with the evidence linked above. Remaining validation is CI, the full suite and Python 3.12; current review evidence is linked from the main report.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

[d.key]: ../../subfleet/dependencies.py#L75
[d.record]: ../../subfleet/dependencies.py#L82
[d.git]: ../../subfleet/dependencies.py#L98
[d.storage]: ../../subfleet/dependencies.py#L152
[d.alternates]: ../../subfleet/dependencies.py#L163
[d.needed]: ../../subfleet/dependencies.py#L187
[d.fenced]: ../../subfleet/dependencies.py#L198
[d.fences]: ../../subfleet/dependencies.py#L209
[d.legacy]: ../../subfleet/dependencies.py#L242
[d.live]: ../../subfleet/dependencies.py#L259
[d.users]: ../../subfleet/dependencies.py#L292
[d.dependents]: ../../subfleet/dependencies.py#L303
[f.canonical]: ../../subfleet/folders.py#L109
[f.present]: ../../subfleet/folders.py#L176
[f.identity]: ../../subfleet/folders.py#L116
[f.fold]: ../../subfleet/folders.py#L332
[f.retiring]: ../../subfleet/folders.py#L353
[f.fences]: ../../subfleet/folders.py#L384
[f.folded]: ../../subfleet/folders.py#L392
[f.turns]: ../../subfleet/folders.py#L420
[f.inside]: ../../subfleet/folders.py#L449
[f.within]: ../../subfleet/folders.py#L313
[f.turn_key]: ../../subfleet/folders.py#L291
[a.folder]: ../../subfleet/daemon.py#L3109
[a.rediscover]: ../../subfleet/daemon.py#L3142
[a.detached]: ../../subfleet/daemon.py#L3196
[a.needs]: ../../subfleet/daemon.py#L3247
[a.writable]: ../../subfleet/daemon.py#L3255
[a.conflicts]: ../../subfleet/daemon.py#L3362
[a.instance]: ../../subfleet/daemon.py#L3087
[a.instance_alive]: ../../subfleet/daemon.py#L3099
[a.refuse_revive]: ../../subfleet/daemon.py#L3333
[a.home]: ../../subfleet/daemon.py#L3430
[a.early]: ../../subfleet/daemon.py#L6646
[a.pass]: ../../subfleet/daemon.py#L6796
[a.capacity]: ../../subfleet/daemon.py#L6592
[a.seen]: ../../subfleet/daemon.py#L6732
[a.admit_kind]: ../../subfleet/daemon.py#L6532
[a.note]: ../../subfleet/daemon.py#L6742
[a.why]: ../../subfleet/daemon.py#L3931
[a.status]: ../../subfleet/daemon.py#L4001
[a.desktop]: ../../subfleet/daemon.py#L2009
[a.export]: ../../subfleet/daemon.py#L10988
[a.export_error]: ../../subfleet/daemon.py#L11057
[a.retention]: ../../subfleet/daemon.py#L5180
[a.notices]: ../../subfleet/daemon.py#L5269
[a.observers]: ../../subfleet/daemon.py#L4836
[a.keep_cwd]: ../../subfleet/daemon.py#L4869
[a.release_ending]: ../../subfleet/daemon.py#L4916
[a.launch]: ../../subfleet/daemon.py#L5784
[a.discard]: ../../subfleet/daemon.py#L5809
[a.bound]: ../../subfleet/daemon.py#L4099
[a.refuse_bound]: ../../subfleet/daemon.py#L4144
[r.pins]: ../../subfleet/retention.py#L207
[r.pass]: ../../subfleet/retention.py#L531
[r.select]: ../../subfleet/retention.py#L830
[r.holders]: ../../subfleet/retention.py#L921
[r.finish]: ../../subfleet/retention.py#L959
[r.recover]: ../../subfleet/retention.py#L680
[r.measure]: ../../subfleet/retention.py#L767
[r.rollback]: ../../subfleet/retention.py#L1044
[r.hosts]: ../../subfleet/retention.py#L146
[ar.owned]: ../../subfleet/retention_archive.py#L319
[ar.begin]: ../../subfleet/retention_archive.py#L408
[ar.identity]: ../../subfleet/retention_archive.py#L689
[ar.job_fence]: ../../subfleet/retention_archive.py#L621
[ar.quarantine]: ../../subfleet/retention_archive.py#L645
[ar.final]: ../../subfleet/retention_archive.py#L820
[ar.bytes]: ../../subfleet/retention_archive.py#L99
[ar.commit]: ../../subfleet/retention_archive.py#L881
[ar.q_fence]: ../../subfleet/retention_archive.py#L992
[ar.reclaim]: ../../subfleet/retention_archive.py#L1030
[ar.git]: ../../subfleet/retention_archive.py#L1925
[ar.verify]: ../../subfleet/retention_archive.py#L2015
[ar.file]: ../../subfleet/retention_archive.py#L1790
[ar.publish]: ../../subfleet/retention_archive.py#L971
[ar.account]: ../../subfleet/retention_archive.py#L1287
[ar.history]: ../../subfleet/retention_archive.py#L535
[ar.max_bundle]: ../../subfleet/retention_archive.py#L1999
[ar.archive]: ../../subfleet/retention_archive.py#L788
[fs.signature]: ../../subfleet/retention_fs.py#L101
[q.argv]: ../../subfleet/retention_qos.py#L64
[g.env]: ../../subfleet/salvage.py#L142
[render.needs]: ../../subfleet/render.py#L521
[render.why]: ../../subfleet/render.py#L537
[c.dispatch]: ../../subfleet/conversations/service.py#L2605
[c.previous]: ../../subfleet/conversations/service.py#L2787
[c.describe]: ../../subfleet/conversations/service.py#L2536
[c.next]: ../../subfleet/conversations/store.py#L1306
[c.native]: ../../subfleet/conversations/store.py#L224
[c.handoff]: ../../subfleet/conversations/service.py#L2088
[c.writer]: ../../subfleet/conversations/catalog.py#L324
[out.prepare]: ../../subfleet/resource_leases.py#L68
[out.holds]: ../../subfleet/resource_leases.py#L98
[native.key]: ../../subfleet/resource_leases.py#L16
[native.holds]: ../../subfleet/resource_leases.py#L34
[native.continuations]: ../../subfleet/resource_leases.py#L45
[capacity.open]: ../../subfleet/capacity.py#L601
[isolation]: ../../subfleet/adapters/isolation.py#L36
[isolation.codex]: ../../subfleet/adapters/isolation.py#L116
[fs.delete]: ../../subfleet/retention_fs.py#L1238
[a.response_status]: ../../subfleet/daemon.py#L3811
[a.submit]: ../../subfleet/daemon.py#L2509
[a.submitted]: ../../subfleet/daemon.py#L2784
[a.workdir]: ../../subfleet/daemon.py#L2624
[a.transaction]: ../../subfleet/daemon.py#L7764
[a.reader]: ../../subfleet/daemon.py#L7819
[a.needs_blockers]: ../../subfleet/daemon.py#L7835
[a.attribute]: ../../subfleet/daemon.py#L7900
[a.native]: ../../subfleet/daemon.py#L7747
[a.resume]: ../../subfleet/daemon.py#L2951
[a.resolved_launch]: ../../subfleet/daemon.py#L8630
[a.wait]: ../../subfleet/daemon.py#L8554
[c.diff]: ../../subfleet/conversations/service.py#L1716
[c.missed]: ../../subfleet/conversations/service.py#L2746
[c.steers]: ../../subfleet/conversations/service.py#L3190
[c.note]: ../../subfleet/conversations/service.py#L2493
[cli.why]: ../../subfleet/cli.py#L2424
[cli.status]: ../../subfleet/cli.py#L566
[r.queries]: ../../subfleet/retention.py#L189
[d.queries]: ../../subfleet/dependencies.py#L64
[r.budget]: ../../subfleet/retention.py#L402
[r.no_delete]: ../../subfleet/retention_archive.py#L2072
[r.regen]: ../../subfleet/retention_fs.py#L514
[c.claude]: ../../subfleet/conversations/claude_turn.py#L214
[c.codex]: ../../subfleet/conversations/codex_turn.py#L426
