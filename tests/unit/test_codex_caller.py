"""C-17.6: Codex callers count as agents; C-5.1 lane behavior is frozen."""
from __future__ import annotations

import ast
import hashlib
import itertools
import os
from pathlib import Path
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import cli
from subfleet.launch_env import build_environment
from tests.fixtures import codex_caller_release217 as before


SESSIONS = [
    ({}, None, "sync", "outside a Claude session"),
    ({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude"},
     "synthetic-claude", "detached", "inside a Claude session — the job must outlive it"),
    ({"CODEX_THREAD_ID": "synthetic-codex"}, "codex:synthetic-codex", "detached",
     "inside a Codex session — the job must outlive it"),
    ({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude",
      "CODEX_THREAD_ID": "synthetic-codex"}, "synthetic-claude", "detached",
     "inside a Claude session — the job must outlive it"),
]
MARKERS = [{}, {"SUBFLEET_JOB": "synthetic-job"},
           {"SUBFLEET_ATTEMPT": "synthetic-job/a1"},
           {"SUBFLEET_JOB": "synthetic-job", "SUBFLEET_ATTEMPT": "synthetic-job/a1"}]
# Cross every argument combination with every recognized environment override,
# including unrecognized and empty settings. --attach changes inline waiting,
# while the dispatch mode continues to describe the detached daemon job.
FLAGS = [dict(zip(("dry_run", "why", "detach", "attach"), values))
         for values in itertools.product((False, True), repeat=4)]
OVERRIDES = [None, "", "0", "false", "no", "off", "1", "true", "yes", "on", "invalid", " TRUE ", " OFF "]


@pytest.mark.parametrize("session,identity,mode,reason", SESSIONS,
                         ids=("none", "claude", "codex", "both"))
@pytest.mark.parametrize("markers", MARKERS, ids=("caller", "job", "attempt", "both-markers"))
def test_c17_6_caller_table_with_every_override(session, identity, mode, reason, markers):
    env = {**session, **markers}
    if markers:
        identity = before.session_id(env)
        mode, reason = before.launch_mode(SimpleNamespace(), env)
    assert cli.session_id(env) == identity
    for flags, override in itertools.product(FLAGS, OVERRIDES):
        current = {**env, **({"SUBFLEET_RUN_DETACH": override} if override is not None else {})}
        args = SimpleNamespace(**flags)
        if flags["dry_run"] or flags["why"]:
            expected = ("sync", "dry-run")
        elif flags["detach"]:
            expected = ("detached", "-d")
        elif override is not None and override.strip().lower() in {"0", "false", "no", "off"}:
            expected = ("sync", "SUBFLEET_RUN_DETACH=0")
        elif override is not None and override.strip().lower() in {"1", "true", "yes", "on"}:
            expected = ("detached", "SUBFLEET_RUN_DETACH=1")
        elif flags["attach"] and mode == "detached":
            expected = (mode, reason.split(" —", 1)[0] + "; --attach waits inline")
        else:
            expected = (mode, reason)
        assert cli.launch_mode(args, current) == expected
        assert cli.session_id(current) == identity


def test_c17_6_codex_defaults_to_detached():
    assert cli.launch_mode(SimpleNamespace(), {"CODEX_THREAD_ID": "synthetic-thread"}) == (
        "detached", "inside a Codex session — the job must outlive it")


@pytest.mark.parametrize("marker", ("SUBFLEET_JOB", "SUBFLEET_ATTEMPT"))
def test_c5_1_codex_lane_retains_release217_behavior(marker):
    env = {"CODEX_THREAD_ID": "synthetic-thread", marker: "synthetic-launch",
           "SUBFLEET_SESSION_ID": "synthetic-parent"}
    assert cli.launch_mode(SimpleNamespace(), env) == before.launch_mode(SimpleNamespace(), env)
    assert cli.session_id(env) == before.session_id(env)


ENV_NAMES = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_CI",
             "CODEX_SESSION_ID", "CODEX_VERSION",
             "SUBFLEET_SESSION_ID", "SUBFLEET_JOB", "SUBFLEET_ATTEMPT", "SUBFLEET_ROOT",
             "SUBFLEET_RUN_DETACH", "SUBFLEET_TURN_JOB", "UNRELATED")
VALUES = st.text(alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\0"), max_size=40)
ENV_KEYS = st.one_of(st.sampled_from(ENV_NAMES), st.text(alphabet=st.characters(
    exclude_categories=("Cs",), exclude_characters="\0="), min_size=1, max_size=40))


@settings(max_examples=300, deadline=None)
@given(env=st.dictionaries(ENV_KEYS, VALUES, max_size=30),
       marker=st.sampled_from(("SUBFLEET_JOB", "SUBFLEET_ATTEMPT")),
       marker_value=VALUES.filter(lambda s: bool(s.strip())),
       flags=st.tuples(*(st.booleans() for _ in range(4))))
def test_c5_1_any_lane_environment_matches_frozen_release217(env, marker, marker_value, flags):
    env = {**env, marker: marker_value}
    args = SimpleNamespace(**dict(zip(("dry_run", "why", "detach", "attach"), flags)))
    assert cli.launch_mode(args, env) == before.launch_mode(args, env)
    assert cli.session_id(env) == before.session_id(env)


@settings(deadline=None)
@given(thread=st.uuids(), claude=st.uuids())
def test_c17_6_codex_identity_cannot_equal_a_native_claude_identity(thread, claude):
    identity = cli.session_id({"CODEX_THREAD_ID": str(thread)})
    assert identity == f"codex:{thread}"
    assert identity != cli.session_id({"CLAUDE_CODE_SESSION_ID": str(claude)})
    assert identity != cli.session_id({"CLAUDE_CODE_SESSION_ID": str(thread)})


@pytest.mark.parametrize("env,expected", [
    ({"CODEX_THREAD_ID": "  synthetic-thread  "}, "codex:synthetic-thread"),
    ({"CODEX_THREAD_ID": " ", "SUBFLEET_SESSION_ID": "synthetic-fallback"}, "synthetic-fallback"),
    ({"CODEX_CI": "1"}, None),
    ({"CODEX_THREAD_ID": "synthetic-thread", "SUBFLEET_SESSION_ID": "synthetic-fallback"}, "codex:synthetic-thread"),
    ({"CODEX_THREAD_ID": "synthetic-thread", "CLAUDE_CODE_SESSION_ID": " "}, "codex:synthetic-thread"),
    ({"CODEX_THREAD_ID": "synthetic-thread", "SUBFLEET_JOB": " ", "SUBFLEET_ATTEMPT": ""}, "codex:synthetic-thread"),
    ({"CODEX_THREAD_ID": "synthetic-thread", "SUBFLEET_ROOT": "synthetic-root"}, "codex:synthetic-thread"),
])
def test_c17_6_identity_fallbacks_and_nonmarkers(env, expected):
    assert cli.session_id(env) == expected


def test_frozen_release217_fixture_provenance_and_integrity():
    assert before.SOURCE_COMMIT == "adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83"
    assert before.SOURCE_SHA256 == "a635c61b8d17e073958586bdde7dbd3370773d05460a38e5a40c3c25946fb2fb"
    text = Path(before.__file__).read_text()
    functions = "\n\n\n".join(ast.get_source_segment(text, node) for node in ast.parse(text).body
                              if isinstance(node, ast.FunctionDef))
    assert hashlib.sha256(functions.encode()).hexdigest() == before.FUNCTIONS_SHA256


def test_c12_3_codex_caller_identity_is_not_ambient_launch_configuration():
    assert "CODEX_THREAD_ID" in cli.STRIPPED_ENV
    assert build_environment(environ={"CODEX_THREAD_ID": "synthetic-thread"}) == {}


@pytest.mark.parametrize("verb", ("wait", "runs"))
def test_c17_6_mine_refusal_names_both_agent_sessions(verb, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {})
    assert cli.main([verb, "--mine"]) == 2
    error = capsys.readouterr().err
    assert "CLAUDE_CODE_SESSION_ID" in error and "CODEX_THREAD_ID" in error
    assert "Claude or Codex session" in error


def test_c15_8_codex_ping_targets_its_own_inbox(daemon, monkeypatch):
    server = daemon({"ping": lambda request: {"delivered": False, "session_id": request.args["session_id"]}})
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-thread")
    assert cli.main(["ping", "hello", "--json"]) == 0
    assert server.args("ping") == {"text": "hello", "session_id": "codex:synthetic-thread"}
