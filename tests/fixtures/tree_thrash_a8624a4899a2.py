"""Frozen D-TREE-THRASH1 differential oracle: exact original method bytes.

Source: a8624a4899a2:subfleet/daemon.py, lines 7561-8914.
Full source SHA256: c78dfc024c802627dfd83f0504fa872baf6d11605b4d5dbab8d2e6818faa753e
Method SHA256: a894326e79493fd6dc79b8fea75fd392add53d8283f123f4e49e328ee4afbcf5

Created by parsing the base source and copying the method's complete lines,
without rewriting its indentation or AST. The test checks those copied bytes
before compiling this method in the current daemon's namespace. The wrapper
exists only to retain the original method indentation; this file is not imported.
"""


class Daemon:
    def _admit_pass(self, holds: dict[str, dict], tally: dict, *, kind: str = "detached") -> None:
        """One pass over the queued jobs of one kind: `turn` or `detached` (C-26.9)."""
        output_identities: dict[str, str] = {}
        # C-6.9: the jobs of this pass that wait on an admission probe (C-11.4), in
        # its order; `_prepare_route` asks it whose turn a probe is. A turn never
        # carries a probe (C-26.9), so the turn pass, which runs beside this one,
        # keeps none and leaves the detached pass's alone.
        probe_line: list[tuple[str, str, frozenset[str] | None]] = []
        ramp_hold: dict | None = None
        workspace_skips: set[str] = set()
        if kind == "detached":
            self._push_preparation_offered = False
            workspace_skips = self._reap_push_workspaces(holds)
            self._disk.begin_pass(self.policy, self._disk_rows() if disk_settings(self.policy)["enabled"] else (), utcnow())
            self._save_disk_floor()
            self._save_disk_latch()
            ramp_config = self._begin_ramp_pass()
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
                    self.pushes.submit(self._run_worker, self._discard_cancelled_push_workspace, job_id)
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
        if kind == "detached":
            self._pin_stale.intersection_update(pending)
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
        # C-11.10: an operator's task hold is a row here too, and no capacity: its
        # release frees nothing a capacity wait waits for (it wakes its own jobs).
        leases_now = frozenset((row["lease_key"], row["holder"]) for row in self.store.query(
            f"SELECT lease_key,holder FROM leases WHERE {probes} AND NOT (lease_key >= ? AND lease_key < ?)",
            LEASE_CENSUS_SKIPS))
        quarantines_now = (frozenset(row["attempt_id"] for row in self.store.query(PROVIDER_QUARANTINES)
                                    if scheduler.uncontained_quarantine(row))
                           if scheduler.provider_caps_on(self.policy) else frozenset())
        with self._admission_lock:                  # the other pass replaces its own entry meanwhile
            freed = bool(self._leases_seen.get(kind, frozenset()) - leases_now
                         or self._provider_quarantines_seen.get(kind, frozenset()) - quarantines_now)
            self._leases_seen = {**self._leases_seen, kind: leases_now}
            self._provider_quarantines_seen = {**self._provider_quarantines_seen, kind: quarantines_now}
        if kind == "detached" and self._take_answer_news():
            freed = True                            # C-6.14: a pilot answered; its lane is free
        cap = policy_cap(self.policy["caps"], "max_active_attempts")
        # C-6.9: who is waiting on each detached job, and C-6.13: how busy the
        # machine is, each read once for the pass. A turn is always `attended` and
        # never held by the guard, so the turn pass reads neither and stays cheap.
        liveness = self._liveness(queued) if kind == "detached" else None
        priority_jobs = self._priority_jobs(queued) if kind == "detached" else {}
        reading = self._machine_reading() if kind == "detached" else None
        if kind == "detached":
            reading = self._machine_guard.advance(self.policy, reading)
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
        # A waiter may still demand another model in its chain. A later job
        # trying a model this waiter is steered away from is compared again
        # on its actual selection, before any reservation.
        steered_demands: dict[str, frozenset[str]] = {}

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
        # C-11.10: the operator's task holds, read once and again only after a hold
        # or release commits (the epoch is read first, so a commit after the read
        # is read at the next job).
        task_hold_read = [self._task_hold_epoch]
        task_hold_read.append(self._task_holds_now())

        def episodes_after_commits(epoch: int) -> None:
            """C-11.10, T6: once a hold or release has committed since the last mark, every
            open pin episode of this pass's jobs is read again from the store before the
            pass looks at its job (`_pin_stale`): the commit paused or gave back its grace there."""
            if kind == "detached" and epoch != self._pin_epoch:
                self._pin_epoch = epoch
                self._pin_stale.update(job["job_id"] for job in queued
                                       if self._pin_episodes.get(job["job_id"], {}).get("open"))
        episodes_after_commits(task_hold_read[0])
        for job in scheduler.ordered_jobs(self.policy, queued, liveness, ancestors=priority_jobs):
            if job["job_id"] in workspace_skips:
                continue
            if task_hold_read[0] != self._task_hold_epoch:
                task_hold_read = [self._task_hold_epoch]
                task_hold_read.append(self._task_holds_now())
                episodes_after_commits(task_hold_read[0])
            # C-11.10: first, before any look: a detached job of a held task waits for the
            # hold, looked at no further, holding no one back (it registers as no waiter
            # and queues for no lease), unless a person must act on it first. A job a hold
            # kept waiting that no hold covers now is queued again and looked at here.
            task_hold = self._task_hold_for(job, task_hold_read[1])
            if task_hold is not None and job["wait_reason"] not in ("approval", "uncertain"):
                self._task_held(job, task_hold, holds)       # reads a stale episode again (T6)
                continue
            if job["job_id"] in self._pin_stale:
                # A release or a hold (the socket's thread) paused this job's pin grace or gave it
                # back in the store: its episode is read again before anything looks at it (T6).
                self._pin_stale.discard(job["job_id"])
                self._sync_pin_episode(job["job_id"])
            if job["wait_reason"] == "task-held":
                job = self._task_hold_ended(job, holds=holds)
                if job is None:
                    continue                    # C-11.10: held again since the door's read
            tier = job["tier"] or ("standard" if "standard" in self.policy["tiers"] else self.policy["tiers"][0])
            tier = scheduler.waiter_class(job, tier)     # C-26.9: turns queue apart from detached jobs
            klass = (scheduler.priority_class(job, liveness, policy=self.policy, jobs=priority_jobs)
                     if job["kind"] != "turn" else "attended")
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
                                         job["job_id"], refusals.scrubbed_words(exc))
                    self._turn_check_errors[job["job_id"]] = type(exc).__name__
                    try:
                        manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json")
                    except (OSError, ValueError):
                        manifest = None
                    turn = manifest.get("turn") if isinstance(manifest, dict) else None
                    # The turn's `state_reason` and `why` read the hold (C-25.7): its words are logged above.
                    hold = {"reason": "conversation-blocked",
                            "conversation_id": turn.get("conversation_id") if isinstance(turn, dict) else None,
                            "error_type": refusals.class_name(exc), "error": "its text is in daemon.log"}
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
                    continue                    # C-11.10: held since the door's look
            if (not job["pinned_lane"] and job["kind"] != "turn"
                    and job["wait_reason"] not in ("approval", "uncertain")
                    and self.store.one("SELECT 1 FROM attempts WHERE job_id=? LIMIT 1", (job["job_id"],))):
                # D-WALL1 leaves admission waits uncharged. A retry that no
                # candidate can ever take instead has C-11.8's durable grace.
                _, excluded, _ = self._retry_pin(job)
                try:
                    effective = {**job, "exclusions": self._merged_exclusions(job, excluded)} if excluded else job
                except Unroutable:
                    permanent = None  # C-6.12 still owns an unevaluable route.
                else:
                    permanent = scheduler.unadmittable(self.policy, pin_view(), effective,
                                                        memo=for_good_memo, permanent_only=True)
                if permanent:
                    self._stuck_pin(job, {"lane_id": None, "reasons": permanent, "closures": [],
                                          "all_lanes": True}, holds)
                    continue
                job = self._unstuck_pin(job, holds)
                if job is None:
                    continue                    # C-11.10: held since the door's look
            due = not (job["next_check_at"] and job["next_check_at"] > utcnow())
            if (job["kind"] != "turn" and job["wait_reason"] not in ("approval", "uncertain")
                    and (due or job["wait_reason"] != "workspace")):
                # C-6.13: at the door, before any git: a saturated machine holds the
                # detached jobs of the classes its guard names. Never a turn or
                # priority job. A due workspace retry is held too, before its git
                # (review of PR #72); one whose clock runs still reports `workspace`
                # (C-6.11).
                busy = self._disk_hold(klass) or scheduler.machine_hold(self.policy, reading, klass)
                if not busy and job["kind"] != "turn" and ramp_config["enabled"]:
                    # Once full, ordinary placements stop for this whole pass.
                    # Otherwise an expiry between two jobs could let the later
                    # one pass the earlier job we just held (C-6.18's FIFO).
                    busy = (ramp_hold if klass != "priority" and ramp_hold
                            else self._ramp.hold(klass, utcnow()))
                    if busy:
                        ramp_hold = busy
                if busy:
                    holds[job["job_id"]] = busy
                    continue
            # C-4.5, C-6.9: while a transient retry is pinned to its last pair, the
            # job can run only there, and its demand is that one model on that lane.
            previous, extra_exclusions, retry = self._retry_pin(job)
            verdict = self._retry_verdicts.get(job["job_id"])
            let_go = bool(retry and verdict and verdict[0] == previous[-1]["attempt_id"] and not verdict[1]
                          and job["next_check_at"] and job["next_check_at"] > utcnow())
            # C-6.9, C-9.5: FIFO must see the same live steer as routing,
            # including when a backed-off waiter is not due for evaluation.
            routed_job = self._route_job(job, previous)
            models = scheduler.demand_models(self.policy, routed_job if let_go else retry or routed_job)
            if routed_job.get("capacity_steer"):
                original = scheduler.demand_models(self.policy, job)
                remaining = scheduler.demand_models(self.policy, routed_job)
                if original is not None and remaining is not None:
                    steered_demands[job["job_id"]] = original - remaining
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
            scope = scheduler.hold_scope(self.policy, job, roster=roster)
            # Keep the old fleet/lane/family scope separate from provider FIFO.
            # With provider caps enabled, narrow demand to the selected provider
            # before applying either competition rule.
            fifo_scope = scheduler.hold_scope(self.policy, job, roster=roster, include_providers=False)
            family = self._ancestors(job, ancestry) if fifo_scope == "family" else frozenset()
            defer_fifo = any(scheduler.provider_cap(self.policy, provider) is not None
                             for provider in self.policy.get("providers", {}))

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

            def ahead(models, lanes, *, chosen=None):
                """The oldest waiter this job may not pass (C-6.9), or None: only one
                waiting for a slot. A waiter on a lease waits for no slot this job
                could take; its lease keeps its place in `lease_queue`, and the last
                slot is kept for it (`kept`). So this job is never held behind one
                waiting for a lease this job holds either, which moves only once this
                job has run (review of PR #72: a retrying resume and a newer one of
                the same native session, the newer ordered first by class, each
                waited for the other until `max_wall_s`)."""
                if fifo_scope is None:
                    return None

                def compared_models(waiter):
                    skipped = steered_demands.get(waiter[0], frozenset())
                    if skipped and models is not None and skipped & models:
                        # The later job may run on a model the waiter cannot use.
                        # Defer only this comparison until selection; if it selects
                        # the waiter's remaining model, FIFO still applies.
                        return frozenset({chosen}) if chosen else frozenset()
                    return models

                return next((waiter[0] for waiter in older_waiters(tier)
                             if not waiter[3]
                             and scheduler.competes(compared_models(waiter), waiter[1], lanes, waiter[2])
                             and (fifo_scope == "pool" or family & self._ancestors(waiter[0], ancestry))), None)

            def unadmittable_now(far_s=None):
                """C-11.8, C-6.12: the standing refusals that keep every lane from this job now (or
                None), and whether its route can be evaluated. The lanes its attempts excluded are
                merged in (C-4.5) as its route merges them: a lane re-enrolled since a `limited`
                attempt is excluded under its successor's id, which only `_retry_pin` knows (review
                of #201 r1). A merge that cannot be made (a stored exclusion that is not a name) is
                its route's problem, a `route` wait once it is looked at, which holds nobody: so such
                a job holds nobody meanwhile either (review of #201 r2). With `far_s` 0, every
                closure in force counts, however soon it ends (C-6.20)."""
                try:
                    effective = ({**job, "exclusions": self._merged_exclusions(job, extra_exclusions)}
                                 if extra_exclusions else job)
                except Unroutable:
                    return None, False
                return scheduler.unadmittable(self.policy, pin_view(), effective, memo=for_good_memo,
                                              far_s=far_s), True
            behind = None if defer_fifo else ahead(models, lanes)
            for_good = None
            if (behind and not saturated.get(pool) and job["wait_reason"] == "capacity"
                    and job["next_check_at"] and job["next_check_at"] > utcnow()):
                # C-6.9, C-6.12: a job held behind an older one while its capacity clock runs still
                # cannot be placed on this pass for want of a slot or a lane, so it still holds back
                # the later jobs of its tier that compete with it, with the demand it has while that
                # clock runs (its own once a look has let a retry's pin go), as it would were nothing
                # ahead of it (below). Before, it registered nothing here, and a younger job that
                # competes with it but not with the older one was placed past it. This adds holds
                # only while the clock runs: a job held here gets no look, so nothing starts it a new
                # clock, and once that clock is due it holds nobody unless a look finds it waiting
                # again (a let-go retry's due look evaluates its pair, which may let the pin go and
                # set the next clock, C-6.12). A lease wait still holds nobody (`ahead`; the lease's
                # queue keeps its place), nor does a job no lane can admit (C-11.8), which its hold
                # says (C-6.11), lease wait or not, nor one whose route cannot be evaluated (C-6.12).
                # As before this change, a job held here is not put in a probe's line: it carries no
                # probe on this pass.
                held = self._capacity_waits.get(job["job_id"])
                for_good, routable = unadmittable_now()
                if routable and not for_good and not (held and held["hold"].get("reason") == "lease-held"):
                    wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
            if saturated.get(pool) or behind:
                holds[job["job_id"]] = ({"reason": job["wait_reason"]} if job["wait_reason"] in NOT_ADMISSIONS_TO_PLACE else
                                        {"reason": "fleet-full", "max_active_attempts": pool_cap} if saturated.get(pool) else
                                        {"reason": "behind-older-job", "behind": behind, "tier": tier,
                                         **({"for_good": for_good} if for_good else {})})
                older_hold = holds.get(behind, {})
                if older_hold.get("key", "").startswith("providers."):
                    holds[job["job_id"]].update({key: older_hold[key] for key in
                                                ("provider", "live", "max_active_attempts", "key")})
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
            # C-6.9, C-6.10: a cached FIFO hold is valid only while its named
            # predecessor still waits for capacity in this pass. A job that
            # started (or ended, became unadmittable, or now waits for a lease)
            # holds nobody back. Recheck now; the current caps still apply.
            predecessor = (known["hold"].get("behind")
                           if known and known["hold"].get("reason") == "behind-older-job" else None)
            unblocked = predecessor is not None and not any(
                waiter[0] == predecessor and not waiter[3] for waiter in older_waiters(tier))
            hurried = bool(freed and known and known["expedite"]) or turn_came or unblocked
            if job["next_check_at"] and job["next_check_at"] > utcnow() and not hurried:
                # C-11.8: a job every lane refuses now for a reason no wait ends waits
                # for nothing another job could take, however its clock was set (a
                # transient retry's 60 s, or lanes that turned since its last look).
                # Asked only where a hold can follow (`hold_scope`), and once per
                # model and job shape per pass.
                lease_held = bool(known and known["hold"].get("reason") == "lease-held")
                asked = job["wait_reason"] == "capacity" and (scope is not None or lease_held)
                for_good, routable = unadmittable_now() if asked else (None, True)
                if job["wait_reason"] == "capacity" and routable and not for_good:
                    # C-6.9: a lease wait, those kept for an older job too, is no slot wait.
                    waiting_for = (frozenset([*(known["hold"].get("leases") or ()), *(known["hold"].get("queued") or ())])
                                   if lease_held else frozenset())
                    wait(tier, klass, (job["job_id"], models, lanes, waiting_for))
                    if probing:
                        probe_line.append((job["job_id"], probing["model"],
                                           self._probe_reach(roster, job, probing["model"], lanes, extra_exclusions)))
                if lease_held and routable and not for_good:
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
            if (kind == "detached" and not previous and job["sandbox"] == "workspace-write"
                    and not job["in_place"] and not job.get("worktree") and job.get("push_branch") is None):
                # C-6.20: a job no attempt has used, which every lane refuses now for a reason no
                # slot ends (a hold, a provider limit, a lost login: any closure in force counts,
                # however soon it ends), could only wait in the tree a look would cut for it. So no
                # tree is cut and no git runs, and one an earlier look cut goes back
                # (`_sweep_unrun_worktrees`, `unprepared`). It waits on its capacity clock (C-6.10)
                # with no decision, as a job held before its look does, holds back whom it held
                # back before (C-6.9), and its due look asks again.
                refused = unadmittable_now(far_s=0)[0]          # None too for a route not evaluated
                if refused:
                    for_good = unadmittable_now()[0]
                    hold = {"reason": scheduler.refusal_label(refused), "refused_now": refused, "unprepared": True,
                            **({"for_good": for_good} if for_good else {})}
                    # No lane would take it: C-23.16's demand, as the look it skipped would have found.
                    rechecks = self._capacity_wait(job["job_id"], "unprepared:" + "|".join(refused), hold,
                                                   lane_wait=True)
                    next_check = after(scheduler.capacity_recheck_delay(rechecks))
                    with self.store.transaction("job.unprepared_wait", job_id=job["job_id"]) as tx:
                        taken = self._task_held_at_write(tx, job, holds)            # C-11.10 (R3-1)
                        if taken is None:
                            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                                       "WHERE job_id=? AND state IN ('queued','waiting') "
                                       "AND cancel_requested_at IS NULL", (next_check, job["job_id"]))
                    if taken is not None:
                        continue
                    if not for_good:
                        wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
                    holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                    continue
            needs = self._needs(job)
            run_folder = folders.canonical(job.get("worktree") or job["workdir"]) if job["kind"] == "turn" else None
            detached_answers = {}                  # filesystem answers for this admission look only
            try:
                try:
                    # C-6.5, C-8.4: submit could not spell this job's folder in full (`unspelled`),
                    # so what it recorded may be below its checkout's top, or not the top its
                    # `core.worktree` names: found again as submit finds it, once, for the look
                    # below and for the reserving transaction alike (`_rediscovered`), then
                    # spelled below like any other.
                    derived = self._rediscovered(job)
                except (OSError, subprocess.SubprocessError, SalvageError):
                    # C-6.8's, as a workspace that cannot be prepared is. Failure to
                    # rediscover does not prove the recorded/current folders are clear:
                    # turns and detached jobs still wait on their retirement fences.
                    if job["kind"] == "turn" and self._fence_hold(job, self._recorded_folder(job), holds, waiters,
                                                                  tier, models, lanes, run_folder, needs=needs):
                        continue
                    if job["kind"] != "turn":
                        job_folders = self._detached_folders(job, checked=detached_answers)
                        fallback_folder, fallback_absent = None, None
                        if job["in_place"] and job["sandbox"] == "workspace-write":
                            fallback_name = self._recorded_folder(job)
                            fallback_folder, fallback_absent = (detached_answers.get(fallback_name)
                                                                or folders.present(fallback_name))
                        if fallback_folder and self._fence_hold(
                                job, fallback_folder, holds, waiters, tier, models, lanes,
                                absent=fallback_absent, write_target=fallback_folder,
                                priority_waiters=priority_waiters if klass == "priority" else None, needs=needs):
                            continue
                        if any(self._fence_hold(
                                job, folder, holds, waiters, tier, models, lanes, fold=fold,
                                absent=source_absent, write_target=fallback_folder,
                                priority_waiters=priority_waiters if klass == "priority" else None, needs=needs)
                               for folder, fold, source_absent in job_folders):
                            continue
                    raise
                # `_turn_folder`, from the one answer above.
                if job["kind"] == "turn" and self._fence_hold(job, derived or self._recorded_folder(job), holds,
                                                              waiters, tier, models, lanes, run_folder, needs=needs):
                    # C-8.4: retention is retiring the tree the turn's folder is in; no
                    # workspace is prepared for a turn that cannot start, and no git runs
                    # there but `_rediscovered`'s read (the transaction below still reads
                    # the fence, and decides).
                    continue
                # An in-place detached writer's row subject may be an external
                # Git top rediscovered above, or an absent alias of a quarantined
                # tree. Spell it once, off the store lock, and carry both answers
                # to the early look and reservation. Source/legacy folds stay separate.
                row_folder, row_absent = None, None
                if job["kind"] != "turn" and job["in_place"] and job["sandbox"] == "workspace-write":
                    row_name = derived or self._write_target(job, job["workdir"])
                    row_folder, row_absent = folders.present(row_name)
                    detached_answers[row_name] = (row_folder, row_absent)
                    detached_answers[row_folder] = (row_folder, row_absent)
                job_folders = self._detached_folders(job, checked=detached_answers) if job["kind"] != "turn" else []
                if row_folder and self._fence_hold(
                        job, row_folder, holds, waiters, tier, models, lanes,
                        absent=row_absent, write_target=row_folder,
                        priority_waiters=priority_waiters if klass == "priority" else None, needs=needs):
                    continue
                if any(self._fence_hold(job, folder, holds, waiters, tier, models, lanes, fold=fold,
                                        absent=source_absent, write_target=row_folder,
                                        priority_waiters=priority_waiters if klass == "priority" else None, needs=needs)
                       for folder, fold, source_absent in job_folders):
                    # C-8.4: retention is retiring the tree the job's folder is in, so no
                    # git runs there for a job that cannot start: not its start snapshot,
                    # nor the `git worktree add` that would register a writer's worktree
                    # inside that tree, in a repository nested there (N4). The transaction
                    # below still reads the fence, and decides.
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
                                           fix="resubmit the job to rebuild its checkout", reason="push-workspace-gone")
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
                                           fix="resubmit the resume from the original job", reason="resume-identity-missing")
                if native_session and job["kind"] in ("resume", "revive"):
                    # C-26.13: the session became a conversation's after this job
                    # was submitted (a person opened it in the app). Refused, not
                    # held: waiting on the `native:` lease would only take turns
                    # with the conversation.
                    self._refuse_conversation_session(native_session, job["kind"])
            except AdapterError as exc:
                self._fail_queued(job, self._refused_job(job["job_id"], exc), rc=exc.code)
                continue
            except (OSError, subprocess.SubprocessError, SalvageError) as exc:
                if self._workspace_failed(job, exc, holds):
                    continue                                        # C-11.10: held since the door's look
                self._capacity_waits.pop(job["job_id"], None)      # C-6.10: the wait is C-6.8's now
                holds[job["job_id"]] = {"reason": "workspace", **self._hold_error(exc)}
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
            # writer's `worktree:`), spelled off the store lock. Detached in-place
            # writers reuse the spelling/absence answer from their early look. Submit spelled
            # it, but a name it could not look up kept the case it was given: a native
            # session's cwd typed `…/jOB/vendor/lib`, spelled while retention had the tree
            # `Job` in quarantine, keyed the row on `jOB`, which no check on the tree
            # matched once the turn's job had ended (review of 8a112986, finding 1). A
            # folder spelled in full here is the one spelling of a folder that is there;
            # one that is not reserves no row (`absent`, below).
            named, absent = read_folder or (write_target if job["kind"] == "turn" or job["in_place"] else None), None
            if named:
                spelled, absent = ((row_folder, row_absent) if row_folder is not None
                                   else folders.present(named))
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
            if job["kind"] != "turn" and (job["in_place"] or job["sandbox"] == "read-only"):
                refreshed = self._detached_folders(job, run_folder=run_folder, checked=detached_answers)
                job_folders.extend(each for each in refreshed if each not in job_folders)
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
            # Keep the steers read for FIFO above, with the job's current workspace
            # fields. Each evaluation checks their expiry against its own clock.
            decision_job = {**job, "capacity_steer": routed_job.get("capacity_steer", {})}
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
                    models = scheduler.demand_models(self.policy, decision_job)
                    lanes = scheduler.demand_lanes(roster, job, self.policy)
                    behind = None if defer_fifo else ahead(models, lanes)
                    # C-6.12: exclusions that cannot be merged are its route's problem, so such a
                    # job is not held here: the route's preparation below settles it as a `route`
                    # wait, which holds nobody (review of #201 r2).
                    for_good, routable = unadmittable_now() if behind else (None, True)
                    if behind and routable:
                        # C-6.10: held after a look, so on a clock like every other
                        # such hold; until it is due the job is held at the top of
                        # the pass with its own demand, with no git and no scoring.
                        # C-6.12: its demand is its own on this pass and while that clock runs, "so it
                        # neither passes an older job it competes with nor is passed by a younger one":
                        # a waiter with that demand now, as on the passes before its clock is due
                        # (above), unless no lane can admit it as submitted (C-11.8).
                        hold = {"reason": "behind-older-job", "behind": behind, "tier": tier,
                                **({"for_good": for_good} if for_good else {})}
                        rechecks = self._capacity_wait(job["job_id"], f"retry-let-go:behind:{behind}", hold)
                        next_check = after(scheduler.capacity_recheck_delay(rechecks))
                        with self.store.transaction("job.retry_let_go", job_id=job["job_id"]) as tx:
                            taken = self._task_held_at_write(tx, job, holds)        # C-11.10 (R3-1)
                            if taken is None:
                                tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                                           "WHERE job_id=? AND state IN ('queued','waiting') "
                                           "AND cancel_requested_at IS NULL", (next_check, job["job_id"]))
                        if taken is not None:
                            continue
                        if not for_good:
                            wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
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
                if current["wait_reason"] == "task-held":
                    # C-11.10 (R3-1): the probe's answer found the task held, and its transaction
                    # took the job (`_prepare_route`'s `job.probe_waiting`).
                    held_now = self._task_hold_for(current, self._task_holds_now())
                    holds[job["job_id"]] = task_holds.wait_hold(held_now) if held_now else {"reason": "task-held"}
                    continue
                # C-6.11: which probe, and whose turn it is when it is not this job's.
                hold = {"reason": "probe-pending", **(probe_note or {})}
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
                        taken = self._task_held_at_write(tx, job, holds)            # C-11.10 (R3-1)
                        if taken is None:
                            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                                       "WHERE job_id=? AND state IN ('queued','waiting') "
                                       "AND cancel_requested_at IS NULL", (next_check, job["job_id"]))
                    if taken is not None:
                        continue
                    hold["next_check_at"] = next_check
                elif clocked:
                    hold.setdefault("next_check_at", current["next_check_at"])
                    self._refresh_hold(job["job_id"], hold)
                wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
                holds[job["job_id"]] = hold
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
                                self._fail_queued(job, self._refused_job(job["job_id"], exc), rc=exc.code)
                                status = "settled"
                                break
                        if tx.execute("SELECT 1 FROM attempts WHERE job_id=? AND state IN "
                                      "('reserved','starting','running','finalizing','quarantined')",
                                      (job["job_id"],)).fetchone():
                            # Checked again here: one job, one attempt at a time, whichever pass got there.
                            holds[job["job_id"]] = {"reason": "attempt-live"}
                            status = "held"
                            break
                        task_hold = self._task_hold_for(job, self._task_holds_now(read))
                        if task_hold is not None:
                            # C-11.10, C-6.3: read again at this transaction's clock, so a hold
                            # committed after the door's read still stops the reservation.
                            self._task_held(job, task_hold, holds, tx=tx)
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
                        provider_hold = None
                        provider_at_limit = False
                        provider_kept = []
                        provider_behind = None
                        fifo_behind = None
                        provider = None
                        if decision.chosen_lane:
                            provider = self.policy["models"][decision.chosen_model]["provider"]
                        elif defer_fifo:
                            # A rejected single-provider route still has that one
                            # provider's FIFO. A mixed route with no selection has
                            # no provider peer to wait behind yet.
                            possible = scheduler.demand_providers(self.policy, models, lanes, roster)
                            if len(possible) == 1:
                                provider = next(iter(possible))
                        if provider is not None:
                            if defer_fifo:
                                selected_models = (frozenset(name for name in models
                                                   if self.policy["models"][name]["provider"] == provider)
                                                   if models is not None else frozenset(
                                                       name for name, model in self.policy["models"].items()
                                                       if model["provider"] == provider))
                                fifo_behind = ahead(selected_models, lanes, chosen=decision.chosen_model)
                            elif steered_demands:
                                fifo_behind = ahead(models, lanes, chosen=decision.chosen_model)
                            provider_limit = scheduler.provider_cap(self.policy, provider)
                            if provider_limit is not None:
                                # C-6.4: both pools share this count. Check inside the
                                # reserving transaction, before taking ANY lease.
                                provider_count = tx.execute(
                                    "SELECT count(*) FROM attempts a JOIN lanes l USING(lane_id) "
                                    "WHERE a.state IN ('reserved','starting','running','finalizing') "
                                    "AND l.provider=?", (provider,)).fetchone()[0]
                                # Separate predicates retain the live/quarantine
                                # partial indexes instead of scanning history.
                                provider_count += sum(scheduler.uncontained_quarantine(dict(row)) for row in tx.execute(
                                    "SELECT a.state,a.quarantine_reason FROM attempts a JOIN lanes l USING(lane_id) "
                                    "WHERE a.state='quarantined' AND l.provider=?", (provider,)))
                                provider_kept = [waiter for waiter in kept if provider in
                                                 scheduler.demand_providers(self.policy, waiter[1], waiter[2], roster)]
                                provider_behind = next((waiter[0] for waiter in provider_kept if not waiter[3]), None)
                                provider_at_limit = provider_count >= provider_limit - bool(provider_kept)
                                provider_hold = {"provider": provider, "live": provider_count,
                                                 "max_active_attempts": provider_limit,
                                                 "key": f"providers.{provider}.max_active_attempts"}
                        if not decision.chosen_lane or at_limit or provider_at_limit or provider_behind or fifo_behind:
                            # C-11.8: a job every lane refuses for a reason no wait ends
                            # (the pinned lane turned so since this pass's check) waits
                            # for no slot, so it holds no later job back (C-6.9).
                            for_good = (None if decision.chosen_lane or rotation
                                        else scheduler.refused_for_good(self.policy, decision, decision_job,
                                                                        pin_view()["lanes"]))
                            if (not decision.chosen_lane and not rotation
                                    and decision_job.get("capacity_steer")
                                    and (not decision.evaluations or for_good)):
                                # C-6.9, C-9.5: a steer ends, so standing refusal must
                                # cover the whole chain, as while the job's clock runs.
                                # A fully steered chain has no refusal evaluations.
                                for_good = unadmittable_now()[0]
                            if not for_good:
                                wait(tier, klass, (job["job_id"], models, lanes, frozenset()))
                            # C-6.10: a wait that reaches the verdict it reached last time
                            # is rechecked later each time and adds no decision row. On
                            # 2026-09-20 three such jobs were each re-evaluated every
                            # second for hours: 2.7 rows of 22 KB a second, 681 MB of a
                            # 709 MB store, and a daemon at a full core doing it.
                            if fifo_behind or provider_behind:
                                label = "behind-older-job"
                            elif not decision.chosen_lane:
                                label = scheduler.dominant_rejection(decision)
                            else:
                                # A lane would take it. Either the fleet is at its cap, or
                                # C-6.9 keeps the last slot for an older job of this tier.
                                label = ("behind-older-job" if fifo_behind or provider_behind else
                                         "provider-cap" if provider_at_limit and provider_hold["live"] >=
                                         provider_hold["max_active_attempts"] else
                                         "provider-slot-kept" if provider_at_limit else
                                         "fleet-full" if saturated[pool] else "slot-kept")
                            hold = {"reason": label, **({"for_good": for_good} if for_good else {}),
                                    **({"max_active_attempts": pool_cap} if label == "fleet-full" else {}),
                                    **({"kept_for": kept[0][0], "tier": tier, "live": live,
                                        "max_active_attempts": pool_cap} if label == "slot-kept" else {})}
                            if label == "provider-cap" and provider_hold is None:
                                provider_hold = next(row["provider_cap"] for evaluation in decision.evaluations
                                                     for row in evaluation["rejections"]
                                                     if row.get("provider_cap") and row["provider_cap"]["live"] >=
                                                     row["provider_cap"]["max_active_attempts"])
                            if label in ("provider-cap", "provider-slot-kept"):
                                hold.update(provider_hold)
                            if label == "provider-slot-kept":
                                hold.update(kept_for=provider_kept[0][0], tier=tier)
                            if fifo_behind or provider_behind:
                                hold.update(behind=fifo_behind or provider_behind, tier=tier)
                                if provider_behind:
                                    hold.update(provider_hold)
                            if kind == "turn" and not decision.chosen_lane:
                                # C-24.4, C-29.11: what each lane said, for the message's
                                # reason (`conversations.waits`), never just "capacity".
                                from .conversations import waits as turn_waits
                                hold["lanes"] = turn_waits.lane_summary(decision)
                            # The verdict alone keys the wait (C-6.10). Whether the pool or
                            # the provider was at its limit, and which older job this one is
                            # behind (C-6.4a, C-6.9), go in the hold, which every look
                            # replaces, so `why` reports them; as part of the key they
                            # recorded an unchanged verdict again on each fleet turnover.
                            rechecks = self._capacity_wait(job["job_id"], scheduler.verdict_signature(decision), hold,
                                                           lane_wait=not decision.chosen_lane)
                            waiting = scheduler.waiting_metadata(
                                decision, rechecks=rechecks, capacity_steer=decision_job.get("capacity_steer"))
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
                        if job_folders:
                            # C-8.4: nor does a detached job start while retention retires
                            # a tree one of its folders is in (`_detached_folders`): the one
                            # it takes no lease on (read-only, or writing in a worktree cut
                            # from the repository there), and where its workdir resolves now.
                            # An in-place writer's own target is its own key, contested
                            # above. `_fence_hold` found none before its workspace was
                            # prepared; this transaction's read decides.
                            own = folders.exclusive_key(write_target) if job["sandbox"] == "workspace-write" \
                                and job["in_place"] else None
                            for folder, fold, source_absent in job_folders:
                                fenced = (folders.retiring_folded(read, folder) if fold else
                                          folders.retiring(read, folder, folded=source_absent is not None))
                                blockers.extend(key for key in fenced if key != own)
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
                        # C-8.4: nor does any job start while retention retires a tree
                        # one of the other places it needs is in (its review root, its
                        # `-o` path, the git storage its checkout uses: `_needs`). A
                        # detached writer's own `worktree:` key is a lease it takes,
                        # contested above, not a key it only needs free.
                        own = (folders.exclusive_key(write_target)
                               if job["sandbox"] == "workspace-write" and job["kind"] != "turn" else None)
                        blockers.extend(key for _, _, keys in dependencies.fences(read, needs) for key in keys
                                        if key != own)
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
                        blocked_by = {key: r[0] for key in dict.fromkeys(blockers)
                                      if (r := tx.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone())
                                      and r[0] != job["job_id"]}
                        blocked = list(blocked_by)
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
                            # `retiring` says which keys are retention's fences, so `why`
                            # says retention is retiring that tree, not that a job holds
                            # it; a detached job's hold then names its folder too (C-8.4).
                            holders = {**{key: rows[0][1] for key, rows in current.items()}, **blocked_by}
                            retiring = [key for key in dict.fromkeys(contested + blocked)
                                        if key.startswith(folders.EXCLUSIVE) and str(holders[key]).startswith(folders.RETENTION)]
                            named = held_folder if job["kind"] == "turn" else None
                            if retiring and job_folders:
                                # The folder the fence is on or above: one the job works in,
                                # else (a writer that is not in place) the worktree cut for it.
                                named = next((folder for folder, fold, source_absent in job_folders
                                              if any(folders.fences(key, folder, fold=fold)
                                                     if fold or source_absent is None else
                                                     folders.within(folders.fold(folder),
                                                                    folders.fold(key[len(folders.EXCLUSIVE):]))
                                                     for key in retiring)),
                                             write_target)
                            role = None
                            if retiring and not any(folders.within(dependencies.key(named or ""),
                                                                   dependencies.key(key[len(folders.EXCLUSIVE):]))
                                                    for key in retiring):
                                # Else the first dependency place a fence is on or above.
                                # Use the blocking query's key, including canonical caseless
                                # Unicode aliases; the legacy folder fold is different (D1).
                                role, named = next(((each, path) for each, path in needs if any(
                                    folders.within(dependencies.key(path),
                                                   dependencies.key(key[len(folders.EXCLUSIVE):])) for key in retiring)),
                                    (None, named))
                            self._seen_held(kind, frozenset((key, str(holders[key])) for key in retiring))
                            hold = {"reason": "lease-held", "leases": contested + blocked,
                                    **({"retiring": retiring} if retiring else {}),
                                    **({"folder": named} if named else {}),
                                    **({"needs": role} if role else {}),
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
                        reserved_at = None
                        if job["kind"] != "turn" and ramp_config["enabled"]:
                            # Workspace preparation can move the clock backwards
                            # after the pass census discarded an expired start.
                            # Read committed rows at the timestamp we will insert,
                            # under the same write lock, before taking any lease.
                            reserved_at = utcnow()
                            self._begin_ramp_pass(now=reserved_at, read=read)
                            hold = self._ramp.hold(klass, reserved_at)
                            if hold:
                                ramp_hold = holds[job["job_id"]] = hold
                                status = "held"
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
                        if reserved_at is None:
                            reserved_at = utcnow()
                        if job["kind"] != "turn" and self._disk.settings["enabled"]:
                            evidence["disk_reservation"] = self._disk.evidence()
                        # C-12.4: capability and publish denial are the launch's, recorded
                        # when it is built (`_record_launch`); a reservation that never
                        # launches claims neither (#221 review r1b, P3-2).
                        tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,baseline_tree,evidence_json,reserved_at) VALUES(?,?,?,?,?,'reserved',?,?,?)",
                                   (aid, job["job_id"], seq, lane_id, self.policy["models"][decision.chosen_model]["id"],
                                    baseline, json.dumps(evidence), reserved_at))
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
                moved_for_good = None if rotation else scheduler.refused_for_good(
                    self.policy, decision, decision_job, pin_view()["lanes"])
                if (not rotation and decision_job.get("capacity_steer")
                        and (not decision.evaluations or moved_for_good)):
                    moved_for_good = unadmittable_now()[0]      # C-6.9, C-9.5: whole chain, as above
                if not moved_for_good:
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
            if job["kind"] != "turn" and ramp_config["enabled"]:
                # Only after the reservation transaction commits: failed lease
                # and route checks consume no start, successes pace this pass.
                self._ramp.reserve(aid, reserved_at)
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
