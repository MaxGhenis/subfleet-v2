"""What a person reads of daemon.log is scrubbed and keeps its lines (C-25.7, #233 review r3).

- `daemon start`'s tail (`cli._log_tail`, P3-3): at cce8f1af7, when folding let the
  scrubber find more anywhere in the kept text, the whole tail became one folded line.
  It now keeps its last lines, each run written only where the folded rescrub shows it.
- `daemon logs` (the reviewer's note): daemon.log is never rewritten, so it holds what a
  daemon before D-LOG1 wrote raw, and `daemon logs` printed it raw. It now prints the
  range as the tail is printed, and `--follow` holds each new line until no match can
  cross the break after it (`daemon_log.LogFollower`).
"""
from __future__ import annotations

import random
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet import cli, daemon_log
from tests.unit import test_daemon_log_handler as handler_tests

SECRET_RUN = re.compile(r"SECRET7[a-z0-9]*")
BOUNDED = settings(max_examples=120, derandomize=True, database=None, deadline=None)


def secrets(text: str) -> set[str]:
    return set(SECRET_RUN.findall(text))


def admissions(count: int, start: int = 0) -> list[str]:
    return [f"admission: placed job {n} on lane codex-0" for n in range(start, start + count)]


TRACEBACK = ["Traceback (most recent call last):", '  File "/x/subfleet/daemon.py", line 12, in work',
             "    do_the_work(item, attempt)", "RuntimeError: the lane went away"]


def review_r3_log(flag_line: int) -> str:
    """Review r3's 60-line daemon.log: `guardian argv: subfleet-guardian --auth-token` at
    `flag_line` (1-based), its value on the next line, the last lines a traceback."""
    lines = admissions(56)
    lines[flag_line - 1:flag_line + 1] = ["guardian argv: subfleet-guardian --auth-token",
                                          "SECRET7authtok9 --lane codex-0"]
    return "\n".join(lines[:56] + TRACEBACK) + "\n"


def test_daemon_start_s_tail_keeps_its_last_twenty_lines(tmp_path):
    """Review r3, P3-3: in a 60-line daemon.log whose line 6 ends in `--auth-token` and
    whose last lines are a traceback, `_log_tail` returned one line of 2,521 characters
    starting from the file's first line. It is the file's last 20 lines."""
    path = tmp_path / "daemon.log"
    path.write_text(review_r3_log(6))
    tail = cli._log_tail(path).splitlines()
    assert "SECRET7" not in "\n".join(tail)
    assert tail == [f"  | {line}" for line in review_r3_log(6).splitlines()[-20:]]


def test_a_flag_s_value_in_the_tail_stays_hidden_with_the_lines_kept(tmp_path):
    """The same log with the flag among the last 20 lines: its value is hidden where it
    stands, and every other line is the file's."""
    path = tmp_path / "daemon.log"
    raw = review_r3_log(46).splitlines()
    path.write_text("\n".join(raw) + "\n")
    tail = [line[4:] for line in cli._log_tail(path).splitlines()]
    assert len(tail) == 20 and "SECRET7authtok9" not in "\n".join(tail)
    assert tail[5] == "guardian argv: subfleet-guardian --auth-token" and tail[6] == "[REDACTED] --lane codex-0"
    assert [line for index, line in enumerate(tail) if index != 6] == [
        line for index, line in enumerate(raw[-20:]) if index != 6]


#: What a daemon before D-LOG1 left in daemon.log raw: a value on its key's line, one on
#: the line after its key, a header's, a flag's on the next line, and one in a group.
PRE_D_LOG1 = ("2026-10-01 an older daemon said: password=SECRET7older9\n"
              "2026-10-01 its next record: token:\nSECRET7nextline9 and more\n"
              "Authorization: Bearer SECRET7header9value\n"
              "guardian argv: subfleet-guardian --auth-token\nSECRET7flagline9 --lane codex-0\n"
              "  | member said: api_key:\n  | SECRET7member9\n")


def test_daemon_logs_prints_an_older_daemon_s_raw_lines_scrubbed(root, capsys):
    """The reviewer's note: `daemon logs` printed daemon.log raw, so what a daemon before
    D-LOG1 wrote reached, for example, an agent's transcript. It prints the range as
    `daemon start`'s tail does: scrubbed, its lines kept."""
    (root / "daemon.log").write_text("\n".join(admissions(30)) + "\n" + PRE_D_LOG1 + "\n".join(admissions(3, 30)) + "\n")
    assert cli.main(["daemon", "logs", "-n", "14"]) == 0
    out = capsys.readouterr().out
    assert "SECRET7" not in out, out
    lines = out.splitlines()
    assert len(lines) == 14
    assert lines[:3] == admissions(3, 27)
    assert "2026-10-01 an older daemon said: password=[REDACTED]" in lines
    assert "[REDACTED] --lane codex-0" in lines and lines[-1] == "admission: placed job 32 on lane codex-0"


def follow(root, monkeypatch, chunks: list[str], lines: int = 3) -> str:
    """`daemon logs -f -n LINES`: each wait appends the next chunk to daemon.log, as a
    daemon would; after the last, the person stops it (^C)."""
    path = root / "daemon.log"
    pending = list(chunks)

    def wait(_seconds):
        if not pending:
            raise KeyboardInterrupt
        with path.open("a") as log:
            log.write(pending.pop(0))
    monkeypatch.setattr(cli.time, "sleep", wait)
    assert cli.main(["daemon", "logs", "-f", "-n", str(lines)]) == 0


def test_daemon_logs_follow_prints_new_lines_scrubbed(root, monkeypatch, capsys):
    """With `--follow`, lines arrive one write at a time. A line ending in a credential
    flag is held until its value's line comes, and both are written scrubbed; so is a
    value on the line after a key the range already showed, and a quoted value whose
    lines arrive apart. Nothing an older daemon wrote raw is printed raw."""
    (root / "daemon.log").write_text("\n".join(admissions(5)) + "\n" + "its next record: token:\n")
    follow(root, monkeypatch, [
        "SECRET7afterkey9 and more\n",
        "guardian argv: subfleet-guardian --auth-tok", "en\n",
        "SECRET7flagwait9 --lane codex-0\nplain line\n",
        "upstream said: password='open\n", "more SECRET7quoted9\n", "close'\n",
        PRE_D_LOG1, "Authorization: Bearer\n",
    ])
    out = capsys.readouterr().out
    assert "SECRET7" not in out, out
    lines = out.splitlines()
    # The file ends in a key whose value is not written yet: that line is held with it.
    assert lines[:3] == admissions(3, 2)
    assert lines[3:5] == ["its next record: token:", "[REDACTED] and more"]
    assert lines[5:8] == ["guardian argv: subfleet-guardian --auth-token", "[REDACTED] --lane codex-0", "plain line"]
    assert "upstream said: password='[REDACTED]'" in lines
    assert lines[-1] == "Authorization: [REDACTED]"            # held at the end, written at ^C


# --- the follower --------------------------------------------------------------------

def test_a_line_ending_in_a_flag_is_held_until_its_value_comes():
    follower = daemon_log.LogFollower("start\n")
    assert follower.feed("deploy --password\n") == []
    assert follower.feed("SECRET7held9 rest\nnext\n") == ["deploy --password", "[REDACTED] rest", "next"]


def test_a_value_whose_key_was_already_written_is_hidden_beside_it():
    """A key written with held text past the hold's bound: the lines after it are
    scrubbed beside what was written before them."""
    follower = daemon_log.LogFollower("start\n")
    held = "x='open\n" + "line\n" * (daemon_log.FOLLOW_HOLD_MAX // 5) + "a record: token:\n"
    assert follower.feed(held)[-1] == "a record: token:"
    assert follower.feed("SECRET7context9 rest\n") == ["[REDACTED] rest"]


def test_a_value_open_when_following_starts_is_held_with_its_lines():
    """The file already ends inside a quoted value when following starts: its lines are
    not printed until the value closes, and then hidden with it."""
    before = "start\nupstream said: password='open\nmore SECRET7already9\n"
    follower = daemon_log.LogFollower(before)
    assert before[:follower.settled] == "start\n"
    assert daemon_log.scrubbed_tail(before[:follower.settled], 20) == ["start"]
    assert follower.feed("close'\nnext\n") == ["upstream said: password='[REDACTED]'", "next"]


def test_what_is_held_is_written_when_following_stops():
    follower = daemon_log.LogFollower("")
    assert follower.feed("deploy --password\n- the rest of the line") == []
    assert follower.flush() == ["deploy --password", "- the rest of the line"]
    assert follower.flush() == []


@pytest.mark.parametrize("opened", ["-----BEGIN PRIVATE KEY-----\nMIIabc\n", "<system-reminder>\nnote\n",
                                    "data:image/png;base64,QUJDREVGR0hJSktMTU5PUFFSU1RVVldY\nQUJD\n"])
def test_a_private_key_a_reminder_or_a_data_uri_is_held_until_it_ends(opened):
    follower = daemon_log.LogFollower("start\n")
    assert follower.feed(opened) == []
    assert follower.feed("SECRET7inside9\n") == []
    closing = {"-": "-----END PRIVATE KEY-----\n", "<": "</system-reminder>\n", "d": "end.\n"}[opened[0]]
    written = follower.feed(closing) + follower.flush()
    assert "SECRET7inside9" not in "\n".join(written), written


@pytest.mark.documented_limitation
def test_held_text_past_the_bound_is_written_as_it_is_documented_limitation():
    """C-25.7's residual limit: held text is written once it passes `FOLLOW_HOLD_MAX`,
    scrubbed as it is, so the part of a quoted value (or a private key, a data URI, or
    what follows a reminder tag) written before its end arrives may show."""
    follower = daemon_log.LogFollower("start\n")
    assert follower.feed("upstream said: password='open\n") == []
    filler = "more of the value\n" * (daemon_log.FOLLOW_HOLD_MAX // 18)
    written = follower.feed(filler + "SECRET7pastbound9\n")
    assert written and "SECRET7pastbound9" in "\n".join(written)


# --- the properties ----------------------------------------------------------------------

PLAIN = ["admission: placed job 7 on lane codex-0", '  File "/x/a.py", line 3, in f', "    raise RuntimeError(text)",
         "it's here", "a = 'one'", 'b: "two"', "plain words only", "  | member line", "  +-+---------------- 1 ----------------",
         "x:", "=", "--verbose", "Traceback (most recent call last):", "stopping: 2 turns"]
LINES = st.lists(st.one_of(st.sampled_from(PLAIN).map(lambda line: line + "\n"),
                           handler_tests.SECRETS.flatmap(handler_tests.lines_with)), min_size=1, max_size=60)
PLAIN_LOGS = st.lists(st.sampled_from(PLAIN), min_size=1, max_size=60).map(lambda lines: "\n".join(lines) + "\n")


def reference(text: str) -> set[str]:
    """The secrets the handler's forms show of the whole text, uncut: the block, its lines
    kept, each run only where the folded rescrub shows it too."""
    return secrets(daemon_log.scrubbed_block(text, group=daemon_log._GROUP_MARK in text, keep_lines=True))


@BOUNDED
@given(lines=LINES, count=st.integers(1, 80), before=st.sampled_from([0, 0, 2000, 40_000]))
def test_a_tail_never_shows_a_secret_the_folded_rescrub_hides(lines, count, before):
    """Property: over generated logs, cut or not, the tail shows no secret that the whole
    log's block with its lines kept (the folded rescrub's runs) hides."""
    text = "plain words only\n" * (before // 17) + "".join(lines)
    assert secrets("\n".join(daemon_log.scrubbed_tail(text, count))) <= reference(text)


@BOUNDED
@given(text=PLAIN_LOGS, count=st.integers(1, 80), before=st.sampled_from([0, 40_000]))
def test_a_tail_with_no_credential_shaped_record_is_the_raw_last_lines(text, count, before):
    text = "plain words only\n" * (before // 17) + text
    assert daemon_log.scrubbed_tail(text, count) == text.splitlines()[-count:]


def fed(text: str, rng: random.Random) -> list[str]:
    """`text` through a follower in random pieces, the first piece already in the file."""
    cuts = sorted(rng.sample(range(len(text) + 1), min(len(text) + 1, rng.randint(1, 12))))
    pieces = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
    first = pieces.pop(0)
    follower = daemon_log.LogFollower(first)
    out = daemon_log.scrubbed_tail(first[:follower.settled], 10_000)
    for piece in pieces:
        out += follower.feed(piece)
    return out + follower.flush()


@BOUNDED
@given(lines=LINES, seed=st.integers(0, 2 ** 16))
def test_following_never_shows_a_secret_the_folded_rescrub_hides(lines, seed):
    """Property: a log written in any pieces, followed from any point, shows no secret the
    whole log's block with its lines kept hides."""
    text = "".join(lines)
    assert secrets("\n".join(fed(text, random.Random(seed)))) <= reference(text), text


@BOUNDED
@given(text=PLAIN_LOGS, seed=st.integers(0, 2 ** 16))
def test_following_a_log_with_no_credential_shaped_record_writes_its_lines(text, seed):
    assert fed(text, random.Random(seed)) == text.splitlines()
