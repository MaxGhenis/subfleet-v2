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

Invariants:

I1 (byte identity) Every pinned Claude block (``claude-blocks/<rule>.sh``) appears in the overlay
   exactly once, byte for byte, as the overlay's block for that rule, cut as the overlay header's
   sync contract cuts one: from its column-0 ``# --- <rule>:`` line up to the next column-0
   ``# --- `` or ``# >>>`` line. The overlay keeps its seven Bash rule blocks in order, and the
   pinned "after" block differs from the pinned "before" block in that one sentence only
   (``DriftTests``).
I2 (same decisions) For every keychain-dump case, the overlay hook and the pinned Claude block,
   run alone under a harness that defines ``block`` as the deny, reach the same decision with the
   same reason; both keep the decisions the old block reached (``test_same_decisions_*``).
I3 (the denial names every task and tier) For every offender the hook detects, the denial starts
   with the offender, names each ``TASK_CHOICES`` member and each ``TIER_CHOICES`` member exactly
   once after it (as the ``--task`` and ``--tier`` alternations, in the CLI's order), and tells the
   caller to background the wait; the hook still returns 0 (``test_denial_*``, Hypothesis over
   generated offenders).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
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
#: The overlay's Bash rule blocks, in file order (the header's "seven Bash rule blocks").
RULE_BLOCKS = ["tg-getupdates", "keychain-dump", "trust-root", "corpus-squash", "corpus-push",
               "stash-shared", "local-main"]

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

    def test_the_overlay_keeps_its_seven_rule_blocks_in_order(self):
        text = HOOK.read_text(encoding="utf-8")
        region = text[:text.index("\n# >>> unscoped-search: shared region begin")]
        heads = re.findall(r"^# --- ([a-z][a-z0-9-]*):", region, flags=re.M)
        self.assertEqual(heads, RULE_BLOCKS)

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
