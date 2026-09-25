"""Probe (pre-existing path, not changed by PR #47): an `attachment.add` in flight on
the service's `files` pool when close() runs. close() shuts that pool down with
wait=False, so the op finishes afterwards; `attachments.add` makes its directory with
parents=True. The delay is injected by holding `sniff` on an Event.

Run from the worktree root: python .probe/attachment_after_close_probe.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subfleet.conversations import attachments  # noqa: E402
from subfleet.conversations.service import ConversationService  # noqa: E402
from tests.unit.test_conversation_service import FakeDaemon  # noqa: E402

base = Path(tempfile.mkdtemp(prefix="pr47-att-"))
root = base / "state"
root.mkdir()
image = base / "pixel.png"
image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
daemon = FakeDaemon(root)
svc = ConversationService(daemon)
entered, go = threading.Event(), threading.Event()
real_sniff = attachments.sniff


def slow_sniff(head):
    entered.set()
    go.wait(30)
    return real_sniff(head)


attachments.sniff = slow_sniff
future = svc.pool_for("attachment.add").submit(svc.handle, "attachment.add", {"path": str(image)}, None)
assert entered.wait(30), "the op never started"
svc.close()
daemon.store.close()
shutil.rmtree(root)
print("closed; root removed:", not root.exists())
go.set()
try:
    print("op result:", future.result(30))
except Exception as exc:
    print("op raised:", type(exc).__name__, exc)
print("root exists after the op:", root.exists(), sorted(p.name for p in root.rglob("*")) if root.exists() else None)
shutil.rmtree(base, ignore_errors=True)
