"""One optional title request, multiplexed through the first Claude turn.

Claude Code 2.1.280's installed binary exposes generate_session_title with
{description: str, persist?: bool}, returning {title: str | null}. Its dispatch
table runs this control asynchronously. No subprocess or account is added.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Callable

from .turn import Frame

if TYPE_CHECKING:
    from .store import ConversationStore

TITLE_REQUEST_ID = "subfleet-session-title"
TITLE_FRAME = "session-title"
TITLE_CANCEL_FRAME = "session-title-cancel"
TITLE_BUDGET_S = 10.0
TITLE_MAX = 120

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


class SessionTitle:
    """Best effort only: neither title errors nor absent responses end a turn.

    The store claims generation once, so a replay/retry cannot regenerate. The
    ten-second result budget is persisted and checked again at the atomic write;
    a late result, a rename, or a title already generated always wins over it.
    """

    def __init__(self, store: ConversationStore, conversation_id: str, message_id: str,
                 *, clock: Callable[[], float] = time.time, log=None):
        self.store = store
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.clock = clock
        self.log = log
        self.deadline: float | None = None

    def request(self, text: str) -> Frame | None:
        try:
            now = self.clock()
            if not self.store.claim_title_generation(self.conversation_id, self.message_id, now):
                return None
            self.deadline = now + TITLE_BUDGET_S
            payload = {"type": "control_request", "request_id": TITLE_REQUEST_ID,
                       "request": {"subtype": "generate_session_title", "description": text[:16_384],
                                   "persist": False}}
            return Frame(TITLE_FRAME, "write", json.dumps(payload, separators=(",", ":")))
        except Exception as exc:
            self._failed(exc)
            return None

    def receive(self, raw: bytes | str) -> None:
        # Most stdout lines never need JSON decoding a second time.
        marker = TITLE_REQUEST_ID.encode() if isinstance(raw, bytes) else TITLE_REQUEST_ID
        if marker not in raw:
            return
        try:
            row = json.loads(raw)
            if not isinstance(row, dict) or row.get("type") != "control_response":
                return
            response = row.get("response")
            if not isinstance(response, dict) or response.get("request_id") != TITLE_REQUEST_ID:
                return
            self.deadline = None
            body = response.get("response")
            if response.get("subtype") != "success" or not isinstance(body, dict):
                return
            title = body.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
                return
            self.store.generated_title(self.conversation_id, self.message_id,
                                       " ".join(title.split()), self.clock())
        except Exception as exc:
            self._failed(exc)

    def expire(self) -> Frame | None:
        """Scoped cancellation is best effort; the result cutoff is unconditional.

        The verified CLI control cancellation may abort an operation if supported.
        Never use interrupt: that would stop the person's turn along with its title.
        """
        if self.deadline is None or self.clock() < self.deadline:
            return None
        self.deadline = None
        return Frame(TITLE_CANCEL_FRAME, "write", json.dumps(
            {"type": "control_cancel_request", "request_id": TITLE_REQUEST_ID}))

    def _failed(self, exc: Exception) -> None:
        if self.log:
            self.log.debug("optional session title for %s unavailable: %s", self.conversation_id, exc)
