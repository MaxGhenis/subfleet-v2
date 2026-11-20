# Before-implementation audit

Audited release/217's committed source; all paths refer to this job's checkout. No live state was read.

## Operator changes and wake seams

`cmd_lanes` constructs `LanesArgs` and sends the `lanes` daemon operation (`cli.py:2317-2349`). The daemon's `dispatch` handles `transfer`, `enroll`, `hold`, `release`, and `release-limits` (`daemon.py:4275-4303`). The parser defines precisely those actions plus read-only `list`/`probe` (`cli.py:3726-3768`). There are no `lanes enable`, `disable`, or `remove` verbs and no policy-reload operation in this release. The only production assignment to `policy_digest` is startup's load/hash (`daemon.py:1001-1005`). Therefore a changed `policy.json` requires restart to affect this daemon; a changed `lanes.json` is seeded only at startup (`_open_lanes`/`_seed_lanes`, `daemon.py:2070-2114`). Editing an existing lane's enabled field in `lanes.json` is not a runtime action and `_seed_lanes` skips existing bindings except credential-reference repair. Direct store changes are discovered by existing pass reads. In-memory replacement of `policy`/`policy_digest`, used by test fixtures, is a separate seam that readiness can compare without reading a file.

Daemon-owned mutation locations:

| Change | Existing mutation and notification |
| --- | --- |
| Submit | `submit`, `job.submitted` transaction inserts queued job, then `_notify` (`daemon.py:3404-3413`). |
| Retry without launch | `_unlaunched` commits `attempt.no_launch`, queues job and releases leases, then `_notify` (`daemon.py:10799-10827`). |
| Attempt result/retry | `_finalize` updates job state/backoff, deletes slot lease, records closure or disables lane, then `_notify` (`daemon.py:11944-12031`). |
| Task hold released | `_release_task` restores held jobs in its transaction; `_task_holds_changed` increments its epoch then `_notify` (`daemon.py:2010-2057`, `1938-1942`). Expiry is time-based, restored by the next pass, rather than an external handler. |
| Operator closure added/released | `_hold_lane`, `lane.held`/`lane.released`, then `_notify` (`daemon.py:1515-1528`). |
| Guessed limits released | `_release_lane_limits`, `lane.limits_released`, then `_notify` (`daemon.py:1550-1563`). |
| Lane enrolled | `_enroll_lane_locked`, `lane.enrolled`/`put_lane`, then `_notify` (`daemon.py:1372-1403`). |
| Ownership transfer | `dispatch` calls `lanes_transfer.transfer` (`daemon.py:4277-4289`); no `_notify` here. Ownership should be among observed lane facts. |
| Provider closure or disable | `_finish_probe`, `_finalize`, plus timers' `timer.probe` and recovery/auth paths. Timers share `daemon.store`; these paths are not all followed by daemon `_notify`. |
| Lease released | Many raw SQL deletes bypass `Store.release_leases`: `_release_ending`, `_release_worktree_hold`, `_finalize`, `_unlaunched`, `_finish_probe`, quarantine resolution, export, queued cancellation. A shared store mutation observer is more complete than hooking `release_leases` alone. |
| Pilot answer / lapse | `_record_answer` sets `_answer_news` when an unproven lane becomes proven, then `_notify` (`daemon.py:2249-2281`); `_read_answer` sets `_answer_news` and `_notify` when an unanswered pilot lapses (`2319-2325`). These are in-memory changes and need explicit admission wakes. |
| Closure expiry | No write or handler occurs at `until_at`. Cache the earliest active closure expiry during a pass and compare that timestamp from readiness; no per-tick store query is necessary. |

A daemon-owned store observer after successful outer commit can catch raw SQL changes from all daemon/timer paths, including operations that never call `_notify`. It should recognize actual queued-job entry, lease deletion, closure changes and lane changes. It must not make ordinary admission wait-clock writes into wakes, otherwise each pass cancels its own rest. Nested/rolled-back transactions must not publish a wake. The observer needs no schema migration if connection-local TEMP triggers track these mutations.

## Capacity-wait consumers

`_reset_demand` (`daemon.py:2725-2780`) copies cached waits and pass/whole-pass holds; it selects current `waiting/capacity` jobs and excludes cancellation or task holds. A cached wait is used only to establish `lane_wait` and whether its label matches admission's latest hold. There is no freshness test on `checked_at`, `since`, `rechecks`, or `next_check_at`. Each remaining job is routed afresh through `_pick` on the current store, with retry exclusions and slot-pinned retry exclusions. Therefore looking at an unchanged event-only wait every 600 seconds does not change the demand answer at any point between looks for the same store, waits and latest holds. An explicit differential test should cover times beyond 30 seconds.

`why` (`_why_job`, `daemon.py:4576-4590`) exposes `rechecks`, `since`, `checked_at`, `label`, and the job's `next_check_at`. It imposes no freshness condition. These timestamps/counts truthfully describe less frequent looks; its explanation remains available. `_admission_status` (`4617+`) reads the separately maintained whole-pass pending/reasons/idle state and builds current open lanes. C-6.11's idle line (`_note_admission`, `7510-7559`) also uses that whole-pass state, its own 60/600-second reporting clocks, and a fresh capacity view when logging; it does not inspect capacity-wait timestamps. Recovery deliberately forgets cached capacity waits and looks again; no persisted stale age is introduced.

## Work retained on the not-due path

Every pass still walks each queued/waiting job after the pass's disk/ramp, identity/registry, queued-job, lease, liveness, priority and machine reads. Before the not-due check it refreshes task/pin/door and FIFO conditions. For a not-due capacity job (`daemon.py:7990-8035`), it still evaluates `unadmittable_now` when a FIFO hold can follow or it waits for a lease; this is cached per model/job shape per pass. It rebuilds the model FIFO waiter entries and probe line, restores only lease keys actually taken to the lease queue (excludes `blocked`), refreshes a probe hold's `behind` when its predecessor changes, and reconstructs the job's visible hold with its existing next clock and current `for_good` result. A cached predecessor disappearing and a probe's own turn arriving bypass the clock exactly as before. None of these steps commits a wait-clock update.

The resulting hold is still supplied to `_admit_kind`'s `_gives_back` collection and `_offer_unrun_sweep` (`daemon.py:7313-7321`), and the sweep revalidates `_gives_back` before returning a tree (`_give_back_unrun`, `9217-9224`), so the rest/backoff changes must preserve this hold reconstruction for C-6.20.

## What an unchanged tree-bearing wait writes

The `attempt.reserved` transaction (`daemon.py:8392+`) is entered before admission knows whether a reservation is possible. When no lane is chosen and the verdict repeats, C-6.10 suppresses the extra decision row (`if not rechecks: add_decision`). It still executes `UPDATE jobs SET state='waiting',wait_reason=?,next_check_at=?` (`daemon.py:8577-8585`). `Store.transaction` sees that write and appends an `attempt.reserved` event with default `{}` at successful commit (`store.py:436-443`). It creates no attempt row and takes no reservation lease on that path. Thus the event name denotes an unsuccessful reservation transaction, not a successfully reserved attempt.
