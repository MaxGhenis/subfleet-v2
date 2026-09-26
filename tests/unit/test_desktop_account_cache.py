"""C-10.3, C-3.7: the desktop login hint is parsed once per version of its file.

`~/.claude.json` (~250 KB) was parsed on every admission pass, up to twenty
times a second, and on every status-style request. It is now parsed again only
when the file is a different file or has been written or touched since, which
any login switch does, so a switched login is still seen at the next call.
"""

from __future__ import annotations

import json
import os

from subfleet import capacity
from subfleet.capacity import read_desktop_account      # the real one, before conftest's stand-in


def login(path, email: str) -> None:
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": email}, "padding": "x" * 1000}))


def test_an_unchanged_login_file_is_parsed_once(tmp_path, monkeypatch):
    path = tmp_path / ".claude.json"
    login(path, "A@Example.org")
    parsed = []
    loads = json.loads
    monkeypatch.setattr(capacity.json, "loads", lambda text, **k: parsed.append(1) or loads(text, **k))
    assert [read_desktop_account(path) for _ in range(20)] == ["a@example.org"] * 20
    assert len(parsed) == 1


def test_a_switched_login_is_seen_at_the_next_call(tmp_path):
    path = tmp_path / ".claude.json"
    login(path, "a@example.org")
    assert read_desktop_account(path) == "a@example.org"
    login(path, "b@example.org")                        # same size, rewritten in place
    assert read_desktop_account(path) == "b@example.org"
    stat = path.stat()
    login(path, "c@example.org")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))   # even with the old mtime put back
    assert read_desktop_account(path) == "c@example.org"
    replacement = tmp_path / "next.json"
    login(replacement, "d@example.org")
    os.replace(replacement, path)                        # an atomic replace is a new file
    assert read_desktop_account(path) == "d@example.org"


def test_an_unreadable_login_is_unknown_and_not_remembered(tmp_path):
    path = tmp_path / ".claude.json"
    assert read_desktop_account(path) is None           # absent
    path.write_text("{not json")
    assert read_desktop_account(path) is None           # unparseable
    login(path, "e@example.org")
    assert read_desktop_account(path) == "e@example.org"
