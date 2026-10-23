"""`daemon_log.scrubbed_tail` as of 1cf653dff (round 5 of #233), for the differential.

Copied verbatim from `subfleet/daemon_log.py` at 1cf653dff, except that the bound is read
from `daemon_log` when it is called, so a test that sets `daemon_log.RECORD_SCRUB_MAX` sets
it for both. Everything else it calls (`scrubbed_block`, `cut_whole`, `failed_line`) is the
same code at round 6. Round 6 changes only what a text with a reminder tag gives
(`daemon_log._reminders_resolved`, `neutralised`): on every text without one, the two agree
byte for byte (`test_daemon_log_reminders`).
"""
from __future__ import annotations

import logging

from subfleet import daemon_log
from subfleet.daemon_log import _GROUP_MARK, _record, _tail_start, failed_line, scrubbed_block
from subfleet.sessions.handoff import cut_whole


def scrubbed_tail(text: str, count: int) -> list[str]:
    """The last `count` lines of `text`, a daemon.log, for a person's terminal (C-25.7:
    `daemon start`'s tail, `daemon logs`): the text scrubbed as the daemon's handler
    scrubs a record of it, cut first (`cut_whole`) to `RECORD_SCRUB_MAX` or, for more
    lines than its tail holds, to twice what they hold and `RECORD_SCRUB_MAX` more,
    then in the block form with its lines kept (`scrubbed_block(..., keep_lines=True)`),
    then its last `count` lines. Whoever wrote the file, an earlier daemon included,
    no line shows a value the handler's own forms would hide. Never raises: what
    cannot be scrubbed is the fixed `[scrub failed]` line."""
    RECORD_SCRUB_MAX = daemon_log.RECORD_SCRUB_MAX
    if count <= 0:
        return []
    try:
        limit = max(RECORD_SCRUB_MAX, 2 * (len(text) - _tail_start(text, count)) + RECORD_SCRUB_MAX)
        block = scrubbed_block(cut_whole(text, limit), group=_GROUP_MARK in text, keep_lines=True)
        return block.splitlines()[-count:]
    except Exception as exc:                # noqa: BLE001 - the marker alone, never the text
        return [failed_line(_record("", logging.INFO), exc)]
