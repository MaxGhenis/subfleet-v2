"""One optional title request, asked of the first Claude turn's own process after its reply.

Claude Code 2.1.280's installed binary exposes generate_session_title with
{description: str, persist?: bool}, returning {title: str | null}. Its dispatch
table runs this control asynchronously. No subprocess or account is added.

The request goes only at a quiescent point (`TurnRunner._title_quiescent`): the
first turn's successful `result` has been read and recorded, the provider is idle
and reading stdin, and nothing else of the conversation waits (a stop, a steer,
an answer, a command, a queued message). There is no running turn then, so the
request can race no stop, and its one line goes to an idle reader. The turn's
stdin close is held after it until the answer, the budget, or anything else for
the conversation (`TurnRunner._title_release`). The CLI runs this control
without awaiting it, and its end-of-input teardown (2.1.280: `onInputClosed`,
then `Gc`) awaits pending shell commands, prompt suggestions and remote-control
calls, not controls, before it closes its output. A close written right after the
request would race the title's model call against that teardown.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

TITLE_REQUEST_ID = "subfleet-session-title"
TITLE_FRAME = "session-title"
# A cancellation an earlier build of this candidate wrote after the budget. None is
# written now (the budget ends the hold and stdin closes); its tag stays known so a
# replay of such an attempt reads the log as the other optional frame.
TITLE_CANCEL_FRAME = "session-title-cancel"
TITLE_BUDGET_S = 10.0
TITLE_MAX = 120
TITLE_DESCRIPTION_MAX = 16_384      # characters of the first message offered to the generator
# Bytes of the request's stdin line. Defense in depth: the line goes only to an idle
# provider, behind nothing it may not have read (`TurnRunner._title_fits`), and this
# cap keeps it far below the smallest pipe buffer (PIPE_FLOOR).
TITLE_LINE_MAX = 8 * 1024
# The least a provider's stdin pipe holds. A pipe here held 64 KiB with no reader
# (measured 2026-09-29, for 100-byte to 64 KiB writes); XNU pipes start at 16 KiB and
# grow on demand, so 16 KiB is the floor the title's write is sized against.
PIPE_FLOOR = 16 * 1024

# The title's states. Only the runner thread claims and sends; anyone may end it.
OPEN, CLAIMED, SENT, ENDED = "open", "claimed", "sent", "ended"

_REQUEST_PREFIX = re.compile(
    r"^(?:(?:please|kindly)(?:\s+|$)|(?:can|could|would|will)\s+you(?:\s+|$)|"
    r"(?:I\s+(?:want|need|would\s+like)\s+(?:you\s+)?to|help\s+me(?:\s+to)?)(?:\s+|$)|"
    r"(?:fix|add|build|create|implement|update|change|remove|delete|write|make|"
    r"debug|investigate|explain|review|refactor|find|show|check|improve)(?:\s+|$))",
    re.IGNORECASE,
)


def fallback_title(text: str) -> str:
    """First clause, about six words; keep identifiers and the person's language."""
    clause = re.split(r"[\n;!?]|[.,](?=\s)", text[:4096].strip(), maxsplit=1)[0]
    while match := _REQUEST_PREFIX.match(clause):
        clause = clause[match.end():].lstrip()
    title = " ".join(clause.split()[:6]).strip(" .:;!?")
    return title[:TITLE_MAX].rstrip() or "New conversation"


def _request_line(description: str) -> str:
    payload = {"type": "control_request", "request_id": TITLE_REQUEST_ID,
               "request": {"subtype": "generate_session_title", "description": description, "persist": False}}
    return json.dumps(payload, separators=(",", ":"))


def request_line(text: str) -> str:
    """The title request's stdin line, at most TITLE_LINE_MAX bytes: its description
    is the longest prefix of the first message that fits. json.dumps escapes every
    character on its own and all but ASCII, so a line's characters are its bytes."""
    description = text[:TITLE_DESCRIPTION_MAX]
    line = _request_line(description)
    if len(line) <= TITLE_LINE_MAX:
        return line
    room = TITLE_LINE_MAX - len(_request_line(""))
    for end, character in enumerate(description):
        room -= len(json.dumps(character)) - 2
        if room < 0:
            return _request_line(description[:end])
    return line                             # unreachable: the whole line did not fit


def parse_answer(raw: bytes | str) -> tuple[bool, str | None]:
    """(whether `raw` is the title's control response, the title it names if usable)."""
    # Most stdout lines never need JSON decoding a second time.
    marker = TITLE_REQUEST_ID.encode() if isinstance(raw, bytes) else TITLE_REQUEST_ID
    if marker not in raw:
        return False, None
    try:
        row = json.loads(raw)
    except ValueError:
        return False, None
    if not isinstance(row, dict) or row.get("type") != "control_response":
        return False, None
    response = row.get("response")
    if not isinstance(response, dict) or response.get("request_id") != TITLE_REQUEST_ID:
        return False, None
    body = response.get("response")
    if response.get("subtype") != "success" or not isinstance(body, dict):
        return True, None
    title = body.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        return True, None
    return True, " ".join(title.split())


class SessionTitle:
    """The conversation's one title request, from its first turn's own process.

    Best effort only: neither title errors nor absent answers change a turn. The
    states go open → claimed → sent → ended, and any of them may end. The runner
    claims (in the transaction that records the turn's result, `ConversationStore.
    append_events`) and sends; a stop ends the title before it is recorded
    (`ConversationService._interrupt`, `TurnRunner.interrupt`). Each transition is a
    compare-and-set under a lock no I/O is done under, so ending never waits on a
    write, and no write begins once the title has ended.
    """

    def __init__(self, conversation_id: str, message_id: str, *, clock: Callable[[], float] = time.time):
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.clock = clock
        self._lock = threading.Lock()
        self.state = OPEN
        self.why: str | None = None             # what ended it
        self.claimed_at: float | None = None
        self.answered = False                   # the provider answered the request, usable or not
        self._answer: tuple[str, float] | None = None   # a generated title not yet recorded

    @property
    def holding(self) -> bool:
        """Claimed or sent, not yet ended: the turn's stdin close waits."""
        return self.state in (CLAIMED, SENT)

    def end(self, why: str) -> str:
        """No title frame is written from now on (one being written goes on). Returns
        the state it ended from. Never waits on I/O."""
        with self._lock:
            previous = self.state
            if previous != ENDED:
                self.state, self.why = ENDED, why
            return previous

    def claim(self, at: float) -> bool:
        """open → claimed, after the store granted the conversation's one request."""
        with self._lock:
            if self.state != OPEN:
                return False
            self.state, self.claimed_at = CLAIMED, at
            return True

    def begin_write(self, may: Callable[[], bool]) -> bool:
        """claimed → sent, when `may()` (in-memory checks only) still holds: the
        decision to write is ordered against every `end`."""
        with self._lock:
            if self.state != CLAIMED or not may():
                return False
            self.state = SENT
            return True

    def expired(self) -> bool:
        return self.claimed_at is not None and self.clock() >= self.claimed_at + TITLE_BUDGET_S

    def receive(self, raw: bytes | str) -> None:
        """The provider's answer, whenever it comes (after the hold, or during a
        replay's read of stdout); recorded with the runner's next event batch."""
        try:
            ours, title = parse_answer(raw)
        except Exception:                       # never a reason for a turn to fail
            return
        if not ours:
            return
        self.answered = True
        if title is not None and self._answer is None:
            self._answer = (title, self.clock())

    def take(self) -> tuple[str, float] | None:
        """The generated title received and not yet handed to the store, once."""
        answer, self._answer = self._answer, None
        return answer

    @property
    def pending(self) -> bool:
        return self._answer is not None
