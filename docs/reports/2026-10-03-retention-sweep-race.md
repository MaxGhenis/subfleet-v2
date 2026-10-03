# Retention sweep race: verification evidence

2026-10-03; baseline `43f09ea4`, saved continuation `461d4a1a`.

The real-Git reproduction moves the tree away during its gitfile lookup and
back either immediately after that lookup or before quarantine. Both cases
failed on an export of `43f09ea4`: the job and tree were removed, no bundle
held its detached HEAD or reflog-only commit, and the remaining admin directory
named a missing checkout. The untracked/ignored files were archived; the shared
stash survived. Both cases pass with the fix and retire safely on a quiet retry.

The design retains the registration chosen at `begin`, journals device/inode
identity and a digest of the exact gitfile bytes read, and checks the quarantined tree and admin
backlink after quarantine and at the end of the final check. Git's lock and
retention's quarantine fence further non-forced sweep moves. A vanished rename
also defers. When successive moves hide both gone-tree lookups, the persistent
admin id keeps the job; git moves change paths/backlinks but never that id.
This closes a second real-Git schedule discovered during this continuation.

Code: `subfleet/retention_archive.py:327` (one gitfile read), `:549` (identity),
`:715` (final check), `:831` (legacy committed recovery), `:891` (live claims),
`:1148` / `:1165` (gone-tree discovery); `subfleet/retention_git.py:272`
(persistent admin id). Design revision 6 and the missing #81 classification
narrative are committed.

The property generates move schedules at 16 boundaries and checks every
retained tree's HEAD/reflog, exact stash list, files and usable registration.
A removed tree's private commits must import from a verified bundle into a
fresh remote clone, and its untracked/ignored bytes must match the archive.
Both orphan directions are checked before/after sweep restoration, followed
by a successful quiet retry. Bounded phase slices passed 8 generated cases
(two seeds, four each) and all 15 explicit schedules (three slices of five).
Each bounded slice finished below ten minutes. An initial combined run also
passed (848.07 pytest seconds; 872.09 including the wrapper), but exceeded
its 550-second cap under load; it was awaited to exit
because required `ps` inspection was denied. No process was abandoned.

| Mutation | Observed result |
|---|---|
| Disable gitfile identity comparison | Both lookup-round-trip cases failed with missing private history/orphan assertions |
| Omit final identity check | Rewritten-gitfile case incorrectly pruned the job and failed |
| Make persistent admin-id lookup return none | Repeated-move case incorrectly pruned the job and failed |

All mutations ran in an isolated source copy and were restored with
`git checkout -- <file>`; SHA-256 comparison confirmed equality with production.

All **214 retention tests passed**: the 201 existing cases (198 deterministic
and three properties), plus 12 sweep/recovery regressions and the new schedule
property. Existing archive properties used their default 10 examples each;
the regenerable property used its default 8 plus explicit examples. The certifying bounded retention slices all finished below ten minutes;
the longest existing-case wrapper took 566.02 seconds. The gitfile mutation
check took 576.21 pytest seconds (590.36 including the wrapper). A setup attempt lacked its basetemp parent, and one survey
expectation used a path containing `tmp` that intentionally means scratch;
corrected isolated system-temp reruns passed.

The required foreground `uv run pytest -q` is still running. Its 7,200-second
wrapper threshold elapsed; the wrapper awaited its owned child without a signal
because `ps` inspection is denied. Final counts will be recorded after it exits.

Three older note-2 tests deliberately bypass earlier discovery to reach their
later injected moves. Their hooks now bypass the new persistent-id guard too;
the affected 20-case cohort passes. No tests outside retention were edited.
Two representative unrelated failures reproduce on `43f09ea4`: admission
priority cannot execute `/bin/ps`, and daemon-stack setup cannot read macOS's
boot identity. The full run collected the old three hooks before they changed;
those results must be distinguished from their corrected follow-up cohort.

An already-committed older archive that missed its live registration preserves
its remaining quarantine, admin and journal for recovery. Its previously
removed database rows cannot be rolled back automatically. A valid older
committed archive completes normally. A stale/conflicting admin id is also
kept conservatively rather than deleting private history on an absence guess.

The sandbox denies shared Git metadata writes. New commits use a workspace-local
Git directory on `fix/retention-sweep-race-cont` with the required coauthor line.
The delivered bundle requires exactly `43f09ea4`; its branch head is printed by
`git bundle list-heads`. Verification includes import into a fresh Git directory.

Preflight native process inspection found all 80 earlier wrapper/child PIDs
absent. Only the owned full-suite chain and its read-only monitor remain active.
The final descendant audit is pending that run's exit.
