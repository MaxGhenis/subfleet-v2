# Retention dependency pins — fix round r8

Started 2026-10-31 07:05 UTC at prerequisite `373a7f5bf588`.

The requested macOS scratch root was denied; scratch is `/private/tmp/sf-fix272r8`.
Shared git metadata rejected `index.lock`; commits use job-local `.git-local`
metadata and delivery will be a verified bundle with `373a7f5bf588` as prerequisite.

## Repair and first evidence

Discovery walks path components, retains physical symlink entries and successful
pointer-file reads, and scans one level of actual storage directories. It skips
locations covered by recorded places and entries directly inside the checkout or
its ancestors. Inferred parent metadata remains a hint, without new scans.

Both r8 cases failed at the owner-retention assertion with `--runxfail` on the
prerequisite. The original pointer property also failed under the linked
`TMPDIR`: its oversized explicit examples compared physical evidence against a
link spelling. See `2026-10-31-retention-pins-fix-r8-data/red-*.{json,log}`.

The initial linked green hold passes the r8 regressions and untouched dependency
unit tests. The nested git-new-workdir control is byte-for-byte equal to base
(`routine-first-comparison.json`). Remaining required evidence is in progress.

## Consumer audit (decision 5)

`needed` returns strings. `fenced` uses them directly (legacy discovery runs
outside transactions). `fences`, `live`, `users`, and `dependents` use normalized,
folded string paths and ancestor comparisons. `legacy` spells the workdir, not
recorded Git paths. Daemon `_needs`, `_early_fences`, reservation near 8540 and
hold reporting near 8601 pass those strings through; the display key named
`folder` can hold a file path. Retention selection and final checks in
`retention.py`, and archive job-folder fencing and commit in
`retention_archive.py`, call these string consumers. No reader requires a Git
place to be a folder or spells it again. Existing output paths were already files.
