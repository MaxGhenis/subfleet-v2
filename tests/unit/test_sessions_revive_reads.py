"""Cold-session inspection never waits on a desktop record (C-23.35)."""

from __future__ import annotations

import json

import pytest

from tests.nonblocking import run_child


@pytest.mark.parametrize("reader", ["store_metadata", "inspect"])
@pytest.mark.parametrize("bad_record", ["fifo", "symlink", "oversized"])
def test_cold_inspection_skips_unsafe_unrelated_desktop_record(tmp_path, reader, bad_record):
    """C-23.35: the desktop index is advisory; a FIFO, symlink or oversized
    record is unreadable and does not prevent locating a later valid copy.
    """
    source = f'''
        import json, os
        from pathlib import Path
        from subfleet.sessions import revive
        tmp = Path({str(tmp_path)!r})
        os.environ["SUBFLEET_SESSION_STORE"] = str(tmp / "desktop")
        os.environ["SUBFLEET_CLAUDE_DIR"] = str(tmp / "claude")
        records = tmp / "desktop" / "account" / "org"
        records.mkdir(parents=True)
        session_id = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
        expected = {{"cliSessionId": session_id, "cwd": str(tmp / "correct"),
                     "permissionMode": "bypassPermissions", "model": "claude-fable-5-1"}}
        (records / "local_999.json").write_text(json.dumps(expected))
        bad = records / "local_000.json"
        if {bad_record!r} == "fifo":
            os.mkfifo(bad)
        elif {bad_record!r} == "symlink":
            target = tmp / "other.json"
            target.write_text(json.dumps({{**expected, "cwd": str(tmp / "incorrect")}}))
            bad.symlink_to(target)
        else:
            bad.write_text(json.dumps({{**expected, "cwd": str(tmp / "incorrect"), "padding": "a" * (1024 * 1024)}}))
        revive.registry.find = lambda *args, **kwargs: None
        print("reading {reader}", flush=True)
        if {reader!r} == "store_metadata":
            found = revive.store_metadata(session_id)
            result = {{"cwd": found.get("cwd"), "model": found.get("model")}}
        else:
            found = revive.inspect(session_id, lane_ids=set(), facts={{}})
            result = {{"cwd": found.cwd, "model": found.model}}
        print(json.dumps(result), flush=True)
    '''
    result = json.loads(run_child(source).strip().splitlines()[-1])
    assert result == {"cwd": str(tmp_path / "correct"), "model": "claude-fable-5-1"}
