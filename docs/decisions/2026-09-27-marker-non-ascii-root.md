# The containment marker under a state root that is not printable ASCII (2026-09-27, revised 2026-10-10, 2026-11-13, 2026-11-15 and 2026-11-17)

C-5.5's third source reads `ps -axEww -o pid=,command=` and keeps the processes whose environment carries `SUBFLEET_ATTEMPT=<attempt id>` and `SUBFLEET_ROOT=<state root>`. It matched the root as written. `procs._read` runs `ps` with `LC_ALL=C`, and in the C locale `ps` prints every byte that is not printable ASCII in vis(3) notation, so under `/tmp/subfleet-José-root` the root marker never matched, a marked process that had left its group and its parent chain was in no source, and a census could come back verified empty while it lived. The shipped root, `~/.subfleet`, is ASCII, so this was latent for the one installation, and live for any user whose home path is not ASCII.

## Facts

- What `ps` prints, measured on macOS 26.6.2 (25G83) for all 255 bytes an environment can hold (`tests/fixtures/ps_vis_bytes.json`): printable ASCII, the space and the backslash as themselves; tab and newline in octal (`\011`, `\012`); other control bytes as `^A` through `^_` and `^?`; 0xA0 in octal (`\240`); 0x80 to 0x9F and 0xFF as `M^` and the control letter; every other byte from 0x80 as `M-` and its low seven bits. `é` (C3 A9) prints as `M-CM-)`.
- Why: `/bin/ps` here is adv_cmds-237, whose `ps/print.c` is byte-identical to 199.0.1's. `getproclline` joins the argv and environment strings with spaces, and `get_command_and_or_args` passes the line to `strvis(vis_args, cmd, VIS_TAB | VIS_NL | VIS_NOSLASH)`. The measured table is that call in a single-byte locale. Libc's own `strvis` in the C locale gives the same strings for the inputs checked. The C locale makes the printing one byte at a time, and the differential property test (`tests/process/test_ps_rendering.py`) confirms that a whole line is the concatenation for arbitrary values.
- The notation is not one-to-one. `VIS_NOSLASH` does two things. It puts no backslash before an `M-` or `^` form (without it, `é` prints as `\M-C\M-)`), so the text `M-CM-)` prints exactly as `é` does. It also leaves a literal backslash unescaped (without it, `\` prints as `\134`), so the text `\012` prints exactly as a newline does.
- The census stripped both ends of each row. A root that ends in a space ends the row when `SUBFLEET_ROOT` is the last variable, and the strip removed it, so that root never matched either. `peers.process_chain` (C-25.6) stripped the same way.
- Every launch path sets the markers from `str(self.root)`: attempts and conversation turns (`Daemon._launch`), admission probes, and a Claude lane's enrolment turn. Each census asks with `root=str(self.root)`.
- Two findings outside this change:
  - `ps -E` prints no environment for Apple's own executables (`/bin/sh`, `/bin/sleep`, `/usr/bin/env`), as measured here and by the review. On release/217, C-5.5's D-KILL2 text records this: the kernel returns a platform, hardened-runtime binary's buffer with no environment strings, so the census finds such a process by the group, the walk, responsibility or a recorded identity, never by its marker. `fix/containment-marker-gap` (f715cb14) was never merged; it survives only as `origin/wip/backup-2026-10/fix/containment-marker-gap--containment-marker-gap`.
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

- A caller's line counts as carrying the root when it holds `SUBFLEET_ROOT=` and the printed root as a substring, wherever they come from. That covers two roots that print alike (`é` and the text `M-CM-)`; a newline and the text `\012`), a root that begins a longer one, a marker inside another variable's value, and a marker in argv. Roots that print alike predated this change in one direction: a check for the ASCII root `/tmp/aM-CM-)` already matched a process whose root is `/tmp/aé` (review r2 finding 4); the change adds the reverse. Since D-CEN1 these cases are C-25.6's alone, where each errs toward refusing a person-only request. The census compares bytes whole and has none of them (section of 2026-11-13).
- The runtime-canary proposal in this paragraph records the September census design; D-CEN1 superseded its census response, and the current C-25.6-only follow-up is in the 2026-11-13 section below. If a later macOS changed the vis flags `ps` passes, matching would break for roots whose bytes those flags treat differently. The process tests fail on such a machine. Nothing makes them run there before a daemon starts, though: CI runs on another host, an installed daemon keeps running across a macOS update, and `daemon install` runs no tests. A runtime canary at daemon start would close that gap: spawn a child with known bytes, compare its `ps` line with `ps_text`, and on a mismatch make the marker source unavailable, so censuses are unverifiable rather than silently empty. It is left as a follow-up, because it adds a process start and a fail-closed lever to every daemon start.

A root or an attempt id that no environment can hold (one with a NUL, or one `os.fsencode` cannot encode) is carried by no process. In the September design, `ps_text` raised for it, so the census reported its marker source unavailable and the result was unverifiable rather than empty. Since D-CEN1, `procs.marker_strings` performs that validation for the census; `peers.ps_text` validates the root for C-25.6, where an unholdable root refuses a person-only request (exit 7).

## Change

The following list records the September design. D-CEN1 superseded its census changes: the census now compares raw marker bytes, and `PS_BYTES` and `ps_text` live in `peers.py` for C-25.6 alone (see the 2026-11-13 section below).

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

## Rebased onto release/217 (2026-11-13)

- **D-CEN1 overtook the census half.** D-CEN1 (8d9fe7e42, 2026-10-13) replaced the `ps -axEww` marker read with in-process `KERN_PROCARGS2` reads. `procs.marker_strings` builds `SUBFLEET_ATTEMPT=` and `SUBFLEET_ROOT=` with `os.fsencode` and refuses a NUL, and `procargs_marked` compares whole strings exactly. Launches write the markers with `os.fsencode` too (`subprocess.py:1904-1907` in CPython 3.14.7; `procs.spawn_disclaimed`). Both sides are the same bytes, with no printing and no strip, so that reader needs no notation. This branch changes no census code: `procs.py` is release/217's byte for byte. `tests/unit/test_census_procargs.py` and `tests/process/test_census_root_bytes_process.py` pin it for every root.
- **What remains is C-25.6.** `peers.process_chain` reads `ps -Ewwp <pid> -o command=` through `_ps` in the C locale. `_ps_byte`, `PS_BYTES` and `ps_text` now live in `peers.py`; the chain strips only the newline; `judge` looks for the root as `ps` prints it; a root no environment can hold is a refusal (exit 7). Design (a) stays.
- **Round 2's findings.**
  1. CI red under a framework Python: the rendering tests read through `peers._ps`, assert a contiguous run bounded by spaces, and check the root-last precondition or skip. Fix round 1 (below) replaced that skip with an assertion.
  2. The signal sentence is cut. Round 2's replacement does not hold on release/217 either: since D-KILL4, C-5.4 and C-5.5 make marked members signal targets in any census ("Markers are signal authority wherever the process runs").
  3. Fail-before, taken afresh on 2026-11-14 on a `git archive` of b51f08f96 (release/217) with this branch's five test files and the fixture copied in, one file at a time: the 29 new census cases pass there (18 in `tests/unit/test_census_procargs.py`, 11 in `tests/process/test_census_root_bytes_process.py`), as they should, because D-CEN1 already compares bytes. The 26 new C-25.6 cases fail. In `tests/unit/test_conversation_peers_attachments.py`, 18 fail: 16 because `peers.ps_text` and `PS_BYTES` do not exist, one because release/217 refuses for the wrong reason, and one because the chain strips a root's trailing space. In that one (`test_a_root_marker_is_found_as_ps_prints_it`), the caller is `/bin/zsh` on `ttys001`, and its parent's line holds the root as `ps` prints it. release/217 looks for the root as written, misses the marker and refuses the caller for its terminal ("this needs the Subfleet app; a terminal (ttys001) is not enough"); the test expects the marker refusal. Nobody is admitted. All 4 in `tests/process/test_ps_rendering.py` fail because `ps_text` does not exist. In `tests/unit/test_person_boundary.py`, 2 fail because `peers.UNHOLDABLE_ROOT` does not exist; the other 2 fail because, with no `ps_text` to raise, the app's caller is taken for a person under a root no environment can hold, and the request reaches the approval lookup (exit 2, `unknown-approval`) instead of exit 7. The report (`docs/reports/2026-11-13-marker-non-ascii-root-217.md`) lists every command.
  4. "All but the first predate this change" is corrected above.
  5. NFD, a variable after the root, and values over 256 bytes now have tests.
  6. `judge` returns a refusal for an unholdable root; `_person` answers exit 7.
  7. Wording fixed in the tests and this record.
- **Follow-up.** Switching C-25.6 to the raw reader would retire both the notation and the runtime canary. Until then the canary is C-25.6's alone: at daemon start, compare one child's `ps` line with `ps_text`, and on a mismatch refuse person-only requests with a named reason. It is not in this branch, because #88 rewrites the same reader and a new reader for a person-only check deserves its own review.

## Fix round 1 (2026-11-15)

The root is validated before `judge` inspects the process chain, so a root no environment can hold is refused with exit 7 even if `ps` would time out. The code was changed to meet C-25.6; the timeout is not an exception to the contract.

The rendering tests now launch the currently loaded Python binary directly, using dyld's `_NSGetExecutablePath`, so a framework launcher cannot append variables after the supplied root. A native `KERN_PROCARGS2` check pins that the synthetic environment ends with the root, and the real-`ps` root-last test requires that precondition instead of skipping on framework Python. Host process-inspection restrictions can still skip the real-`ps` tests.

Fix round 1 added five C-25.6 cases: four in `tests/unit/test_person_boundary.py` (`test_an_unholdable_root_is_refused_before_ps_can_time_out` and `test_an_unholdable_root_answers_exit_7_even_when_ps_would_time_out`, each for a NUL and a lone surrogate) and the native-buffer case `test_child_keeps_the_given_environment_last` in `tests/process/test_ps_rendering.py`. All five failed before the fix round's changes: the four against `judge`'s old order, the native-buffer case with the old child helper. They came after the fail-before run above and were not run on b51f08f96, so the 26 counted there are the C-25.6 cases the branch had on 2026-11-14.
