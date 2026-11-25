"""The guard sync (2026-11-11, the hub's instruction audit of 2026-11-10 and 11).

Two changes, each held to an invariant below:

* The private overlay's ``[keychain-dump]`` block (``private/guard/never-rules-hook.sh``) ended
  "Use 'agent-secret' / 'security find-generic-password -s <item>' for specific secrets." The
  agent-secrets skill bars that bare ``security`` read: it raises a password dialog while the
  agent-secrets keychain stays locked. The block now ends with ``NEW_SENTENCE``. Its logic did not
  change. The Claude hook (``~/.claude/hooks/guard-never-rules.sh``) carries the same block; the
  hub edits it to the same sentence at install, and the repository pins the Claude side as the
  fixtures under ``tests/fixtures/guard/claude-blocks/``: ``keychain-dump.before.sh`` is the
  Claude block as the 2026-09-05 port copied it byte for byte (it equals the block in the original
  reviewed hook a60d1c51..., which ``tests/unit/test_guard_subfleet_root.py`` rebuilds and checks),
  and ``keychain-dump.sh`` is the same block with the one sentence replaced, the text the hub
  installs.
* ``subfleet/hooks.py``'s attached-runner denial named ``--task build|review|research``, three of
  the CLI's eight tasks, and ended with a bare ``subfleet wait <job-id>``. ``attached_denial`` now
  builds the task and tier lists from ``cli.TASK_CHOICES`` and ``cli.TIER_CHOICES`` and tells the
  caller to arm the wait as a background task.
* r2 (2026-11-11): the Claude hook blocks two things the overlay let through, a ``subfleet run``
  into TheAxiomFoundation whose brief names RuleSpec or encoding but carries no ENCODING REGIME
  block (``[axiom-brief-regime]``), and a ``git commit`` in a ``TheAxiomFoundation/rulespec-*``
  checkout that stages rule YAML with no encoder apply manifest (``[hand-authored-rulespec]``).
  The hub cut both blocks from the Claude hook on 2026-11-11; they are pinned here as
  ``claude-blocks/<rule>.sh`` and pasted into the overlay byte for byte, in the Claude hook's order
  (after trust-root, before corpus-squash). The overlay's first prefilter literal is again the
  Claude hook's line (``claude-blocks/prefilter.txt``), whose ``subfleet run`` and ``git … commit``
  terms let the two blocks run.
* Fix r1 (2026-11-15, review r1 of #303): the hand-authored-rulespec block read the index of a folder the
  command names, which makes git run that repository's ``core.fsmonitor`` command on the host; both hooks
  now pin it off right after their input read (``claude-blocks/git-config-pin.txt``). The block read two
  manifests per staged file (about 0.1 s each at load, so 700 files passed the 60 s timeout, which allows);
  one ``cat-file --batch-check`` now reads them all. Both blocks' command parsing missed ``git -c …``,
  ``--no-pager``, ``cd <rs> && git commit``, a relative ``-C`` and ``-p<brief>``; each block now walks the
  command's words, every ``git … commit`` and every ``subfleet run`` in it, from every folder the shell can
  be in, and refuses more than 16 places to check. The supplied blocks and prefilter line are kept as
  ``<name>.before.*``; ``<rule>.sh``, ``prefilter.txt`` and the pin are what the hub installs in the Claude
  hook.
* Fix r2 (2026-11-17, review r2 of #303): the 16-place bound counted path strings, so `cd .; cd ./.; …` made
  16 keys for one checkout, and 16 real checkouts still outlasted the timeout. Both blocks now resolve each
  folder before they use it and check it once; past its first two folders the hand block stops its git
  work at a 15 s budget and denies. It also read git's quoted staged names, so a non-ASCII rule name got through; it now reads them raw
  (``-z``), and with ``--no-relative``. The fix r1 text is kept under ``claude-blocks/fix-r1/`` for the undo
  chain.

Invariants:

I1 (byte identity) Every pinned Claude block (``claude-blocks/<rule>.sh``) appears in the overlay
   exactly once, byte for byte, as the overlay's block for that rule, cut as the overlay header's
   sync contract cuts one: from its column-0 ``# --- <rule>:`` line up to the next column-0
   ``# --- `` or ``# >>>`` line. The pinned "after" keychain block differs from the pinned
   "before" block in that one sentence only, and the overlay's first prefilter literal is the
   Claude hook's prefilter line byte for byte (``DriftTests``).
I2 (same decisions) For every keychain-dump case, the overlay hook and the pinned Claude block,
   run alone under a harness that defines ``block`` as the deny, reach the same decision with the
   same reason; both keep the decisions the old block reached (``test_same_decisions_*``). The
   same holds for both Axiom blocks, read from a Bash payload as the hooks read it, over the
   Claude harness's nine cases and this file's, each in its own temporary git fixture
   (``test_same_decisions_axiom_*``), and, for hand-authored-rulespec, over generated staged-file
   lists checked against an independent oracle (``test_same_decisions_on_generated_staged_files``).
I3 (the denial names every task and tier) For every offender the hook detects, the denial starts
   with the offender, names each ``TASK_CHOICES`` member and each ``TIER_CHOICES`` member exactly
   once after it (as the ``--task`` and ``--tier`` alternations, in the CLI's order), and tells the
   caller to background the wait; the hook still returns 0 (``test_denial_*``, Hypothesis over
   generated offenders).
I4 (order) The overlay's nine Bash rule blocks appear in the Claude hook's order
   (``DriftTests.test_the_overlay_keeps_its_nine_rule_blocks_in_order``), the two Axiom blocks
   adjacent, directly before corpus-squash.
I5 (no configured command runs) Neither hook makes git run a repository's ``core.fsmonitor``: the pin
   sits once in the overlay, after the input read and before any git call, outside every block
   (``DriftTests.test_the_git_config_pin_…``), and a checkout whose fsmonitor writes a marker leaves it
   absent while the decision stands (``test_no_git_call_runs_the_repositorys_fsmonitor``, with git's own
   index read and the unpinned Claude block as controls that write it).
I6 (bounded work) The hand-authored-rulespec block's git processes do not grow with the staged files
   (``test_the_staged_file_count_…``). Both blocks resolve each folder before they use it, so every
   spelling of one checkout or brief is checked once (``test_one_checkout_spelled_…``,
   ``test_one_brief_spelled_…``) and a folder that is not there starts no git process
   (``test_a_folder_that_is_not_there_…``). The hand block checks its first two folders in full and starts
   no git call for a later one once 15 s have passed, then denies
   (``test_the_hand_block_denies_when_its_time_budget_…``); either block refuses more than 64
   paths to resolve, and the brief block more than 16 distinct briefs (``test_too_many_places_…``). Since
   fix r2 the bound is time, not a count of path strings.
I8 (every staged name) The hand-authored-rulespec block reads staged names raw, so a name under the anchor is
   checked with its own bytes, whatever they are, and one it cannot look up is denied
   (``test_same_decisions_axiom_hand_authored_rulespec``, fix r2's rows;
   ``test_a_relative_diff_config_hides_no_staged_file``).
I7 (later rules' inputs) Each rule after the two blocks gives the same denial, reason for reason, when a
   command the blocks run on and allow comes first (``test_later_rules_decide_alike_…``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import resource
import signal
import subprocess
import unittest
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import cli, hooks
from subfleet.guard import preflight as guard

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "private/guard/never-rules-hook.sh"
BLOCKS = REPO / "tests/fixtures/guard/claude-blocks"

OLD_SENTENCE = "Use 'agent-secret' / 'security find-generic-password -s <item>' for specific secrets."
NEW_SENTENCE = ("Read one secret with 'agent-secret get <service>'; 'agent-secret search <query>' finds its "
                "name. A bare 'security find-generic-password' on the agent-secrets keychain raises a "
                "password dialog while that keychain stays locked.")
#: The Claude block as the 2026-09-05 port copied it (and as r12d's overlay, e869dd4c..., still held it).
BEFORE_SHA256 = "85ba8c86856dde8d6d2e081cf20b2bf59c28f15fcd726902660dc964006ce394"
#: The Claude block the hub installs: the same bytes with OLD_SENTENCE replaced by NEW_SENTENCE.
AFTER_SHA256 = "b7e8c83695a309ace6de9acdb34e63b4e23d74480034751749e08512ea80e818"
#: The overlay's Bash rule blocks, in file order (the header's "nine Bash rule blocks"), which is the
#: Claude hook's order: the 2026-09-05 port copied the first seven in it, and the hub's 2026-11-11 cut
#: puts axiom-brief-regime (Claude hook lines 198-218), then hand-authored-rulespec (219-247), between
#: trust-root and corpus-squash.
RULE_BLOCKS = ["tg-getupdates", "keychain-dump", "trust-root", "axiom-brief-regime", "hand-authored-rulespec",
               "corpus-squash", "corpus-push", "stash-shared", "local-main"]
#: The hub's cut of the two Axiom blocks (supply/<rule>.sh, checked before use) and of the Claude hook's
#: prefilter line (hook line 138), on 2026-11-11: since the fix r1 (2026-11-15) the ``.before`` fixtures.
AXIOM_BEFORE_SHA256 = {"axiom-brief-regime": "77002282d3c865f10e1f640595ebcbd9a244a024d97e8df97dba9496941bd571",
                       "hand-authored-rulespec": "f870acdf650677c3fea2856e1b1044668bad043152b9f7273099353c3595558c"}
PREFILTER_BEFORE_SHA256 = "c46d8eec748c649daaae06b0982bb5fc0cf6011918a35144d0ea1de06596781f"
#: The fix r2's blocks (2026-11-17) and the fix r1's prefilter line (2026-11-15), which the hub installs in the
#: Claude hook, and the git config pin both hooks carry after their input read.
AXIOM_SHA256 = {"axiom-brief-regime": "34c9279dbf1226cde2108ce983b1073142c663038a4fa45cda90665a43df42d5",
                "hand-authored-rulespec": "407e2dfae5cfc0447c466738ce2c15a2324ac2a213f9436503a9a775fb61a106"}
#: The fix r1's blocks (2026-11-15), kept byte for byte under ``claude-blocks/fix-r1/`` for the undo chain
#: (tests/unit/test_guard_subfleet_root.py: undoing GS4_TEXT gives the fix r1 hook from this one).
AXIOM_R1_SHA256 = {"axiom-brief-regime": "45775b4dd77fcac19c71c3e341559dcd129ad634e2848b8b74d2380dfe2781d4",
                   "hand-authored-rulespec": "0b6b418cf137ef56bbfc0a92b558f412b1fe4c602f6d11b517792f1aceca969b"}
PREFILTER_SHA256 = "2acb063ed0efa2a3c87b671bbd43fda4bf72436e72372ab8156f349540aff678"
PIN_SHA256 = "f653d463e57190edd5f5405978e0a20d749e5bebced6b8536f241a4fb88b6389"
#: The prefilter's git term, before the fix r1 and after it.
COMMIT_TERM_BEFORE = "git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit"
COMMIT_TERM = "git[[:space:]].*(commit|\\\\$)"

needs_jq = pytest.mark.skipif(not guard._jq_available(),
                              reason="the never-rules hook needs jq (its preflight refuses without it)")


def blocks(text: str) -> dict[str, list[str]]:
    """{rule: [block text, ...]} for every column-0 ``# --- <rule>:`` header, cut by the sync contract."""
    lines = text.splitlines(keepends=True)
    found: dict[str, list[str]] = {}
    for start, line in enumerate(lines):
        head = re.match(r"# --- ([a-z][a-z0-9-]*):", line)
        if not head:
            continue
        end = next((n for n in range(start + 1, len(lines))
                    if lines[n].startswith("# --- ") or lines[n].startswith("# >>>")), len(lines))
        found.setdefault(head[1], []).append("".join(lines[start:end]))
    return found


def pinned() -> dict[str, bytes]:
    """{rule: bytes} for each pinned Claude block the overlay must carry (``<rule>.sh``)."""
    return {path.name[:-3]: path.read_bytes() for path in sorted(BLOCKS.glob("*.sh"))
            if not path.name.endswith(".before.sh")}


class DriftTests(unittest.TestCase):
    """I1: the overlay carries each pinned Claude block byte for byte."""

    def test_every_pinned_block_is_in_the_overlay_byte_for_byte(self):
        overlay = blocks(HOOK.read_text(encoding="utf-8"))
        self.assertTrue(pinned(), "no pinned Claude block")
        for rule, data in pinned().items():
            self.assertEqual(len(overlay.get(rule, [])), 1, rule)
            self.assertEqual(overlay[rule][0].encode("utf-8"), data, rule)

    def test_each_pinned_block_occurs_once_in_the_overlay(self):
        data = HOOK.read_bytes()
        for rule, block in pinned().items():
            self.assertEqual(data.count(block), 1, rule)
        for rule, digest in AXIOM_SHA256.items():
            self.assertEqual(hashlib.sha256((BLOCKS / f"{rule}.sh").read_bytes()).hexdigest(), digest, rule)

    def test_the_fix_r1_blocks_are_kept_byte_for_byte_and_are_not_in_the_overlay(self):
        """Fix r2: ``claude-blocks/fix-r1/<rule>.sh`` is the fix r1's text (the undo chain reads it); ``pinned()``
        reads only ``claude-blocks/*.sh``, so the overlay carries the fix r2's text, not these."""
        data = HOOK.read_bytes()
        for rule, digest in AXIOM_R1_SHA256.items():
            block = (BLOCKS / "fix-r1" / f"{rule}.sh").read_bytes()
            self.assertEqual(hashlib.sha256(block).hexdigest(), digest, rule)
            self.assertEqual(data.count(block), 0, rule)
        self.assertEqual(sorted(p.name for p in (BLOCKS / "fix-r1").iterdir()),
                         ["axiom-brief-regime.sh", "hand-authored-rulespec.sh"])

    def test_the_overlay_keeps_its_nine_rule_blocks_in_order(self):
        text = HOOK.read_text(encoding="utf-8")
        region = text[:text.index("\n# >>> unscoped-search: shared region begin")]
        heads = re.findall(r"^# --- ([a-z][a-z0-9-]*):", region, flags=re.M)
        self.assertEqual(heads, RULE_BLOCKS)
        # The Claude hook's lines 198-247: the two Axiom blocks back to back, then corpus-squash.
        pair = b"".join((BLOCKS / f"{rule}.sh").read_bytes() for rule in AXIOM_SHA256)
        self.assertEqual(HOOK.read_bytes().count(pair + b"# --- corpus-squash:"), 1)

    def test_the_first_prefilter_literal_is_the_claude_hooks_line(self):
        """A block whose trigger word misses the prefilter never runs, so the first literal is the
        Claude hook's prefilter line byte for byte: the overlay's line is that line with its
        ``|| exit 0`` replaced by the continuation into the codex additions."""
        line = (BLOCKS / "prefilter.txt").read_bytes()
        self.assertEqual(hashlib.sha256(line).hexdigest(), PREFILTER_SHA256)
        claude = line.decode("utf-8")
        self.assertTrue(claude.endswith("' || exit 0\n"))
        text = HOOK.read_text(encoding="utf-8")
        branch = text[text.index("\n# ---------- Bash branch ----------\n"):]
        first = next(row for row in branch.splitlines() if not row.startswith("#") and row.strip())
        self.assertEqual(first + "\n", claude[:-len(" || exit 0\n")] + " \\\n")
        self.assertEqual(text.count(claude[:-len(" || exit 0\n")]), 1)
        for term in ("subfleet[[:space:]]+run", COMMIT_TERM):
            self.assertIn("|" + term + "|", first)

    def test_the_fix_changed_only_the_blocks_parsing_and_the_prefilters_git_term(self):
        """The fix r1's Claude-side text against the hub's 2026-11-11 cut: the ``.before`` fixtures hash to
        the supplied bytes; each new block keeps its header line, the old block's comment lines and its
        denial text; the new prefilter line is the old one with only the git term widened."""
        for rule, digest in AXIOM_BEFORE_SHA256.items():
            before = (BLOCKS / f"{rule}.before.sh").read_text(encoding="utf-8")
            self.assertEqual(hashlib.sha256(before.encode()).hexdigest(), digest, rule)
            after = (BLOCKS / f"{rule}.sh").read_text(encoding="utf-8")
            old_lines, new_lines = before.splitlines(), [line.strip() for line in after.splitlines()]
            self.assertEqual(after.splitlines()[0], old_lines[0])
            for line in old_lines:
                if line.startswith("#") or line.lstrip().startswith("block "):
                    self.assertEqual(new_lines.count(line.strip()), 1, (rule, line))
        before = (BLOCKS / "prefilter.before.txt").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(before.encode()).hexdigest(), PREFILTER_BEFORE_SHA256)
        self.assertEqual(before.count("|" + COMMIT_TERM_BEFORE + "|"), 1)
        self.assertEqual(before.replace(COMMIT_TERM_BEFORE, COMMIT_TERM),
                         (BLOCKS / "prefilter.txt").read_text(encoding="utf-8"))

    def test_the_git_config_pin_precedes_every_git_call_outside_the_blocks(self):
        """I5: the pin is one line, once in the overlay, right after the input read (after its comment), before
        any git call, and in no rule block, so neither block's bytes carry it."""
        pin = (BLOCKS / "git-config-pin.txt").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(pin.encode()).hexdigest(), PIN_SHA256)
        self.assertEqual(pin, PIN_LINE)
        text = HOOK.read_text(encoding="utf-8")
        self.assertEqual(text.count(pin), 1)
        at = text.index(pin)
        read = text.index('\ninput=$(cat 2>/dev/null) || input=""\n')
        between = text[read + 1:at].splitlines()[1:]
        self.assertTrue(between and all(line.startswith("# ") for line in between), between)
        code = [(n, line) for n, line in enumerate(text.splitlines(keepends=True)) if not line.lstrip().startswith("#")]
        first_git = next(n for n, line in code if re.search(r"(^|[\s($])git\s", line))
        self.assertLess(text[:at].count("\n"), first_git)
        for found in blocks(text).values():
            for block_text in found:
                self.assertNotIn("GIT_CONFIG", block_text)

    def test_the_pinned_blocks_differ_by_the_one_sentence(self):
        before = (BLOCKS / "keychain-dump.before.sh").read_bytes()
        after = (BLOCKS / "keychain-dump.sh").read_bytes()
        self.assertEqual(hashlib.sha256(before).hexdigest(), BEFORE_SHA256)
        self.assertEqual(hashlib.sha256(after).hexdigest(), AFTER_SHA256)
        self.assertEqual(before.count(OLD_SENTENCE.encode()), 1)
        self.assertEqual(before.replace(OLD_SENTENCE.encode(), NEW_SENTENCE.encode()), after)
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))

    def test_the_old_sentence_is_nowhere_in_the_overlay(self):
        text = HOOK.read_text(encoding="utf-8")
        self.assertNotIn(OLD_SENTENCE, text)
        self.assertNotIn("security find-generic-password -s <item>", text)
        self.assertEqual(text.count(NEW_SENTENCE), 1)


# ---------------------------------------------------------------- I2: same decisions

#: (command, denied) for the keychain-dump rule: the harness's own cases (tests/unit/
#: test_guard_subfleet_root.py OTHER_RULES) first, then this file's.
KEYCHAIN_CASES = [
    ("security dump-keychain -d", True),
    ("security find-generic-password -s x", False),
    ("security dump-keychain -d login.keychain-db", True),
    ("security dump-keychain -d ~/Library/Keychains/login.keychain-db", True),
    ("/usr/bin/security dump-keychain -a -d", True),
    ("security dump-keychain", False),
    ("security dump-keychain -d ~/Library/Keychains/agent-secrets.keychain-db", False),
    ("security dump-keychain -dx", False),
    ("echo dump-keychain -d is the rule's name", True),
    ("security dump-keychain -d; echo done", False),
]

#: A harness that runs one pinned block alone on the command it reads from stdin, as the hook reads
#: its payload: ``block`` prints its reason and exits, as the hooks' own ``block`` denies; reaching
#: the end allows. The launch keeps the session's PATH, whose stand-in folder the test session's
#: keychain exec guard (C-20.6) requires before the system folders; the script then reads
#: /usr/bin:/bin first, as the hook does.
HARNESS = """set -u
export PATH=/usr/bin:/bin
command=$(cat)
block() { printf '%s' "$1"; exit 0; }
"""


def claude_reason(block_text: str, command: str, tmp: Path) -> str:
    """The reason the pinned block alone gives for ``command`` (empty: allowed)."""
    script = tmp / f"harness-{hashlib.sha256(block_text.encode()).hexdigest()[:12]}.sh"
    script.write_text(HARNESS + block_text + "exit 0\n", encoding="utf-8")
    out = subprocess.run(["/bin/bash", str(script)], input=command, capture_output=True, text=True,
                         timeout=30, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, cwd=tmp)
    assert out.returncode == 0, out.stderr
    return out.stdout


def overlay_reason(command: str, tmp: Path) -> str:
    """The reason the overlay hook gives for ``command`` as a Bash payload (empty: allowed). The
    hook judges paths under an empty fixture root, so no real path is read."""
    (tmp / "fs").mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home"),
           "CODEX_GUARD_LOG": os.devnull, "CODEX_GUARD_TEST_FSROOT": str(tmp / "fs")}
    payload = json.dumps({"tool_name": "Bash", "cwd": str(tmp), "tool_input": {"command": command}})
    out = subprocess.run(["/bin/bash", str(HOOK)], input=payload, capture_output=True, text=True,
                         env=env, timeout=120, cwd=tmp)
    assert out.returncode == 0, out.stderr
    if not out.stdout:
        return ""
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


@needs_jq
@pytest.mark.parametrize("command,denied", KEYCHAIN_CASES)
def test_same_decisions_overlay_and_pinned_claude_block(command, denied, tmp_path):
    after = (BLOCKS / "keychain-dump.sh").read_text(encoding="utf-8")
    before = (BLOCKS / "keychain-dump.before.sh").read_text(encoding="utf-8")
    claude, overlay = claude_reason(after, command, tmp_path), overlay_reason(command, tmp_path)
    old = claude_reason(before, command, tmp_path)
    assert bool(claude) == bool(overlay) == bool(old) == denied, (command, claude, overlay, old)
    assert claude == overlay, command
    assert claude == old.replace(OLD_SENTENCE, NEW_SENTENCE), command
    if denied:
        assert claude.startswith("[keychain-dump] ") and claude.endswith(NEW_SENTENCE)


WORDS = ["security", "dump-keychain", "-d", "-a", "-dx", "login.keychain-db", "agent-secrets.keychain-db",
         "x.keychain", "find-generic-password", "-s", "item", "echo", ";", "&&", "|", "cat"]


@needs_jq
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.sampled_from(WORDS), min_size=1, max_size=7))
def test_same_decisions_on_generated_keychain_commands(tmp_path, words):
    command = " ".join(words)
    after = (BLOCKS / "keychain-dump.sh").read_text(encoding="utf-8")
    assert claude_reason(after, command, tmp_path) == overlay_reason(command, tmp_path), command


# ---------------------------------------------------------------- I2: the two Axiom blocks (r2)

#: HARNESS for a block that reads the cwd: it takes the hooks' Bash payload on stdin and reads the
#: command and the cwd from it as both hooks do (the overlay at its jq lines; the Claude harness's
#: ``run_bash`` builds the same payload). HOME is the launch's, as in a hook. Right after the input
#: read comes the git config pin (``claude-blocks/git-config-pin.txt``), where the hub puts it in the
#: Claude hook and where the overlay has it (fix r1); ``payload_harness(pin=False)`` is the Claude hook
#: before that edit.
PAYLOAD_HARNESS_HEAD = """set -u
export PATH=/usr/bin:/bin
input=$(cat)
"""
PAYLOAD_HARNESS_TAIL = """command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
block() { printf '%s' "$1"; exit 0; }
"""
#: The pin both hooks carry after their input read (fix r1): reading an index makes git run the
#: repository's core.fsmonitor command, and the hooks run on the host, outside any sandbox.
PIN_LINE = "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false\n"


def payload_harness(pin: bool = True) -> str:
    return PAYLOAD_HARNESS_HEAD + ((BLOCKS / "git-config-pin.txt").read_text(encoding="utf-8") if pin else "") \
        + PAYLOAD_HARNESS_TAIL
YAML = "format: rulespec/v1\n"
SECTION = "il/statutes/income-tax-ordinance/section-121"
ORIGIN = "https://github.com/TheAxiomFoundation/rulespec-il.git"
HAND_PREFIX = "[hand-authored-rulespec] Staged RuleSpec content with no encoder apply manifest: "
BRIEF_REASON = ("[axiom-brief-regime] This brief touches RuleSpec/encoding but has no ENCODING REGIME block. "
                "Modules come only from 'axiom-encode encode --apply' with apply manifests;")


def payload(command: str, cwd: Path) -> str:
    return json.dumps({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}})


class AxiomFixture:
    """The Claude harness's fixtures (its lines 15-42) under a temporary HOME: ``plain`` (one empty
    commit, no origin), ``rulespec`` (origin TheAxiomFoundation/rulespec-il, the harness's folders),
    and ``briefs``; also ``~/TheAxiomFoundation`` for the cwd and ``-C`` forms. Every path the blocks
    read is under this root: ``~`` is its ``home``, so ``-C ~/TheAxiomFoundation`` names no real
    folder, and the blocks only test that path's text."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.plain, self.rulespec, self.briefs = self.home / "plain", self.home / "rulespec", self.home / "briefs"
        self.axiom = self.home / "TheAxiomFoundation"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        for folder in (self.plain, self.briefs, self.axiom,
                       self.rulespec / "il/statutes/income-tax-ordinance", self.rulespec / "il/statutes/composed",
                       self.rulespec / ".axiom/encoding-manifests/il/statutes/income-tax-ordinance"):
            folder.mkdir(parents=True, exist_ok=True)
        for repo in (self.plain, self.rulespec):
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        self.git(self.rulespec, "remote", "add", "origin", ORIGIN)
        briefs = {"no-regime.md": "Encode rulespec-il modules from the corpus.\n",
                  "with-regime.md": "ENCODING REGIME: modules come only from axiom-encode encode --apply.\n"
                                    "Encode rulespec-il modules.\n",
                  "site-only.md": "Update the axiom.org landing page tests.\n",
                  "lower-encoding.md": "Fix the encoding of the landing page footer.\n",
                  "mixed-regime.md": "Encoding regime: modules come only from the encoder.\nEncode rulespec-il.\n"}
        for name, text in briefs.items():
            (self.briefs / name).write_text(text, encoding="utf-8")
        (self.axiom / "no-regime.md").write_text(briefs["no-regime.md"], encoding="utf-8")
        (self.home / "no-regime.md").write_text(briefs["no-regime.md"], encoding="utf-8")

    def git(self, repo: Path, *args: str, stdin: str | None = None) -> str:
        out = subprocess.run(["git", "-C", str(repo), *args], env=self.env, input=stdin, capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, (args, out.stderr)
        return out.stdout

    def stage(self, repo: Path, files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if files:
            self.git(repo, "add", "--", *files)

    def claude(self, rule: str, command: str, cwd: Path, pin: bool = True, env: dict | None = None) -> str:
        """The pinned Claude block alone (empty: allowed). ``env`` adds to the launch's environment."""
        script = self.root / f"harness-{rule}{'' if pin else '-unpinned'}.sh"
        if not script.exists():
            script.write_text(payload_harness(pin) + (BLOCKS / f"{rule}.sh").read_text(encoding="utf-8")
                              + "exit 0\n", encoding="utf-8")
        out = subprocess.run(["/bin/bash", str(script)], input=payload(command, cwd), capture_output=True,
                             text=True, timeout=120,
                             env={"PATH": self.env["PATH"], "HOME": str(self.home), **(env or {})}, cwd=self.root)
        assert out.returncode == 0, out.stderr
        return out.stdout

    def overlay(self, command: str, cwd: Path, env: dict | None = None) -> str:
        """The overlay hook's reason for the Bash payload (empty: allowed)."""
        (self.root / "fs").mkdir(exist_ok=True)
        env = {"PATH": self.env["PATH"], "HOME": str(self.home), "CODEX_GUARD_LOG": os.devnull,
               "CODEX_GUARD_TEST_FSROOT": str(self.root / "fs"), **(env or {})}
        out = subprocess.run(["/bin/bash", str(HOOK)], input=payload(command, cwd), capture_output=True, text=True,
                             env=env, timeout=120, cwd=self.root)
        assert out.returncode == 0, out.stderr
        if not out.stdout:
            return ""
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        return decision["permissionDecisionReason"]

    def same(self, rule: str, command: str, cwd: Path) -> str:
        """I2 for one case: both sides give the same reason; returns it."""
        claude, overlay = self.claude(rule, command, cwd), self.overlay(command, cwd)
        assert claude == overlay, (command, claude, overlay)
        return claude


#: (name, command, cwd, denied) for axiom-brief-regime: the Claude harness's four (its lines 384-387,
#: {briefs} for $FIX/briefs, cwd $FIX/plain), then this file's.
BRIEF_CASES = [
    ("brief w/o regime into Axiom blocked", "subfleet run --task build --tier standard -C ~/TheAxiomFoundation "
     "-n x -p {briefs}/no-regime.md -o /tmp/out.md", "plain", True),
    ("brief with regime allowed", "subfleet run --task build --tier standard -C ~/TheAxiomFoundation "
     "-p {briefs}/with-regime.md -o /tmp/out.md", "plain", False),
    ("brief not about rulespec allowed", "subfleet run --task build --tier standard -C ~/TheAxiomFoundation "
     "-p {briefs}/site-only.md -o /tmp/out.md", "plain", False),
    ("brief w/o regime outside Axiom allowed", "subfleet run --task build --tier standard -C ~/PolicyEngine "
     "-p {briefs}/no-regime.md -o /tmp/out.md", "plain", False),
    # r2: a brief that names encoding only in lower case (the block's grep is -i over 'encod')
    ("lower-case encoding only blocked", "subfleet run --task build --tier easy -C ~/TheAxiomFoundation "
     "-p {briefs}/lower-encoding.md -o out.md", "plain", True),
    # the regime check is case-sensitive: 'Encoding regime' is not the block
    ("regime spelled in another case blocked", "subfleet run --task build -C ~/TheAxiomFoundation "
     "-p {briefs}/mixed-regime.md -o out.md", "plain", True),
    # a relative -p is read against the payload cwd, never against -C: the brief sits in -C, not in the
    # cwd, so neither side reads it (a gap both share)
    ("brief path relative to -C allowed", "subfleet run --task build --tier standard -C ~/TheAxiomFoundation "
     "-p no-regime.md -o out.md", "plain", False),
    ("relative brief path found in the cwd blocked", "subfleet run --task build -C ~/TheAxiomFoundation "
     "-p no-regime.md -o out.md", "briefs", True),
    # no -p: no brief file to read (a prompt on stdin is not judged)
    ("subfleet run with no -p allowed", "subfleet run --task build --tier standard -C ~/TheAxiomFoundation -n x "
     "-o /tmp/out.md < {briefs}/no-regime.md", "plain", False),
    # no -C: the payload cwd is the workdir
    ("cwd inside TheAxiomFoundation, no -C, blocked", "subfleet run --task build -p no-regime.md -o out.md",
     "axiom", True),
    ("-p ~/... brief blocked", "subfleet run --task build -C ~/TheAxiomFoundation -p ~/no-regime.md -o out.md",
     "plain", True),
    ("a brief that does not exist allowed", "subfleet run --task build -C ~/TheAxiomFoundation "
     "-p {briefs}/missing.md -o out.md", "plain", False),
    # fix r1 (2026-11-15): a cd before the run moves the folder a relative -p is read from (it was a gap
    # both sides shared: the payload cwd alone was read)
    ("cd then subfleet run reads the folder cd names", "cd {briefs} && subfleet run --task build "
     "-C ~/TheAxiomFoundation -p no-regime.md", "plain", True),
    ("-p attached to the path blocked", "subfleet run --task build -C ~/TheAxiomFoundation "
     "-p{briefs}/no-regime.md -o out.md", "plain", True),
    ("-p=path blocked", "subfleet run --task build -C ~/TheAxiomFoundation -p={briefs}/no-regime.md", "plain", True),
    ("-C attached to the path blocked", "subfleet run --task build -C{axiom} -p {briefs}/no-regime.md", "plain", True),
    ("a relative -C resolves against the cwd", "subfleet run --task build -C . -p no-regime.md -o out.md", "axiom",
     True),
    ("every subfleet run is judged, not only the last", "subfleet run --task build -C ~/TheAxiomFoundation "
     "-p {briefs}/no-regime.md -o a.md; subfleet run --task build -C ~/PolicyEngine -p {briefs}/site-only.md",
     "plain", True),
    ("every -p is read, not only the first", "subfleet run --task build -C ~/TheAxiomFoundation "
     "-p {briefs}/with-regime.md -p {briefs}/no-regime.md", "plain", True),
    ("a line continuation inside the run blocked", "subfleet run --task build \\\n  -C ~/TheAxiomFoundation "
     "\\\n  -p {briefs}/no-regime.md", "plain", True),
    ("a run inside bash -c blocked", "bash -c 'cd {briefs} && subfleet run -C ~/TheAxiomFoundation -p no-regime.md'",
     "plain", True),
    # the run's words end at the command's end: a -p in a later command is not its brief (the old block read
    # the first -p anywhere after the last `subfleet run`, and blocked this)
    ("a -p in a later command is not the run's", "subfleet run --task build -C ~/TheAxiomFoundation -o out.md; "
     "echo -p {briefs}/no-regime.md", "plain", False),
]


@needs_jq
@pytest.mark.parametrize("name,command,cwd,denied", BRIEF_CASES, ids=[case[0] for case in BRIEF_CASES])
def test_same_decisions_axiom_brief_regime(name, command, cwd, denied, tmp_path):
    fix = AxiomFixture(tmp_path)
    reason = fix.same("axiom-brief-regime", command.format(briefs=fix.briefs, axiom=fix.axiom), getattr(fix, cwd))
    assert bool(reason) == denied, (name, reason)
    if denied:
        assert reason.startswith(BRIEF_REASON), reason


#: (name, {repo: staged files}, {committed manifests}, command, cwd, missing) for hand-authored-rulespec:
#: the Claude harness's five (its lines 389-402, each from a fresh fixture where the harness reuses one;
#: {rulespec} for $FIX/rulespec), then this file's. ``missing`` is the files the denial names, in order.
HAND_CASES = [
    ("staged rule YAML w/o manifest blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     'git commit -m "add section 121"', "rulespec", [f"{SECTION}.yaml"]),
    ("git -C form w/o manifest blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C {rulespec} commit -m x", "plain", [f"{SECTION}.yaml"]),
    ("staged rule YAML with manifest allowed",
     {"rulespec": {f"{SECTION}.yaml": YAML, f".axiom/encoding-manifests/{SECTION}.json": "{}"}}, {},
     'git commit -m "add section 121"', "rulespec", []),
    ("companion test alone allowed", {"rulespec": {f"{SECTION}.test.yaml": "- name: t\n"}}, {},
     'git commit -m "tests"', "rulespec", []),
    ("composed pipeline allowed", {"rulespec": {"il/statutes/composed/pipeline.yaml": YAML}}, {},
     'git commit -m "composed"', "rulespec", []),
    # r2
    ("git -C ~/... form blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C ~/rulespec commit -m x", "plain", [f"{SECTION}.yaml"]),
    (".yml file blocked", {"rulespec": {"il/statutes/income-tax-ordinance/section-7.yml": YAML}}, {},
     "git commit -m yml", "rulespec", ["il/statutes/income-tax-ordinance/section-7.yml"]),
    ("regulations/ path blocked", {"rulespec": {"us-ca/regulations/title-18/reg-17951.yaml": YAML}}, {},
     "git commit -m regs", "rulespec", ["us-ca/regulations/title-18/reg-17951.yaml"]),
    ("a manifest in HEAD allowed", {"rulespec": {f"{SECTION}.yaml": YAML}},
     {f".axiom/encoding-manifests/{SECTION}.json": "{}"}, "git commit -m x", "rulespec", []),
    ("only the files without a manifest are named",
     {"rulespec": {f"{SECTION}.yaml": YAML, f".axiom/encoding-manifests/{SECTION}.json": "{}",
                   "il/policies/p-1.yaml": YAML, "il/programs/q-2.yml": YAML}}, {},
     "git commit -m x", "rulespec", ["il/policies/p-1.yaml", "il/programs/q-2.yml"]),
    ("rule YAML in a checkout with no rulespec origin allowed", {"plain": {f"{SECTION}.yaml": YAML}}, {},
     "git commit -m x", "plain", []),
    ("git -C to a missing folder allowed", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C ~/nowhere commit -m x", "rulespec", []),
    ("paths outside <cc>/<kind>/ allowed",
     {"rulespec": {"docs/statutes/x.yaml": YAML, "il/notes/x.yaml": YAML, "usa/statutes/x.yaml": YAML,
                   ".axiom/encoding-manifests/il/statutes/y.json": "{}"}}, {},
     "git commit -m x", "rulespec", []),
    # the hook judges the index before the command runs: an add in the same command is not seen yet
    ("add and commit in one command allowed", {}, {}, f"git add {SECTION}.yaml && git commit -m x", "rulespec", []),
    ("git -C then commit after other words blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "cd /tmp; git -C {rulespec} commit -q -m x", "plain", [f"{SECTION}.yaml"]),
    # fix r1 (2026-11-15): the review's five forms the block's parser missed (both sides allowed each), then
    # the other spellings its parser now reads
    ("git -c before commit blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -c core.quotepath=off commit -m x", "rulespec", [f"{SECTION}.yaml"]),
    ("git --no-pager before commit blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git --no-pager commit -m x", "rulespec", [f"{SECTION}.yaml"]),
    ("git -C then -c before commit blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C {rulespec} -c a=b commit -m y", "plain", [f"{SECTION}.yaml"]),
    ("cd into the checkout then commit blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "cd {rulespec} && git commit -m x", "plain", [f"{SECTION}.yaml"]),
    ("a relative -C resolves against the payload cwd", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C rulespec commit -m x", "home", [f"{SECTION}.yaml"]),
    ("a relative cd resolves against the payload cwd", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "cd rulespec; git commit -m x", "home", [f"{SECTION}.yaml"]),
    ("git -p and a path to git blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "/usr/bin/git -p -C {rulespec} commit -m x", "plain", [f"{SECTION}.yaml"]),
    ("--git-dir names the checkout blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git --git-dir={rulespec}/.git commit -m x", "plain", [f"{SECTION}.yaml"]),
    ("GIT_DIR names the checkout blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "GIT_DIR={rulespec}/.git git commit -m x", "plain", [f"{SECTION}.yaml"]),
    ("every commit in the command is judged", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C {plain} commit -m a && git -C {rulespec} commit -m b", "plain", [f"{SECTION}.yaml"]),
    ("a commit inside bash -c blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "bash -c \"cd {rulespec} && git commit -m x\"", "plain", [f"{SECTION}.yaml"]),
    ("a line continuation before commit blocked", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git \\\n  -C {rulespec} \\\n  commit -m x", "plain", [f"{SECTION}.yaml"]),
    # a subshell's cd still counts: the block cannot tell where a subshell ends, so it checks every folder
    # the command may be in (an over-approximation; it errs toward blocking)
    ("a subshell's cd still counts", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "(cd {rulespec} && true); git commit -m x", "plain", [f"{SECTION}.yaml"]),
    ("git log naming commit is not a commit", {"rulespec": {f"{SECTION}.yaml": YAML}}, {},
     "git -C {rulespec} log --grep commit", "plain", []),
    # fix r2 (2026-11-17, review r2 of #303): the block read git's quoted names, and git quotes a name holding a
    # byte outside printable ASCII, a double quote, a backslash or a control character (the last three even
    # with core.quotepath=off); the anchor never matched the leading quote, so both sides and the hub's cut
    # allowed each. The ASCII row is the control. The block now reads the names raw (-z): every name under the
    # anchor is checked with its own bytes, and a name holding a newline, which the line-based lookup cannot
    # carry, is denied with or without its manifest. The denial shows a control byte as ?.
    ("an ASCII rule name w/o manifest blocked", {"rulespec": {"il/statutes/s-1.yaml": YAML}}, {},
     "git commit -m x", "rulespec", ["il/statutes/s-1.yaml"]),
    ("a non-ASCII rule name w/o manifest blocked", {"rulespec": {"il/statutes/sécti-2.yaml": YAML}}, {},
     "git commit -m x", "rulespec", ["il/statutes/sécti-2.yaml"]),
    ("a non-ASCII rule name with its manifest allowed",
     {"rulespec": {"il/statutes/sécti-2.yaml": YAML, ".axiom/encoding-manifests/il/statutes/sécti-2.json": "{}"}},
     {}, "git commit -m x", "rulespec", []),
    ("a double quote in the name w/o manifest blocked", {"rulespec": {'il/statutes/a"b.yaml': YAML}}, {},
     "git commit -m x", "rulespec", ['il/statutes/a"b.yaml']),
    ("a double quote in the name with its manifest allowed",
     {"rulespec": {'il/statutes/a"b.yaml': YAML, '.axiom/encoding-manifests/il/statutes/a"b.json': "{}"}}, {},
     "git commit -m x", "rulespec", []),
    ("a backslash in the name w/o manifest blocked", {"rulespec": {"il/statutes/a\\b.yaml": YAML}}, {},
     "git commit -m x", "rulespec", ["il/statutes/a\\b.yaml"]),
    ("a tab in the name w/o manifest blocked", {"rulespec": {"il/statutes/a\tb.yaml": YAML}}, {},
     "git commit -m x", "rulespec", ["il/statutes/a?b.yaml"]),
    ("a tab in the name with its manifest allowed",
     {"rulespec": {"il/statutes/a\tb.yaml": YAML, ".axiom/encoding-manifests/il/statutes/a\tb.json": "{}"}}, {},
     "git commit -m x", "rulespec", []),
    ("a newline in the name blocked, its manifest staged",
     {"rulespec": {"il/statutes/x\ny.yaml": YAML, ".axiom/encoding-manifests/il/statutes/x\ny.json": "{}"}}, {},
     "git commit -m x", "rulespec", ["il/statutes/x?y.yaml"]),
    # the lookup carries a newline as \001, so a manifest named with \001 in its place must not let it through
    ("a newline in the name blocked, a \\001 manifest staged",
     {"rulespec": {"il/statutes/x\ny.yaml": YAML, ".axiom/encoding-manifests/il/statutes/x\x01y.json": "{}"}}, {},
     "git commit -m x", "rulespec", ["il/statutes/x?y.yaml"]),
    ("a non-ASCII name through git -C blocked", {"rulespec": {"il/statutes/sécti-2.yaml": YAML}}, {},
     "git -C {rulespec} commit -m x", "plain", ["il/statutes/sécti-2.yaml"]),
]


@needs_jq
@pytest.mark.parametrize("name,staged,head,command,cwd,missing", HAND_CASES, ids=[case[0] for case in HAND_CASES])
def test_same_decisions_axiom_hand_authored_rulespec(name, staged, head, command, cwd, missing, tmp_path):
    fix = AxiomFixture(tmp_path)
    if head:
        fix.stage(fix.rulespec, head)
        fix.git(fix.rulespec, "commit", "-q", "-m", "manifests")
    (fix.rulespec / f"{SECTION}.yaml").write_text(YAML, encoding="utf-8")  # on disk, staged only by a case
    for repo, files in staged.items():
        fix.stage(getattr(fix, repo), files)
    reason = fix.same("hand-authored-rulespec", command.format(rulespec=fix.rulespec, plain=fix.plain),
                      getattr(fix, cwd))
    assert bool(reason) == bool(missing), (name, reason)
    if missing:
        assert reason.startswith(HAND_PREFIX + " ".join(missing) + ". RuleSpec modules come only from "), reason


#: The block's own path rule, restated: a rule file is <cc>/(statutes|...|programs)/<rest>.ya?ml.
RULE_PATH = re.compile(r"^[a-z]{2}(-[a-z0-9-]+)?/(statutes|regulations|policies|legislation|programs)/.+\.ya?ml$")
#: A manifest committed in the property fixture's HEAD.
TRACKED = "il/statutes/tracked/s-1"


def oracle_missing(staged: set[str], head: set[str]) -> list[str]:
    """An independent statement of the rule: each staged rule file, in git's (byte) order, that is not a
    companion test, not under a composed/ folder and not under .axiom/, whose manifest
    ``.axiom/encoding-manifests/<path without its extension>.json`` is neither staged nor in HEAD."""
    missing = []
    for path in sorted(staged):
        if fnmatch.fnmatchcase(path, "*.test.yaml") or fnmatch.fnmatchcase(path, "*/composed/*") \
                or path.startswith(".axiom/") or not RULE_PATH.match(path):
            continue
        manifest = ".axiom/encoding-manifests/" + path.rsplit(".", 1)[0] + ".json"
        if manifest not in staged and manifest not in head:
            missing.append(path)
    return missing


@pytest.fixture(scope="module")
def staged_fixture(tmp_path_factory) -> AxiomFixture:
    fix = AxiomFixture(tmp_path_factory.mktemp("staged"))
    fix.stage(fix.rulespec, {f".axiom/encoding-manifests/{TRACKED}.json": "{}"})
    fix.git(fix.rulespec, "commit", "-q", "-m", "tracked manifest")
    return fix


RULE_FILES = st.builds(
    lambda cc, kind, stem, ext: f"{cc}/{kind}/{stem}{ext}",
    st.sampled_from(["il", "us", "us-ca", "uk", "ab-x1", "usa", "IL", "docs"]),
    st.sampled_from(["statutes", "regulations", "policies", "legislation", "programs", "notes"]),
    st.sampled_from(["s-1", "section-121", "a/b/s-2", "composed/pipeline", "tracked/s-1", "x.v2/s-3"]),
    st.sampled_from([".yaml", ".yml", ".test.yaml", ".json", ".md", ".YAML"]))
OTHER_FILES = st.sampled_from([".axiom/encoding-manifests/il/statutes/s-1.json", ".axiom/notes.md", "README.md",
                               "composed/top.yaml", "il/statutes/composed/p.yaml"])
STAGED = st.lists(st.tuples(st.one_of(RULE_FILES, OTHER_FILES), st.booleans()), max_size=6,
                  unique_by=lambda entry: entry[0])


@needs_jq
@settings(max_examples=40, deadline=None, derandomize=True)
@given(STAGED, st.sampled_from(["cwd", "-C", "-C ~", "-c", "cd", "relative -C"]))
def test_same_decisions_on_generated_staged_files(staged_fixture, entries, form):
    """I2 over generated staged-file lists: rule paths under <cc>/(statutes|regulations|policies|
    legislation|programs)/ and near misses, companion tests, composed/ and .axiom/ paths, each with or
    without its manifest staged (and one manifest in HEAD). Both sides give the oracle's decision and
    name the oracle's files, in its order."""
    fix = staged_fixture
    staged = set()
    for path, with_manifest in entries:
        staged.add(path)
        if with_manifest and "." in path:
            staged.add(".axiom/encoding-manifests/" + path.rsplit(".", 1)[0] + ".json")
    blob = fix.git(fix.rulespec, "hash-object", "-w", "--stdin", stdin=YAML).strip()
    fix.git(fix.rulespec, "read-tree", "HEAD")
    if staged:
        fix.git(fix.rulespec, "update-index", "--index-info",
                stdin="".join(f"100644 {blob}\t{path}\n" for path in sorted(staged)))
    command, cwd = {"cwd": ("git commit -m x", fix.rulespec),
                    "-C": (f"git -C {fix.rulespec} commit -m x", fix.plain),
                    "-C ~": ("git -C ~/rulespec commit -m x", fix.plain),
                    "-c": ("git -c a=b --no-pager commit -m x", fix.rulespec),
                    "cd": (f"cd {fix.rulespec} && git commit -m x", fix.plain),
                    "relative -C": ("git -C rulespec commit -m x", fix.home)}[form]
    missing = oracle_missing(staged, {f".axiom/encoding-manifests/{TRACKED}.json"})
    reason = fix.same("hand-authored-rulespec", command, cwd)
    if missing:
        assert reason.startswith(HAND_PREFIX + " ".join(missing) + ". "), (sorted(staged), reason)
    else:
        assert reason == "", (sorted(staged), reason)


# ---------------------------------------------------------------- I5-I7: fix r1 (2026-11-15)

FSMONITOR = "#!/bin/sh\nprintf 'ran\\n' >> '{marker}'\nexit 0\n"
#: (name, command, cwd, what is staged, denied): the review's two payloads, the same with nothing staged, and one
#: that reaches every rule that runs git, which the staged manifest lets past the hand-authored-rulespec block.
FSMONITOR_CASES = [
    ("git -C names the checkout", "git -C {rulespec} commit -m x", "plain", "rule", True),
    ("the payload cwd is the checkout", "git commit -m x", "rulespec", "rule", True),
    ("nothing staged", "git commit -m x", "rulespec", "", False),
    # corpus-squash's git call is left out: in a rulespec checkout it denies (remote get-url reads no index)
    ("the rules that run git", "git -C {rulespec} commit -m x && git stash && git checkout -b f && git push",
     "rulespec", "rule and manifest", False),
]


@needs_jq
@pytest.mark.parametrize("name,command,cwd,staged,denied", FSMONITOR_CASES, ids=[c[0] for c in FSMONITOR_CASES])
def test_no_git_call_runs_the_repositorys_fsmonitor(name, command, cwd, staged, denied, tmp_path):
    """I5: neither hook makes git run a repository's configured core.fsmonitor command. The rulespec
    checkout's fsmonitor appends to a marker. git itself runs it for an index read (the control), and so
    does the Claude block without the pin; with the pin neither side runs it, and the decision stands."""
    fix = AxiomFixture(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(FSMONITOR.format(marker=marker), encoding="utf-8")
    script.chmod(0o755)
    if staged:
        files = {f"{SECTION}.yaml": YAML}
        if "manifest" in staged:
            files[f".axiom/encoding-manifests/{SECTION}.json"] = "{}"
        fix.stage(fix.rulespec, files)
    fix.git(fix.rulespec, "config", "core.fsmonitor", str(script))
    fix.git(fix.rulespec, "diff", "--cached", "--name-only")
    assert marker.exists(), "control: git runs the fsmonitor command for an index read"
    marker.unlink()
    command, where = command.format(rulespec=fix.rulespec), getattr(fix, cwd)
    if denied:
        assert fix.claude("hand-authored-rulespec", command, where, pin=False).startswith(HAND_PREFIX)
        assert marker.exists(), "control: the Claude block without the pin runs it"
        marker.unlink()
    reason = fix.same("hand-authored-rulespec", command, where)
    assert bool(reason) == denied, reason
    assert not marker.exists(), marker.read_text()


def git_starts(trace: Path) -> int:
    """The git processes a GIT_TRACE2_EVENT file records (one ``start`` event each)."""
    if not trace.exists():
        return 0
    return sum(json.loads(line).get("event") == "start" for line in trace.read_text().splitlines() if line)


@needs_jq
def test_the_staged_file_count_does_not_multiply_git_processes(tmp_path):
    """I6: the block's git processes do not grow with the staged rule files (one cat-file --batch-check
    reads every manifest; it was up to two cat-file calls per file). Both sides name all the files."""
    fix = AxiomFixture(tmp_path)
    blob = fix.git(fix.rulespec, "hash-object", "-w", "--stdin", stdin=YAML).strip()
    starts = {}
    for count in (2, 60):
        files = [f"il/statutes/s-{i:03d}.yaml" for i in range(count)]
        fix.git(fix.rulespec, "read-tree", "HEAD")
        fix.git(fix.rulespec, "update-index", "--index-info",
                stdin="".join(f"100644 {blob}\t{path}\n" for path in files))
        for side in ("claude", "overlay"):
            trace = tmp_path / f"trace-{side}-{count}.json"
            env = {"GIT_TRACE2_EVENT": str(trace)}
            reason = (fix.claude("hand-authored-rulespec", "git commit -m x", fix.rulespec, env=env) if side == "claude"
                      else fix.overlay("git commit -m x", fix.rulespec, env=env))
            assert reason.startswith(HAND_PREFIX + " ".join(files) + ". "), reason[:200]
            starts[side, count] = git_starts(trace)
    assert starts["claude", 2] == starts["claude", 60] and starts["overlay", 2] == starts["overlay", 60], starts
    assert 0 < starts["overlay", 60] <= 4, starts


#: Fix r2's denials (2026-11-17): the hand block's time budget, and the paths either block will resolve.
HAND_BUDGET = ("[hand-authored-rulespec] The guard's 15 s budget for checking staged indexes ran out before it "
               "checked every folder this command commits from")
HAND_TOOMANY = "[hand-authored-rulespec] This command names more than 64 folders to commit from"
BRIEF_TOOMANY = "[axiom-brief-regime] This command names more than 16 briefs to read"
#: Review r2's case: the payload cwd spelled 15 ways (cd ., cd ./., cd ././., ...), then a commit and a stash.
SPELLED_15 = "; ".join("cd " + "/".join(["."] * n) for n in range(1, 16))
#: A git that logs its first argument and sleeps before running the real one: an exported bash function, which
#: both hooks' bash imports and which their PATH (/usr/bin first) cannot bypass.
SLOW_GIT = "() {{ printf '%s\\n' \"$1\" >> '{log}'; sleep {pause}; command git \"$@\"; }}"
#: A grep that logs every regular file it reads (its last argument), then runs the real one.
LOGGED_GREP = "() {{ [ -f \"${{@: -1}}\" ] && printf '%s\\n' \"${{@: -1}}\" >> '{log}'; command grep \"$@\"; }}"


def side_reason(fix: AxiomFixture, side: str, rule: str, command: str, cwd: Path, env: dict) -> str:
    return fix.claude(rule, command, cwd, env=env) if side == "claude" else fix.overlay(command, cwd, env=env)


def stage_index(fix: AxiomFixture, repo: Path, paths: list[str]) -> None:
    """Stage ``paths`` (one blob each) in ``repo``'s index without writing them to disk."""
    blob = fix.git(repo, "hash-object", "-w", "--stdin", stdin=YAML).strip()
    fix.git(repo, "read-tree", "HEAD")
    fix.git(repo, "update-index", "--index-info", stdin="".join(f"100644 {blob}\t{path}\n" for path in paths))


@needs_jq
def test_one_checkout_spelled_many_ways_is_checked_once(tmp_path):
    """I6 (fix r2): review r2's P1. The block keyed each folder by its path text, and `.`, `..`, repeated `/` and
    links each made a new key for the same checkout, every one costing three git processes and a pass over
    the whole index. Each folder is now resolved (`cd -P`, `pwd -P`) before git runs and checked once, so the
    review's 15 spellings, and spellings through `..`, `//`, a trailing `/` and a link, cost what one `cd`
    costs, on both sides (git `start` events in GIT_TRACE2_EVENT). The decisions stand: the hand block finds
    every rule file's manifest, and the overlay's stash-shared rule denies the stash."""
    fix = AxiomFixture(tmp_path)
    fix.git(fix.rulespec, "worktree", "add", "-q", "-b", "wt", str(fix.home / "rulespec-wt"))
    rules = [f"il/statutes/s-{i:03d}.yaml" for i in range(40)]
    stage_index(fix, fix.rulespec, rules + [".axiom/encoding-manifests/" + r[:-5] + ".json" for r in rules])
    (fix.home / "rulespec-link").symlink_to(fix.rulespec)
    others = (f"cd il/..; cd .//il/../il/..; cd {fix.rulespec}/; cd {fix.home}/rulespec-link; "
              f"pushd {fix.home}//rulespec/./il/..; cd ~/rulespec; cd ../rulespec/il/statutes/../..")
    commands = {"one cd": "cd .; git commit -m x; git stash",
                "review r2's 15": f"{SPELLED_15}; git commit -m x; git stash",
                "other spellings": f"{others}; git commit -m x; git stash"}
    starts = {}
    for side in ("claude", "overlay"):
        for name, command in commands.items():
            trace = tmp_path / f"trace-{side}-{len(starts)}.json"
            reason = side_reason(fix, side, "hand-authored-rulespec", command, fix.rulespec,
                                 {"GIT_TRACE2_EVENT": str(trace)})
            assert reason == "" if side == "claude" else reason.startswith("[stash-shared] "), (name, reason)
            starts[side, name] = git_starts(trace)
    for side in ("claude", "overlay"):
        assert len({starts[side, name] for name in commands}) == 1, starts
    assert starts["claude", "one cd"] == 3, starts


@needs_jq
def test_the_hand_block_denies_when_its_time_budget_is_spent(tmp_path):
    """I6 (fix r2): the block's git work is bounded by time, not by a count of folders. Here git sleeps 6 s
    before each call, and four distinct rulespec checkouts would cost twelve calls, 72 s, past the 60 s hook
    timeout, which allows. The first two folders are checked in full (six calls, 36 s); before each git call
    for a later one the block checks its budget (15 s since it began), so it stops there, on both sides, and
    denies with its own reason: it never allows a folder it did not check. The assertions count calls, not
    seconds."""
    fix = AxiomFixture(tmp_path)
    checkouts = [fix.home / f"rulespec-{n}" for n in range(4)]
    for repo in checkouts:
        repo.mkdir()
        fix.git(repo, "init", "-q", "-b", "main")
        fix.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        fix.git(repo, "remote", "add", "origin", ORIGIN)
        fix.stage(repo, {f"{SECTION}.yaml": YAML, f".axiom/encoding-manifests/{SECTION}.json": "{}"})
    command = "; ".join(f"git -C {repo} commit -m x" for repo in checkouts)
    for side in ("claude", "overlay"):
        log = tmp_path / f"git-calls-{side}"
        reason = side_reason(fix, side, "hand-authored-rulespec", command, fix.plain,
                             {"BASH_FUNC_git%%": SLOW_GIT.format(log=log, pause=6)})
        assert reason.startswith(HAND_BUDGET), (side, reason)
        calls = log.read_text().splitlines()
        assert len(calls) == 6, (side, calls)


@needs_jq
def test_a_folder_that_is_not_there_starts_no_git_process(tmp_path):
    """Review r2's question 3: a candidate folder that does not exist is dropped before git runs (git would fail
    there anyway), so no git process starts, however many there are; the command is allowed on both sides."""
    fix = AxiomFixture(tmp_path)
    gone = tmp_path / "gone"
    hops = "; ".join(f"cd d{i}" for i in range(17))
    command = (f"{hops}; git commit -m x; git -C nowhere commit -m y; cd {tmp_path}/also-gone && git commit -m z; "
               "pushd ../gone-too; git commit -m w")
    for side in ("claude", "overlay"):
        trace = tmp_path / f"trace-{side}.json"
        reason = side_reason(fix, side, "hand-authored-rulespec", command, gone, {"GIT_TRACE2_EVENT": str(trace)})
        assert reason == "", (side, reason)
        assert git_starts(trace) == 0, side


@needs_jq
def test_one_brief_spelled_17_ways_is_read_once(tmp_path):
    """Fix r2: the brief block keyed each brief by its path text too, so 17 spellings of one brief were refused as
    17 briefs. Each brief's folder is now resolved before it is read: 17 spellings (`./`, `..`, a link) of one
    brief are read once (two greps of that one file) and not refused, on both sides, and the same 17 of a
    brief without the regime block are denied for that."""
    fix = AxiomFixture(tmp_path)
    (fix.home / "briefs-link").symlink_to(fix.briefs)
    for brief, denied in (("with-regime.md", False), ("no-regime.md", True)):
        spellings = ([f"{fix.briefs}/" + "./" * n + brief for n in range(8)]
                     + [f"{fix.briefs}" + "/../briefs" * n + "/" + brief for n in range(1, 9)]
                     + [f"{fix.home}/briefs-link/{brief}"])
        assert len(set(spellings)) == 17
        command = "subfleet run --task build -C ~/TheAxiomFoundation " + " ".join(f"-p {s}" for s in spellings)
        for side in ("claude", "overlay"):
            log = tmp_path / f"reads-{side}-{brief}"
            reason = side_reason(fix, side, "axiom-brief-regime", command, fix.plain,
                                 {"BASH_FUNC_grep%%": LOGGED_GREP.format(log=log)})
            assert reason.startswith(BRIEF_REASON) if denied else reason == "", (side, brief, reason)
            reads = [path for path in (log.read_text().splitlines() if log.exists() else []) if path.endswith(brief)]
            assert reads == [os.path.realpath(fix.briefs / brief)] * 2, (side, brief, reads)


@needs_jq
def test_too_many_places_to_check_is_refused(tmp_path):
    """I6 (fix r2): each block resolves at most 64 paths, counted after `.` and repeated `/` collapse, and the brief
    block reads at most 16 distinct briefs; past either bound both sides refuse, never allow. Spellings that
    differ by `..` stay apart until resolved, so 65 of them are refused even for one folder; that bounds the
    resolving, and the hand block's git work is bounded by its time budget
    (test_the_hand_block_denies_when_its_time_budget_is_spent)."""
    fix = AxiomFixture(tmp_path)
    hops = "; ".join("cd il/.." for _ in range(64))
    reason = fix.same("hand-authored-rulespec", f"{hops}; git commit -m x", fix.rulespec)
    assert reason.startswith(HAND_TOOMANY), reason
    hops = "; ".join("cd il/.." for _ in range(63))
    assert fix.same("hand-authored-rulespec", f"{hops}; git commit -m x", fix.rulespec) == ""
    paths = " ".join(f"-p {fix.briefs}" + "/../briefs" * n + "/with-regime.md" for n in range(65))
    reason = fix.same("axiom-brief-regime", f"subfleet run -C ~/TheAxiomFoundation {paths}", fix.plain)
    assert reason.startswith(BRIEF_TOOMANY), reason
    for n in range(17):
        (fix.briefs / f"b{n}.md").write_text("Update the landing page.\n", encoding="utf-8")
    briefs = " ".join(f"-p b{n}.md" for n in range(16))
    assert fix.same("axiom-brief-regime", f"subfleet run -C ~/TheAxiomFoundation {briefs}", fix.briefs) == ""
    reason = fix.same("axiom-brief-regime", f"subfleet run -C ~/TheAxiomFoundation {briefs} -p b16.md", fix.briefs)
    assert reason.startswith(BRIEF_TOOMANY), reason


@needs_jq
def test_a_relative_diff_config_hides_no_staged_file(tmp_path):
    """Fix r2, found while fixing P2: with diff.relative=true in a checkout's config, `git diff` run from a subfolder
    prints paths relative to it and drops the rest, so `cd <rulespec>/il && git commit` hid every rule file from
    the anchor. The block's diff passes --no-relative; both sides deny."""
    fix = AxiomFixture(tmp_path)
    fix.stage(fix.rulespec, {f"{SECTION}.yaml": YAML})
    fix.git(fix.rulespec, "config", "diff.relative", "true")
    for command in (f"cd {fix.rulespec}/il && git commit -m x", f"git -C {fix.rulespec}/il/statutes commit -m x"):
        reason = fix.same("hand-authored-rulespec", command, fix.plain)
        assert reason.startswith(HAND_PREFIX + f"{SECTION}.yaml. "), (command, reason)


# ---------------------------------------------------------------- fix r3: the parse's cost, logical `..`, the cap

#: The hook's timeout (60 s, which allows); a timed run past it is killed with its process group and fails.
HOOK_TIMEOUT_S = 60


def timed(argv: list[str], stdin: str, env: dict, cwd: Path) -> tuple[float, str | None]:
    """(CPU seconds of ``argv`` and of every descendant it waited for, its stdout, or None after the timeout).
    ``RUSAGE_CHILDREN`` deltas around the run read the CPU (test_the_cpu_measure_counts_a_grandchilds_cpu checks
    that a `$(…)` subshell's awk shows there). The run gets its own session, so on a timeout its whole process
    group, subshells and awk included, is killed and reaped: a plain kill of the hook's bash left review r3's awk
    running with ppid 1."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=cwd, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=HOOK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        out = None
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if out is not None:
        assert proc.returncode == 0, err
    return after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime, out


def timed_overlay(fix: AxiomFixture, command: str, cwd: Path, hook: Path = HOOK) -> tuple[float, str | None]:
    """``AxiomFixture.overlay``, timed: (CPU seconds, the reason, empty when allowed, or None after the timeout)."""
    (fix.root / "fs").mkdir(exist_ok=True)
    env = {"PATH": fix.env["PATH"], "HOME": str(fix.home), "CODEX_GUARD_LOG": os.devnull,
           "CODEX_GUARD_TEST_FSROOT": str(fix.root / "fs")}
    cpu, out = timed(["/bin/bash", str(hook)], payload(command, cwd), env, fix.root)
    if not out:
        return cpu, out
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return cpu, decision["permissionDecisionReason"]


def timed_claude(fix: AxiomFixture, block: str, command: str, cwd: Path) -> tuple[float, str | None]:
    """``AxiomFixture.claude`` for the block text ``block``, timed: (CPU seconds, the reason or None)."""
    script = fix.root / f"harness-timed-{hashlib.sha256(block.encode()).hexdigest()[:12]}.sh"
    if not script.exists():
        script.write_text(payload_harness() + block + "exit 0\n", encoding="utf-8")
    return timed(["/bin/bash", str(script)], payload(command, cwd), {"PATH": fix.env["PATH"], "HOME": str(fix.home)},
                 fix.root)


def test_the_cpu_measure_counts_a_grandchilds_cpu(tmp_path):
    """The CPU bounds below read RUSAGE_CHILDREN, which counts a descendant's CPU only once its parent has waited
    for it. A busy awk loop run inside `$(…)` by bash, as the blocks run their awk, shows its CPU there."""
    loop = "BEGIN { for (i = 0; i < 3000000; i++) s += i; print s }"
    env = {"PATH": "/usr/bin:/bin"}
    direct, out = timed(["/usr/bin/awk", loop], "", env, tmp_path)
    nested, wrapped = timed(["/bin/bash", "-c", f"x=$(/usr/bin/awk '{loop}'); printf '%s\\n' \"$x\""], "", env,
                            tmp_path)
    assert out == wrapped and direct > 0.05, (direct, out, wrapped)
    assert nested >= 0.7 * direct, (direct, nested)


#: Review r3's P1. Fix r2's `join` split and rebuilt the whole path at every `cd`, so a relative `cd` chain cost the
#: square of its length (13 s of CPU at 4,000 `cd a; ` on the overlay, against fix r1's 0.24 s). Each row is
#: ``count`` copies of ``word`` and then that block's tail: the hand block's runs on `git commit`, the brief block's
#: only on `subfleet run` (review r3's brief case). `cd a/..` is this round's logical and physical `..` path.
CHAIN_ROWS = [("hand", "cd a; ", 4000), ("hand", "cd a; ", 8000), ("hand", "cd a/..; ", 4000),
              ("brief", "cd a; ", 4000), ("brief", "cd a; ", 8000), ("brief", "cd a/..; ", 4000)]
CHAIN_TAIL = {"hand": "git commit -m x; git stash",
              "brief": "subfleet run -C /nowhere/TheAxiomFoundation -p b.md; git stash"}
CHAIN_RULE = {"hand": "hand-authored-rulespec", "brief": "axiom-brief-regime"}
#: The CPU bound (s) per (block, word, count, side): 10 times fix r1's CPU on that row and side, measured once
#: (fix r1's overlay hook 324d2a84… and its blocks in claude-blocks/fix-r1/; the measurements are in the report's
#: fix round 3 section), and never under 1 s, since 10 times a few hundredths of a second is CPU jitter under -n 4.
#: Fix r1 took, overlay / Claude block: hand 0.116 / 0.074 s (4,000), 0.308 / 0.258 s (8,000) and 0.201 / 0.169 s
#: (4,000 `cd a/..`); brief 0.122 / 0.075, 0.300 / 0.267 and 0.207 / 0.161 s; at a load of about 11.
CHAIN_CPU_BOUND = {
    ("hand", "cd a; ", 4000, "overlay"): 1.16, ("hand", "cd a; ", 4000, "claude"): 1.0,
    ("hand", "cd a; ", 8000, "overlay"): 3.08, ("hand", "cd a; ", 8000, "claude"): 2.58,
    ("hand", "cd a/..; ", 4000, "overlay"): 2.01, ("hand", "cd a/..; ", 4000, "claude"): 1.69,
    ("brief", "cd a; ", 4000, "overlay"): 1.22, ("brief", "cd a; ", 4000, "claude"): 1.0,
    ("brief", "cd a; ", 8000, "overlay"): 3.0, ("brief", "cd a; ", 8000, "claude"): 2.67,
    ("brief", "cd a/..; ", 4000, "overlay"): 2.07, ("brief", "cd a/..; ", 4000, "claude"): 1.61,
}


@needs_jq
@pytest.mark.parametrize("side", ["overlay", "claude"])
@pytest.mark.parametrize("block,word,count", CHAIN_ROWS,
                         ids=[f"{b}-{w.strip(' ;').replace(' ', '_').replace('/', '')}-{n}" for b, w, n in CHAIN_ROWS])
def test_a_long_cd_chain_costs_cpu_near_linear_in_its_length(block, word, count, side, tmp_path):
    """Fix r3 (review r3's P1): every stored folder is kept normalized as the words are read, so a `cd` normalizes
    only its own words and appends them to a folder already in that form; nothing re-splits a stored folder. The
    hook's CPU (its children's included) stays within CHAIN_CPU_BOUND on each row, on the overlay and on the
    Claude block alone; the payload cwd is a checkout with a second worktree. Both sides refuse every row with
    the block's own refusal (the overlay must deny each row, and the Claude block gives the overlay block's
    reason). A run past the 60 s hook timeout is killed with its process group and fails."""
    fix = AxiomFixture(tmp_path)
    fix.git(fix.plain, "worktree", "add", "-q", "-b", "wt", str(fix.home / "plain-wt"))
    command = word * count + CHAIN_TAIL[block]
    if side == "overlay":
        cpu, reason = timed_overlay(fix, command, fix.plain)
    else:
        cpu, reason = timed_claude(fix, (BLOCKS / f"{CHAIN_RULE[block]}.sh").read_text(encoding="utf-8"), command,
                                   fix.plain)
    bound = CHAIN_CPU_BOUND[block, word, count, side]
    assert reason is not None, f"killed at the {HOOK_TIMEOUT_S} s timeout after {cpu:.2f} s of CPU (bound {bound} s)"
    assert cpu <= bound, f"{cpu:.2f} s of CPU, over the {bound} s bound"
    assert reason.startswith(HAND_TOOMANY if block == "hand" else BRIEF_TOOMANY), reason[:300]


def link_fixture(fix: AxiomFixture) -> tuple[Path, Path]:
    """Review r3's P2 setup, under the fixture's home: ``a/`` (the payload cwd), ``x/y``, the link ``a/link`` ->
    ``x/y``, and two rulespec checkouts, ``a/rs`` and ``x/rs2``, each staging a rule file with no manifest; also
    ``a/briefs/no-regime.md``, with no ``x/briefs``. Returns (a, x)."""
    a, x = fix.home / "lnk" / "a", fix.home / "lnk" / "x"
    (x / "y").mkdir(parents=True)
    a.mkdir()
    (a / "link").symlink_to(x / "y")
    for repo in (a / "rs", x / "rs2"):
        repo.mkdir()
        fix.git(repo, "init", "-q", "-b", "main")
        fix.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        fix.git(repo, "remote", "add", "origin", ORIGIN)
        fix.stage(repo, {f"{SECTION}.yaml": YAML})
    (a / "briefs").mkdir()
    (a / "briefs" / "no-regime.md").write_text("Encode rulespec-il modules from the corpus.\n", encoding="utf-8")
    return a, x


def lands(shell: str, script: str, cwd: Path) -> str:
    """Where ``shell`` (no rc files) ends up after ``script``, which runs only cd, set -P and pwd."""
    out = subprocess.run([shell, "-f", "-c", script] if shell.endswith("zsh") else [shell, "--norc", "-c", script],
                         capture_output=True, text=True, timeout=30, cwd=cwd, env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


#: Review r3's P2: from a/, `cd link/../rs` lands in a/rs under bash and zsh, which resolve `..` logically by default.
LINK_LOGICAL = ["cd link/../rs && git commit -m x", "pushd link/../rs; git commit -m x",
                "cd link/.. && cd ./rs/ && git commit -m x"]
#: The same target resolved physically (cd -P, bash's set -P, zsh's CHASE_LINKS or CHASE_DOTS, git -C) is x/rs2:
#: each was denied before fix r3, and must stay denied.
LINK_PHYSICAL = ["cd -P link/../rs2 && git commit -m x", "set -P; cd link/../rs2 && git commit -m x",
                 "setopt CHASE_LINKS; cd link/../rs2 && git commit -m x",
                 "setopt CHASE_DOTS; cd link/../rs2 && git commit -m x", "git -C link/../rs2 commit -m x"]


@needs_jq
def test_a_cd_through_a_link_and_dotdot_is_judged_where_the_shell_lands(tmp_path):
    """Fix r3 (review r3's P2): the blocks resolved a `cd` target physically (`cd -P`), but bash and zsh resolve it
    logically by default, so from a/, with a/link -> x/y, `cd link/../rs` lands in a/rs while the block checked x/rs,
    which is not there, and allowed. Each block now takes a `cd` or `pushd` target's `..` off textually, as a
    logical cd does, before `cd -P`: both sides deny, for the hand block and for the brief block (a/briefs has the
    brief; x/briefs does not exist)."""
    fix = AxiomFixture(tmp_path)
    a, _x = link_fixture(fix)
    for shell in ("/bin/bash", "/bin/zsh"):
        if Path(shell).exists():
            assert lands(shell, "cd link/../rs && pwd", a) == os.path.realpath(a / "rs"), shell
    for command in LINK_LOGICAL:
        reason = fix.same("hand-authored-rulespec", command, a)
        assert reason.startswith(HAND_PREFIX + f"{SECTION}.yaml. "), (command, reason)
    reason = fix.same("axiom-brief-regime", "cd link/../briefs && subfleet run -C ~/TheAxiomFoundation -p no-regime.md",
                      a)
    assert reason.startswith(BRIEF_REASON), reason


@needs_jq
def test_the_physical_spelling_of_a_cd_target_is_still_judged(tmp_path):
    """Fix r3 keeps each `cd` target's physical spelling beside the logical one (both count toward the 64), so a shell
    that resolves links first (cd -P, bash's set -P, zsh's CHASE_LINKS or CHASE_DOTS, none of which the blocks
    parse) is still judged where it lands, x/rs2, and git -C keeps `..` as written, as git resolves it. Each row was
    denied before the fix and is denied after it, on both sides; the control `cd rs` is denied too."""
    fix = AxiomFixture(tmp_path)
    a, x = link_fixture(fix)
    for script in ("cd -P link/../rs2 && pwd", "set -P; cd link/../rs2 && pwd"):
        assert lands("/bin/bash", script, a) == os.path.realpath(x / "rs2"), script
    if Path("/bin/zsh").exists():
        assert lands("/bin/zsh", "cd -P link/../rs2 && pwd", a) == os.path.realpath(x / "rs2")
    for command in LINK_PHYSICAL + ["cd rs && git commit -m x"]:
        reason = fix.same("hand-authored-rulespec", command, a)
        assert reason.startswith(HAND_PREFIX + f"{SECTION}.yaml. "), (command, reason)


@needs_jq
def test_too_many_folder_and_command_pairs_are_refused(tmp_path):
    """Fix r3: every `git … commit` (`subfleet run`) is judged from every folder met before it, so N `cd` then N
    commits that each name one absolute -C cost N² path joins while giving one key, which the 64-path refusal never
    counts (2,000 of each passed the 8,000-row bound; see the report). Each block counts every (folder, command) pair
    it examines, once more for each -C (and -p) word applied to it, and refuses past 4,096, the 64-folder bound
    squared; it never allows past it. 64 folders (the payload cwd and 63 `cd`) × 32 commits × (1 + one -C) is 4,096,
    allowed; one more `cd` is refused, on both sides, and likewise for 16 runs × (1 + one -C + two -p)."""
    fix = AxiomFixture(tmp_path)

    def hops(n: int) -> str:
        return "; ".join(f"cd d{i}" for i in range(n))
    commits = "; ".join("git -C /nowhere/x commit -m x" for _ in range(32))
    assert fix.same("hand-authored-rulespec", f"{hops(63)}; {commits}", fix.plain) == ""
    reason = fix.same("hand-authored-rulespec", f"{hops(64)}; {commits}", fix.plain)
    assert reason.startswith(HAND_TOOMANY), reason
    runs = "; ".join("subfleet run -C /nowhere/TheAxiomFoundation -p /nowhere/b.md -p /nowhere/c.md" for _ in range(16))
    assert fix.same("axiom-brief-regime", f"{hops(63)}; {runs}", fix.plain) == ""
    reason = fix.same("axiom-brief-regime", f"{hops(64)}; {runs}", fix.plain)
    assert reason.startswith(BRIEF_TOOMANY), reason


CORPUS_ORIGIN = "https://github.com/TheAxiomFoundation/axiom-corpus.git"
#: (rule, command, the folder it is judged in) for the rules after the two Axiom blocks that read the payload cwd
#: or $HOME, each a denial on its own.
LATER_RULES = [
    ("corpus-squash", "gh pr merge 7 --squash", "corpus"),
    ("corpus-push", "git push origin main", "corpus"),
    ("stash-shared", "git stash", "shared"),
    ("local-main", "git checkout -b feat", "mainline"),
    ("unscoped-search", "find ~ -name y", "plain"),
]
#: Commands the Axiom blocks run on and allow, each naming another folder (the review's M3 case is the first).
AXIOM_PREFIXES = [
    "git -C {plain} commit -m x && ",
    "git -c a=b -C {plain} --no-pager commit -m x; ",
    "cd {plain} && git commit -m x; ",
    "GIT_DIR={plain}/.git git commit -m x; ",
    "subfleet run --task build -C ~/TheAxiomFoundation -p {briefs}/with-regime.md -o out.md && ",
    "subfleet run --task build -C {plain} -p {briefs}/no-regime.md; ",
]


@needs_jq
@pytest.mark.parametrize("rule,command,cwd", LATER_RULES, ids=[rule for rule, _, _ in LATER_RULES])
def test_later_rules_decide_alike_after_an_axiom_command(rule, command, cwd, tmp_path):
    """I7: the two Axiom blocks leave later rules' inputs alone. Each later rule's denial is the same, reason
    for reason, when an Axiom command the blocks allow comes first in the same command. The review's mutant
    M3 (corpus-squash reading the hand block's folder as its cwd) turns the first such case into an allow."""
    fix = AxiomFixture(tmp_path)
    fix.corpus, fix.shared, fix.mainline = fix.home / "axiom-corpus", fix.home / "shared", fix.home / "mainline"
    for repo in (fix.corpus, fix.shared, fix.mainline):
        repo.mkdir()
        fix.git(repo, "init", "-q", "-b", "main")
        fix.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    fix.git(fix.corpus, "remote", "add", "origin", CORPUS_ORIGIN)
    fix.git(fix.shared, "worktree", "add", "-q", "-b", "wt", str(fix.home / "shared-wt"))
    fix.git(fix.mainline, "update-ref", "refs/remotes/origin/main", "HEAD")
    where = getattr(fix, cwd)
    alone = fix.overlay(command, where)
    assert alone.startswith(f"[{rule}] "), (rule, alone)
    for prefix in AXIOM_PREFIXES:
        both = prefix.format(plain=fix.plain, briefs=fix.briefs) + command
        assert fix.overlay(both, where) == alone, both


# ---------------------------------------------------------------- I3: the attached-runner denial

#: (command, offender) pairs ``attached_runner`` detects, one per offender form.
OFFENDERS = [
    ("subfleet codex exec x", "subfleet codex"), ("subfleet claude -p x", "subfleet claude"),
    ("codex-run x", "codex-run"), ("claude-lane x", "claude-lane"),
    ("subfleet-codex x", "subfleet-codex"), ("subfleet-claude x", "subfleet-claude"),
    ("codex exec x", "codex exec"), ("codex e x", "codex e"), ("codex review x", "codex review"),
]

EXPECTED = ("{offender} runs attached to this session and can die when it restarts. Use subfleet run "
            "--task lookup|research|sweep|review|build|authored-prose|strategy|adjudication "
            "--tier trivial|easy|standard|hard -C <dir> -p prompt.md -o out.md, then arm subfleet wait "
            "<job-id> as a background task. For an intentional short attached call, prefix that command "
            "with SUBFLEET_ATTACHED_OK=1.")


def denial(command: str) -> tuple[int, str]:
    out = io.StringIO()
    code = hooks.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": command}}, stdout=out, env={})
    if not out.getvalue():
        return code, ""
    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert decision == {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": decision["permissionDecisionReason"]}
    return code, decision["permissionDecisionReason"]


def check_names(reason: str, offender: str) -> None:
    """I3 for one denial."""
    prefix = f"{offender} runs attached to this session and can die when it restarts. "
    assert reason.startswith(prefix), reason
    rest = reason[len(prefix):]
    assert re.search(r"--task (\S+) ", rest)[1].split("|") == list(cli.TASK_CHOICES)
    assert re.search(r"--tier (\S+) ", rest)[1].split("|") == list(cli.TIER_CHOICES)
    for name in (*cli.TASK_CHOICES, *cli.TIER_CHOICES):
        assert len(re.findall(rf"(?<![\w-]){re.escape(name)}(?![\w-])", rest)) == 1, name
    assert "then arm subfleet wait <job-id> as a background task." in rest
    assert rest.endswith("prefix that command with SUBFLEET_ATTACHED_OK=1.")


def test_denial_text_is_the_hubs_exactly():
    """Brief change 3's text, byte for byte, for every offender form."""
    for command, offender in OFFENDERS:
        assert denial(command) == (0, EXPECTED.format(offender=offender)), command


def test_denial_names_every_task_and_tier_once():
    for command, offender in OFFENDERS:
        code, reason = denial(command)
        assert code == 0
        check_names(reason, offender)


def test_denial_follows_the_cli_choices(monkeypatch):
    """A ninth task (or a fifth tier) the CLI gains appears in the denial on its own."""
    monkeypatch.setattr(cli, "TASK_CHOICES", (*cli.TASK_CHOICES, "ninth-task"))
    monkeypatch.setattr(cli, "TIER_CHOICES", (*cli.TIER_CHOICES, "fifth-tier"))
    reason = hooks.attached_denial("codex exec")
    assert "|adjudication|ninth-task --tier" in reason and "|hard|fifth-tier -C" in reason
    check_names(reason, "codex exec")


def test_an_override_or_a_detach_still_passes():
    for command in ("SUBFLEET_ATTACHED_OK=1 codex exec x", "subfleet codex -d x", "codex-run -d x"):
        assert denial(command) == (0, ""), command


PREFIXES = st.lists(st.sampled_from(["env", "nohup", "time", "command", "FOO=1", "A_B=x", "env --"]), max_size=3)
ARGS = st.lists(st.from_regex(r"[A-Za-z0-9_./:=+,@%]{1,12}", fullmatch=True).filter(
    lambda word: word != "-d" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word)), max_size=4)


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(OFFENDERS), PREFIXES, ARGS, st.sampled_from(["", "/usr/local/bin/", "./"]))
def test_denial_names_every_task_and_tier_for_generated_offenders(case, prefixes, args, path):
    """I3 over generated launches: env/wrapper prefixes, a path before the binary, extra arguments."""
    command, offender = case
    binary, _, tail = command.partition(" ")
    launch = " ".join([*prefixes, path + binary, tail, *args])
    code, reason = denial(launch)
    assert code == 0 and reason, launch
    check_names(reason, offender)
    assert reason == EXPECTED.format(offender=offender)
