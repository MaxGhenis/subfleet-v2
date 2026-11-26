"""Apply each D-SCRUB-BOUNDARY1 mutant, run the tests that should kill it, restore the file."""
import subprocess, sys
from pathlib import Path

ROOT = Path(sys.argv[1])
SRC = ROOT / "subfleet/sessions/handoff.py"
T = "tests/unit/test_sessions_handoff_excerpts.py::"
MUTANTS = [
    ("M1 no masks on the bounded path's head side",
     "        text, strip_reminders, count = resolved, False, tally.total\n",
     "        text, strip_reminders, count = resolved, False, tally.total\n        masks = []\n",
     [T + "test_the_stored_example_keeps_only_what_the_whole_scrub_keeps",
      T + "test_a_value_at_an_omission_marker_keeps_only_what_the_whole_scrub_keeps[after-the-marker-budget+1]",
      T + "test_an_excerpt_edge_keeps_no_value_a_data_uri_split[marker-then-value-head-end]"]),
    ("M2 excerpt scrub resolves blocks unguarded",
     "        text = _guarded_blocks(text, strip_reminders, tally)\n    if not multiline:",
     "        text = _resolve_blocks(text, strip_reminders, tally)\n    if not multiline:",
     [T + "test_a_value_at_an_omission_marker_keeps_only_what_the_whole_scrub_keeps[after-the-marker-budget-1]",
      T + "test_no_block_leaves_the_rest_of_a_value_it_splits[data-uri-before-separator-and-quote]",
      T + "test_the_stored_example_keeps_only_what_the_whole_scrub_keeps"]),
    ("M3 no run-on to the next closer",
     "            masks.append((closer.end(), following.end() if following is not None else value_end))\n",
     "            masks.append((closer.end(), value_end))\n            break\n",
     [T + "test_no_block_leaves_the_rest_of_a_value_it_splits[private-key-closer-in-a-value]",
      T + "test_no_block_leaves_the_rest_of_a_value_it_splits[reminder-closer-in-a-value]",
      T + "test_no_block_leaves_the_rest_of_a_value_it_splits[private-key-holds-key-and-quote]"]),
    ("M4 a key split inside its identifier is not read",
     "            word += more.group()                       # the key runs on past the body (`api` `_key`)\n",
     "            pass\n",
     [T + "test_no_block_leaves_the_rest_of_a_value_it_splits[data-uri-inside-the-key]",
      T + "test_no_block_leaves_the_rest_of_a_value_it_splits[data-uri-before-colon]"]),
]
original = SRC.read_text()
try:
    for name, old, new, tests in MUTANTS:
        assert original.count(old) == 1, name
        SRC.write_text(original.replace(old, new))
        print(f"== {name}", flush=True)
        for test in tests:
            r = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "pytest", test, "-q", "-p", "no:cacheprovider",
                                "-W", "ignore", f"--basetemp={sys.argv[2]}/mut", "-x"],
                               cwd=ROOT, capture_output=True, text=True)
            last = [l for l in r.stdout.splitlines() if l.strip()][-1] if r.stdout.strip() else r.stderr[-200:]
            print(f"   {'KILLED' if r.returncode else 'survived'}  {test.split('::')[1]}  ({last[:90]})", flush=True)
finally:
    SRC.write_text(original)
print("restored:", SRC.read_text() == original)
