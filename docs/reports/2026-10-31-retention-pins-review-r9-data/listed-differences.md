## Listed differences

The round's stop rule classifies these as listed differences. R9-F1 uses explicitly covered pointer/direct-entry routes and remains a P2 requiring repair.

| Difference | Executed input and result | One line for the doc |
|---|---|---|
| D-R6-1 | Spaced admin, trimmed twin, spaced common alias and physical common controls; valid Git reads precede archive/survey misses or trimmed metadata selection. `readers_probe.py`, eight controls with rows below. | Outside discovery, archive and survey retain legacy whitespace parsing: some valid spaced admin/common pointer spellings are missed or select a trimmed twin. |
| D-R6-2 | Nested gitfiles target external admin names with space/tab suffixes; Git reads HEAD, survey classifies them internal. | Nested gitfile classification still strips target bytes and can treat a whitespace-suffixed external admin as internal. |
| D-R6-3 | Valid common-space and trimmed registrations; cleanup removes the trimmed twin, leaving the actual admin. | Registration cleanup still strips common-directory output and can act on a whitespace target's trimmed twin. |
| D-R6-4 | A complete bundle verifies despite the stripped alternate; no incorrect verification acceptance is established. | Bundle verification still strips common targets when constructing its alternate; a complete pack can verify without using that alternate. |
| D-R7-1 | Named admin has healthy own objects; unused inferred-parent alternates line is 1,048,577 bytes. Base and head globally pin an unrelated owner. | An unused inferred parent's over-bound alternates can still globally pin trees while the borrower is live or exporting. |
| D-R8-1 | `stable/.git/refs/heads/main -> owner/branch`: complete discovery records no owner place, Git reads before removal (0) and fails after removal (128). | Links deeper than one level inside storage remain outside discovery unless another tracked metadata path traverses them. |
| D-R8-2 | Unused direct entry points to unrelated/unused, owner/broad, owner, worktrees parent, fixture home, fixture root or `/`; commit reads are unchanged by removing unrelated/owner. Targets inside/equal to owner pin owner only while the reader is running; broad ancestors do not pin descendant trees. An outside-checkout input-folder alias also adds an auxiliary entry versus base. | Auxiliary link discovery can retain an unused target or an input-folder link outside Git; only a recorded path equal to or inside a candidate tree pins it, and complete Git pins end with the job. |

The original forward implication has the expressly documented D-R8-1 boundary. The universal converse is false for unused direct links, as the round already states. Width does not turn a broad target into a global descendant pin. With complete evidence, pending output alone retains output paths after the job ends.

One old docstring sentence needs qualification for D-R8-2. Replace `Empty outside a checkout` in `subfleet/dependencies.py:138` with: `Without checkout metadata, discovery may still retain physical symlink entries traversed to reach the supplied folder.` This is the permitted extra-location difference; it does not hold the verdict.
