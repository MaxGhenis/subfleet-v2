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
"""

from __future__ import annotations

import fnmatch
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
#: The overlay's Bash rule blocks, in file order (the header's "nine Bash rule blocks"), which is the
#: Claude hook's order: the 2026-09-05 port copied the first seven in it, and the hub's 2026-11-11 cut
#: puts axiom-brief-regime (Claude hook lines 198-218), then hand-authored-rulespec (219-247), between
#: trust-root and corpus-squash.
RULE_BLOCKS = ["tg-getupdates", "keychain-dump", "trust-root", "axiom-brief-regime", "hand-authored-rulespec",
               "corpus-squash", "corpus-push", "stash-shared", "local-main"]
#: The hub's cut of the two Axiom blocks (supply/<rule>.sh, checked before use) and of the Claude hook's
#: prefilter line (hook line 138).
AXIOM_SHA256 = {"axiom-brief-regime": "77002282d3c865f10e1f640595ebcbd9a244a024d97e8df97dba9496941bd571",
                "hand-authored-rulespec": "f870acdf650677c3fea2856e1b1044668bad043152b9f7273099353c3595558c"}
PREFILTER_SHA256 = "c46d8eec748c649daaae06b0982bb5fc0cf6011918a35144d0ea1de06596781f"

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
        for term in ("subfleet[[:space:]]+run", "git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit"):
            self.assertIn("|" + term + "|", first)

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
#: ``run_bash`` builds the same payload). HOME is the launch's, as in a hook.
PAYLOAD_HARNESS = """set -u
export PATH=/usr/bin:/bin
input=$(cat)
command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
block() { printf '%s' "$1"; exit 0; }
"""
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

    def claude(self, rule: str, command: str, cwd: Path) -> str:
        """The pinned Claude block alone (empty: allowed)."""
        script = self.root / f"harness-{rule}.sh"
        if not script.exists():
            script.write_text(PAYLOAD_HARNESS + (BLOCKS / f"{rule}.sh").read_text(encoding="utf-8") + "exit 0\n",
                              encoding="utf-8")
        out = subprocess.run(["/bin/bash", str(script)], input=payload(command, cwd), capture_output=True,
                             text=True, timeout=60, env={"PATH": self.env["PATH"], "HOME": str(self.home)},
                             cwd=self.root)
        assert out.returncode == 0, out.stderr
        return out.stdout

    def overlay(self, command: str, cwd: Path) -> str:
        """The overlay hook's reason for the Bash payload (empty: allowed)."""
        (self.root / "fs").mkdir(exist_ok=True)
        env = {"PATH": self.env["PATH"], "HOME": str(self.home), "CODEX_GUARD_LOG": os.devnull,
               "CODEX_GUARD_TEST_FSROOT": str(self.root / "fs")}
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
    ("cd then subfleet run reads the payload cwd", "cd {briefs} && subfleet run --task build "
     "-C ~/TheAxiomFoundation -p no-regime.md", "plain", False),
]


@needs_jq
@pytest.mark.parametrize("name,command,cwd,denied", BRIEF_CASES, ids=[case[0] for case in BRIEF_CASES])
def test_same_decisions_axiom_brief_regime(name, command, cwd, denied, tmp_path):
    fix = AxiomFixture(tmp_path)
    reason = fix.same("axiom-brief-regime", command.format(briefs=fix.briefs), getattr(fix, cwd))
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
    reason = fix.same("hand-authored-rulespec", command.format(rulespec=fix.rulespec), getattr(fix, cwd))
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
@given(STAGED, st.sampled_from(["cwd", "-C", "-C ~"]))
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
                    "-C ~": ("git -C ~/rulespec commit -m x", fix.plain)}[form]
    missing = oracle_missing(staged, {f".axiom/encoding-manifests/{TRACKED}.json"})
    reason = fix.same("hand-authored-rulespec", command, cwd)
    if missing:
        assert reason.startswith(HAND_PREFIX + " ".join(missing) + ". "), (sorted(staged), reason)
    else:
        assert reason == "", (sorted(staged), reason)


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
