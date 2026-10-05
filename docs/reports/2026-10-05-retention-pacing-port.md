# Retention pacing port for Subfleet 2.1.11

Base: `08d6a09a0d566d2b94e14a109259f23001d95576` (#129, including #76).
Source: #109, `d5e6a7a6`, and `review-109-r1.md` §5 / `review-109-r2.md`.
All source work, synthetic measurements and foreground validation use the assigned
workspace or disposable temporary directories. No live Subfleet state is used.

## Before editing: #76 against the six §5 rules

The locations below refer to the **base commit**, before this port.

| §5 rule | #76 behavior, with base file:line | Gap |
|---|---|---|
| 1. Outcome order; cancellation / empty deadline / advancing deadline / completion; arm last | `subfleet/daemon.py:3400` marks cancellation but returns without setting `_last_maintenance`; `:3404` raises every deadline; `:3421` and `:3431` mark before arming successful catch-up/completion | Cancellation must rearm; empty deadlines must warn once with job count and rearm without raising; advancing deadlines must warn and raise. Preserve mark-before-arm. |
| 2. Advancement counts as progress; only no advancement waits an hour; update C-8.4 | `subfleet/retention.py:468` counts measured sizes, archive work, starts, deferrals and reclaims in `acted`; `:610` returns `progressed`; `:384` and `:396` interrupt returns lose it. `subfleet/daemon.py:3408` resets advancing catch-up to 5 s but doubles no-progress waits from 10 s toward an hour | Keep progress in interrupted results, including cached size advancement. No-progress catch-up must wait the hour immediately. C-8.4 (`docs/acceptance-contract.md:213`) still describes doubled waits. |
| 3. Notice prune first, once per pass, never fails the pass | `subfleet/daemon.py:3405` calls it after maintenance; `:3436` deletes delivered service notices in one transaction | Interrupted/raising passes miss the notice prune; its exception fails retention. Move it first and log its failure. |
| 4. Both budgets, turn keep time, pins on every batch; pins re-asked at deletion | `subfleet/daemon.py:3392` passes both budgets, `turn_keep_s`, conversation `pins` and remote-less history limit on every call. `subfleet/retention.py:499` / `:513` re-ask pins; `subfleet/retention_archive.py:731` calls `Context.pinned` inside the delete transaction | No argument or transaction gap; preserve and extend multi-batch wiring coverage. |
| 5. Leftover `retention:<job>` lease on half-removed job goes first | `subfleet/retention.py:514` recovers journals first; `:626` releases journal-less retention leases, then `:515` selects jobs by ordinary age | Journal recovery already goes first. Remember journal-less leftover jobs and select them before ordinary candidates, with archive and pin checks intact. |
| 6. Port the tests and measurement tool | `tests/fake/test_native_maintenance_startup.py:58` covers generic deadline retries, `:103` wiring, `:128` doubled catch-up waits; archive tests cover resumption. #109's two-clock model, real deadline passes and notice cases are absent; measurement tool absent | Port all #109 cases onto journal/batch boundaries, retain #76 coverage, and measure the full live-shaped counts in the foreground. |

## Required invariants

- Retention never deletes a byte its archive does not hold (including verified
  remote blobs and #76's explicit regenerable-file proofs).
- A pass that advances is retried promptly while work remains.
- A pass that does not advance waits the hour.
- The notice prune runs once per pass.

## Implementation and validation

Pending at the mapping commit; completed results will be recorded here.
