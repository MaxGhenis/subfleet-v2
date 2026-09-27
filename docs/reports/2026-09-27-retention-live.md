# Read-only retention audit, 2026-09-27

The database snapshot began at 21:15 UTC; bounded filesystem and Git reads continued through 21:31 UTC. The live store had **618 jobs (602 detached, 16 turns)**. The directory snapshot contained **162 worktrees**, versus the incident report's earlier 155. Per-directory `du -sk` accounted for **57.947 GiB** (60,762,112 KiB), with **1 unresolved size(s)**. Counts and sizes below describe this newer snapshot, not an exact reconstruction of the earlier 57 GB.

The audit opened the databases with `sqlite3 -readonly 'file:/Users/maxghenis/.subfleet/state.sqlite3?mode=ro'` (and the conversation database equivalent). It read the retention policy, rows needed by `_pins`, conversation message/block evidence, recorded retention errors, and `daemon.log`. It did not instantiate the live `Store`, invoke maintenance/the installed daemon, modify refs, or remove live files. Artifacts were written only in this assigned workspace.

The first per-worktree `du -sk` pass used six readers and a 90-second subprocess timeout. Seven trees timed out; they were retried with three readers and a 180-second timeout. Those completed retries took 38–167 seconds, demonstrating that even one worktree can exceed the daemon's entire 60-second maintenance deadline. Unresolved paths: 20260927-043608-pr1011-puf982-v2. `du` counts allocated blocks, whereas retention's byte budget counts file lengths; APFS clones, sparse files, hard links and concurrent changes mean these are estimates of physical space recoverable.

## Worktree breakdown

The first table is additive within each grouping; unresolved sizes contribute zero to the measured lower bound, not an assumed zero size.

| Category | Worktrees | Measured GiB | Unknown sizes |
| --- | ---: | ---: | ---: |
| All on disk | 162 | 57.947 | 1 |
| Terminal jobs | 147 | 55.161 | 1 |
| Live jobs | 9 | 1.928 | 0 |
| Unowned/orphan paths | 6 | 0.859 | 0 |
| Detached jobs | 156 | 57.089 | 1 |
| Turn jobs | 0 | 0.000 | 0 |
| Pinned by current policy | 146 | 54.737 | 1 |
| Unpinned by current policy | 10 | 2.352 | 0 |
| Pinned after salvage proof | 49 | 21.993 | 1 |
| Unpinned after salvage proof | 107 | 35.096 | 0 |

One live job acquired its worktree between the initial database query and directory enumeration; a later read-only query verified the owner and it is included as live. Of the six unowned paths, five have no job row and one (`20260927-054501-ccr3-review-opus`) has a cancelled row with `worktree=NULL`. Retention cannot claim ownership of these paths merely from their names, so the estimate excludes their 0.859 GiB.

Pins overlap: do not add the following rows.

| Pin reason | Worktrees | Measured GiB |
| --- | ---: | ---: |
| Salvage, because the daemon supplies no proof | 113 | 41.064 |
| Unread notice addressed to a session | 32 | 17.545 |
| Quarantined attempt | 15 | 4.330 |
| Worktree lease | 23 | 5.779 |
| Live job | 9 | 1.928 |
| Turn keep window | 0 | 0.000 |
| Conversation message/block evidence | 0 | 0.000 |
| Gate/merge evidence or gate-review job | 0 | 0.000 |

The 16 turn rows remain inside their 14-day keep window but own no measured job worktrees. Gate-review rows (112), parent pins (4), and remaining conversation evidence similarly account for no allocated worktree bytes here. In-memory runner objects cannot be inspected through a read-only store; persisted live attempts and turn keep windows provide the observable protection. Measurements and pins are a changing, non-atomic snapshot.

## Salvage evidence and decision

Of **220 salvage artifacts**, **196** were proved to hold the recorded commit in a shared Git common directory outside the paths retention deletes. Each resolved commit matched the artifact's `sha256`, which `_salvage` records as SHA-256 of the commit ID. The other **24** could not be proved because their recorded source paths/worktrees were missing; their job records remain pinned. Every one of the **113 salvage-bearing worktrees still on disk** had a valid proof.

A shared `refs/subfleet-salvage/...` ref itself is sufficient. It and its objects survive `git worktree remove`; requiring an additional branch would retain nearly all of these trees. Of 178 bounded `for-each-ref --contains` queries that completed, 177 found only that salvage ref. One commit also had other refs; 18 ancestry queries timed out despite their own shared refs resolving successfully.

Concrete examples:

- `20260919-122126-oh-deck`: commit `6b930e4a23254ca3078e0451335474c7d96f9dd2` is held by `refs/subfleet-salvage/detached-20260919T162946Z-a1`; its common directory is `/Users/maxghenis/PolicyEngine/policyengine-slides/.git`, outside the allocated job worktree.
- `20260925-192153-descriptor-followups-and-app-busy`: commit `55a3461955a4368600f05cd714425f751403a13e` is held by its salvage ref, `refs/heads/release/217`, and other named refs.

The proof never treats a detached HEAD, a reflog, an unreferenced object, or an inaccessible repository as preservation. Dirty worktree deletion still requires the existing exact-tree salvage check.

## Expected reclaim and remaining errors

The fix makes **107 worktrees / 35.096 measured GiB** eligible for deletion attempts, versus 2.352 GiB under the current salvage policy. Historical `retention.worktree_error` events identify `20260926-094224-mc1034-review2` as dirty with no recorded salvage snapshot (**0.599 GiB**); a read-only status attempt itself exceeded 20 seconds, so the audit does not claim that error has cleared. Excluding this known failure leaves **about 34.496 GiB in reclaim candidates**, with every eligible candidate successfully sized. Actual reclamation remains subject to fresh pins, successful filesystem removal, the dirty-tree check and filesystem sharing. Job-directory bytes are additional and were not sized for this worktree-only estimate.

The other historical failure, `20260925-130918-ar-9612-review`, referred to Git worktree inspection/removal; its allocated path is now absent and contributes no measured worktree bytes. Failed deletion must keep its row and audit event so later passes can retry.

Even after these eligible candidates are removed, **21.993 measured GiB** remains pinned by legitimate notices/live/quarantined/lease evidence, exceeding the detached 2 GiB budget. The attainable outcome for this snapshot is therefore that all remaining jobs are protected, rather than forcing the pool below budget.

The live volume had changed independently since the incident report: `df -k` reported 96% used and 161,568,644 KiB available (154.1 GiB) during this audit. The current daemon log contained 188 `worker retention failed: TimeoutError` lines; those lines carry no timestamps, so the audit does not independently assign all 188 to today.

Per-path measurements and salvage evidence are retained in [the JSON audit](2026-09-27-retention-live.json); aggregate figures are in [the summary JSON](2026-09-27-retention-live-summary.json).
