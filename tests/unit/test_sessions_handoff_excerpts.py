"""A text over the scrubber's budget keeps a scrubbed head and tail (C-23.14, C-23.36,
C-25.5); credential-read suppression classifies an input of any size (C-23.14).

Review of 7da13417, finding 2: the 262,144-character cap had replaced a whole text
with one marker: a long answer or thinking block vanished from the timeline, a
brief over the cap was dispatched as the marker alone, and any tool input over it
was shown as a credential read. These tests hold the replacement to what a scrub of
the whole text would remove (a differential property against it), to its limit, to
its work bound, and to the old behavior within the budget.

`Zq…` strings are made-up values shaped like secrets; none is, or was, a credential.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet.conversations import redact
from subfleet.policy import DEFAULT_POLICY_PATH, HANDOFF_BRIEF_MAX_CHARS, PolicyError, load_policy
from subfleet.sessions import handoff

BUDGET = handoff.MAX_SCRUB_CHARS
HEAD = handoff.EXCERPT_CHARS
TAIL = handoff.EXCERPT_CHARS - handoff.EXCERPT_LOOKBEHIND
PROSE = "ordinary words in a line of prose, with nothing in it worth hiding\n"


def prose(n: int, line: str = PROSE) -> str:
    """Exactly `n` characters of ordinary lines (the last one may be cut)."""
    return (line * (n // len(line) + 1))[:n]


def around(position: int, shape: str, total: int, *, before: str = "", after_line: str = PROSE) -> str:
    """`total` characters of prose with `shape` starting at `position`."""
    text = before + prose(position - len(before)) + shape
    return text + prose(total - len(text), after_line)


# --- within the budget: exactly scrub, then truncate ------------------------------------

FRAGMENTS = st.sampled_from([
    "password = ", "\"hunter2Zq9\"", "Bearer ", "Zq1abcdef99", "\n", " ", "=", ":", "api_key",
    "-----BEGIN PRIVATE KEY-----\n", "-----END PRIVATE KEY-----\n", "<system-reminder>",
    "</system-reminder>", "Authorization: ", "token", "sk-ant-" + "A" * 20, "ordinary text ",
    "data:image/png;base64,", "QUJD" * 10, "https://user:Zq7pass@host/", "--password ",
])


@hypothesis.settings(max_examples=200, deadline=None)
@hypothesis.given(parts=st.lists(FRAGMENTS, max_size=40), limit=st.integers(0, 400),
                  strip=st.booleans())
def test_within_the_budget_it_is_scrub_then_truncate(parts, limit, strip):
    """C-23.36: the bounded scrub changes nothing for a text within the budget."""
    text = "".join(parts)
    scrubbed, count = handoff.scrub_secrets(text, strip_reminders=strip)
    assert handoff.scrub_bounded(text, limit, strip_reminders=strip) == (handoff.truncate(scrubbed, limit), count)
    assert handoff.scrub_bounded(text, None, strip_reminders=strip) == (scrubbed, count)


# --- over the budget: what the excerpts keep ------------------------------------------

def test_an_oversized_text_keeps_a_scrubbed_head_and_tail_within_the_work_bound(monkeypatch):
    """C-23.14, C-25.5: the head and the tail of a long block survive, scrubbed, and no
    matcher is given more than one excerpt."""
    seen: list[int] = []
    real = handoff._scrub
    monkeypatch.setattr(handoff, "_scrub", lambda text, strip: (seen.append(len(text)), real(text, strip))[1])
    text = ('first line, api_key = "Zq1headsecret99"\n' + prose(400_000)
            + 'last lines, password = "Zq2tailsecret99"\n' + prose(2_000))
    out, count = handoff.scrub_bounded(text, redact.TEXT_EVENT_MAX, strip_reminders=True)
    assert len(out) <= redact.TEXT_EVENT_MAX
    assert out.startswith('first line, api_key = "[REDACTED]"')
    assert 'last lines, password = "[REDACTED]"' in out
    assert "Zq1" not in out and "Zq2" not in out and count == 2
    assert re.search(r"\n… \[[\d,]+ characters omitted\] …\n", out)
    assert seen and max(seen) <= HEAD and sum(seen) <= BUDGET


def test_a_line_longer_than_an_excerpt_is_left_out_whole():
    """C-23.14: a cut inside a line could split a quoted value from its key, so such a
    line is never cut: a single-line text over the budget keeps only the marker."""
    text = 'password="' + "private value " * 25_000 + '"'
    out, count = handoff.scrub_secrets(text)
    assert out == f"… [{len(text):,} characters omitted] …" and count == 0
    assert "\n" not in out                      # still one line (a diff keeps its line counts)


LEADING = "Zq9leadingvalue77"


@pytest.mark.parametrize("shape", [
    "password =\n" + LEADING + "\n",
    "password\n=\n" + LEADING + "\n",
    "password\n\n=\n\n" + LEADING + "\n",
    "client_secret =\n\"" + LEADING + " and more\"\n",
    "Authorization:\n  " + LEADING + "\n",
    "Bearer\n  " + LEADING + "\n",
    "password = <system-reminder>\nnote\n</system-reminder>" + LEADING + "\n",
], ids=["value-below", "separator-and-value-below", "blank-lines", "quoted-below", "header", "bearer",
        "reminder-between"])
@pytest.mark.parametrize("shift", range(-40, 8, 4))
def test_a_value_whose_name_is_above_the_tail_cut_is_not_shown(shape, shift):
    """C-23.14: the tail cannot see a key, header or `Bearer` above its first line, so
    that line goes (after the scrub, so a key it holds still covers its value)."""
    total = 300_000
    text = around(total - TAIL + shift, "\n" + shape, total)     # its name starts a line
    whole = handoff.scrub_secrets(text, strip_reminders=True, whole=True)[0]
    assert LEADING not in whole
    out = handoff.scrub_bounded(text, None, strip_reminders=True)[0]
    assert LEADING not in out


KEY = "-----BEGIN RSA PRIVATE KEY-----\n" + "".join(f"MIIZq{i}keybody{i:02d}\n" for i in range(12)) \
    + "-----END RSA PRIVATE KEY-----\n"
REMINDER = "<system-reminder>\n" + "".join(f"Zq{i} reminder line {i}\n" for i in range(12)) + "</system-reminder>\n"


@pytest.mark.parametrize("block", [KEY, REMINDER], ids=["private-key", "reminder"])
@pytest.mark.parametrize("edge", ["head-end", "tail-start"])
@pytest.mark.parametrize("shift", [-300, -150, -60, -20, -1, 0, 1, 40])
def test_a_block_an_excerpt_edge_cuts_open_is_not_shown(block, edge, shift):
    """C-23.14, C-25.5: a private key or reminder cut by an excerpt's edge shows none
    of its body, as the scrub of the whole text shows none."""
    total = 300_000
    at = (HEAD if edge == "head-end" else total - TAIL) + shift
    text = around(at, block, total)
    out = handoff.scrub_bounded(text, None, strip_reminders=True)[0]
    assert "Zq" not in out
    assert "Zq" not in handoff.scrub_secrets(text, strip_reminders=True, whole=True)[0]


def test_a_complete_reminder_in_the_tail_keeps_the_text_before_it():
    """C-25.5: a reminder whole within the tail (as Claude Code appends one to a long
    message) is removed alone; the tail's text before it stays."""
    text = prose(299_000) + "the last words before it\n" + "<system-reminder>Zq1 note</system-reminder>\n"
    out = handoff.scrub_bounded(text, 2_000, strip_reminders=True)[0]
    assert out.endswith("the last words before it") and "Zq1" not in out


def test_a_data_uri_across_the_tail_cut_is_omitted():
    """C-23.14: encoded binary a cut separates from its `data:` prefix is not shown."""
    payload = "".join(f"QUJDZq{i:04d}" + "QUJD" * 16 + "\n" for i in range(40))
    total = 300_000
    text = around(total - TAIL - 200, "see data:image/png;base64,\n" + payload + "! done\n", total)
    assert "Zq" not in handoff.scrub_secrets(text, whole=True)[0]
    assert "Zq" not in handoff.scrub_bounded(text, None)[0]


# --- the differential property: never more than a whole scrub shows ---------------------

SHAPES = [
    'password = "{s}"\n', "password =\n{s}\n", "password\n=\n{s}\n", "password\n\n=\n\n{s}\n",
    "Authorization:\n  {s}\n", "Authorization: Bearer {s}\n", "Bearer\n {s}\n", "api_key: {s}\n",
    'client_secret =\n"{s} with spaces"\n', "token={s}\n", "mysql --password {s}\n",
    "postgres://user:{s}@db.example/x\n", "cookie: a={s}; b=Zq{s}\n",
    "-----BEGIN PRIVATE KEY-----\n{s}\n{s}\n-----END PRIVATE KEY-----\n",
    "-----BEGIN EC PRIVATE KEY-----\nMII{s}\n" + "QUJD{s}\n" * 3 + "-----END EC PRIVATE KEY-----\n",
    "<system-reminder>\n{s} note\n</system-reminder>\n", "<system-reminder>{s}</system-reminder>",
    "password = <system-reminder>x</system-reminder>{s}\n",
    "data:image/png;base64,\n" + "QUJD" * 20 + "\n{s}\n",
    "-----END PRIVATE KEY-----\n{s}\n", "</system-reminder>\n{s}\n",       # stray closers
    "-----BEGIN PRIVATE KEY-----\n{s}\n", "<system-reminder>\n{s}\n",     # stray openers
    "sk-ant-{s}AAAAAAAAAAAA\n", "{s}\n",
]


@st.composite
def long_texts(draw):
    """A text over the budget with shapes placed across the head's end, across the
    tail's start, and inside the tail's lookbehind."""
    total = draw(st.integers(BUDGET + 1, BUDGET + 120_000))
    line = draw(st.sampled_from([PROSE, "x = 1\n", "a line with a colon: in it\n",
                                 "    indented code(); # comment\n", "\n"]))
    placed = []
    for n, (anchor, spread) in enumerate([(HEAD, 400), (total - TAIL, 400),
                                          (total - TAIL - handoff.EXCERPT_LOOKBEHIND // 2, 8_000)]):
        shape = draw(st.sampled_from(SHAPES)).replace("{s}", f"Zq{n}x{draw(st.integers(10**5, 10**6))}")
        placed.append((anchor + draw(st.integers(-spread - len(shape), spread)), shape))
    text, cursor = [], 0
    for at, shape in sorted(placed):
        at = max(at, cursor)
        text += [prose(at - cursor, line), shape]
        cursor = at + len(shape)
    text.append(prose(max(0, total - cursor), line))
    return "".join(text)


@hypothesis.settings(max_examples=60, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.too_slow, hypothesis.HealthCheck.data_too_large])
@hypothesis.given(text=long_texts(), strip=st.booleans(), limit=st.sampled_from([None, 0, 5, 40, 2_048, 60_000]))
def test_an_excerpt_never_shows_a_value_the_whole_scrub_removes(text, strip, limit):
    """C-23.14, C-25.5 (invariants): for any text over the budget,
    - every made-up value a scrub of the whole text removes is absent from the excerpts;
    - the result is never longer than its limit;
    - the same text gives the same result."""
    whole = handoff.scrub_secrets(text, strip_reminders=strip, whole=True)[0]
    out, _count = handoff.scrub_bounded(text, limit, strip_reminders=strip)
    for value in set(re.findall(r"Zq\d+x\d+", text)):
        if value not in whole:
            assert value not in out, value
    if limit is not None:
        assert len(out) <= limit
    assert handoff.scrub_bounded(text, limit, strip_reminders=strip) == (out, _count)


# --- the assembled brief is scrubbed whole (C-23.36) -----------------------------------

def test_a_brief_over_the_budget_is_scrubbed_whole_not_replaced(tmp_path):
    """C-23.14, C-23.36: the final pass over an assembled brief is not the excerpting
    scrub: a brief over the budget had become the omission marker alone. A value
    between where a head and a tail excerpt would end is still found."""
    middle = "handoff note: token = Zq1middlesecret99\n"
    recent = prose(200_000) + middle + prose(100_000)
    text, redactions = handoff.assemble(
        provider="Claude Code", source_label="session", session_id="s", transcript="/t.jsonl",
        source_cwd=None, cwd=tmp_path, original="the task", recent_title="Recent", recent=recent,
        progress="Not present.", repository="Not collected.", redactions=0)
    assert text.startswith("# Cross-agent handoff") and "## Repository state" in text
    assert "characters omitted" not in text
    assert "Zq1middlesecret99" not in text and "handoff note: token = [REDACTED]" in text
    assert redactions == 1 and "Credential/binary redactions in this brief: 1" in text


def write_policy(tmp_path, **caps) -> Path:
    policy = json.loads(DEFAULT_POLICY_PATH.read_text())
    policy["sessions"]["handoff_caps"].update(caps)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return path


def test_policy_refuses_brief_caps_the_final_scrub_could_not_hold(tmp_path):
    """C-23.36: the brief's sections (and room for its header) must fit the
    scrubber's bound, which the final whole-brief pass relies on."""
    defaults = load_policy(DEFAULT_POLICY_PATH)["sessions"]["handoff_caps"]
    others = defaults["recent"] + defaults["progress"] + defaults["repository"]
    fits = load_policy(write_policy(tmp_path, original_task=HANDOFF_BRIEF_MAX_CHARS - others))
    assert sum(fits["sessions"]["handoff_caps"][k] for k in
               ("original_task", "recent", "progress", "repository")) == HANDOFF_BRIEF_MAX_CHARS
    with pytest.raises(PolicyError, match=r"sessions\.handoff_caps.*at most 245,760"):
        load_policy(write_policy(tmp_path, original_task=HANDOFF_BRIEF_MAX_CHARS - others + 1))


# --- credential-read suppression for inputs of any size (C-23.14) -----------------------

BIG = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(12_000))


@pytest.mark.parametrize("name,value,sensitive", [
    ("Write", {"file_path": "/work/app.py", "content": BIG}, False),
    ("Edit", {"file_path": "/work/app.py", "old_string": BIG, "new_string": BIG + "\n"}, False),
    ("Write", {"file_path": "/work/.env", "content": BIG}, True),
    ("Bash", {"command": "cat > gen.py <<'EOF'\n" + BIG + "EOF\nprintenv\n" + BIG}, True),
    ("Bash", {"command": BIG + "\nsudo -E -H env | sort\n"}, True),
], ids=["write", "edit", "write-dotenv", "printenv-in-the-middle", "wrapper-env-at-the-end"])
def test_a_tool_input_of_any_size_is_classified_by_what_it_holds(name, value, sensitive):
    """C-23.14, C-25.5 (review of 7da13417, finding 2): an input over the budget was
    called a credential read, so a large `Write` showed as hidden and its result was
    left out of handoffs. It is matched in full, in linear time, like any other."""
    assert len(handoff._tool_corpus(value)) > BUDGET
    assert handoff.sensitive_tool_call(name, value) is sensitive
    started = redact.tool_started(name, value, tool_id="t1")
    assert started["hidden"] is sensitive
    if not sensitive:
        assert started["summary"] == "file_path: /work/app.py"


def _probe(script: str) -> dict:
    """A regressed, GIL-holding matcher runs in a bounded child of its own."""
    source = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
              "from subfleet.sessions.handoff import sensitive_tool_call\n" + script)
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("run", ["-time ", "-exec ", "sudo -a,time ", "nice -n 5 "])
def test_wrapper_flag_runs_are_matched_in_linear_time(run):
    """C-23.14: every wrapper word inside a run of flags (`-time -time …`) had
    searched the rest of the run again, quadratic in its length: 0.46 s of CPU for
    16 KiB on 7da13417, about two minutes for 256 KiB, while the scrubber's bound
    let such an input through."""
    measured = _probe(f"run = {run!r}\n" + '''
import json, time
samples = {}
for size in (16384, 65536):
    text = run * (size // len(run))
    start = time.thread_time()
    assert not sensitive_tool_call("Bash", {"command": text})
    samples[size] = time.thread_time() - start
print(json.dumps(samples))
''')
    assert measured["65536"] < max(measured["16384"], 0.001) * 8, measured
    assert measured["65536"] < 0.5, measured


#: The single pattern `_env_after_wrapper` replaces, as it stood in 7da13417: the
#: reference its walk is checked against.
ORIGINAL_ENV = re.compile(
    r"(?:^[ \t]*|[;&|(`]\s*|\b(?:sudo|doas|nice|nohup|time|command|exec|xargs)\s+(?:-\S+\s+)*"
    r"|[\"'`](?:command|cmd)[\"'`]?\s*:\s*[\"'`]|\bcmd\s*:\s*[\"'`]|\s-l?c\s+[\"'])"
    r"(?:[\w.~-]*/)*env(?=[\s;&|)\"'`]|$)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
ENV_PATTERN = next(p for p in handoff._SENSITIVE_TOOL_PATTERNS if p.pattern.endswith(handoff._ENV_COMMAND))
TOKENS = st.sampled_from([
    "sudo", "SUDO", "time", "-time", "nice", "xargs", "command", "exec", "doas", "nohup", "env", "ENV",
    "/usr/bin/env", "./env", "-x/env", "-E", "-n", "5", "--", "-", "-a,time", "x", "environ", "envy",
    ";", "&", "|", "(", ")", "`", "'", '"', ":", "cmd", "-c", "-lc", "sh", "\\",
])
SPACES = st.sampled_from([" ", "  ", "\t", "\n", ""])


@hypothesis.settings(max_examples=2_000, deadline=None)
@hypothesis.given(st.lists(st.tuples(TOKENS, SPACES), max_size=14))
def test_the_wrapper_walk_matches_the_single_pattern(parts):
    """C-23.14 (differential): the linear walk plus the remaining pattern find `env`
    exactly where the single 7da13417 pattern did."""
    text = "".join(token + space for token, space in parts)
    split = ENV_PATTERN.search(text) is not None or handoff._env_after_wrapper(text)
    assert split is (ORIGINAL_ENV.search(text) is not None), text
