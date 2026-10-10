# The containment marker under a state root that is not printable ASCII (2026-09-27, revised 2026-10-10)

C-5.5's third source reads `ps -axEww -o pid=,command=` and keeps the processes whose environment carries `SUBFLEET_ATTEMPT=<attempt id>` and `SUBFLEET_ROOT=<state root>`. It matched the root as written. `procs._read` runs `ps` with `LC_ALL=C`, and in the C locale `ps` prints every byte that is not printable ASCII in vis(3) notation, so under `/tmp/subfleet-José-root` the root marker never matched, a marked process that had left its group and its parent chain was in no source, and a census could come back verified empty while it lived. The shipped root, `~/.subfleet`, is ASCII, so this was latent for the one installation, and live for any user whose home path is not ASCII.

## Facts

- What `ps` prints, measured on macOS 26.6.2 (25G83) for all 255 bytes an environment can hold (`tests/fixtures/ps_vis_bytes.json`): printable ASCII, the space and the backslash as themselves; tab and newline in octal (`\011`, `\012`); other control bytes as `^A` through `^_` and `^?`; 0xA0 in octal (`\240`); 0x80 to 0x9F and 0xFF as `M^` and the control letter; every other byte from 0x80 as `M-` and its low seven bits. `é` (C3 A9) prints as `M-CM-)`.
- Why: `/bin/ps` here is adv_cmds-237, whose `ps/print.c` is byte-identical to 199.0.1's. `getproclline` joins the argv and environment strings with spaces, and `get_command_and_or_args` passes the line to `strvis(vis_args, cmd, VIS_TAB | VIS_NL | VIS_NOSLASH)`. The measured table is that call in a single-byte locale. Libc's own `strvis` in the C locale gives the same strings for the inputs checked. The C locale makes the printing one byte at a time, and the differential property test (`tests/process/test_ps_rendering.py`) confirms that a whole line is the concatenation for arbitrary values.
- The notation is not one-to-one. `VIS_NOSLASH` does two things. It puts no backslash before an `M-` or `^` form (without it, `é` prints as `\M-C\M-)`), so the text `M-CM-)` prints exactly as `é` does. It also leaves a literal backslash unescaped (without it, `\` prints as `\134`), so the text `\012` prints exactly as a newline does.
- The census stripped both ends of each row. A root that ends in a space ends the row when `SUBFLEET_ROOT` is the last variable, and the strip removed it, so that root never matched either. `peers.process_chain` (C-25.6) stripped the same way.
- Every launch path sets the markers from `str(self.root)`: attempts and conversation turns (`Daemon._launch`), admission probes, and a Claude lane's enrolment turn. Each census asks with `root=str(self.root)`.
- Two findings outside this change:
  - `ps -E` prints no environment for Apple's own executables (`/bin/sh`, `/bin/sleep`, `/usr/bin/env`), as measured here and by the review. That gap is known, with a fix on the unmerged branch `fix/containment-marker-gap` (f715cb14).
  - Once, on 2026-09-27 at a load average near 130, a `ps` read taken as soon as `Popen` returned also printed the kernel's `apple[]` strings (`ptr_munge=`, `stack_guard=` and the like) after the environment. A rerun of the same example did not. The review's 801 early reads, some of a child still suspended before its first instruction, never did. Why it happened is not established. Those strings carry no marker, so the census is unaffected either way.

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

What (a) accepts:

- A process counts when its printed line holds both markers as space-bounded tokens, wherever they come from. That covers two roots that print alike (`é` and the text `M-CM-)`; a newline and the text `\012`), a marker inside a longer root that holds a space, a marker in another variable's value, and a marker in argv. All but the first predate this change. Each needs the same attempt id too, the collision C-5.5's second half guards. Each errs toward holding on: no pid that only the marker source found is ever signalled. C-5.6 signals the recorded group and the members recorded as owned, which are taken only from the group while the recorded leader lives (`Daemon._kill_attempt`, `_contain_probe`). The unit tests label these cases intended.
- If a later macOS changed the vis flags `ps` passes, matching would break for roots whose bytes those flags treat differently. The process tests fail on such a machine. Nothing makes them run there before a daemon starts, though: CI runs on another host, an installed daemon keeps running across a macOS update, and `daemon install` runs no tests. A runtime canary at daemon start would close that gap: spawn a child with known bytes, compare its `ps` line with `ps_text`, and on a mismatch make the marker source unavailable, so censuses are unverifiable rather than silently empty. It is left as a follow-up, because it adds a process start and a fail-closed lever to every daemon start.

A root or an attempt id that no environment can hold (one with a NUL, or one `os.fsencode` cannot encode) is carried by no process. `ps_text` raises for it, so the census reports its marker source unavailable and the result is unverifiable rather than empty.

## Change

- `procs.PS_BYTES` and `procs.ps_text`: the notation, from the measured rules.
- `procs.containment`: builds both marker patterns from `ps_text` and strips only a row's leading padding.
- `peers.judge` and `peers.process_chain` (C-25.6): the root marker is looked for as `ps` prints it, and only the newline is stripped from the line.
- C-5.5 says all of this; C-5.1 is unchanged.
- Tests: `tests/unit/test_procs.py`, `tests/unit/test_conversation_peers_attachments.py`, `tests/process/test_ps_rendering.py`.

What the new tests do on fd7797a2, the commit before this change (29 fail and 97 pass there; all 126 pass with it; `~/reviews/marker-non-ascii-root-2026-09-27/before-fd7797a2-r2.txt`):

- The process census tests (six of the seven named roots) and four unit census tests fail with `frozenset() == {pid}`: verified empty, no errors, while the child lived. The seventh root, a backslash and a space, prints as itself and passes there as a regression guard.
- The real-process Hypothesis property fails as well. The review's run minimized it to `raw=b'\x01'`.
- The peers tests fail on the verdict, its reason or the command string.
- The lone-surrogate test fails because the census is not unverifiable.
- The intended over-inclusion tests pass, as they should.
- The rest fail because `ps_text` does not exist yet.

## Review

An independent Opus review of 480b75f (lane job `20260927-151431-marker-root-review-opus`, `~/reviews/marker-non-ascii-root-2026-09-27/review-opus.md`) found the code correct, and also confirmed it end to end. Under fake-provider daemons on fd7797a2, with a non-ASCII root or one ending in a space, an attempt was released as `interrupted` while its escaped writer lived (5 of 5 runs that reached the kill). With the change it was quarantined (10 of 10).

It asked for these documentation fixes, all made in this revision:

- the `VIS_NOSLASH` mechanism above;
- the `apple[]` observation stated as observed;
- the claim about when the process tests fail;
- every over-inclusion case named;
- the version cited;
- this fail-before list.

It also suggested a test of the attempt id's rendering (368cf3c) and a refusal of a NUL (done).
