# Review r13 notes (uncommitted working notes)

- Start: 2026-10-31 21:10:26Z, load 95.23 81.98 68.22
- Scratch: /var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-r272r13 (granted, not /private/tmp)
- venv synced 21:13:27-21:13:44Z, editable install of the worktree.
- Product diff ced02912b..5d3a56ff5 touches only subfleet/dependencies.py: docstring, git_storage filter, _unreadable_location (new), _Aliases.locations entries filter.

## Holds so far (all linked TMPDIR: $S/link -> $S/real, under /var -> /private/var)
- h01 r12/r11/r10/r9 files: 24 passed (21:20:28Z, load 93.17)
- h02 readable: 36 passed, 427 s (21:23:54Z, load 92.43)
- h03 cycles+spelling+locations: 53 passed (21:31:48Z, load 95.95)
- h04 inputs+pointer_inputs: 3 passed (21:34:47Z, load 81.46)
- h05 my retarget+NUL rows: 3 passed. Retarget link: head = base places + alias-changed evidence on <base>/loop; base no evidence. Retarget resolved-target: head drops places, 4 alias-changed entries; base keeps stale <base>/admin and <base>/loop/tail/objects, no evidence. NUL: both ValueError "lstat: embedded null character in path"; git read 0 through the truncated line.
- h06/h07 stat pairs: 98 fixed + 207 location + 196 spelling = 501 places over 128 inputs; stat before == stat after for all; no 0->ELOOP.
- Location strategy's 55 draws: 53 distinct, position alternates-line never drawn (10 of 11 positions). Spelling strategy: 55 distinct classes (full cross product).
- 496 = 2 runs x (9 cycle + 5 routine + 55 location + 55 spelling + 4 long = 128) + plugin 2 x (55 + 65).
- check 1 routes (21:54:47Z, load 66.60): ced02912b == head: control 2/0, scan 2/0, alt-line 2/2, alt-tail 2/2, gitfile 0/4 (fsck/read 128), commondir 1/3 (128), objects+alt 2/2. Every returned string meets both equations, stat 0. routes-*.json.
- R12 inputs: baseline record == head record (3 places, no incomplete) both routes, prefix-normalized from h01 junit properties.
- h08-h10 three-way evidence (my ThreeWay wrapper on the modules' own tests): cycles 18 (16 differ only by 20 ELOOP strings, incomplete identical; scan rows complete 0), routine 5 equal, inputs 55 equal, spelling 65 equal (55 given + 4 r9-sibling + 6 routine-alias). 143 comparisons.
- Stray empty .venv (76K) created by one uv run without env vars at 21:57; removed with guarded rmtree.
- h11b/h11c transaction probes (22:07:22Z, load 43.86): r11's transaction_probes.py 32 passed; r12's file has an IndentationError at line 246 as committed (cut-off review); my copy r12_transaction_probes_indent_fixed.py dedents that one line: 31 passed + test_outside_checkout_alias rerun passed after copying its r8-prerequisite.py.txt fixture beside it (first failure was the copy's missing sibling file).
- h12 shard tests: 122 passed. Shard map 39404ea61->ced02912b: comment and groups unchanged, tools/ unchanged, source rewritten to "run 41218031368", seconds +153 files, 0 removed; every name in branch head ∪ 785dda05c; 3 union files unmapped (review r11, review r12, readable) -> crc32 placement.
- h13 earlier A 97 passed; h14 earlier B 144 passed; h15 r8+worktrees+qos+turns 45 passed; h16 dependency set A 138 passed (729.89 s, past the 10-min tool cap, polled to completion); h17 dep properties 1+2: 2 passed (518 s).
- Mutants 22:51-23:10Z: 19/19 killed, every assertion list equal to fix r12's receipt (20 assertions). mutants/*.json.
- h18 dep prop C 1 passed (500 s); h19 dep prop D 1 passed; h20 shared examples 58 passed; h21 shared props A+B 2 passed (690 s, polled); h22 guards 63 passed (my new file present; names guard passes); h23 archive 108 passed, live lsof deselected (787 s, polled); h24 archive props+nested+81 notes 67 passed.
- h25 salvage+sweep examples (linked): 12 passed, 2 failed: test_retention_salvage_properties.py both properties, at explicit examples, comparing unresolved linked TMPDIR world.root with git worktree list's /private/var path. File byte-identical to 785dda05c (release/217). h26 resolved TMPDIR: 2 passed (506 s). Environment, not this PR.
- h27 sweep property 1 passed; h28/h29 shared props C, D 1+1 passed.
- Red replay (00:31:20Z, load 41.32): review r12 file with 92a8635a0's reader: 2 failed (filter rows, owner-retention assertion line 107) / 2 passed; review r11 file with ced02912b's reader: 2 failed / 2 passed. Green at head in h01.
- Cost (check 6): os.stat calls ced02912b 44 / head 44 (plain), 49 / 49 (worktree); min ms 0.811/0.777 and 0.863/0.869; medians within noise at load ~41-56.
- 40-link acyclic chain: stat ELOOP, canonical -> physical target (stat 0), _unreadable_location(raw chain) True. So "live cycle" holds for canonical strings on a still filesystem, not as a general reading of ELOOP.
- Cleanup: r8-prerequisite.py.txt copy removed after h11c. No /tmp/sf-failed, no __pycache__, no .hypothesis in the tree.
