# Retention dependency pins — fix round r12

Work in progress. Input head `92a8635a05d6`; started 2026-10-31 20:10:51 UTC.

Darwin scratch creation was denied; using `/private/tmp/sf-fix272r12`. Durable evidence is in the adjacent data folder. Holds stop starting at 23:10:51 UTC.

Red at input head: 2 passed / 2 failed; both current-reader routes retire owner and change the identical read from 0 to 128. Both baseline routes retain owner and read 0. Private Git metadata is required because the shared ref lock was denied.

Decision 4 counterexample: the four requested long-cycle rows contain a shorter ELOOP path (701 or 830 bytes), which decision 1 must omit. Exact baseline equality at 2/4 and 3/3 conflicts with the round invariant. Product remains unchanged. The smallest test adjustment expects the baseline record minus that ELOOP path, at 1/4 and 2/3, while preserving every ENAMETOOLONG string and ordered incomplete entry. See data/decision-4-counterexample.json. Remaining holds were paused before making this adjustment.

The four corrected long-cycle rows pass in the focused locked hold. The full required hold list is resumed, reusing only completed unchanged-file receipts.
