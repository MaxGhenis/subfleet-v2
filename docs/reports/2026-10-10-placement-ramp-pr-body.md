When disk admission or the machine guard reopened, detached admission could place the entire pending queue before machine load caught up. Add opt-in `admission.ramp` (off by default, 12 starts per 60 seconds), counting durable detached reservations at startup and once per detached pass, plus commits in that pass. Holds take no lease or capacity backoff, preserve admission order through the rest of the pass, and reopen through the existing periodic admission control loop. All accepted non-turn kinds (`dispatch`, `resume`, `revive`, `gate-review`, `handoff`) share the window. Priority work bypasses holds but consumes starts; turns and probes are exempt. Jobs with recent detached rows are pinned through retention deletion so restart reconstruction remains valid. Status and why show usage and the next slot; disabling or removing the policy immediately hides cached status usage.

The fake incident replay changes 56 starts in one pass to cumulative 12/24/36/48/56 at 0/60/120/180/240 seconds on 10 lanes after disk rises from 41.5 to 135.9 GB.

Validation invariants exercised by the tests below:

- R1: generated forward-moving clocks keep ordinary starts within the rolling-window allowance K = per_minute × window_s / 60; 12/60 holds job 13. Priority starts count too. The loader accepts only whole K, and every accepted policy admits exactly K starts into an empty window. A separate unit case covers rollback within one pass.
- R2: finite free queues drain within ceil(n/rate) minutes plus one pass from an empty window; 56 at 12/minute finish at t=240 seconds. From a window already holding up to K starts they finish by ceil(n/K) × window_s. Control ticks prove wakeup without events.
- R3: attended work and probes never read, wait on or consume the ramp; one turn plus one dispatch uses one start.
- R4: an unchanged eligible queue retains priority/tier/FIFO order, including expiry during a pass.
- R5: restart with two completed retry starts at rate 2 grants zero fresh slots at t=2, one at t=60; retention preserves that ledger and rechecks policy enabled during archive inside deletion. All five detached kinds reconstruct and consume starts; a real gate-review scenario shares the dispatch allowance across restart.
- R6: disabled/absent policy produces identical normalized serialized holds, job states, placements and decisions, policy hashes included, to frozen pre-change admission across generated queues, clocks, restarts and gate reopenings, retaining all compared fields. Each new decision names the SHA-256 of the loaded policy file. A ramp-absent policy file hashes its exact bytes, as release/217's loader does. The bundled policy keeps its pre-change bytes and raw hash for fresh daemons; disabled ramp defaults come from the loader. Immediate status omits cached full-gate usage after policy disable/removal without counting or reading starts.

The literal comparison would admit a fractional allowance's ceiling (12/minute over a 1-second window: one start a second, 60 a minute), so the loader refuses any rate and window whose product is not a multiple of 60, enabled or not, naming `admission.ramp.window_s`. Release/217 rejects `admission.ramp` outright, so no existing policy is affected. Windows other than 60 seconds that start full, and continuing priority arrivals, carry the liveness qualifications stated in C-6.18. The requested 12/60 configuration meets both bounds.

The generated tests align routing and reservation clocks and keep capacity readings fresh. The differential compares every selected field with frozen pre-change admission, normalizes independently generated job IDs longest-first, then sorts map keys again after aliases; temporary workspace roots are normalized too. A minimized explicit case covers overlapping IDs, disk reopening and restart. Its last failure on `b6fdd0b28` was a fixture artefact: each run's restart wrote the loaded policy back with the loader's `_policy_path`, its own temporary root, so the two files hashed differently. Release/217's loader gives those two files different hashes too. Restarts now write the policy without loader metadata.

TEST_RESULTS_PENDING

MUTATION_RESULTS_PENDING

No machine-guard thresholds or disk defaults changed. No push; prepared PR text only.
