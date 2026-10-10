When disk admission or the machine guard reopened, detached admission could place the entire pending queue before machine load caught up. Add opt-in `admission.ramp` (off by default, 12 starts per 60 seconds), counting durable detached reservations at startup and once per detached pass, plus commits in that pass. Holds take no lease or capacity backoff, preserve admission order through the rest of the pass, and reopen through the existing periodic admission control loop. All accepted non-turn kinds (`dispatch`, `resume`, `revive`, `gate-review`, `handoff`) share the window. Priority work bypasses holds but consumes starts; turns and probes are exempt. Jobs with recent detached rows are pinned through retention deletion so restart reconstruction remains valid. Status and why show usage and the next slot; disabling or removing the policy immediately hides cached status usage.

The fake incident replay changes 56 starts in one pass to cumulative 12/24/36/48/56 at 0/60/120/180/240 seconds on 10 lanes after disk rises from 41.5 to 135.9 GB.

Validation invariants exercised by the tests below:

- R1: generated forward-moving clocks keep ordinary starts within the integral rolling-window allowance; 12/60 holds job 13. Priority starts count too. A separate unit case covers rollback within one pass.
- R2: finite free queues drain within ceil(n/rate) minutes plus one pass from an empty window; 56 at 12/minute finish at t=240 seconds. Control ticks prove wakeup without events.
- R3: attended work and probes never read, wait on or consume the ramp; one turn plus one dispatch uses one start.
- R4: an unchanged eligible queue retains priority/tier/FIFO order, including expiry during a pass.
- R5: restart with two completed retry starts at rate 2 grants zero fresh slots at t=2, one at t=60; retention preserves that ledger and rechecks policy enabled during archive inside deletion. All five detached kinds reconstruct and consume starts; a real gate-review scenario shares the dispatch allowance across restart.
- R6: disabled/absent policy produces identical normalized serialized decisions and placements to frozen pre-change admission across generated queues, clocks, restarts and gate reopenings, retaining all compared fields. Immediate status omits cached full-gate usage after policy disable/removal without counting or reading starts.

The hub's literal comparison allows ceiling rounding for fractional allowances (12/minute with window 1 permits one start against allowance 0.2), so unrounded R1 is inconsistent for those valid policies. Prefilled windows longer than 60 seconds and continuing priority arrivals require the liveness qualifications stated in C-6.18. The requested 12/60 configuration meets both bounds.

Initial required-suite run: 672 passed, 2 failed because fake reservation and routing clocks diverged. The fixture now aligns those clocks and also keeps generated capacity readings fresh; strict differential fields are unchanged. Final rerun pending.

TEST_RESULTS_PENDING

MUTATION_RESULTS_PENDING

No machine-guard thresholds or disk defaults changed. No push; prepared PR text only.
