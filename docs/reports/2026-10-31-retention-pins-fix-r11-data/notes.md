# Fix round r11 working notes

Started 2026-10-31 18:02:38 UTC at ced02912bea6. Hold-start cutoff: 22:02:38 UTC.
DARWIN_USER_TEMP_DIR scratch creation was denied; using /private/tmp/sf-fix272r11.
Shared git refs are read-only. Commits use worktree-local .git-local metadata, with a
read-only object alternate. Deliver a verified bundle with ced02912b as prerequisite.
No sub-agents, per the lane rules. No live stores, CLI, providers, fetch, merge, push or gh.

Red review r11: 2 baseline passes, 2 current failures at owner retention; both current
reads change 0 -> 128. See red-review-r11.json and .log.

New tests compare full ordered incomplete lists against both 475f63278 and ced02912b.
The stat table uses real stat for all six outcomes. Only ENOENT, ENOTDIR, EACCES and
ENAMETOOLONG require a controlled failed canonical equation. Success uses a real
case alias; ELOOP uses a suffix-growing symlink. Discovery outputs are injected
only for this isolated return-filter table, exercising both return sites.

Initial new-file red: 24 passed / 10 failed, including the eight expected keep-table
failures and the parent rename. The initial Unicode fixture introduced an owner-side
symlink and incorrectly expected three rather than four places at baseline. That
fixture was corrected to rename a Unicode source-directory component directly,
without an independently pinning symlink; red-rename-final records the corrected
input before repair. This was a test construction correction, not a product gap.
For the table's found route every row injects the final pre-filter spelling;
controlled_failed_equation identifies the four rows also controlling the equations.

Added two explicit original-input comparisons: restore each identical r11 fixture and replay the same boundary rename against baseline; assert complete record equality, then use that record for the original owner/read check. These add 2 normal and 2 linked passes to the initial 34-node module.
