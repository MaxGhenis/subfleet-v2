#!/usr/bin/env python3
"""Run C-5.10's mutants one at a time in a copy of the source, never in this checkout.

Usage (from anywhere; the interpreter must have pytest and hypothesis, such as this
checkout's `.venv/bin/python`):

    python docs/reports/2026-11-12-worker-failure-mutants.py --scratch DIR --out FILE [--budget S] [NAME ...]

`DIR` must exist and have a path component starting `sf-`; the runner makes one copy of
the checkout's tracked `subfleet/`, `tests/` and top-level files under it, applies each
mutant alone to the copy, runs that mutant's tests there with the copy first on the
import path, puts the copied file's bytes back and checks them, and removes the copy at
the end. `FILE` collects one result per mutant; a mutant already in it is not run again,
so a run stopped by a time limit resumes. Naming mutants runs only those. The checkout's
mutated files are hashed before and after: the runner never writes to them.

Fix r1 replaced the build round's runner, which rewrote `subfleet/daemon.py` in place
(review r1 of #308, P3). The build round's seven mutants (B1 to B7) are here with their
anchors as the code reads now; fix r1's (F1 to F16), fix r2's (G1 to G6) and fix r3's (H1 to
H6) follow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
CAUSE = "tests/unit/test_worker_failure_cause.py"
ROUTES = "tests/unit/test_worker_failure_url_and_terminal.py"
HANDOFF = "subfleet/sessions/handoff.py"
DAEMON = "subfleet/daemon.py"
DAEMON_LOG = "subfleet/daemon_log.py"
REDACT = "subfleet/conversations/redact.py"
R3 = "tests/unit/test_worker_failure_r3_properties.py"
ONE_SHOT = ('                    self.log.error("worker %s failed: %s", key, failure_class(exc), '
            'exc_info=True)')
#: Fix r2 made the URL rule one line, its password one branch: to the last `@` before
#: whitespace or the next `://`. Fix r1's mutants below narrow that line back.
URL_HEAD = 'r"(?i)([a-z0-9+._-]+://[^\\s/:]*:)'
URL_PASSWORD = '((?:(?!://)\\S)*(?=@))(@)")'
URL_SCHEME = '_URL_SCHEME_RE = re.compile(r"[a-z0-9+._-]+", re.IGNORECASE)'
URL_TRY = "                    match = pattern.match(text, token.start())"
FIRST_TRACE = '''        try:
            if signature in traced:
                return False
            if len(traced) >= TRACED_CAUSES_MAX:
                traced.clear()
            traced.add(signature)
        except Exception:                   # noqa: BLE001 - never raise into the failure handler
            pass
        return True'''
LINKS = '''            outer = inner = None
            if tb is not None:
                outer = _frame_location(tb)
                while tb.tb_next is not None:
                    tb = tb.tb_next
                inner = _frame_location(tb)
            links.append((_plain(_TYPE_QUALNAME.__get__(type(link))), inner, outer))'''


def mutant(name, path, edits, tests, what):
    return {"name": name, "file": path, "edits": edits, "tests": tests, "what": what}


MUTANTS = [
    mutant("B1-cause-on-every-paced-failure", DAEMON,
           [("                cause = count % WORKER_CAUSE_EVERY == 1          # 1, 33, 65, ...",
             "                cause = True  # MUTANT")],
           [f"{CAUSE}::test_paced_cause_and_exact_summary_cadence"], "every paced failure carries its cause"),
    mutant("B2-cause-rendering-raises-out-of-done", DAEMON,
           [(ONE_SHOT, "                    str(exc)  # MUTANT\n" + ONE_SHOT)],
           [f"{CAUSE}::test_hostile_exception_cause_never_escapes_or_skips_handler"],
           "render the exception before the protected handler"),
    mutant("B3-retention-error-holds-message", DAEMON,
           [('error=failure_class(exc), next_due=after(delay))', 'error=str(exc), next_due=after(delay))')],
           [f"{CAUSE}::test_retention_error_is_type_only_and_deferred_keeps_count"], "str(exc) in timers.mark"),
    mutant("B4-control-set-never-clears", DAEMON,
           [("                self._control_traced.clear()", "                pass  # MUTANT")],
           [f"{CAUSE}::test_control_cause_deduplication_resets_after_completed_iteration"],
           "a completed iteration keeps the control set"),
    mutant("B5-remembered-set-grows-past-256", DAEMON,
           [("            if len(traced) >= TRACED_CAUSES_MAX:\n                traced.clear()",
             "            if False:  # MUTANT\n                traced.clear()")],
           [f"{CAUSE}::test_first_trace_holds_at_most_256_and_starts_again_when_full"], "a set past 256"),
    mutant("B6-summary-text-changes", DAEMON,
           [('"worker %s failed: %s (%d in a row, next try in %g s)"',
             '"worker %s failed: %s (%d consecutive, retry after %g s)"')],
           [f"{CAUSE}::test_paced_cause_and_exact_summary_cadence"], "the summary line's words"),
    mutant("B7-cause-bypasses-formatter", DAEMON,
           [(ONE_SHOT, "                    print(exc, file=sys.stderr)  # MUTANT\n" + ONE_SHOT)],
           [f"{CAUSE}::test_exception_message_notes_chain_and_source_are_scrubbed"], "a raw print to stderr"),
    # Fix r1, finding 1: each URL form, the rule narrowed back for it.
    mutant("F1-url-underscore", HANDOFF,
           [(URL_SCHEME, URL_SCHEME.replace("+._-", "+.-")),
            (URL_HEAD, URL_HEAD.replace('(?i)([a-z0-9+._-]+', '(?i)(?<!\\w)([a-z0-9+.-]+'))],
           [f"{ROUTES}::test_r1_url_forms_are_hidden_on_every_route", "-k", "underscore"],
           "no `_` in a scheme, which starts at a word boundary (release/217's reading)"),
    mutant("F2-url-digit-first", HANDOFF,
           [(URL_HEAD, URL_HEAD.replace("([a-z0-9+._-]+://", "([a-z][a-z0-9+._-]*://"))],
           [f"{ROUTES}::test_r1_url_forms_are_hidden_on_every_route", "-k", "digit"],
           "a scheme must start with a letter"),
    mutant("F3-url-after-digit", HANDOFF,
           [(URL_TRY, "                    start = re.compile(r'\\b[a-z]', re.I).search(text, token.start(), "
                      "token.end())  # MUTANT\n"
                      "                    match = start and pattern.match(text, start.start())")],
           [f"{ROUTES}::test_r1_url_forms_are_hidden_on_every_route", "-k", "digit"],
           "tried at the run's first letter after a word boundary (release/217's start)"),
    mutant("F4-url-empty-user", HANDOFF,
           [(URL_HEAD, URL_HEAD.replace("[^\\s/:]*:", "[^\\s/:]+:"))],
           [f"{ROUTES}::test_r1_url_forms_are_hidden_on_every_route", "-k", "empty-user"],
           "a user must hold a character"),
    mutant("F5-url-raw-slash", HANDOFF,
           [(URL_PASSWORD, '([^\\s/]*(?=@))(@)")')],
           [f"{ROUTES}::test_r1_url_forms_are_hidden_on_every_route", "-k", "raw-slash"],
           "no `/` in a password"),
    mutant("F6-url-second-at", HANDOFF,
           [(URL_PASSWORD, '([^\\s@]*(?=@))(@)")')],
           [f"{ROUTES}::test_r1_url_forms_are_hidden_on_every_route", "-k", "second-at"],
           "a password ends at the first `@`"),
    # Finding 2: the qualified name read through the metaclass, unguarded.
    mutant("F7-unguarded-qualname", DAEMON,
           [("links.append((_plain(_TYPE_QUALNAME.__get__(type(link))), inner, outer))",
             "links.append((type(link).__qualname__, inner, outer))")],
           [f"{CAUSE}::test_hostile_exception_types_never_raise_into_control_or_request",
            f"{CAUSE}::test_raise_signature_of_a_hostile_type_is_plain_and_hashable"],
           "`type(link).__qualname__` as read before fix r1"),
    mutant("F8-first-trace-unguarded", DAEMON,
           [(FIRST_TRACE, "        if signature in traced:\n            return False\n"
                          "        if len(traced) >= TRACED_CAUSES_MAX:\n            traced.clear()\n"
                          "        traced.add(signature)\n        return True")],
           [f"{CAUSE}::test_first_trace_treats_an_uncomparable_signature_as_new"], "`_first_trace` without its guard"),
    mutant("F9-class-name-through-metaclass", DAEMON,
           [('        name = type.__dict__["__name__"].__get__(type(exc))\n',
             '        return type(exc).__name__  # MUTANT\n')],
           [f"{CAUSE}::test_hostile_exception_types_never_raise_into_control_or_request"],
           "the summary's class name read through the metaclass"),
    # Finding 3: a link keyed on its whole traceback.
    mutant("F10-whole-traceback-keying", DAEMON,
           [(LINKS, "            frames = []\n            while tb is not None:\n"
                    "                frames.append(_frame_location(tb))\n                tb = tb.tb_next\n"
                    "            links.append((_plain(_TYPE_QUALNAME.__get__(type(link))), tuple(frames)))")],
           [f"{CAUSE}::test_one_object_raised_on_three_control_ticks_logs_one_cause",
            f"{CAUSE}::test_one_object_raised_from_three_requests_logs_one_cause"], "every frame of the traceback"),
    # Finding 4: review r1's R1, R2 and R3 on `raise_signature`.
    mutant("F11-R1-suppress-context-ignored", DAEMON,
           [("                    if BaseException.__suppress_context__.__get__(link)\n",
             "                    if False\n")],
           [f"{CAUSE}::test_raise_signature_honours_raise_from_none"], "`raise ... from None` ignored"),
    mutant("F12-R2-line-number-zero", DAEMON,
           [("    return (_plain(code.co_filename), _plain(tb.tb_lineno), _plain(code.co_name))",
             "    return (_plain(code.co_filename), 0, _plain(code.co_name))")],
           [f"{CAUSE}::test_raise_signature_reads_line_numbers"], "line numbers replaced by 0"),
    mutant("F13-R3-first-link-only", DAEMON,
           [(LINKS, LINKS + "\n            break")],
           [f"{CAUSE}::test_raise_signature_reads_the_whole_chain"], "the chain's first link only"),
    # Finding 5: escapes reach the output.
    mutant("F14-escapes-not-neutralized", DAEMON_LOG,
           [("    if not _UNSHOWN.search(source):\n        return []",
             "    if True:  # MUTANT\n        return []")],
           [f"{ROUTES}::test_osc52_and_screen_clear_are_written_as_visible_escapes",
            f"{ROUTES}::test_no_control_character_but_tab_and_line_breaks_is_ever_written"],
           "the handler, the tail and the follower write control characters as they are"),
    mutant("F15-terminal-reading-ignored", DAEMON_LOG,
           [("                if plain and not all(plain in held for held in shown):",
             "                if False:  # MUTANT")],
           [f"{ROUTES}::test_a_key_an_escape_splits_is_still_a_password"],
           "no run hidden for what the terminal reading hides"),
    # Finding 6 (P3): the chain is rendered whole again.
    mutant("F16-chain-not-capped", DAEMON_LOG,
           [("        ends = _chain_ends(value) if isinstance(value, BaseException) else None",
             "        ends = None  # MUTANT")],
           [f"{ROUTES}::test_a_longer_chain_renders_only_its_two_ends"], "every link rendered"),
    # Fix r2 (review r2 of #308). P1: fix r1's two-branch password, the first `/` first.
    mutant("G1-url-first-slash-branch-first", HANDOFF,
           [(URL_PASSWORD, '([^\\s/@]*(?:@[^\\s/@]*)*(?=@)|(?:(?!://)\\S)*(?=@))(@)")')],
           [f"{ROUTES}::test_r2_a_password_with_an_at_and_then_a_raw_slash_is_hidden_on_every_route",
            f"{ROUTES}::test_r2_an_at_in_a_path_hides_what_comes_before_it_on_every_route"],
           "fix r1's rule: to the last `@` before the first `/` when there is one"),
    # P2: review r2's M1, the outermost frame dropped from each link.
    mutant("G2-M1-outer-frame-dropped", DAEMON,
           [("                outer = _frame_location(tb)\n", "                outer = None  # MUTANT\n")],
           [f"{CAUSE}::test_raise_signature_reads_the_outermost_frame",
            f"{CAUSE}::test_one_helper_reached_from_two_control_lines_logs_two_causes"],
           "each link keyed on its innermost frame only"),
    # P2: what a terminal draws nothing for, each part of fix r2 undone alone.
    mutant("G3-format-characters-shown", DAEMON_LOG,
           [('_INVISIBLE = re.compile(r"[\\x00-\\x08\\x0e-\\x1f\\x7f-\\x84\\x86-\\x9f" + _FORMAT + "]")',
             '_INVISIBLE = re.compile(r"[\\x00-\\x08\\x0e-\\x1f\\x7f-\\x84\\x86-\\x9f]")  # MUTANT')],
           [f"{ROUTES}::test_r2_a_key_an_invisible_character_splits_is_still_a_password",
            f"{ROUTES}::test_r2_the_format_characters_are_unicode_cf"],
           "the terminal reading keeps format characters"),
    mutant("G4-separators-shown", DAEMON_LOG,
           [('_INVISIBLE = re.compile(r"[\\x00-\\x08\\x0e-\\x1f\\x7f-\\x84\\x86-\\x9f" + _FORMAT + "]")',
             '_INVISIBLE = re.compile(r"[\\x00-\\x08\\x0e-\\x1b\\x7f-\\x84\\x86-\\x9f" + _FORMAT + "]")')],
           [f"{ROUTES}::test_r2_a_key_an_invisible_character_splits_is_still_a_password",
            f"{ROUTES}::test_r2_words_a_separator_joins_are_one_run_that_hides_nothing"],
           "the terminal reading keeps the information separators (fix r1's reading)"),
    mutant("G5-format-characters-not-read", DAEMON_LOG,
           [('_UNSHOWN = re.compile(r"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f" + _FORMAT + "]")',
             '_UNSHOWN = re.compile(r"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f]")  # MUTANT')],
           [f"{ROUTES}::test_r2_a_key_an_invisible_character_splits_is_still_a_password",
            f"{ROUTES}::test_r2_the_format_characters_are_unicode_cf"],
           "a text with format characters and no control is not read as a terminal shows it"),
    mutant("G6-run-split-at-separators", DAEMON_LOG,
           [('_SHOWN_RUN = re.compile(r"(?:[^\\s]|[\\x1c-\\x1f])+")', '_SHOWN_RUN = re.compile(r"\\S+")  # MUTANT')],
           [f"{ROUTES}::test_r2_words_a_separator_joins_are_one_run_that_hides_nothing"],
           "a run ends at an information separator"),
    # Fix r3 (review r3 of #308). P1: bb118f63d's reading dropped, as fix r2 had it.
    mutant("H1-bb118f63d-reading-dropped", DAEMON_LOG,
           [("    return [(_rendered, _SHOWN_RUN), (_rendered_controls, _RUN)]",
             "    return [(_rendered, _SHOWN_RUN)]  # MUTANT")],
           [f"{ROUTES}::test_r3_a_format_character_before_bearer_keeps_bb118f63ds_mask",
            f"{R3}::test_r3_no_character_bb118f63d_masked_is_written"],
           "a text is read only as fix r2 read it"),
    # P2: the follower reads only what it writes as a terminal shows it, as before.
    mutant("H2-follower-context-not-read", DAEMON_LOG,
           [("        if around:\n            shown.append(", "        if False:  # MUTANT\n            shown.append(")],
           [f"{R3}::test_r3_an_invisible_key_split_across_feeds_is_still_a_password",
            f"{R3}::test_r3_a_follower_fed_in_any_split_hides_what_it_hides_whole"],
           "the follower's earlier text is not read as a terminal shows it"),
    mutant("H3-follower-settles-in-the-text-only", DAEMON_LOG,
           [("    for reading, _ in _readings(text):\n        read = reading(text)",
             "    for reading, _ in []:  # MUTANT\n        read = reading(text)")],
           [f"{R3}::test_r3_a_private_key_an_invisible_character_splits_is_held_until_it_ends"],
           "a break is settled by the text alone, not its terminal readings"),
    # P1: an approval's URL password, each part of the fix undone alone.
    mutant("H4-approval-password-stops-at-a-mask", REDACT,
           [('    r"((?:" + _MASK + r"|(?!://)\\S)*(?=@))(@)")', '    r"((?:(?!://)\\S)*(?=@))(@)")  # MUTANT')],
           [f"{R3}::test_r3_review_urls_mask_their_whole_password"],
           "the head's URL password cannot read through release's mask"),
    mutant("H5-approval-password-refused-for-a-metacharacter", REDACT,
           [("                          match.group(1) + _masked_password(match.group(2), path) + match.group(3)))",
             "                          match.group(0) if any(meta in match.group(2) for meta in _SHELL_META)"
             " else match.group(1) + _masked_password(match.group(2), path) + match.group(3)))  # MUTANT")],
           [f"{R3}::test_r3_review_urls_mask_their_whole_password",
            f"{R3}::test_r3_an_approval_masks_every_password_character_but_shell_metacharacters"],
           "a URL password holding a shell metacharacter is shown whole, as fix r2 had it"),
    mutant("H6-approval-password-hides-metacharacters", REDACT,
           [('_PASSWORD_KEPT_RE = re.compile(_MASK + "|" + "|".join(re.escape(meta) for meta in _SHELL_META))',
             '_PASSWORD_KEPT_RE = re.compile(_MASK)  # MUTANT')],
           [f"{R3}::test_r3_an_approval_masks_every_password_character_but_shell_metacharacters"],
           "a shell metacharacter in a URL password is masked with it"),
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mutated(source: str, edits) -> str:
    for old, new in edits:
        count = source.count(old)
        if count != 1:
            raise AssertionError(f"expected one anchor, found {count}: {old!r}")
        source = source.replace(old, new, 1)
    return source


def guarded_rmtree(path: Path, scratch: Path) -> None:
    resolved = path.resolve()
    assert resolved.parent == scratch.resolve() and resolved.name.startswith("mutant-copy-"), resolved
    assert any(part.startswith("sf-") for part in resolved.parts), resolved
    shutil.rmtree(resolved)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--budget", type=float, default=float("inf"),
                        help="seconds after which no further mutant starts")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    assert scratch.is_dir() and any(part.startswith("sf-") for part in scratch.parts), scratch
    assert not scratch.is_relative_to(CHECKOUT.resolve()), "the copy is never inside the checkout"
    chosen = [spec for spec in MUTANTS if not args.names or spec["name"] in args.names]
    results = json.loads(args.out.read_text())["results"] if args.out.exists() else []
    finished = {row["name"] for row in results if row.get("killed") is not None}
    files = sorted({spec["file"] for spec in MUTANTS})
    before = {name: sha((CHECKOUT / name).read_bytes()) for name in files}
    # Every mutant is built before anything runs, so an outdated anchor stops the run.
    variants = {spec["name"]: mutated((CHECKOUT / spec["file"]).read_text(), spec["edits"]) for spec in chosen}
    tracked = subprocess.run(["git", "-C", str(CHECKOUT), "ls-files", "-z"], check=True,
                             capture_output=True).stdout.decode().split("\0")
    copy = scratch / f"mutant-copy-{time.time_ns()}"
    copy.mkdir()
    try:
        for name in tracked:
            if name and (name.startswith(("subfleet/", "tests/")) or "/" not in name):
                target = copy / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(CHECKOUT / name, target)
        probe = subprocess.run([sys.executable, "-c", "import subfleet; print(subfleet.__file__)"], cwd=copy,
                               capture_output=True, text=True, check=True).stdout.strip()
        assert Path(probe).resolve().is_relative_to(copy.resolve()), probe
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        run_started = time.monotonic()
        for spec in chosen:
            if spec["name"] in finished:
                continue
            if time.monotonic() - run_started > args.budget:
                break
            target = copy / spec["file"]
            pristine = target.read_bytes()
            assert sha(pristine) == before[spec["file"]]
            command = [sys.executable, "-m", "pytest", *spec["tests"], "-q", "-p", "no:cacheprovider",
                       f"--basetemp={scratch / ('pytest-' + spec['name'])}"]
            started, load = time.monotonic(), os.getloadavg()[0]
            target.write_text(variants[spec["name"]])
            try:
                done = subprocess.run(command, cwd=copy, env=env, capture_output=True, text=True, timeout=400)
                output, code = done.stdout + done.stderr, done.returncode
            except subprocess.TimeoutExpired as exc:
                output, code = str(exc), None
            finally:
                target.write_bytes(pristine)
                restored = sha(target.read_bytes())
                assert restored == before[spec["file"]], spec["name"]
            tail = [line for line in output.splitlines() if " passed" in line or " failed" in line]
            failed = [line.split(" ")[1] for line in output.splitlines() if line.startswith("FAILED ")]
            row = {"name": spec["name"], "file": spec["file"], "what": spec["what"], "tests": spec["tests"],
                   "returncode": code, "killed": None if code is None else code == 1,
                   "summary": tail[-1] if tail else "", "failed": failed, "restored_sha256": restored,
                   "seconds": round(time.monotonic() - started, 1), "load_start": round(load, 1),
                   "load_end": round(os.getloadavg()[0], 1)}
            results = [r for r in results if r["name"] != spec["name"]] + [row]
            args.out.write_text(json.dumps({"checkout_sha256": before, "results": results}, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    finally:
        guarded_rmtree(copy, scratch)
    after = {name: sha((CHECKOUT / name).read_bytes()) for name in files}
    assert after == before, "the checkout's sources changed during the run"
    ran = {row["name"]: row for row in results}
    return 0 if all(ran.get(name, {}).get("killed") for name in variants) else 1


if __name__ == "__main__":
    raise SystemExit(main())
