"""No read on a worker the daemon's close waits for waits in open() (C-25.3, C-23.28).

`Daemon.close()` calls `Timers.stop()`, which waits for the timers' workers (a probe,
a keepalive, the mirror), and then waits for its own pools: requests, workers (every
`submit`, `gate.*`, attempt finalization, the conversation tick) and the rest. A FIFO
with no writer where one of their reads opened plainly held it in open() until a
writer came, and `close()` with it: a lane's or the desktop's `auth.json`, a Claude
home's `.credentials.json` or `.claude.json`, the guard's TRUST and cached verdicts,
a lane's seed files, `conversations.sqlite3` for `status.json`, the mirror's lock,
an attempt's records and artifacts, a Codex rollout a turn attests, a prompt, a
gate's brief, plan or state, v1's rosters. (The workflow inventory of 2026-09-26 that
followed the review of aa41312 found them.) Each is checked on its own, in a child
process (`tests.nonblocking`): a regression fails its case and leaves nothing behind.

Each child prints one JSON line with what the reader answered.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from tests.nonblocking import run_child

PRELUDE = """
import json, os, sys, uuid
from pathlib import Path
from types import SimpleNamespace
tmp = Path({tmp!r})

def fifo(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    os.mkfifo(path)
    return path

def outcome(call):
    try:
        return {{"value": call()}}
    except Exception as exc:
        return {{"error": type(exc).__name__}}

def report(**values):
    print(json.dumps(values, default=repr), flush=True)
"""

READERS = {
    # --- the timers' workers (Timers.stop) --------------------------------------------
    "codex._read_auth (the desktop's and a lane's auth.json)": ("""
        from subfleet.adapters import codex
        fifo(tmp / "home" / "auth.json")
        report(**outcome(lambda: codex._read_auth(tmp / "home")))
        """, {"value": {}}),
    "Timers._epoch (a lane's auth.json)": ("""
        from subfleet.timers import Timers
        fifo(tmp / "home" / "auth.json")
        lane = SimpleNamespace(home=str(tmp / "home"), credential=SimpleNamespace(ref=str(tmp / "home"), epoch="E"))
        report(**outcome(lambda: Timers._epoch(None, lane)))
        """, {"value": "E"}),
    "claude.home_login (.credentials.json)": ("""
        from subfleet.adapters import claude
        claude._keychain_blob = lambda service, **kw: None      # no keychain in a test
        fifo(tmp / "home" / ".credentials.json")
        report(**outcome(lambda: claude.home_login(tmp / "home")))
        """, {"value": None}),
    "ClaudeAdapter.account_from_home (.claude.json)": ("""
        from subfleet.adapters.claude import ClaudeAdapter
        fifo(tmp / "home" / ".claude.json")
        report(**outcome(lambda: ClaudeAdapter.account_from_home(tmp / "home")))
        """, {"value": None}),
    "preflight.load_guard (TRUST)": ("""
        from subfleet.guard import preflight
        hook, pin = preflight.guard_paths(tmp / "state")
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\\n")
        fifo(pin)
        report(**outcome(lambda: preflight.load_guard(tmp / "state")))
        """, {"error": "ValueError"}),
    "preflight.read_seed_files (a lane's seed file, a FIFO once checked)": ("""
        from subfleet.guard import preflight
        for name in preflight.SEED_FILES:
            fifo(tmp / "home" / name)
        Path.is_file = lambda self: True                   # a regular file when checked, a FIFO when read
        report(**outcome(lambda: preflight.read_seed_files(tmp / "home")))
        """, {"value": {}}),
    "preflight.read_cached_verdict (a guard-cache marker)": ("""
        from subfleet.guard import preflight
        fifo(preflight._marker_path(tmp / "cache", "k"))
        report(**outcome(lambda: preflight.read_cached_verdict(tmp / "cache", "k")))
        """, {"value": None}),
    "store.status_summary (conversations.sqlite3)": ("""
        from subfleet.conversations.store import status_summary
        fifo(tmp / "state" / "conversations.sqlite3")
        report(**outcome(lambda: status_summary(tmp / "state")["available"]))
        """, {"value": False}),
    "Mirror._lock (mirror.lock)": ("""
        from subfleet.sessions import mirror
        running = mirror.Mirror(tmp / "state")
        fifo(running.dir / mirror.LOCK_NAME)
        report(**outcome(lambda: running._lock()))
        """, {"value": None}),
    # --- finalization and attestation (the workers pool) ------------------------------
    "codex._events (a rollout or a stream)": ("""
        from subfleet.adapters import codex
        report(**outcome(lambda: list(codex._events(fifo(tmp / "sessions" / "rollout.jsonl")))))
        """, {"value": []}),
    "Adapter.read_text (stderr)": ("""
        from subfleet.adapters.base import Adapter
        report(**outcome(lambda: Adapter.read_text(fifo(tmp / "a1" / "stderr"))))
        """, {"value": ""}),
    "ClaudeAdapter.stream_summary (stdout)": ("""
        from subfleet.adapters.claude import ClaudeAdapter
        fifo(tmp / "a1" / "stdout")
        adapter = ClaudeAdapter.__new__(ClaudeAdapter)
        report(**outcome(lambda: adapter.stream_summary(tmp / "a1").lines_total))
        """, {"value": 0}),
    "claude.link_raw_stream (a FIFO at the stream's name mid-copy)": ("""
        from subfleet.adapters import claude
        (tmp / "a1").mkdir()
        (tmp / "a1" / "stdout").write_text("{}\\n")
        launch = SimpleNamespace(raw_stream_path=str(tmp / "a1" / "stream.jsonl"), stdout_path=None)
        def no_link(*a, **k):
            raise OSError("cross-device link")
        claude.os.link = no_link
        real, real_bytes = getattr(claude, "read_regular", None), Path.read_bytes
        def planted(read):
            def call(path, *a, **k):
                fifo(tmp / "a1" / "stream.jsonl")          # appears after the check, before the copy
                return read(path, *a, **k)
            return call
        claude.read_regular = planted(real) if real else None
        Path.read_bytes = planted(real_bytes)
        report(**outcome(lambda: claude.link_raw_stream(tmp / "a1", launch)))
        """, {"value": None}),
    "Daemon._read_json (exit.json)": ("""
        from subfleet.daemon import Daemon
        report(**outcome(lambda: Daemon._read_json(fifo(tmp / "a1" / "exit.json"))))
        """, {"value": None}),
    "Daemon._artifact (an attempt's artifact)": ("""
        from subfleet.daemon import Daemon
        report(**outcome(lambda: Daemon._artifact(fifo(tmp / "a1" / "stdout"), "stdout")))
        """, {"value": None}),
    "Daemon._validate_home (a Codex lane's auth.json)": ("""
        from subfleet.contracts import Credential, Lane, LaneOwner
        from subfleet.daemon import Daemon
        fifo(tmp / "home" / "auth.json")
        Path.is_file = lambda self: True                   # a regular file when checked, a FIFO when read
        lane = Lane("codex-1", "codex", "codex:one", Credential("codex", str(tmp / "home"), "home"),
                    str(tmp / "home"), LaneOwner.V2, False)
        report(**outcome(lambda: Daemon._validate_home(lane)))
        """, {"value": None}),
    # --- ops on the requests and workers pools ----------------------------------------
    "capacity.read_desktop_account (~/.claude.json)": ("""
        from subfleet import capacity
        report(**outcome(lambda: capacity.read_desktop_account(fifo(tmp / ".claude.json"))))
        """, {"value": None}),
    "capacity.cached_desktop_identity (~/.claude.json)": ("""
        from subfleet import capacity
        report(**outcome(lambda: capacity.cached_desktop_identity(fifo(tmp / ".claude.json"))))
        """, {"value": {}}),
    "gate read_context (--brief)": ("""
        from subfleet.gate import service
        report(**outcome(lambda: service.read_context(str(fifo(tmp / "brief.md")), "brief")))
        """, {"error": "GateError"}),
    "gate revision.plan (the plan file, a FIFO once checked)": ("""
        from subfleet.gate import revision
        plan = fifo(tmp / "plan.md")
        Path.is_file = lambda self: True                   # a regular file when checked, a FIFO when read
        report(**outcome(lambda: revision.plan(plan)))
        """, {"error": "GateError"}),
    "gate certificate.load_state (gate.json)": ("""
        from subfleet.gate import certificate
        fifo(tmp / "gate" / "gate.json")
        report(**outcome(lambda: certificate.load_state(tmp / "gate")))
        """, {"error": "GateError"}),
    "lanes_transfer._read_text (a v1 roster)": ("""
        from subfleet import lanes_transfer
        report(**outcome(lambda: lanes_transfer._read_text(fifo(tmp / "roster.json"))))
        """, {"value": ""}),
    "lanes_transfer._publish (a FIFO at the old temporary name)": ("""
        import stat
        from subfleet import lanes_transfer
        roster = tmp / "roster.json"
        roster.write_text("{}")
        old = fifo(tmp / "roster.json.tmp")
        result = outcome(lambda: lanes_transfer._publish(roster, '{"moved": true}'))
        report(**result, written=roster.read_text(), fifo=stat.S_ISFIFO(os.lstat(old).st_mode))
        """, {"value": None, "written": '{"moved": true}', "fifo": True}),
    "lanes_transfer._backup (a v1 roster that is a FIFO)": ("""
        from subfleet import lanes_transfer
        report(**outcome(lambda: lanes_transfer._backup(fifo(tmp / "roster.json"))))
        """, {"error": "NotRegularFile"}),
}


@pytest.mark.parametrize("reader", list(READERS))
def test_a_fifo_where_a_waited_for_worker_reads_answers_at_once(reader, tmp_path):
    """C-23.28, C-25.3: each reader answers for a FIFO as for an unreadable file
    (nothing, or the refusal it gives any unreadable file), at once."""
    source, expected = READERS[reader]
    out = run_child(PRELUDE.format(tmp=str(tmp_path)) + textwrap.dedent(source))
    got = json.loads(out.strip().splitlines()[-1])
    assert {k: got.get(k) for k in expected} == expected, got


SUBMIT = """
import logging, threading
from subfleet import daemon as daemon_module, protocol
from subfleet.daemon import Daemon
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.store import Store
root = tmp / "state"
(root / "jobs").mkdir(parents=True)
work = tmp / "work"
work.mkdir()
daemon = object.__new__(Daemon)
daemon.root, daemon.store = root, Store(root / "state.sqlite3")
daemon.policy, daemon.policy_digest = load_policy(DEFAULT_POLICY_PATH), "test"
daemon._submit_lock = threading.Lock()
daemon.log = logging.getLogger("fifo-submit")
daemon._notify = lambda: None
prompt = fifo(tmp / "prompt.md")
args = protocol.SubmitArgs(request_id="fifo-prompt", kind="run", workdir=str(work), prompt_path=str(prompt),
                           sandbox="read-only", allow_tmp=True, pinned_model="astra")
try:
    daemon.submit(args)
    result = {"value": "submitted"}
except Exception as exc:
    result = {"error": type(exc).__name__, "message": str(exc)}
report(**result, lock_free=daemon._submit_lock.acquire(timeout=5))
"""


def test_a_submit_naming_a_fifo_as_its_prompt_answers_at_once_and_frees_the_submit_lock(tmp_path):
    """C-6.2: `submit` read its prompt with a plain open() inside `_submit_lock`: a
    FIFO named as the prompt held that submit, every submit after it (gate rounds and
    conversation turns among them), and the workers pool `Daemon.close()` waits for.
    It is refused at once, as an unreadable prompt is, and the lock is free."""
    out = run_child(PRELUDE.format(tmp=str(tmp_path)) + SUBMIT)
    got = json.loads(out.strip().splitlines()[-1])
    assert "not a regular file" in got.get("message", "") and got["lock_free"] is True, got
