"""Scratch review probe — deleted after the run."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.fake.test_sessions_end_to_end import (  # noqa: F401
    ALICE, world, live_pids, run, workdir, stage, cold_desktop_session, until,
)
from subfleet.sessions import revive as revive_module
from subfleet.sessions import nudge as nudge_module
from tests import sessions_fixtures as fx


def test_revived_session_becomes_a_lane_session(world):
    service, client, home, store_dir, root, policy, base = world
    run(service)
    repo = workdir(base)
    cold_desktop_session(home, store_dir, repo)
    result = revive_module.revive(client, policy, ALICE, stage_prompt=stage(root),
                                  opt_in=True, model="astra", now=fx.NOW)
    assert result.admitted
    until(lambda: service.store.get_job(result.job_id)["state"] in
          ("succeeded", "failed", "cancelled", "lost"), timeout=25)
    attempts = service.store.list_attempts(result.job_id)
    print("ATTEMPT native_session_id:", [a["native_session_id"] for a in attempts])
    print("lane_sessions:", client.state()["lane_sessions"])
    print("ALICE in lane_sessions:", ALICE in client.state()["lane_sessions"])
    assert ALICE not in client.state()["lane_sessions"], "the revived session is now a lane run"
