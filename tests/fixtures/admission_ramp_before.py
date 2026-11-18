"""Frozen pre-ramp admission pass from release/217 at 5bd0a3a674c6 for R6 differential tests.

Re-frozen 2026-10-13 after release/217 gained #140 (detached-writer nested fence),
#185 (recycled-pid census), #183 (host push) and #179 (reset-credit lane_wait):
the ramp, disabled or absent, must still behave exactly like the current release pass.

Re-frozen again 2026-10-13 when #199 (admission properties) joined the hub's merge
chain after #194: `Daemon._admit_pass` extracted (ast, unchanged) from #199's head
dc9501d21, which is release/217 at 5bd0a3a674c6 plus #199. #199 changed the pass in
four places, so they are the only differences from the copy before: a capacity wait
keyed on `scheduler.verdict_signature` alone (C-6.10), the retry-exclusion merge
checked where the route is prepared and in the reservation (`_merged_exclusions`,
C-6.12), and a job whose probe was quarantined held as `uncertain` on that pass (C-4.1).
"""

def _admit_pass(self, holds: dict[str, dict], tally: dict, *, kind: str = "detached") -> None:
    """One pass over the queued jobs of one kind: `turn` or `detached` (C-26.9)."""
    output_identities: dict[str, str] = {}
    # C-6.9: the jobs of this pass that wait on an admission probe (C-11.4), in
    # its order; `_prepare_route` asks it whose turn a probe is. A turn never
    # carries a probe (C-26.9), so the turn pass, which runs beside this one,
    # keeps none and leaves the detached pass's alone.
    probe_line: list[tuple[str, str, frozenset[str] | None]] = []
    workspace_skips: set[str] = set()
    if kind == "detached":
        self._push_preparation_offered = False
        workspace_skips = self._reap_push_workspaces(holds)
        self._disk.begin_pass(self.policy, self._disk_rows() if disk_settings(self.policy)["enabled"] else (), utcnow())
        self._save_disk_floor()
        self._save_disk_latch()
        self._recover_probes()
        self._probe_line = probe_line
    desktop_account = self._desktop_identity()
    if kind == "detached":
        self._desktop_in_use()
        self._record_desktop_use()
    queued = self.store.query("SELECT * FROM jobs WHERE state IN ('queued','waiting') AND cancel_requested_at IS NULL ORDER BY created_at,rowid")
    # What admission remembers of a job goes when the job is no longer queued,
    # whichever pass notices. Each dict is walked as a copy: the other kind's
    # pass writes its own jobs' entries meanwhile (`dict.copy` is one C call).
    pending = {job["job_id"] for job in queued}
    if kind == "detached":
        for job_id, future in list(self._push_workspaces.items()):
            if job_id not in pending and (future.done() or future.cancel()):
                self._push_workspaces.pop(job_id, None)
                self._push_workspace_store_errors.pop(job_id, None)
                self._push_workspace_store_failures.pop(job_id, None)
                self._push_workspace_store_first_failure.pop(job_id, None)
                self.pushes.submit(self._discard_cancelled_push_workspace, job_id)
    tables = (self._capacity_waits, self._route_deferrals, self._retry_verdicts, self._turn_check_errors,
              self._pin_episodes, self._probe_misses)
    gone = {job_id for table in tables for job_id in table.copy() if job_id not in pending}
    if gone:
        # A job submitted after the read above, whose entry the other pass has
        # just written, is not gone: asked again, by id.
        marks = ",".join("?" * len(gone))
        gone -= {row["job_id"] for row in self.store.query(
            f"SELECT job_id FROM jobs WHERE job_id IN ({marks}) AND state IN ('queued','waiting') "
            "AND cancel_requested_at IS NULL", tuple(gone))}
    for table in tables:
        for job_id in gone:
            table.pop(job_id, None)
    queued = [job for job in queued if self._in_pass(job, kind)]
    # C-6.10: a lease that was held at the last pass and is not now is capacity
    # that came free (an attempt ended, a job let go of its worktree or its
    # output path), so backed-off capacity waits are looked at on this pass
    # rather than up to 30 s later. Timer probes come and go every cycle and
    # free nothing a job was waiting for. A detached job's admission probe
    # (C-11.4) runs in the detached pass, beside the turn pass (C-26.9), and
    # holds its lane from every job while it runs: a turn held off that lane
    # waits for exactly that lease, so the turn pass counts it, and looks
    # again as soon as the probe ends rather than on its backed-off clock.
    probes = "holder NOT LIKE 'probe:timer:%'" if kind == "turn" else "holder NOT LIKE 'probe:%'"
    leases_now = frozenset((row["lease_key"], row["holder"]) for row in self.store.query(
        f"SELECT lease_key,holder FROM leases WHERE {probes}"))
    with self._admission_lock:                  # the other pass replaces its own entry meanwhile
        freed = bool(self._leases_seen.get(kind, frozenset()) - leases_now)
        self._leases_seen = {**self._leases_seen, kind: leases_now}
    if kind == "detached" and self._take_answer_news():
        freed = True                            # C-6.14: a pilot answered; its lane is free
    cap = policy_cap(self.policy["caps"], "max_active_attempts")
    # C-6.9: who is waiting on each detached job, and C-6.13: how busy the
    # machine is, each read once for the pass. A turn is always `attended` and
    # never held by the guard, so the turn pass reads neither and stays cheap.
    liveness = self._liveness(queued) if kind == "detached" else None
    priority_jobs = self._priority_jobs(queued) if kind == "detached" else {}
    reading = machine.read() if kind == "detached" else None
    # C-6.9: FIFO within a tier holds among jobs that compete for a model. An
    # older job that cannot be placed holds back the later jobs that could run
    # where it could, and nothing else: on 2026-09-20 an Opus review with no
    # admissible lane kept three Fable-pinned jobs queued for hours beside
    # eleven free Fable lanes.
    # Each waiter carries the leases it waits for (held by another, needed free,
    # or kept for an older job): a job never waits behind a waiter for a lease
    # the job itself holds. A waiter with any is waiting for a lease, not a slot,
    # so it holds no later job back; it only keeps the last slot (`kept`), and
    # its lease keeps its order through `lease_queue` (fenced-turn-precheck
    # review P3-4: with a turn cap, a turn waiting on retention's fence held
    # every competing later turn for the whole retirement, slots free).
    waiters: dict[str, list[tuple[str, frozenset[str] | None, frozenset[str] | None, frozenset[str]]]] = {}
    # C-6.16: a priority job waiting for a slot holds back later competing detached jobs
    # of every tier, as any waiter holds back its own tier's (review of PR
    # #147: a waiting hard-tier priority job let later standard and trivial
    # jobs take the slots it was waiting for).
    priority_waiters: list[tuple[str, frozenset[str] | None, frozenset[str] | None, frozenset[str]]] = []

    def wait(tier, klass, waiter):
        waiters.setdefault(tier, []).append(waiter)
        if klass == "priority":
            priority_waiters.append(waiter)

    def older_waiters(tier):
        """The waiters a job of this queue may not pass, oldest first: every
        priority waiter, which the pass's order puts first, then its tier's."""
        own = waiters.get(tier, ())
        if not priority_waiters:              # a turn pass never has any: turns are `attended`
            return own
        first = {waiter[0] for waiter in priority_waiters}
        return [*priority_waiters, *(waiter for waiter in own if waiter[0] not in first)]
    # C-6.9: job id -> its ancestors, for holds scoped to a family (`hold_scope`).
    ancestry: dict[str, frozenset[str]] = {job["job_id"]: frozenset() for job in queued
                                           if not job.get("parent_job_id")}
    # C-6.9, C-26.9: FIFO on a lease. A job held waiting for a lease (a writable
    # checkout, an output path, a conversation, a native session) queues for it
    # here, first in the pass's order, and no later job of this pass takes it:
    # without this, a lease released after the pass began went to whichever job
    # looked next, and with no count cap nothing else keeps a later job behind
    # an earlier one (review of 1b38d641 for turns; detached jobs lost C-6.9's
    # hold-back with their caps on 2026-09-27, so two writable jobs in one
    # checkout raced the same way).
    lease_queue: dict[str, str] = {}

    def queue_for(keys, job_id):
        # A lane slot is never queued for: each job takes the lowest free one.
        for key in keys:
            if not key.startswith("lane:"):
                lease_queue.setdefault(key, job_id)
    roster = self._pin_roster()              # C-6.9: lane pins are compared by lane id (C-11.2)
    pin_views: list[dict] = []
    for_good_memo: dict = {}                 # C-11.8: `scheduler.unadmittable`'s, for this pass

    def pin_view() -> dict:
        """C-11.8: read once per pass, and only by a pass that needs it."""
        if not pin_views:
            pin_views.append(self._pin_view(desktop_account, refresh=kind == "detached"))
        return pin_views[0]
    # C-26.9: turns and detached jobs fill separate pools, so one being full
    # holds back only its own kind. Neither pool has a cap unless the policy
    # sets one (`conversations.max_active_turns`, `caps.max_active_attempts`):
    # an uncapped pool is never full.
    saturated: dict[str, bool] = {}
    turns_cap = turn_cap(self.policy.get("conversations"), "max_active_turns")
    # C-6.9, C-6.16: attended, priority, session, background. Priority is
    # FIFO across tiers; the other classes keep tier then FIFO.
    for job in scheduler.ordered_jobs(self.policy, queued, liveness, ancestors=priority_jobs):
        if job["job_id"] in workspace_skips:
            continue
        tier = job["tier"] or ("standard" if "standard" in self.policy["tiers"] else self.policy["tiers"][0])
        tier = scheduler.waiter_class(job, tier)     # C-26.9: turns queue apart from detached jobs
        klass = (scheduler.priority_class(job, liveness, policy=self.policy, jobs=priority_jobs)
                 if job["kind"] != "turn" else "attended")
        if job["started_at"] and age(job["started_at"]) >= job["max_wall_s"]:
            self.kill(protocol.KillArgs(job["job_id"]))
            continue
        if job["wait_reason"] == "route" and job["next_check_at"] and job["next_check_at"] > utcnow():
            # C-6.12: not a capacity wait, so it holds nobody back (C-6.9) and
            # no released lease brings it forward. It says what it met.
            holds[job["job_id"]] = self._route_hold(job["job_id"], job["next_check_at"])
            continue
        if job["kind"] == "turn":
            # C-24.5, C-30.4: a turn job whose conversation became blocked after
            # it was created (the legacy import holds one while the daemon is
            # down) places nothing and holds nobody back until both blocks clear.
            try:
                hold = self.conversations.admission_hold(job)
                if hold and hold.get("error_type"):
                    # A turn manifest that cannot be read: held, and said once.
                    if self._turn_check_errors.get(job["job_id"]) != hold["error_type"]:
                        self.log.warning("admission: job %s held: %s", job["job_id"], hold.get("error"))
                    self._turn_check_errors[job["job_id"]] = hold["error_type"]
                else:
                    self._turn_check_errors.pop(job["job_id"], None)
            except (sqlite3.Error, OSError):
                raise                           # C-6.12: a store error is the pass's; C-5.10 retries it
            except Exception as exc:            # C-6.12: anything else is this job's, never the pass's
                if self._turn_check_errors.get(job["job_id"]) != type(exc).__name__:
                    self.log.warning("admission: job %s: its conversation could not be checked: %s",
                                     job["job_id"], type(exc).__name__)
                self._turn_check_errors[job["job_id"]] = type(exc).__name__
                try:
                    manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json")
                except (OSError, ValueError):
                    manifest = None
                turn = manifest.get("turn") if isinstance(manifest, dict) else None
                hold = {"reason": "conversation-blocked",
                        "conversation_id": turn.get("conversation_id") if isinstance(turn, dict) else None,
                        "error_type": type(exc).__name__, "error": str(exc)[:200]}
            if hold:
                holds[job["job_id"]] = hold
                continue
        if job["pinned_lane"] and job["wait_reason"] not in ("approval", "uncertain"):
            # C-11.8: a job pinned to a lane that can never admit it is held here,
            # every pass, before it could wait behind or ahead of anyone (C-6.9):
            # it holds no other job back, its caller is told once, and it fails
            # once `admission.pin_grace_s` has passed with the lane still so.
            stuck = scheduler.pin_unadmittable(self.policy, pin_view(), job)
            if stuck:
                self._stuck_pin(job, stuck, holds)
                continue
            job = self._unstuck_pin(job, holds)
            if job is None:
                continue
        due = not (job["next_check_at"] and job["next_check_at"] > utcnow())
        if (job["kind"] != "turn" and job["wait_reason"] not in ("approval", "uncertain")
                and (due or job["wait_reason"] != "workspace")):
            # C-6.13: at the door, before any git: a saturated machine holds the
            # detached jobs of the classes its guard names. Never a turn or
            # priority job. A due workspace retry is held too, before its git
            # (review of PR #72); one whose clock runs still reports `workspace`
            # (C-6.11).
            busy = self._disk_hold(klass) or scheduler.machine_hold(self.policy, reading, klass)
            if busy:
                holds[job["job_id"]] = busy
                continue
        # C-4.5, C-6.9: while a transient retry is pinned to its last pair, the
        # job can run only there, and its demand is that one model on that lane.
        previous, extra_exclusions, retry = self._retry_pin(job)
        verdict = self._retry_verdicts.get(job["job_id"])
        let_go = bool(retry and verdict and verdict[0] == previous[-1]["attempt_id"] and not verdict[1]
                      and job["next_check_at"] and job["next_check_at"] > utcnow())
        models = scheduler.demand_models(self.policy, job if let_go else retry or job)
        lanes = scheduler.demand_lanes(roster, job if let_go else retry or job, self.policy)
        pool = "turn" if job["kind"] == "turn" else "detached"
        pool_cap = turns_cap if pool == "turn" else cap
        # C-6.9's FIFO exists so a later job cannot take the slot an older one
        # waits for. It holds only in a pool with a count one job could take
        # from another (`scheduler.pool_capped`): with no cap (the default) no
        # job waits for a slot, and an older job that cannot be placed (its lane
        # closed, its checkout leased) would otherwise keep every later one
        # waiting while lanes were free. A lease keeps its FIFO below, capped or
        # not, and a job waiting for one holds no later job back (`ahead`).
        scope = scheduler.hold_scope(self.policy, job)
        family = self._ancestors(job, ancestry) if scope == "family" else frozenset()

        own_here: list[frozenset[str]] = []

        def own_leases():
            """The leases this job holds itself: a retry keeps its job-held ones."""
            if not own_here:
                own_here.append(frozenset(row["lease_key"] for row in self.store.query(
                    "SELECT lease_key FROM leases WHERE holder=?", (job["job_id"],))))
            return own_here[0]

        def blocks(waiter):
            """Whether a waiter of this tier may keep a slot from this job: never
            one waiting for a lease this job holds, which moves only once this
            job has run (review of PR #72)."""
            return not (waiter[3] and waiter[3] & own_leases())

        def ahead(models, lanes):
            """The oldest waiter this job may not pass (C-6.9), or None: only one
            waiting for a slot. A waiter on a lease waits for no slot this job
            could take; its lease keeps its place in `lease_queue`, and the last
            slot is kept for it (`kept`). So this job is never held behind one
            waiting for a lease this job holds either, which moves only once this
            job has run (review of PR #72: a retrying resume and a newer one of
            the same native session, the newer ordered first by class, each
            waited for the other until `max_wall_s`)."""
            if scope is None:
                return None
            return next((waiter[0] for waiter in older_waiters(tier)
                         if not waiter[3]
                         and scheduler.competes(models, waiter[1], lanes, waiter[2])
                         and (scope == "pool" or family & self._ancestors(waiter[0], ancestry))), None)
        behind = ahead(models, lanes)
        if saturated.get(pool) or behind:
            holds[job["job_id"]] = ({"reason": job["wait_reason"]} if job["wait_reason"] in NOT_ADMISSIONS_TO_PLACE else
                                    {"reason": "fleet-full", "max_active_attempts": pool_cap} if saturated.get(pool) else
                                    {"reason": "behind-older-job", "behind": behind, "tier": tier})
            continue
        if job["wait_reason"] in ("approval", "uncertain"):
            holds[job["job_id"]] = {"reason": job["wait_reason"]}
            continue
        known = self._capacity_waits.get(job["job_id"])
        if known and job["wait_reason"] != "capacity":
            # C-6.10: the wait is no longer a capacity wait. A workspace
            # deferral (C-6.8) took it over, and that clock counts retries: a
            # look brought forward by an unrelated lease would spend one, and
            # eight of them end the job.
            self._capacity_waits.pop(job["job_id"], None)
            known = None
        # C-6.9: what the job's last look left it waiting on, when that is a probe.
        probing = (known["hold"] if kind == "detached" and known and known["hold"].get("reason") == "probe-pending"
                   and known["hold"].get("model") else None)
        # Held for an older job's turn at a probe, and nobody ahead of it on
        # this pass still waits for that probe: its own turn has come, and it
        # is not left to its backed-off clock (C-6.10) to find that out.
        turn = (scheduler.probe_turn(probe_line, probing["lane"], probing["model"])
                if probing and probing.get("behind") else None)
        turn_came = bool(probing and probing.get("behind") and turn is None)
        hurried = bool(freed and known and known["expedite"]) or turn_came
        if job["next_check_at"] and job["next_check_at"] > utcnow() and not hurried:
            # C-11.8: a job every lane refuses now for a reason no wait ends waits
            # for nothing another job could take, however its clock was set (a
            # transient retry's 60 s, or lanes that turned since its last look).
            # Asked only where a hold can follow (`hold_scope`), and once per
            # model and job shape per pass.
            lease_held = bool(known and known["hold"].get("reason") == "lease-held")
            asked = job["wait_reason"] == "capacity" and (scope is not None or lease_held)
            for_good = scheduler.unadmittable(self.policy, pin_view(), job, memo=for_good_memo) if asked else None
            if job["wait_reason"] == "capacity" and not for_good:
                # C-6.9: a lease wait, those kept for an older job too, is no slot wait.
                waiting_for = (frozenset([*(known["hold"].get("leases") or ()), *(known["hold"].get("queued") or ())])
                               if lease_held else frozenset())
                wait(tier, klass, (job["job_id"], models, lanes, waiting_for))
                if probing:
                    probe_line.append((job["job_id"], probing["model"],
                                       self._probe_reach(roster, job, probing["model"], lanes, extra_exclusions)))
            if lease_held and not for_good:
                # C-6.9, C-11.8: nor does such a job keep a lease's place in its queue.
                # Only the keys it takes: one it needs free but does not take (`blocked`,
                # retention's fence above a writer's folder, say) is never queued for
                # (C-6.11), on its clock as at its look (review of 5253faa2, P3).
                blocked = set(known.get("blocked") or ())
                queue_for([*(key for key in known["hold"].get("leases") or () if key not in blocked),
                           *(known["hold"].get("queued") or ())], job["job_id"])
            last = dict(known["hold"]) if known else {"reason": job["wait_reason"] or "waiting"}
            if turn and turn != last.get("behind"):
                # C-6.11: whose turn it is now; the job it was held for has started or ended.
                last["behind"] = turn
                self._capacity_waits[job["job_id"]] = {**known, "hold": dict(last)}
            if asked:
                last.pop("for_good", None)          # C-6.11: this pass's answer, not the last look's
            holds[job["job_id"]] = {**last, **({"for_good": for_good} if for_good else {}),
                                    "next_check_at": job["next_check_at"]}
            continue
        if self.store.one("SELECT 1 FROM attempts WHERE job_id=? AND state IN ('reserved','starting','running','finalizing','quarantined')", (job["job_id"],)):
            holds[job["job_id"]] = {"reason": "attempt-live"}
            continue
        run_folder = folders.canonical(job.get("worktree") or job["workdir"]) if job["kind"] == "turn" else None
        try:
            try:
                # C-6.5, C-8.4: submit could not spell this job's folder in full (`unspelled`),
                # so what it recorded may be below its checkout's top, or not the top its
                # `core.worktree` names: found again as submit finds it, once, for the look
                # below and for the reserving transaction alike (`_rediscovered`), then
                # spelled below like any other.
                derived = self._rediscovered(job)
            except (OSError, subprocess.SubprocessError, SalvageError):
                # C-6.8's, as a workspace that cannot be prepared is. A turn whose folder
                # submit recorded is in a tree retention is retiring still waits on that
                # fence, as it did before git was asked.
                if job["kind"] == "turn" and self._fence_hold(job, self._recorded_folder(job), holds, waiters,
                                                              tier, models, lanes, run_folder):
                    continue
                raise
            # `_turn_folder`, from the one answer above.
            if job["kind"] == "turn" and self._fence_hold(job, derived or self._recorded_folder(job), holds,
                                                          waiters, tier, models, lanes, run_folder):
                # C-8.4: retention is retiring the tree the turn's folder is in; no
                # workspace is prepared for a turn that cannot start, and no git runs
                # there but `_rediscovered`'s read (the transaction below still reads
                # the fence, and decides).
                continue
            baseline_at = utcnow_ms()           # C-26.14: before the start snapshot
            if not self._push_workspace_ready(job):
                holds[job["job_id"]] = {"reason": "workspace", "preparing": True}
                continue
            if (job.get("push_branch") is not None and job["sandbox"] == "workspace-write"
                    and not job["in_place"]):
                prepared = job.get("worktree") or str(self.root / "worktrees" / job["job_id"])
                if not Path(prepared).is_dir():
                    raise AdapterError("host push workspace disappeared after preparation", code=7,
                                       fix="resubmit the job to rebuild its checkout")
                # Readiness may race cancellation or removal. This local
                # ownership snapshot keeps _workspace's direct-call fallback
                # out of admission even if the directory disappears now.
                job = {**job, "worktree": prepared}
            workspace, head, baseline, skipped = self._workspace(job)
            pinned = self._pin_baseline(job, previous, workspace, head, baseline, skipped)
            native_session = job["caller_session"] if job["kind"] == "revive" else None
            if job["kind"] == "resume":
                manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}
                native_session = (manifest.get("resume") or {}).get("native_session_id")
                if not native_session:
                    raise AdapterError("resume source identity is missing",
                                       fix="resubmit the resume from the original job")
            if native_session and job["kind"] in ("resume", "revive"):
                # C-26.13: the session became a conversation's after this job
                # was submitted (a person opened it in the app). Refused, not
                # held: waiting on the `native:` lease would only take turns
                # with the conversation.
                self._refuse_conversation_session(native_session, job["kind"])
        except AdapterError as exc:
            self._fail_queued(job, str(exc) + (f"; fix: {exc.fix}" if exc.fix else ""), rc=exc.code)
            continue
        except (OSError, subprocess.SubprocessError, SalvageError) as exc:
            self._workspace_failed(job, exc)
            self._capacity_waits.pop(job["job_id"], None)      # C-6.10: the wait is C-6.8's now
            holds[job["job_id"]] = {"reason": "workspace", "error_type": type(exc).__name__,
                                    "error": str(exc)[:200]}
            continue
        write_target = self._write_target(job, workspace) if job["sandbox"] == "workspace-write" else None
        # C-8.4: a read-only turn's Git hold, which may differ from its cwd.
        read_folder = (self._submitted(job["job_id"]).get("folder") or workspace
                       if job["kind"] == "turn" and write_target is None else None)
        if derived is not None and read_folder:
            read_folder = derived
        elif derived is not None and write_target:
            write_target = derived
        # C-8.4, C-24.5, C-6.5: the folder a row will name (a turn's, or an in-place
        # writer's `worktree:`), spelled again now, off the store lock. Submit spelled
        # it, but a name it could not look up kept the case it was given: a native
        # session's cwd typed `…/jOB/vendor/lib`, spelled while retention had the tree
        # `Job` in quarantine, keyed the row on `jOB`, which no check on the tree
        # matched once the turn's job had ended (review of 8a112986, finding 1). A
        # folder spelled in full here is the one spelling of a folder that is there;
        # one that is not reserves no row (`absent`, below).
        named, absent = read_folder or (write_target if job["kind"] == "turn" or job["in_place"] else None), None
        if named:
            spelled, absent = folders.present(named)
            named = spelled
            if read_folder:
                read_folder = spelled
            else:
                write_target = spelled
        git_absent = absent
        # The provider's cwd may differ from Git's hold (core.worktree).
        # Require that extra reader row's folder to be present too.
        # Detached jobs keep the resolved cwd through launch and quarantine;
        # turns also require every name of their extra reader row to be present.
        run_folder, run_absent = (folders.present(workspace) if job["kind"] == "turn"
                                  else (folders.canonical(workspace), None))
        if absent is None and run_absent is not None:
            named, absent = run_folder, run_absent
        if absent is None:
            self._workspace_deferrals.pop(job["job_id"], None)
        if job["wait_reason"] == "workspace" and absent is None:
            # The workspace is ready; what the job waits for next is not it.
            with self.store.transaction("job.workspace_ready", job_id=job["job_id"]) as tx:
                tx.execute("UPDATE jobs SET state='queued',wait_reason=NULL,next_check_at=NULL "
                           "WHERE job_id=? AND state='waiting' AND wait_reason='workspace'", (job["job_id"],))
            job = {**job, "state": "queued", "wait_reason": None, "next_check_at": None}
        decision_job = job
        turn_block = None
        if job["kind"] == "turn":
            turn_block = (self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}).get("turn") or {}
            if turn_block.get("affinity_lane"):
                decision_job = {**job, "affinity_lane": turn_block["affinity_lane"]}   # C-26.2
        if retry:
            retry = {**job, "pinned_lane": retry["pinned_lane"], "pinned_model": retry["pinned_model"]}
            kept = self._retry_waits_on_a_slot(retry, extra_exclusions, desktop_account)
            self._retry_verdicts[job["job_id"]] = (previous[-1]["attempt_id"], kept)
            if kept:
                decision_job = retry
                models = scheduler.demand_models(self.policy, retry)
                lanes = scheduler.demand_lanes(roster, retry, self.policy)
            else:
                # C-4.5 "then next candidate": the pair's lane refuses it for
                # something a slot will not end, so the job routes as submitted,
                # and as submitted it keeps C-6.9's place behind older jobs.
                models = scheduler.demand_models(self.policy, job)
                lanes = scheduler.demand_lanes(roster, job, self.policy)
                behind = ahead(models, lanes)
                if behind:
                    # C-6.10: held after a look, so on a clock like every other
                    # such hold; until it is due the job is held at the top of
                    # the pass with its own demand, with no git and no scoring.
                    hold = {"reason": "behind-older-job", "behind": behind, "tier": tier}
                    rechecks = self._capacity_wait(job["job_id"], f"retry-let-go:behind:{behind}", hold)
                    next_check = after(scheduler.capacity_recheck_delay(rechecks))
                    with self.store.transaction("job.retry_let_go", job_id=job["job_id"]) as tx:
                        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                                   "WHERE job_id=? AND state IN ('queued','waiting') AND cancel_requested_at IS NULL",
                                   (next_check, job["job_id"]))
                    holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                    continue
        try:
            if extra_exclusions:
                # C-6.12: checked where the route is prepared, so a failure here counts
                # toward the deferral backoff instead of following a reset of it.
                self._merged_exclusions(job, extra_exclusions)
            approved, desktop_account = self._prepare_route(job, decision_job, extra_exclusions)
        except Unroutable as exc:
            self._early_routes.pop(job["job_id"], None)
            self._unroutable(job, exc, holds)
            continue
        finally:
            probe_note = self._probe_notes.pop(job["job_id"], None)
        early = self._early_routes.pop(job["job_id"], None)
        # C-6.12: this pass could evaluate the route, so the next failure starts the count again.
        self._route_deferrals.pop(job["job_id"], None)
        if approved is None:
            current = self._job(job["job_id"])
            if current["state"] in TERMINAL or current["cancel_requested_at"]:
                continue
            if current["wait_reason"] in ("approval", "uncertain"):
                # C-4.1, C-6.11: preparing the route quarantined its probe, so the job now
                # waits on a person. It holds back nobody and says so on this pass too.
                holds[job["job_id"]] = {"reason": current["wait_reason"]}
                continue
            wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
            # C-6.11: which probe, and whose turn it is when it is not this job's.
            hold = holds[job["job_id"]] = {"reason": "probe-pending", **(probe_note or {})}
            # A probe that ran set the job's clock itself (C-6.10), which is due
            # at once when the probe used the whole of it.
            clocked = "next_check_at" in hold or (current["next_check_at"] and current["next_check_at"] > utcnow())
            if not clocked:
                # C-6.10: the route could not be prepared and nothing set a
                # clock (the probe's slot is held, the lane changed hands, an
                # older job has the turn at the probe). The job would otherwise
                # be prepared, and a probe directory made, on every 50 ms tick
                # for as long as that lasts.
                behind = hold.get("behind")
                rechecks = self._capacity_wait(job["job_id"], f"probe-pending:{behind}" if behind else "probe-pending",
                                               hold)
                next_check = after(scheduler.capacity_recheck_delay(rechecks))
                with self.store.transaction("job.probe_deferred", job_id=job["job_id"],
                                            data={"behind": behind} if behind else None) as tx:
                    tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                               "WHERE job_id=? AND state IN ('queued','waiting') AND cancel_requested_at IS NULL",
                               (next_check, job["job_id"]))
                hold["next_check_at"] = next_check
            elif clocked:
                hold.setdefault("next_check_at", current["next_check_at"])
                self._refresh_hold(job["job_id"], hold)
            if kind == "detached" and hold.get("model"):
                # C-6.9: it waits on this probe, so it carries the next one (a job
                # whose probe was quarantined waits on an operator and left above).
                probe_line.append((job["job_id"], hold["model"],
                                   self._probe_reach(roster, job, hold["model"], lanes, extra_exclusions)))
            continue
        # C-6.3, C-3.7: the route is evaluated off the store lock, on rows read
        # in one snapshot (`_capacity_rows`): `_prepare_route`'s evaluation is
        # the one reserved on. The reserving transaction never evaluates a
        # route. It checks the decision against the rows it reads, a few by
        # index (`_route_stands`), at its own clock: the lanes the decision
        # looks at whose rows changed, or whose own clock reached its horizon
        # (a reading aged out, a closure ended), are judged again, and every
        # lane of the walk when a fleet or parent cap began or ended. It goes
        # on with exactly the decision an evaluation of those rows would make
        # at that clock, the same lane or another. Only a pin that names
        # another lane now (or an evaluation that would raise) rolls it back;
        # the route is evaluated again, off the lock, and checked again, up
        # to ROUTE_TRIES times in this pass. A clock on a lane the job could
        # never take (another provider's, one its pin does not name) is never
        # looked at: sixty Claude lanes' staggered readings refused every
        # check of a Codex job for as long as they were refreshed (review of
        # d04b8b3), and a cap that flipped between each evaluation and its
        # check refused them as long as it flipped (review of 5d14f98). Evaluating a whole capacity
        # view inside this transaction held the store lock for 13.5 s with the
        # daemon held to 5% of a core, and for 7.7 to 12.6 s on 2026-09-26
        # (load 110-170), when some commit landed between the two evaluations
        # nearly every time.
        if early is None:
            # Nothing handed over (a `_prepare_route` that stops early): evaluate here, off the lock.
            basis = {}
            try:
                early = (self._route(decision_job, extra_exclusions=extra_exclusions, desktop=desktop_account,
                                     basis=basis), basis)
            except Unroutable as exc:
                self._unroutable(job, exc, holds)
                continue
        decision, basis = early
        # C-11.4: the lanes `_probe_choice` left out stay out if the route is evaluated again,
        # and what it chose is what the reservation may take.
        rotation = tuple(basis.get("rotation", ()))
        route_exclusions = (*extra_exclusions, *rotation)
        rotated = (decision.chosen_lane, decision.chosen_model)
        status, route, last, probed = "moved", {"failed": False}, None, frozenset()
        for tries in range(1, ROUTE_TRIES + 1):
            # C-10.3: refreshed off the lock, at most `REGISTRY_READ_TTL_S` old; the
            # check inside reads the answer without touching the registry. A
            # change it places by is recorded (review of PR #72).
            self._desktop_in_use()
            self._record_desktop_use()
            output_claim = (resource_leases.OutputClaim.prepare(self.store.query, job["out_path"],
                                                                identities=output_identities)
                            if job["out_path"] else None)
            try:
                # C-6.12: outside the transaction, so a route that fails here rolls it back first.
                with self._isolated_route(job, holds) as route, \
                        self.store.transaction("attempt.reserved", job_id=job["job_id"]) as tx:
                    read = lambda sql, params: tx.execute(sql, params).fetchall()   # noqa: E731
                    job = self._job(job["job_id"])
                    if job["cancel_requested_at"] or job["state"] in TERMINAL:
                        status = "gone"
                        break
                    if native_session and job["kind"] in ("resume", "revive"):
                        # C-26.13: public open may have bound it during route
                        # preparation, after the early ownership check.
                        try:
                            self._refuse_conversation_session(native_session, job["kind"])
                        except AdapterError as exc:
                            self._fail_queued(job, str(exc) + (f"; fix: {exc.fix}" if exc.fix else ""), rc=exc.code)
                            status = "settled"
                            break
                    if tx.execute("SELECT 1 FROM attempts WHERE job_id=? AND state IN "
                                  "('reserved','starting','running','finalizing','quarantined')",
                                  (job["job_id"],)).fetchone():
                        # Checked again here: one job, one attempt at a time, whichever pass got there.
                        holds[job["job_id"]] = {"reason": "attempt-live"}
                        status = "held"
                        break
                    why, judged, standing = self._route_stands(basis, decision)
                    if why is not None:
                        raise _RouteMoved(why, judged)
                    # The decision as an evaluation now makes it, from the lanes
                    # whose rows changed: the same lane, or the one that now
                    # ranks first; a wait recorded on it is clocked by now's
                    # verdict (C-6.10).
                    same = (standing.chosen_lane, standing.chosen_model) == (decision.chosen_lane,
                                                                            decision.chosen_model)
                    decision = standing
                    self._count_route(**{"reused" if same else "rechosen": 1}, rejudged=judged)
                    if rotation and (scheduler.promoted_past(decision, rotated[1]) if decision.chosen_lane
                                     else scheduler.left_out_only(decision, rotation)):
                        # C-11.4: the lane `_probe_choice` chose stopped taking the job
                        # between its probe and this reservation, and with the lanes it
                        # left out still out the chain would walk on past its model (an
                        # earlier model is no promotion: review of #153 r2, P3-A), or a lane
                        # it left out is all that would take the job (review of #154). Rotation
                        # never promotes a job, nor refuses it a lane it left out: the
                        # job waits on C-6.10's clock and looks again. A verdict that
                        # holds whatever was left out (a full fleet) goes on as any does.
                        wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
                        hold = {"reason": "probe-pending", "lane": rotated[0], "model": rotated[1]}
                        if kind == "detached":                                              # C-6.9
                            probe_line.append((job["job_id"], rotated[1], self._probe_reach(
                                roster, job, rotated[1], lanes, extra_exclusions)))
                        rechecks = self._capacity_wait(job["job_id"], "probe-pending", hold)
                        next_check = after(scheduler.capacity_recheck_delay(rechecks))
                        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?",
                                   (next_check, job["job_id"]))
                        holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                        status = "held"
                        break
                    if extra_exclusions:
                        job["exclusions"] = self._merged_exclusions(job, extra_exclusions)
                        tx.execute("UPDATE jobs SET exclusions=? WHERE job_id=?", (job["exclusions"], job["job_id"]))
                    needs_probe = self._needs_probe(decision, job)
                    live = tx.execute("SELECT count(*) FROM attempts a JOIN jobs j USING(job_id) "
                                      "WHERE a.state IN ('reserved','starting','running','finalizing') "
                                      "AND (j.kind = 'turn') = ?", (pool == "turn",)).fetchone()[0]
                    saturated[pool] = pool_cap is not None and live >= pool_cap
                    # A job that passes an older waiting job of its tier leaves one
                    # active slot free, so the older job can start the moment its
                    # capacity appears instead of waiting out the jobs that passed it:
                    # its lane, or a lease it waits for (which holds no job back,
                    # `ahead`). An uncapped pool (C-26.9) has no last slot to keep.
                    kept = [waiter for waiter in older_waiters(tier) if blocks(waiter)]
                    limit = None if pool_cap is None else pool_cap - 1 if kept else pool_cap
                    at_limit = limit is not None and live >= limit
                    if not decision.chosen_lane or at_limit:
                        # C-11.8: a job every lane refuses for a reason no wait ends
                        # (the pinned lane turned so since this pass's check) waits
                        # for no slot, so it holds no later job back (C-6.9).
                        for_good = (None if decision.chosen_lane or rotation
                                    else scheduler.refused_for_good(self.policy, decision, decision_job,
                                                                    pin_view()["lanes"]))
                        if not for_good:
                            wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
                        # C-6.10: a wait that reaches the verdict it reached last time
                        # is rechecked later each time and adds no decision row. On
                        # 2026-09-20 three such jobs were each re-evaluated every
                        # second for hours: 2.7 rows of 22 KB a second, 681 MB of a
                        # 709 MB store, and a daemon at a full core doing it.
                        if not decision.chosen_lane:
                            label = scheduler.dominant_rejection(decision)
                        else:
                            # A lane would take it. Either the fleet is at its cap, or
                            # C-6.9 keeps the last slot for an older job of this tier.
                            label = "fleet-full" if saturated[pool] else "slot-kept"
                        hold = {"reason": label, **({"for_good": for_good} if for_good else {}),
                                **({"max_active_attempts": pool_cap} if label == "fleet-full" else {}),
                                **({"kept_for": kept[0][0], "tier": tier, "live": live,
                                    "max_active_attempts": pool_cap} if label == "slot-kept" else {})}
                        if kind == "turn" and not decision.chosen_lane:
                            # C-24.4, C-29.11: what each lane said, for the message's
                            # reason (`conversations.waits`), never just "capacity".
                            from .conversations import waits as turn_waits
                            hold["lanes"] = turn_waits.lane_summary(decision)
                        rechecks = self._capacity_wait(job["job_id"], scheduler.verdict_signature(decision), hold,
                                                       lane_wait=not decision.chosen_lane)
                        waiting = scheduler.waiting_metadata(decision, rechecks=rechecks)
                        if not rechecks:
                            self.store.add_decision(job["job_id"], decision)
                        tx.execute("UPDATE jobs SET state='waiting',wait_reason=?,next_check_at=? WHERE job_id=?",
                                   (waiting["wait_reason"], waiting["next_check_at"], job["job_id"]))
                        holds[job["job_id"]] = {**hold, "next_check_at": waiting["next_check_at"]}
                        status = "held"
                        if kind == "turn":
                            probed = self._probes_holding(decision)
                        break
                    if needs_probe and (decision.chosen_lane, decision.chosen_model) not in approved:
                        # The chosen identity changed after its probe; a later pass
                        # probes the new pair (`_prepare_route`). C-6.10: on a clock,
                        # or a lane whose state keeps moving is probed every tick.
                        wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
                        hold = {"reason": "probe-pending", "lane": decision.chosen_lane,
                                "model": decision.chosen_model}
                        if kind == "detached":                                              # C-6.9
                            probe_line.append((job["job_id"], decision.chosen_model, self._probe_reach(
                                roster, job, decision.chosen_model, lanes, extra_exclusions)))
                        rechecks = self._capacity_wait(job["job_id"], "probe-pending", hold)
                        next_check = after(scheduler.capacity_recheck_delay(rechecks))
                        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?", (next_check, job["job_id"]))
                        holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                        status = "held"
                        break
                    seq = len(previous) + 1
                    aid = ids.attempt_id(job["job_id"], seq)
                    lane_id = decision.chosen_lane
                    leases = [(self._slot_lease(tx, lane_id, pool == "turn"), aid)]
                    if native_session:
                        # C-12.3/4, C-12.6: filesystem read-only permissions do not
                        # isolate a provider transcript. Resume and revive share
                        # this job-held lease through retry, export and quarantine.
                        leases.append((native_session_lease_key(lane_id, native_session), job["job_id"]))
                        # C-26.3: one writer per native session whichever lane, so a
                        # resume or revive and a conversation turn exclude each other.
                        provider = self.policy["models"][decision.chosen_model]["provider"]
                        leases.append((resource_leases.native_key(provider, native_session), job["job_id"]))
                    if turn_block is not None:
                        leases.append((f"conversation:{turn_block['conversation_id']}", job["job_id"]))
                        if turn_block.get("native_session_id") or turn_block.get("new_session_id"):
                            sid = turn_block.get("native_session_id") or turn_block.get("new_session_id")
                            leases.append((resource_leases.native_key(turn_block['provider'], sid), job["job_id"]))
                            for held_key, holder in resource_leases.continuation_holds(read, sid):
                                if holder != job["job_id"]:
                                    leases.append((held_key, job["job_id"]))  # waits for the resume/revive
                    if job.get("round_lease"):
                        leases.append((job["round_lease"], f"gate-round:{job['job_id']}"))
                    if job["out_path"]:
                        leases.append((output_claim.key, job["job_id"]))
                    # Keys this job needs free but does not take (C-6.5, C-24.5): a turn
                    # shares its folder with other turns, so it never holds the
                    # exclusive key, yet it may not write beside a detached writer.
                    blockers: list[str] = []
                    if job["sandbox"] == "workspace-write" and job["kind"] == "turn":
                        # C-24.5 (the owner's ruling of 2026-09-28, "nothing should be
                        # queued"): conversations that share a folder run at once, as
                        # sessions of the Claude app do. Each turn holds its own row, so
                        # retention and a detached writer still see the folder in use.
                        leases.append((folders.turn_key(write_target, job["job_id"], writable=True), job["job_id"]))
                        blockers.append(folders.exclusive_key(write_target))
                    elif job["sandbox"] == "workspace-write":
                        # C-6.5: the hold is where the job writes. A session is not a
                        # place, so it takes no lease; its instances are told apart
                        # at submit. A detached writer still writes alone: it waits
                        # while a conversation turn writes there.
                        own = folders.exclusive_key(write_target)
                        leases.append((own, job["job_id"]))
                        blockers.extend(key for key, _ in folders.turn_holds(read, write_target, (folders.TURN,)))
                        # C-8.4: nor while retention retires a tree its folder is in,
                        # a repository nested in a job's worktree (its own fence is
                        # `own`, contested above). A detached writer above it holds
                        # another checkout, not this one (C-6.5).
                        blockers.extend(key for key in folders.retiring(read, write_target) if key != own)
                    elif job["kind"] == "turn" and read_folder:
                        # C-8.4, C-13.4: a read-only turn excludes no writer, but
                        # retention never removes a folder a turn is working in, or
                        # the tree it is nested in.
                        leases.append((folders.turn_key(read_folder, job["job_id"], writable=False), job["job_id"]))
                    held_folder = write_target or read_folder
                    if job["kind"] == "turn":
                        # C-8.4: only retention's fences above either folder
                        # hold a turn; a detached writer above holds another
                        # checkout (C-6.5). Use the early look's comparison.
                        fence, held_folder = self._turn_fences(read, held_folder, git_absent,
                                                                run_folder, run_absent)
                        blockers.extend(fence)
                    else:
                        # The writer already contests its own exclusive key.
                        own_run_key = folders.exclusive_key(write_target) if write_target else None
                        blockers.extend(key for key in folders.retiring(read, run_folder)
                                        if key != own_run_key)
                    if run_folder and (job["kind"] != "turn" or run_folder != (write_target or read_folder)):
                        # This job-held reader row keeps the resolved cwd through
                        # retries and quarantine without changing the Git hold's
                        # writer rules. Holder-keyed cleanup releases it.
                        leases.append((folders.turn_key(run_folder, job["job_id"], writable=False), job["job_id"]))
                    if absent is not None and job["kind"] != "turn":
                        # C-8.4: its folder is not there now, so the names that could not
                        # be looked up may not be the ones its volume stores: retention may
                        # have its tree in quarantine under a fence spelled `Job`. Any
                        # fence of retention's that folds alike above it holds it, as the
                        # fence on its spelling would; with none it reserves nothing.
                        taken = {key for key, _ in leases}       # contested already when held
                        blockers.extend(key for key in folders.retiring(read, named, folded=True)
                                        if key not in taken)
                    revive_key = (revive_lease_key(job["caller_session"])
                                  if job["kind"] == "revive" and job["caller_session"] else None)
                    if revive_key:
                        # C-23.55: the census the sweep skips on is the lease rows,
                        # read inside the admitting transaction, not a snapshot taken
                        # at the start of the pass.
                        held = next((holder for _, holder in resource_leases.native_holds(read, revive_key)
                                     if holder != job["job_id"]), None)
                        if held:
                            # Skipped, not queued: waiting for the other revive to
                            # end would launch the twin the moment it did.
                            self._skip_revive(tx, job, held)
                            status = "held"
                            break
                        leases.append((revive_key, job["job_id"]))
                    def lease_holds(key):
                        return (output_claim.holds(read) if output_claim and key == output_claim.key
                                else resource_leases.native_holds(read, key))
                    current = {key: rows for key, _ in leases if (rows := lease_holds(key))}
                    contested = [key for key, holder in leases if any(owner != holder for _, owner in current.get(key, ()))]
                    blocked = [key for key in dict.fromkeys(blockers)
                               if (r := tx.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone())
                               and r[0] != job["job_id"]]
                    # A lease this job already holds (a retry keeps its job-held
                    # ones) is never queued behind a job waiting for it: that job
                    # waits for this one to run and release it (review of PR #72).
                    queued = [key for key, holder in leases if key not in current
                              and lease_queue.get(key, job["job_id"]) != job["job_id"]]
                    if contested or blocked or queued:
                        wait(tier, klass, (job["job_id"], models, lanes, frozenset(contested + blocked + queued)))
                        # `leases` are held by another job; `queued` are free but kept for
                        # an older job waiting for them (C-6.9, C-26.9), named by `queued_behind`.
                        # Either way it waits for a lease, not a slot: it holds no later job
                        # back, and passing jobs keep the last slot for it (C-6.9).
                        # A key it only needs free (`blocked`) is never queued for: turns
                        # share their folder, so no turn takes it from another, and no
                        # detached job takes a turn's row. It is among the keys the waiter
                        # waits for, so a job holding it is never held behind the waiter.
                        # A turn's hold names its folder: a fence it waits for may be
                        # on a tree that folder is in, which its reason says (C-6.11).
                        hold = {"reason": "lease-held", "leases": contested + blocked,
                                **({"folder": held_folder}
                                   if job["kind"] == "turn" and (write_target or read_folder) else {}),
                                **({"queued": queued, "queued_behind": sorted({lease_queue[key] for key in queued})}
                                   if queued else {})}
                        queue_for(contested + queued, job["job_id"])
                        rechecks = self._capacity_wait(job["job_id"], "lease-held:" + ",".join(sorted(contested + blocked + queued)), hold,
                                                       blocked=blocked)
                        next_check = after(scheduler.capacity_recheck_delay(rechecks))
                        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?", (next_check, job["job_id"]))
                        holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                        status = "held"
                        break
                    if absent is not None:
                        status = "absent"           # no row on a folder that is not there now
                        break
                    for key, holder in leases:
                        tx.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)", (key, holder, utcnow()))
                    evidence = {"baseline_commit": head, "model_short": decision.chosen_model,
                                "run_folder": run_folder,
                                **({"baseline_ref": pinned["path"]} if pinned else {}),
                                **({"baseline_skipped": _skipped(skipped)} if skipped else {})}
                    if job["kind"] == "turn":
                        # C-26.14: the turn's window opens before its start snapshot, and
                        # its folder is where another turn's window may overlap it.
                        evidence.update(baseline_at=baseline_at, folder=write_target or read_folder)
                    reserved_at = utcnow()
                    if job["kind"] != "turn" and self._disk.settings["enabled"]:
                        evidence["disk_reservation"] = self._disk.evidence()
                    tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,baseline_tree,evidence_json,reserved_at) VALUES(?,?,?,?,?,'reserved',?,?,?)",
                               (aid, job["job_id"], seq, lane_id, self.policy["models"][decision.chosen_model]["id"], baseline,
                                json.dumps(evidence), reserved_at))
                    if pinned:
                        self.store.add_artifact(aid, **pinned)      # C-13.1: kept, as a salvage ref is
                    tx.execute("INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json) VALUES(?,?,?,?,?)",
                               (job["job_id"], aid, utcnow(), self.policy_digest, json.dumps(dataclasses.asdict(decision))))
                    tx.execute("UPDATE jobs SET state='running',wait_reason=NULL,next_check_at=NULL,worktree=?,started_at=COALESCE(started_at,?) WHERE job_id=?",
                               (workspace if job["sandbox"] == "workspace-write" else None, utcnow(), job["job_id"]))
                    with self._busy_lock:
                        self._busy.add(aid)
                    status = "placed"
            except _RouteMoved as moved:
                # C-6.3: rolled back, nothing written. Evaluated again off the
                # lock, on a new snapshot, and checked again.
                self._count_route(**{moved.why: 1}, rejudged=moved.judged)
                last = moved
                if tries == ROUTE_TRIES:
                    break
                self._count_route(again=1)
                basis = {}
                try:
                    decision = self._route(decision_job, extra_exclusions=route_exclusions,
                                           desktop=desktop_account, basis=basis)
                except Unroutable as exc:
                    self._unroutable(job, exc, holds)
                    status = "settled"
                    break
                continue
            break
        if probed:
            # C-6.10, C-26.9: a turn held off a lane by a detached job's admission
            # probe waits for exactly that lease, whenever the probe began: it is
            # counted as seen, so the next turn pass finds it gone the moment the
            # probe ends, and looks at once (review of 5d14f98: a probe that began
            # after this pass read its leases went unseen, and the turn waited out
            # its backed-off clock).
            with self._admission_lock:
                self._leases_seen = {**self._leases_seen,
                                     kind: self._leases_seen.get(kind, frozenset()) | probed}
        if route["failed"]:
            continue
        if status == "moved" and last is not None and last.error is not None:
            self._unroutable(job, Unroutable(last.error), holds)     # C-6.12: named, backed off, warned
            continue
        if status == "moved":
            # C-6.3: ROUTE_TRIES evaluations in a row were overtaken by commits
            # before they could be reserved. The job keeps its place: the later
            # jobs it competes with wait behind it (C-6.9), and the next pass,
            # which follows this one at once, looks at it again.
            self._count_route(deferred=1)
            # C-11.8: never from a decision with rotation's lanes left out, which read
            # `excluded`, a standing refusal they are not (review of #153 r2, P3-B).
            if rotation or not scheduler.refused_for_good(self.policy, decision, decision_job, pin_view()["lanes"]):
                wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
            holds[job["job_id"]] = {"reason": "route-moved", "tries": ROUTE_TRIES}
            self._refresh_hold(job["job_id"], holds[job["job_id"]])     # C-6.11: the last look's finding
            continue
        if status == "absent":
            self._folder_absent(job, named, absent, holds)
            continue
        if status != "placed":
            continue
        if job["kind"] != "turn":
            self._disk.reserve(aid, reserved_at)
        tally["placed"] += 1
        tally.setdefault("placed_jobs", []).append(job["job_id"])
        self._capacity_waits.pop(job["job_id"], None)
        # C-6.10: taken after this pass's snapshot. If the attempt ends before
        # the next one, that is a release the next pass must still see.
        with self._admission_lock:
            self._placed_unnoted += 1
            self._leases_seen = {**self._leases_seen,
                                 kind: self._leases_seen.get(kind, frozenset()) | frozenset(leases)}
        try:
            self._boundary("reserved", job["job_id"], aid)
            self._pending_launches.add(aid)
        finally:
            with self._busy_lock:
                self._busy.discard(aid)
        self._notify()
