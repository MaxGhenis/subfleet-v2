# The containment marker under a state root that is not printable ASCII (2026-09-27)

C-5.5's third source reads `ps -axEww -o pid=,command=` and keeps the processes whose environment carries `SUBFLEET_ATTEMPT=<attempt id>` and `SUBFLEET_ROOT=<state root>`. It matched the root as written. `procs._read` runs `ps` with `LC_ALL=C`, and in the C locale `ps` prints every byte that is not printable ASCII in vis(3) notation, so under `/tmp/subfleet-José-root` the root marker never matched, a marked process that had left its group and its parent chain was in no source, and a census could come back verified empty while it lived. The shipped root, `~/.subfleet`, is ASCII, so this was latent for the one installation, and live for any user whose home path is not ASCII.

## Facts

- What `ps` prints, measured on macOS 26.6.2 (25G83) for all 255 bytes an environment can hold (`tests/fixtures/ps_vis_bytes.json`): printable ASCII, the space and the backslash as themselves; tab and newline in octal (`\011`, `\012`); other control bytes as `^A` through `^_` and `^?`; 0xA0 in octal (`\240`); 0x80 to 0x9F and 0xFF as `M^` and the control letter; every other byte from 0x80 as `M-` and its low seven bits. `é` (C3 A9) prints as `M-CM-)`.
- Why: adv_cmds 199.0.1 `ps/print.c`, `get_command_and_or_args`, joins the argv and environment strings with spaces and calls `strvis(vis_args, cmd, VIS_TAB | VIS_NL | VIS_NOSLASH)`. The measured table is that call in a single-byte locale. The C locale makes the printing one byte at a time; the differential property test (`tests/process/test_ps_rendering.py`) confirms a whole line is the concatenation for arbitrary values.
- The notation is not one-to-one. `VIS_NOSLASH` leaves a backslash unescaped, so the text `M-CM-)` or `\012` prints exactly as `é` or a newline does.
- The census stripped both ends of each row. A root that ends in a space ends the row when `SUBFLEET_ROOT` is the last variable, and the strip removed it, so that root never matched either. `peers.process_chain` (C-25.6) stripped the same way.
- Every launch path sets the markers from `str(self.root)`: attempts and conversation turns (`Daemon._launch`), admission probes, and a Claude lane's enrolment turn. Each census asks with `root=str(self.root)`.
- Two findings outside this change. `ps -E` prints no environment for Apple's own executables (`/bin/sh`, `/bin/sleep`, `/usr/bin/env`, measured here too). That gap is known, with a fix on the unmerged branch `fix/containment-marker-gap` (f715cb14). And a `ps` read taken while a process is still in dyld prints the kernel's `apple[]` strings after its environment until dyld clears them. They carry no marker, so the census is unaffected.

## Options

- (a) Match each marker as `ps` prints it: render the root and the attempt id in `ps`'s notation before building the patterns. It changes only the reader.
- (b) Refuse to start the daemon on a root that is not printable ASCII, with a named fix.
- (c) Mark with an ASCII-only token (a hash of the root in `SUBFLEET_ROOT_ID`) and keep `SUBFLEET_ROOT`.

## Decision: (a)

Why (a) is the smallest change that is correct for every root:

1. It changes no process's environment. C-5.1, the guardian and the four launch paths stay as they are, and nothing new reaches a provider's environment.
2. It also finds processes launched before the fix. An attempt that outlives a daemon restart across the upgrade (recovered, quarantined, or being killed) carries only `SUBFLEET_ROOT`. Under (c) the census would have to keep matching `SUBFLEET_ROOT` for those processes, and under a root that is not ASCII that fallback needs (a) anyway. So (c) done correctly is (a) plus a marker change.
3. The notation is fully determined and pinned three ways: the recorded table checked in unit tests, the exhaustive 255-byte measurement repeated by the process test on the machine that runs it, and a Hypothesis differential test of whole lines against the real `ps`.
4. (b) would refuse a working configuration to avoid a bug that (a) fixes.

What (a) accepts, each case erring toward holding on:

- Two roots that print alike are one root to the census: `é` and the text `M-CM-)`. A process of either root counts for both, and only with the same attempt id, the collision C-5.5's second half guards. This is tested and labelled intended.
- A root still also matches the start of a longer root that continues with a space (`/tmp/a` counts a process of `/tmp/a b`). This predates the change and is now stated in C-5.5.
- If a later macOS changed the vis flags `ps` passes, matching would break for roots whose bytes those flags treat differently. The process tests fail on such a machine before a census misses a marker there.

A root that `os.fsencode` cannot encode (a lone surrogate) is carried by no process. The census reports its marker source unavailable, so the result is unverifiable rather than empty.

## Change

- `procs.PS_BYTES` and `procs.ps_text`: the notation, from the measured rules.
- `procs.containment`: builds both marker patterns from `ps_text` and strips only a row's leading padding.
- `peers.judge` and `peers.process_chain` (C-25.6): the root marker is looked for as `ps` prints it, and only the newline is stripped from the line.
- C-5.5 says all of this; C-5.1 is unchanged.
- Tests: `tests/unit/test_procs.py`, `tests/unit/test_conversation_peers_attachments.py`, `tests/process/test_ps_rendering.py`. The census and peers tests fail on release/217 because the marker is not found (`frozenset() == {pid}`); the other new tests fail there because `ps_text` does not exist yet.
