"""Mutants of D-LANE-SCOPE1's guard edit, each to be killed by a named test.

    python tools/guard_subfleet_root_mutations.py --scratch <dir containing /sf->

Each mutant is applied to a fresh `git archive` of HEAD under the scratch
directory (never to this checkout), then its named test in
tests/unit/test_guard_subfleet_root.py runs there. A kill is a failed test
assertion: an error, a skip or a pass is not. The unmutated control must pass
every named test first. Prints one JSON row per mutant; exit 0 only when the
control passes and every mutant is killed. Exports are removed afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/unit/test_guard_subfleet_root.py"
HOOK = "private/guard/never-rules-hook.sh"
TRUST = "private/guard/TRUST"
#: The original reviewed pair's commit (release/217 at abe117e67). Its TRUST still pins codex-cli 0.153.3,
#: behind the installed TRUST (0.159.0) that this revision's TRUST is built on.
BASE = "abe117e676b042b0cc8b4208dc25da52820221e2"
NEW_ROOTS = "|\\.subfleet(/[^/[:space:]'\\\"]+)?"
#: r2 (the hub's review r1, P2-3): $SUBFLEET_ROOT / $SUBFLEET_HOME as spellings of ~/.subfleet, and the
#: ${…} normalization on both `judged` lines.
ENV_ROOTS = "|\\\\\\$SUBFLEET_(ROOT|HOME)(/[^/[:space:]'\\\"]+)?"
ENV_SED = " s/\\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g'"
OLD_HOOK_SHA256 = "a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db"
NEW_HOOK_SHA256 = "7a5ad79d3f7db4c3c08bf07b8455344a4bdae3e66224218094b398cdf1091a1a"
ROOT_CORE = 'root_core="/|/\\\\*|/Users/?|(${broad_re})/?"'

# name -> (file, old text, new text, the named test that must kill it)
MUTATIONS = {
    # r1's root edits: the path-word pass now judges every resolvable word against snapshot-5's roots, so
    # these mutants show only on a word the text rules judge alone (one still holding $ after resolving).
    # An edit outside the r3 blocks makes the ``hooks`` fixture error (its r2 rebuild no longer hashes to
    # R2_HOOK_SHA256), and an error is no kill, so each is named by a test that reads HOOK directly.
    "new-roots-removed": (HOOK, NEW_ROOTS, "", "test_each_direct_child_is_a_root"),
    "child-form-dropped": (HOOK, NEW_ROOTS, "|\\.subfleet", "test_each_direct_child_is_a_root"),
    "prefix-match": (HOOK, NEW_ROOTS, "|\\.subfleet(/[^[:space:]'\\\"]+)?",
                     "test_near_misses_patterns_prose_and_bounded_searches_stay_allowed"),
    # The roots moved to their own root_core alternative, without the shared `/?`.
    "trailing-slash-missed": (HOOK, None, None, "test_trailing_slash_spellings_are_denied"),
    "trust-hash-stale": (TRUST, None, None, "test_trust_pins_the_new_hook_bytes"),
    # The hub review's finding: a TRUST built on the repository's stale copy rolls the lane pin back.
    "trust-built-on-repo-copy": (TRUST, None, None, "test_trust_keeps_the_installed_lane_pin"),
    # r2: the env spellings dropped (their broad_re alternative and their ${…} normalization).
    "env-spellings-dropped": (HOOK, None, None, "test_every_env_spelling_is_a_root_one_level_down_and_no_deeper"),
    # r3 (decisions 1-3), each as EDITS rewrites r3b's code.
    "file-exception-removed": (HOOK, None, None, "test_r3_a_file_is_never_a_broad_root"),
    "not-dir-dropped": (HOOK, None, None, "test_r3_a_directory_a_link_to_one_and_a_name_stay_roots"),
    "exists-dropped": (HOOK, None, None, "test_r3_a_directory_a_link_to_one_and_a_name_stay_roots"),
    "env-value-used": (HOOK, None, None, "test_r3_env_values_are_never_read"),
    "glob-as-single-file": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "dot-rule-dropped": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "nomatch-judged-by-text": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "case-insensitive": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "slash-star-left-as-text": (HOOK, None, None, "test_r3_a_bare_star_after_a_root_is_that_root"),
    "state-not-restored": (HOOK, None, None, "test_r3_glob_expansion_restores_every_setting"),
    # r3b (decisions 1-4): the brief's eleven.
    "stall-mount-listed": (HOOK, None, None, "test_r3b_nothing_inside_a_mount_is_read"),
    "volumes-not-listed": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "plain-name-unchecked": (HOOK, None, None, "test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed"),
    "volumes-name-stated": (HOOK, None, None, "test_r3b_nothing_inside_a_mount_is_read"),
    "usr-dropped": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "case-sensitive-roots": (HOOK, None, None, "test_r3b_root_table"),
    "alias-folding-dropped": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "holds-root-fixed-list": (HOOK, None, None, "test_r3b_listable_folders_are_computed"),
    "non-last-keeps-all": (HOOK, None, None, "test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed"),
    "written-link-dropped": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "file-test-on-stall": (HOOK, None, None, "test_r3b_nothing_inside_a_mount_is_read"),
    # r3c (the hub's decisions on review r2): the brief's seven.
    "r3c-wild-only-dropped": (HOOK, None, None, "test_r3c_globs_that_match_every_entry_are_their_folder"),
    "r3c-subchild-dropped": (HOOK, None, None, "test_r3c_globs_that_match_every_entry_are_their_folder"),
    "r3c-volumes-mapping-dropped": (HOOK, None, None, "test_r3c_volumes_entries_are_judged_as_the_rest_of_the_path"),
    "r3c-braces-dropped": (HOOK, None, None, "test_r3c_braces_and_alternation_are_judged_by_their_alternatives"),
    "r3c-physical-dots-dropped": (HOOK, None, None, "test_r3c_dotdot_after_a_link_is_physical"),
    "r3c-basename-dropped": (HOOK, None, None, "test_r3c_searchers_are_found_by_basename"),
    "r3c-unescape-dropped": (HOOK, None, None, "test_r3c_the_other_escapes_are_closed"),
}
#: r3's and r3b's mutants: (old text, new text) pairs, each old text exactly once in the hook.
LIST_GUARD = '      elif pw_holds "$p"; then\n'
RESTORE = """  if [ -n "$gi_set" ]; then GLOBIGNORE=$gi; fi
  if [ -n "$n" ]; then shopt -s nullglob; else shopt -u nullglob; fi
  if [ -n "$d" ]; then shopt -s dotglob; else shopt -u dotglob; fi
  if [ -n "$c" ]; then shopt -s nocaseglob; else shopt -u nocaseglob; fi
  if [ -n "$e" ]; then shopt -s extglob; else shopt -u extglob; fi
  if [ -n "$g" ]; then shopt -s failglob; else shopt -u failglob; fi
  if [ -n "$f" ]; then set -f; fi
"""
HOLDS = """pw_holds() {
  local n=""
  if pw_stall "$1" && ! pw_volumes "$1"; then return 1; fi
  for n in TheAxiomFoundation RulesFoundation PolicyEngine Library Caches .cache .local .subfleet \\
           Users Volumes System private tmp var folders opt homebrew Applications usr Data @sf-any@; do
    pw_normalize "$1/$n"
    pw_isbroad "$pw_n" && return 0
  done
  return 1
}
"""
#: r3c: pw_alt's trailing rule (1(b): a component made only of wildcards, or .* or **, is its folder).
WILD_LINE = "          if [ \"$c\" = '.*' ] || [ \"$c\" = '**' ] || pw_wild \"$c\"; then b=${b%/*}; walk=1; else break; fi ;;\n"
EDITS = {
    "file-exception-removed": [('    pw_isfile "$1" && return 1\n    return 0\n', "    return 0\n")],
    "not-dir-dropped": [('pw_nondir() { [ -e "$pw_fs$1" ] && [ ! -d "$pw_fs$1" ]; }',
                         'pw_nondir() { [ -e "$pw_fs$1" ]; }')],
    "exists-dropped": [('pw_nondir() { [ -e "$pw_fs$1" ] && [ ! -d "$pw_fs$1" ]; }',
                        'pw_nondir() { [ ! -d "$pw_fs$1" ]; }')],
    "env-value-used": [("""    '$SUBFLEET_ROOT'|'$SUBFLEET_HOME') pw_p="$HOME/.subfleet" ;;
    '$SUBFLEET_ROOT/'*|'$SUBFLEET_HOME/'*) pw_p="$HOME/.subfleet/${1#*/}" ;;
""", """    '$SUBFLEET_ROOT') pw_p=${SUBFLEET_ROOT:-$HOME/.subfleet} ;;
    '$SUBFLEET_HOME') pw_p=${SUBFLEET_HOME:-$HOME/.subfleet} ;;
    '$SUBFLEET_ROOT/'*) pw_p="${SUBFLEET_ROOT:-$HOME/.subfleet}/${1#*/}" ;;
    '$SUBFLEET_HOME/'*) pw_p="${SUBFLEET_HOME:-$HOME/.subfleet}/${1#*/}" ;;
""")],
    "glob-as-single-file": [("    '/*') return 0 ;;\n    *[*?[]*) ;;\n", "    '/*') return 0 ;;\n")],
    "dot-rule-dropped": [('    case "$x" in .*) [ -n "$dot" ] || continue ;; esac\n', "")],
    "nomatch-judged-by-text": [('  if pw_globbroad "$pw_p" "$f"; then pw_av=G; else pw_av=NG; fi',
                                '  if pw_globbroad "$pw_p" "$f"; then pw_av=G; fi')],
    "case-insensitive": [("  shopt -u nocasematch extglob\n", "  shopt -s nocasematch; shopt -u extglob\n")],
    # r3c: the trailing run is now read in pw_alt's loop (r3's bare * among its forms)
    "slash-star-left-as-text": [(WILD_LINE, "          break ;;\n")],
    "state-not-restored": [(RESTORE, "")],
    "stall-mount-listed": [(LIST_GUARD, '      elif pw_holds "$p" || pw_stall "$p"; then\n')],
    "volumes-not-listed": [(LIST_GUARD, '      elif ! pw_stall "$p" && pw_holds "$p"; then\n')],
    "plain-name-unchecked": [('        if pw_lexists "$pw_n"; then np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=u; fi\n',
                              '        np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=u\n')],
    "volumes-name-stated": [('      elif [ -z "$glob" ] && ! pw_stall "$p"; then\n', '      elif [ -z "$glob" ]; then\n')],
    "usr-dropped": [('/Library /Applications /usr"', '/Library /Applications"')],
    "case-sensitive-roots": [('  local p=$1 rest="" nc="" r=1 sv=$pw_n\n  shopt -q nocasematch && nc=1\n  shopt -s nocasematch\n',
                              '  local p=$1 rest="" nc="" r=1 sv=$pw_n\n  shopt -q nocasematch && nc=1\n  shopt -u nocasematch\n')],
    "alias-folding-dropped": [("""  case "$pw_n" in
    /system/volumes/data) pw_n=/ ;;
    /system/volumes/data/*) pw_n=${pw_n:20} ;;
  esac
  case "$pw_n" in
    /private/tmp|/private/tmp/*|/private/var|/private/var/*|/private/etc|/private/etc/*) pw_n=${pw_n:8} ;;
  esac
""", "")],
    # r3's fixed list (pw_listed): /, /Users, /private, the home, its Library and .subfleet, /Users/<any>
    # and its Library and .subfleet.
    "holds-root-fixed-list": [(HOLDS, """pw_holds() {
  case "$1" in /|/Users|/private|"$pw_home"|"$pw_home/Library"|"$pw_home/.subfleet") return 0 ;; esac
  case "$1" in /Users/*/*/*) return 1 ;; /Users/*/Library|/Users/*/.subfleet) return 0 ;; /Users/*/*) return 1 ;; /Users/*) return 0 ;; esac
  return 1
}
""")],
    "non-last-keeps-all": [('        if pw_isbroad "${np[$m]}" && ! pw_isfile "${np[$m]}"; then', '        if :; then')],
    "written-link-dropped": [("""  pw_physical "$1"
  [ "$pw_r" != "$1" ] && pw_isbroad "$pw_r" && ! pw_isfile "$pw_r"
}
""", "  return 1\n}\n")],
    "file-test-on-stall": [('pw_isfile() { pw_stall "$1" && return 1; pw_nondir "$1"; }', 'pw_isfile() { pw_nondir "$1"; }')],
    # r3c, decision by decision
    "r3c-wild-only-dropped": [(WILD_LINE, WILD_LINE.replace('|| pw_wild "$c"', "|| [ \"$c\" = '*' ]"))],
    "r3c-subchild-dropped": [('      if [ -n "$glob" ] && pw_subchild "$p" && ! pw_isfile "$p"; then return 0; fi\n', "")],
    "r3c-volumes-mapping-dropped": [('  while [[ $p == /volumes/*/* ]]; do rest=${p:9}; pw_normalize "/${rest#*/}"; p=$pw_n; done\n', "")],
    "r3c-braces-dropped": [('  local LC_ALL=C s=$1 n=${#1} k=$2 j=0 d=0 nested="" commas=0\n',
                            '  local LC_ALL=C s=$1 n=${#1} k=$2 j=0 d=0 nested="" commas=0\n  return 1\n')],
    "r3c-physical-dots-dropped": [('  local rest="$1/" c="" path=""\n  pw_pd=$1\n', '  local rest="$1/" c="" path=""\n  pw_pd=$1\n  return 0\n')],
    "r3c-basename-dropped": [('    if (t <= nt) { b = base(val[t]); if (isname(b) && tk[t] != canon(b)) { tk[t] = canon(b); val[t] = tk[t] } }\n', "")],
    "r3c-unescape-dropped": [('        if (c == "\\\\") { if (i < n) out = out (mode ? lit(substr(run, i + 1, 1)) : substr(run, i + 1, 1)); i += 2; continue }\n',
                              '        if (c == "\\\\") { out = out c; i++; continue }\n')],
}
FENCE = "test_only_the_roots_the_header_and_the_message_changed"


#: What the named tests need of HEAD (the rest of the tree, the app and the docs, is a sixth of
#: the export's bytes and none of its use).
EXPORTED = ["subfleet", "tests", "tools", "private", "bin", "pyproject.toml"]


def export(destination: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD", *EXPORTED],
                             check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="tar")


def mutate(tree: Path, name: str) -> None:
    file, old, new, _ = MUTATIONS[name]
    path = tree / file
    if name == "trust-hash-stale":                 # the new TRUST, its hook_sha256 left at the old bytes'
        text = path.read_text(encoding="utf-8")
        assert text.count(NEW_HOOK_SHA256) == 1
        path.write_text(text.replace(NEW_HOOK_SHA256, OLD_HOOK_SHA256), encoding="utf-8")
        return
    if name == "trust-built-on-repo-copy":         # the repository's copy at BASE, new hook hash, new sentence
        base = json.loads(subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE}:{TRUST}"],
                                         check=True, capture_output=True, text=True).stdout)
        new = json.loads(path.read_text(encoding="utf-8"))
        base["hook_sha256"] = new["hook_sha256"]
        base["provenance"] += new["provenance"][new["provenance"].index(" 2026-10-24, D-LANE-SCOPE1:"):]
        path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if name in EDITS:
        for old_text, new_text in EDITS[name]:
            assert text.count(old_text) == 1, (name, old_text[:60])
            text = text.replace(old_text, new_text)
    elif name == "env-spellings-dropped":
        assert text.count(ENV_ROOTS) == 1 and text.count(ENV_SED) == 2, (text.count(ENV_ROOTS), text.count(ENV_SED))
        text = text.replace(ENV_ROOTS, "").replace(ENV_SED, "'").replace("/$HOME/g;'", "/$HOME/g'")
    elif name == "trailing-slash-missed":
        assert text.count(NEW_ROOTS) == 1 and text.count(ROOT_CORE) == 1
        text = text.replace(NEW_ROOTS, "").replace(
            ROOT_CORE, ROOT_CORE[:-1] + "|(/Users/[^/[:space:]'\\\"]+|~|\\\\\\$HOME)/\\.subfleet"
                                        "(/[^/[:space:]'\\\"]+)?\"")
    else:
        assert text.count(old) == 1, (name, old)
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def pytest(tree: Path, tests: list[str], scratch: Path) -> dict[str, str]:
    """{test name: passed|failed|error|skipped} from one pytest run in ``tree``."""
    report = scratch / f"{tree.name}.xml"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["TMPDIR"] = str(scratch)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    *[f"{TEST}::{test}" for test in tests]], cwd=tree, env=env, capture_output=True)
    import xml.etree.ElementTree as ElementTree
    outcomes = {}
    rank = ["passed", "skipped", "error", "failed"]
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        # A parametrized test's cases fold into its name: failed if any failed.
        name = case.get("name").split("[", 1)[0]
        outcomes[name] = max(outcomes.get(name, "passed"), outcome, key=rank.index)
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    # r3b's mutants first, then r3's: a run its hold cuts short still reports the newest decisions.
    order = ([name for name in MUTATIONS if name in EDITS][10:] + [name for name in MUTATIONS if name in EDITS][:10]
             + [name for name in MUTATIONS if name not in EDITS])
    parser.add_argument("--only", nargs="*", default=order)
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    ok = True
    try:
        named = sorted({MUTATIONS[name][3] for name in args.only} | {FENCE})
        control = scratch / "control"
        export(control)
        outcome = pytest(control, named, scratch)
        print(json.dumps({"mutant": "control", "outcomes": outcome}), flush=True)
        ok = all(outcome.get(test) == "passed" for test in named)
        for name in args.only:
            tree = scratch / name
            export(tree)
            mutate(tree, name)
            test = MUTATIONS[name][3]
            outcome = pytest(tree, [test, FENCE], scratch)
            killed = outcome.get(test) == "failed"
            ok = ok and killed
            print(json.dumps({"mutant": name, "named_test": test, "killed": killed, "outcomes": outcome}),
                  flush=True)
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
