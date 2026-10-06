#!/usr/bin/env python3
"""Build `tests/fixtures/claude/<case>/` from recorded evidence (C-12.7).

Run: `python3 tests/fixtures/claude/make_fixtures.py`. The generated files are
committed; this script is the provenance record, so every case says where its
bytes came from and whether the stream around them is synthetic.

Provenance rules honoured here:

* Every `rate_limit_info` payload marked `"synthetic": false` is copied verbatim
  from `docs/reports/experiment-0-rate-limit-event.md` (four live probes, 2026-09-05).
* Message strings marked real are quoted from a source read on 2026-09-05: the
  installed Claude Code 2.1.260 binary's own string table, v1's classifier comments
  in `~/chief-of-staff/subfleet/bin/subfleet-claude` (which quote messages observed
  in production), or a v1 run artifact under `~/chief-of-staff/state/subfleet/runs/`.
* No fixture carries a token, a cookie, an `Authorization` value, or prompt text.
  Emails are kept, as the brief requires: they are the account keys (C-1.4).
* A case whose event sequence was assembled rather than captured is
  `"synthetic": true`, and `provenance` says which parts are real.

Every attempt's stream is written to `stdout` because `--output-format stream-json`
puts the stream on stdout; the adapter reads `stream.jsonl` first and falls back to
`stdout`, so one file serves both.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The clock every fixture-driven test pins. All experiment-0 reset epochs fall
# after it, so a "reported" clock is always in the future and a "guessed" one
# (now + 3600 s) is unambiguous.
NOW_ISO = "2026-09-05T11:30:00Z"

HAIKU = "claude-haiku-4-5-20251001"
OPUS = "claude-opus-5"
FABLE = "claude-fable-5-1"

# --- verbatim payloads from docs/reports/experiment-0-rate-limit-event.md ----

RL_AXIOM = {
    "status": "allowed",
    "resetsAt": 1788624000,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.05, "resetsAt": 1788624000},
        "seven_day": {"utilization": 0.25, "resetsAt": 1789056000},
    },
}

RL_THESIS = {
    "status": "allowed",
    "resetsAt": 1788612600,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.29, "resetsAt": 1788612600},
        "seven_day": {"utilization": 0.18, "resetsAt": 1788613200},
    },
}

RL_FABLE_REJECTED = {
    "status": "rejected",
    "resetsAt": 1790812800,
    "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
    "errorCode": "credits_required",
    "canUserPurchaseCredits": True,
    "hasChargeableSavedPaymentMethod": True,
}

RL_GMAIL = {
    "status": "allowed",
    "resetsAt": 1788612600,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.42, "resetsAt": 1788612600},
        "seven_day": {"utilization": 0.33, "resetsAt": 1788616800},
    },
}

# --- real message strings ---------------------------------------------------

# experiment-0: the `result` text of the rejected Fable probe on max@policyengine.org.
TEXT_OUT_OF_CREDITS = (
    "You're out of usage credits. Switch to another model, or manage usage credits "
    "at claude.ai/settings/usage, to continue."
)
# Claude Code 2.1.260 string table (identifier XZr).
TEXT_ORG_BLOCK = (
    "Your organization has disabled Claude subscription access for Claude Code · "
    "Use an Anthropic API key instead, or ask your admin to enable access"
)
# Claude Code 2.1.260 string table: the current version-gate copy.
TEXT_UPDATE_FOR_MODEL = "Update Claude Code to use this model"
# Quoted in v1 bin/subfleet-claude as observed in production on Claude Code 2.1.228.
TEXT_CLI_TOO_OLD = (
    "Claude Code 2.1.228 does not support this model; version 2.1.251 or newer is required"
)
# Claude Code 2.1.260 string table (identifier YZr): explicitly NOT a usage limit.
TEXT_SERVER_THROTTLE = "Server is temporarily limiting requests (not your usage limit)"
# Claude Code 2.1.260 string table.
TEXT_CONNECTION = "Unable to connect to API. Check your internet connection."
# Real stderr from v1 run 20260905-063243-us-housing-source-graph (rc 0).
TEXT_BG_TASKS = (
    "Background tasks still running after 600s; terminating. "
    "Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely."
)

SID = {
    "success-allowed": "281408bb-280e-43da-9455-b9f3ddebf275",
    "allowed-out-of-credits-overage": "bf80957d-887d-4cd8-a131-e70a97f14a54",
    "rejected-credits-fable": "fa8e5306-d775-4435-bd42-29f1396af991",
    "allowed-on-table-exhausted-lane": "cc1514f8-ebac-4d71-ba0e-356d663b8784",
}


# --- stream construction ----------------------------------------------------


def init_event(session_id: str, model: str, cwd: str = "/Users/maxghenis/subfleet-v2") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "cwd": cwd,
        "session_id": session_id,
        "claude_code_version": "2.1.260",
        "tools": ["Read", "Glob", "Grep"],
        "mcp_servers": [],
        "model": model,
        "permissionMode": "plan",
        "slash_commands": [],
        "skills": [],
        "plugins": [],
        "apiKeySource": "none",
        "output_style": "default",
        "uuid": f"{session_id[:8]}-0000-4000-8000-000000000001",
    }


def assistant_event(session_id: str, model: str, text: str, *,
                    stop_reason: str | None = "end_turn",
                    error: str | None = None) -> dict:
    row = {
        "type": "assistant",
        "message": {
            "id": "msg_01FIXTURE",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
        "parent_tool_use_id": None,
        "session_id": session_id,
        "uuid": f"{session_id[:8]}-0000-4000-8000-000000000002",
    }
    if error is not None:
        row["error"] = error
    return row


def rate_limit_event(session_id: str, info: dict) -> dict:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": info,
        "session_id": session_id,
        "uuid": f"{session_id[:8]}-0000-4000-8000-000000000003",
    }


def api_retry_event(session_id: str, *, attempt: int, error: str,
                    error_status: int | None) -> dict:
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": 3,
        "retry_delay_ms": 1000,
        "error_status": error_status,
        "error": error,
        "session_id": session_id,
        "uuid": f"{session_id[:8]}-0000-4000-8000-00000000000{attempt}",
    }


def result_success(session_id: str, text: str, *, is_error: bool = False,
                   stop_reason: str | None = "end_turn",
                   api_error_status: int | None = None) -> dict:
    row = {
        "type": "result",
        "subtype": "success",
        "duration_ms": 4120,
        "duration_api_ms": 3980,
        "is_error": is_error,
        "num_turns": 1,
        "result": text,
        "stop_reason": stop_reason,
        "total_cost_usd": 0.0021,
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "modelUsage": {},
        "permission_denials": [],
        "session_id": session_id,
        "uuid": f"{session_id[:8]}-0000-4000-8000-000000000009",
    }
    if api_error_status is not None:
        row["api_error_status"] = api_error_status
    return row


def result_error(session_id: str, errors: list[str], *,
                 subtype: str = "error_during_execution") -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 2100,
        "duration_api_ms": 2000,
        "is_error": True,
        "num_turns": 1,
        "stop_reason": None,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 12, "output_tokens": 0},
        "modelUsage": {},
        "permission_denials": [],
        "errors": errors,
        "session_id": session_id,
        "uuid": f"{session_id[:8]}-0000-4000-8000-000000000009",
    }


def transcript_rows(session_id: str, models: list[str], text: str,
                    cwd: str = "/Users/maxghenis/subfleet-v2") -> list[dict]:
    """A minimal `~/.claude/projects/<encoded cwd>/<sid>.jsonl` (C-12.5, C-12.6)."""
    rows = [{
        "type": "user",
        "cwd": cwd,
        "sessionId": session_id,
        "version": "2.1.260",
        "gitBranch": "lane/claude-adapter",
        "uuid": f"{session_id[:8]}-1111-4000-8000-000000000001",
        "timestamp": "2026-09-05T11:30:01.000Z",
        "message": {"role": "user", "content": "[prompt redacted]"},
    }]
    for index, model in enumerate(models):
        rows.append({
            "type": "assistant",
            "cwd": cwd,
            "sessionId": session_id,
            "version": "2.1.260",
            "uuid": f"{session_id[:8]}-1111-4000-8000-00000000000{index + 2}",
            "timestamp": f"2026-09-05T11:30:0{index + 2}.000Z",
            "message": {
                "id": f"msg_01FIXTURE{index}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        })
    return rows


def stream(rows: list[dict], *, truncate_tail: bool = False) -> str:
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    if truncate_tail:
        # Drop the closing half of the last line the way a killed pipe does.
        text = text.rstrip("\n")
        text = text[: len(text) - 40] if len(text) > 60 else text[: len(text) // 2]
    return text


def provider_readings(info: dict) -> list[dict]:
    out = []
    for window, value in (info.get("unifiedWindows") or {}).items():
        out.append({
            "scope": "account",
            "window": window,
            "utilization": value["utilization"],
            "resets_at_epoch": value["resetsAt"],
            "label": "provider",
            "source": "rate_limit_event",
        })
    return out


def write_case(name: str, *, stdout: str, stderr: str, rc: int, expected: dict,
               transcript: list[dict] | None = None) -> None:
    case = ROOT / name
    case.mkdir(parents=True, exist_ok=True)
    (case / "stdout").write_text(stdout, encoding="utf-8")
    (case / "stderr").write_text(stderr, encoding="utf-8")
    (case / "rc").write_text(f"{rc}\n", encoding="utf-8")
    expected = {"case": name, "now": NOW_ISO, "rc": rc, **expected}
    (case / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    if transcript is not None:
        (case / "transcript.jsonl").write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in transcript),
            encoding="utf-8",
        )
    elif (case / "transcript.jsonl").exists():
        (case / "transcript.jsonl").unlink()


def allowed_case(name: str, session_id: str, info: dict, model: str, *,
                 account: str, provenance: str) -> None:
    rows = [
        init_event(session_id, model),
        assistant_event(session_id, model, "ok"),
        rate_limit_event(session_id, info),
        result_success(session_id, "ok"),
    ]
    write_case(
        name,
        stdout=stream(rows),
        stderr="",
        rc=0,
        transcript=transcript_rows(session_id, [model], "ok"),
        expected={
            "synthetic": True,
            "provenance": provenance,
            "account": account,
            "requested_model": model,
            "session_id": session_id,
            "class": "ok",
            "detail_contains": "rate_limit_event",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=allowed",
                "quota": "rate_limit_event.unifiedWindows",
            },
            "closure": None,
            "readings": provider_readings(info),
            "overage": {
                "overage_status": info.get("overageStatus"),
                "overage_disabled_reason": info.get("overageDisabledReason"),
                "is_using_overage": info.get("isUsingOverage"),
            },
            "deliverable": "ok",
            "attestation": {"status": "attested", "served_model": model},
            "stream": {
                "init": True,
                "assistants": 1,
                "assistant_models": [model],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )


def build() -> None:
    # 1..3 — the three `status: allowed` experiment-0 payloads.
    allowed_case(
        "success-allowed", SID["success-allowed"], RL_AXIOM, HAIKU,
        account="max@axiom.org",
        provenance=(
            "rate_limit_info verbatim from docs/reports/experiment-0-rate-limit-event.md "
            "(max@axiom.org, live probe 2026-09-05 07:2x EDT); the surrounding "
            "system/init, assistant, result frames and the transcript are assembled "
            "to the shapes Claude Code 2.1.260 validates its own output against."
        ),
    )
    allowed_case(
        "allowed-out-of-credits-overage", SID["allowed-out-of-credits-overage"],
        RL_THESIS, HAIKU,
        account="max@thesisinstitute.org",
        provenance=(
            "rate_limit_info verbatim from experiment-0 (max@thesisinstitute.org). "
            "overageDisabledReason is out_of_credits while status is allowed: the case "
            "that proves overageStatus is never admission evidence (C-9.8). Surrounding "
            "frames assembled."
        ),
    )
    allowed_case(
        "allowed-on-table-exhausted-lane", SID["allowed-on-table-exhausted-lane"],
        RL_GMAIL, HAIKU,
        account="max.ghenis@gmail.com",
        provenance=(
            "rate_limit_info verbatim from experiment-0 (max.ghenis@gmail.com, whose "
            "cached v1 table said EXHAUSTED at 147% of the week while the server "
            "reported 0.42 / 0.33 and allowed the turn). Surrounding frames assembled."
        ),
    )

    # 4 — the rejected `credits_required` payload.
    sid = SID["rejected-credits-fable"]
    rows = [
        init_event(sid, FABLE),
        rate_limit_event(sid, RL_FABLE_REJECTED),
        result_success(sid, TEXT_OUT_OF_CREDITS, is_error=True, stop_reason=None),
    ]
    write_case(
        "rejected-credits-fable",
        stdout=stream(rows),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "rate_limit_info and the result text verbatim from experiment-0 "
                "(max@policyengine.org, --model claude-fable-5-1, rc=1, is_error true). "
                "system/init and the result envelope around them are assembled."
            ),
            "account": "max@policyengine.org",
            "requested_model": FABLE,
            "session_id": sid,
            "class": "limited",
            "detail_contains": "credits_required",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=rejected",
                "quota": "errorCode=credits_required",
            },
            "closure": {
                "scope": FABLE,
                "reason": "credits",
                "clock_source": "reported",
                "until_at_epoch": 1790812800,
                "source_event": "rate_limit_event",
            },
            "readings": [{
                "scope": FABLE,
                "window": "admission",
                "utilization": None,
                "resets_at_epoch": 1790812800,
                "label": "admission-observed",
                "source": "rate_limit_event",
            }],
            "overage": {
                "overage_status": None,
                "overage_disabled_reason": "out_of_credits",
                "is_using_overage": False,
            },
            "deliverable": TEXT_OUT_OF_CREDITS,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 5 — a session limit whose only clock is prose (no rate_limit_event reached us).
    sid = "11111111-1111-4111-8111-111111111111"
    text = (
        "You've reached your session limit. Your limit will reset at "
        "6:40pm (America/New_York)."
    )
    rows = [init_event(sid, OPUS), result_success(sid, text, is_error=True, stop_reason=None)]
    write_case(
        "limit-session-with-clock",
        stdout=stream(rows),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. Phrasing composed from the limit vocabulary v1's classifier "
                "matches (bin/subfleet-claude: 'session limit', 'hit your limit') and the "
                "'<clock> (<zone>)' reset form v1's util.parse_reset_clock parses. No "
                "captured payload retains this text: v1 run dirs keep an empty err.log "
                "for text-classified limits."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "limited",
            "detail_contains": "session limit",
            "evidence": {
                "auth": "system/init",
                "admission": "no rate_limit_event",
                "quota": "result text",
            },
            "closure": {
                "scope": "account",
                "reason": "provider-limit",
                "clock_source": "reported",
                "until_at": "2026-09-05T22:40:00Z",
                "source_event": "result-text",
            },
            "readings": [],
            "deliverable": text,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 6 — a weekly limit the server reported through a rejected admission event.
    sid = "22222222-2222-4222-8222-222222222222"
    info = {
        "status": "rejected",
        "resetsAt": 1789056000,
        "rateLimitType": "seven_day",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.61, "resetsAt": 1788624000},
            "seven_day": {"utilization": 1.0, "resetsAt": 1789056000},
        },
    }
    text = "You've hit your weekly limit."
    rows = [
        init_event(sid, OPUS),
        rate_limit_event(sid, info),
        result_success(sid, text, is_error=True, stop_reason=None),
    ]
    write_case(
        "limit-weekly-with-clock",
        stdout=stream(rows),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. The rate_limit_info follows the shape and field set of the "
                "experiment-0 payloads and the rate_limit_info validator embedded in "
                "Claude Code 2.1.260 (rateLimitType 'seven_day', unifiedWindows with a "
                "spent weekly window). No live weekly rejection was captured."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "limited",
            "detail_contains": "rejected",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=rejected",
                "quota": "rate_limit_event.resetsAt",
            },
            "closure": {
                "scope": "account",
                "reason": "provider-limit",
                "clock_source": "reported",
                "until_at_epoch": 1789056000,
                "source_event": "rate_limit_event",
            },
            "readings": [{
                "scope": OPUS,
                "window": "admission",
                "utilization": None,
                "resets_at_epoch": 1789056000,
                "label": "admission-observed",
                "source": "rate_limit_event",
            }],
            "windows_in_evidence": {"five_hour": 0.61, "seven_day": 1.0},
            "deliverable": text,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 7 — a limit with no clock anywhere: the guessed-closure path (C-9.4).
    sid = "33333333-3333-4333-8333-333333333333"
    text = "You've hit your usage limit."
    rows = [init_event(sid, OPUS), result_success(sid, text, is_error=True, stop_reason=None)]
    write_case(
        "limit-no-clock",
        stdout=stream(rows),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. Minimal limit phrasing from v1's classifier vocabulary with "
                "every clock removed, to exercise the guessed-closure branch."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "limited",
            "detail_contains": "usage limit",
            "evidence": {
                "auth": "system/init",
                "admission": "no rate_limit_event",
                "quota": "result text",
            },
            "closure": {
                "scope": "account",
                "reason": "provider-limit",
                "clock_source": "guessed",
                "until_at_offset_s": 3600,
                "source_event": "result-text",
            },
            "readings": [],
            "deliverable": text,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 8 — a dead token: no system/init at all (C-9.3).
    sid = "44444444-4444-4444-8444-444444444444"
    stderr = (
        'API Error: 401 {"type":"error","error":{"type":"authentication_error",'
        '"message":"OAuth token has expired. Please obtain a new token or refresh '
        'your existing token."}}\n'
    )
    write_case(
        "auth-401",
        stdout="",
        stderr=stderr,
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. The envelope is the Anthropic authentication_error shape "
                "Claude Code 2.1.260 carries in its error-type table; no captured 401 "
                "artifact survives in the newest 150 v1 run directories."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "auth-dead",
            "detail_contains": "401",
            "evidence": {
                "auth": "no system/init and a 401 in stderr",
                "admission": "not reached",
                "quota": "not reached",
            },
            "closure": None,
            "readings": [],
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": False,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": None,
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 9 — an organisation block: system/init succeeded and it is still auth-dead.
    sid = "55555555-5555-4555-8555-555555555555"
    rows = [
        init_event(sid, OPUS),
        assistant_event(sid, OPUS, TEXT_ORG_BLOCK, stop_reason=None,
                        error="oauth_org_not_allowed"),
        result_error(sid, [TEXT_ORG_BLOCK]),
    ]
    write_case(
        "org-block",
        stdout=stream(rows),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "The org-block sentence is verbatim from the Claude Code 2.1.260 string "
                "table and matches the message v1 recorded on max@policybench.org "
                "2026-08-23. The frames around it are assembled; `error` is the "
                "oauth_org_not_allowed member of the CLI's own error enum."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "auth-dead",
            "detail_contains": "organization has disabled",
            "evidence": {
                "auth": "organisation block",
                "admission": "not reached",
                "quota": "not reached",
            },
            "closure": None,
            "readings": [],
            "deliverable": TEXT_ORG_BLOCK,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 1,
                "assistant_models": [OPUS],
                "rate_limit_events": 0,
                "result_subtype": "error_during_execution",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 9b — the organisation block as Claude Code 2.1.284 wrote it on 2026-09-30, when an
    #      organisation disabled claude-5's Claude Code access: no request was served.
    sid = "55555555-5555-4555-8555-5555555555b9"
    hook = {"type": "system", "subtype": "hook_started", "hook_id": "f1x7a2e0-0000-4000-8000-000000000001",
            "hook_name": "SessionStart:startup", "hook_event": "SessionStart",
            "uuid": f"{sid[:8]}-0000-4000-8000-000000000010", "session_id": sid}
    hooked = {"type": "system", "subtype": "hook_response", "hook_id": hook["hook_id"],
              "hook_name": "SessionStart:startup", "hook_event": "SessionStart", "output": "", "stdout": "",
              "stderr": "", "exit_code": 0, "outcome": "success",
              "uuid": f"{sid[:8]}-0000-4000-8000-000000000011", "session_id": sid}
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": None, "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}}
    placeholder = {
        "type": "assistant",
        "message": {"id": "2758f296-0000-4000-8000-000000000012", "container": None, "model": "<synthetic>",
                    "role": "assistant", "stop_reason": "stop_sequence", "stop_sequence": "", "type": "message",
                    "usage": zero, "content": [{"type": "text", "text": TEXT_ORG_BLOCK}],
                    "context_management": None},
        "parent_tool_use_id": None, "session_id": sid, "uuid": f"{sid[:8]}-0000-4000-8000-000000000013",
        "error": "oauth_org_not_allowed", "is_api_error_message": True,
        "api_error_code": "oauth_not_allowed_for_organization",
    }
    ended = {"type": "result", "subtype": "success", "duration_ms": 31904, "duration_api_ms": 0, "is_error": True,
             "num_turns": 1, "result": TEXT_ORG_BLOCK, "stop_reason": "stop_sequence", "total_cost_usd": 0,
             "usage": {**zero, "output_tokens_details": {"thinking_tokens": 0}}, "modelUsage": {},
             "permission_denials": [], "terminal_reason": "api_error", "api_error_status": 403,
             "api_error_code": "oauth_not_allowed_for_organization", "session_id": sid,
             "uuid": f"{sid[:8]}-0000-4000-8000-000000000014"}
    write_case(
        "org-block-synthetic",
        stdout=stream([hook, hooked, {**init_event(sid, "claude-opus-5-5"), "claude_code_version": "2.1.284"},
                       placeholder, ended]),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "The shape of the 37 streams of 2026-09-30 15:43Z on claude-5 (for one, "
                "~/.subfleet/jobs/20260930-114330-salvage-r3-cont3/a1/stdout, read-only): SessionStart "
                "hook events, a system/init, Claude Code's placeholder frame (model <synthetic>, "
                "is_api_error_message, error oauth_org_not_allowed, every usage counter zero) and a "
                "`success` result marked is_error with api_error_status 403 and duration_api_ms 0. "
                "Ids and uuids are replaced; the sentence and field values are verbatim."
            ),
            "requested_model": "claude-opus-5-5",
            "session_id": sid,
            "class": "auth-dead",
            "detail_contains": "organization has disabled",
            "evidence": {
                "auth": "organisation block, in the placeholder frame and the error result",
                "admission": "not reached",
                "quota": "not reached",
                "model_answered": False,
            },
            "closure": None,
            "readings": [],
            "deliverable": TEXT_ORG_BLOCK,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 1,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 10 — the host CLI is older than the model: never the lane's fault.
    sid = "66666666-6666-4666-8666-666666666666"
    write_case(
        "cli-too-old",
        stdout="",
        stderr=f"API Error: 400 {TEXT_CLI_TOO_OLD}\n",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "The sentence is quoted verbatim in v1 bin/subfleet-claude as the message "
                "observed from Claude Code 2.1.228; the 400 envelope around it is assembled."
            ),
            "requested_model": FABLE,
            "session_id": sid,
            "class": "cli-too-old",
            "detail_contains": "does not support this model",
            "evidence": {
                "auth": "not reached",
                "admission": "not reached",
                "quota": "not reached",
                "cli": "version gate",
            },
            "closure": None,
            "readings": [],
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": False,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": None,
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 11 — the 2.1.260 wording of the same gate.
    sid = "77777777-7777-4777-8777-777777777777"
    write_case(
        "cli-too-old-current",
        stdout="",
        stderr=f"API Error: 400 {TEXT_UPDATE_FOR_MODEL}\n",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "'Update Claude Code to use this model' is verbatim from the installed "
                "Claude Code 2.1.260 string table (the current wording of the gate v1 "
                "matched as 'does not support this model'); the envelope is assembled."
            ),
            "requested_model": FABLE,
            "session_id": sid,
            "class": "cli-too-old",
            "detail_contains": "Update Claude Code",
            "evidence": {
                "auth": "not reached",
                "admission": "not reached",
                "quota": "not reached",
                "cli": "version gate",
            },
            "closure": None,
            "readings": [],
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": False,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": None,
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 12 — the stream stops mid-line after a retry storm.
    sid = "88888888-8888-4888-8888-888888888888"
    rows = [
        init_event(sid, OPUS),
        api_retry_event(sid, attempt=1, error="server_error", error_status=500),
        api_retry_event(sid, attempt=2, error="overloaded", error_status=529),
        assistant_event(sid, OPUS, "Partial answer before the socket dropped."),
    ]
    write_case(
        "stream-disconnect",
        stdout=stream(rows, truncate_tail=True),
        stderr=f"API Error: Connection error\n{TEXT_CONNECTION}\n",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. The api_retry frames use the CLI's own schema and its "
                "`error` enum members server_error / overloaded; the stderr sentence is "
                "verbatim from the Claude Code 2.1.260 string table. The final line is "
                "cut mid-JSON the way a killed pipe leaves it."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "transient",
            "detail_contains": "connection",
            "evidence": {
                "auth": "system/init",
                "admission": "no rate_limit_event",
                "quota": "not reached",
            },
            "closure": None,
            "readings": [],
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": None,
                "truncated_tail": True,
                "bad_lines": 0,
            },
        },
    )

    # 13 — a different model served the turn (C-12.5).
    sid = "99999999-9999-4999-8999-999999999999"
    info = dict(RL_AXIOM)
    rows = [
        init_event(sid, FABLE),
        assistant_event(sid, OPUS, "Answer served by a fallback model."),
        rate_limit_event(sid, info),
        result_success(sid, "Answer served by a fallback model."),
    ]
    write_case(
        "model-downgrade",
        stdout=stream(rows),
        stderr="",
        rc=0,
        transcript=transcript_rows(sid, [OPUS, OPUS], "Answer served by a fallback model."),
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. Models the safety-classifier fallback v1 documents in "
                "bin/subfleet-claude ('a safety-classifier fallback silently swaps in "
                "Opus'): claude-fable-5-1 requested, claude-opus-5 served. The "
                "rate_limit_info is the verbatim experiment-0 max@axiom.org payload."
            ),
            "requested_model": FABLE,
            "session_id": sid,
            "class": "ok",
            "detail_contains": "rate_limit_event",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=allowed",
                "quota": "rate_limit_event.unifiedWindows",
            },
            "closure": None,
            "readings": provider_readings(info),
            "deliverable": "Answer served by a fallback model.",
            "attestation": {"status": "mismatch", "served_model": OPUS},
            "stream": {
                "init": True,
                "assistants": 1,
                "assistant_models": [OPUS],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 14 — rc 0 with nothing to deliver is `unknown`, not `ok` (C-12.6).
    sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    rows = [
        init_event(sid, HAIKU),
        rate_limit_event(sid, RL_AXIOM),
        result_success(sid, ""),
    ]
    write_case(
        "empty-result-rc0",
        stdout=stream(rows),
        stderr="",
        rc=0,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. rc 0 with an empty `result` string and no assistant text; "
                "the rate_limit_info is the verbatim experiment-0 max@axiom.org payload, "
                "so the readings survive a class of `unknown`."
            ),
            "requested_model": HAIKU,
            "session_id": sid,
            "class": "unknown",
            "detail_contains": "empty",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=allowed",
                "quota": "rate_limit_event.unifiedWindows",
            },
            "closure": None,
            "readings": provider_readings(RL_AXIOM),
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 15 — server-side throttling that says in words it is not a usage limit.
    sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    rows = [
        init_event(sid, OPUS),
        api_retry_event(sid, attempt=1, error="overloaded", error_status=529),
        result_error(sid, [TEXT_SERVER_THROTTLE]),
    ]
    write_case(
        "transient-server-throttle",
        stdout=stream(rows),
        stderr="",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "The sentence is verbatim from the Claude Code 2.1.260 string table and "
                "contains the substring 'usage limit' while explicitly denying one: the "
                "regression guard against a text classifier cooling a healthy lane. "
                "Frames assembled."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "transient",
            "detail_contains": "temporarily limiting",
            "evidence": {
                "auth": "system/init",
                "admission": "no rate_limit_event",
                "quota": "not reached",
            },
            "closure": None,
            "readings": [],
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": "error_during_execution",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 16 — an auth-shaped signature with a live credential is not auth-dead (C-9.3).
    sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    rows = [
        init_event(sid, OPUS),
        rate_limit_event(sid, RL_AXIOM),
        result_error(sid, ["MCP server github: 401 unauthorized while listing tools"]),
    ]
    write_case(
        "auth-signature-false-positive",
        stdout=stream(rows),
        stderr="MCP server github: 401 unauthorized\n",
        rc=1,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic, modelled on the false positive v1 guards against with a live "
                "token probe (bin/subfleet-claude AUTH_SIGNATURE_FALSE_POSITIVE): a 401 "
                "from something other than the lane credential, with system/init present "
                "and the server still reporting quota. rate_limit_info is the verbatim "
                "experiment-0 max@axiom.org payload."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "transient",
            "detail_contains": "auth signature",
            "evidence": {
                "auth": "system/init present; 401 signature not from the lane credential",
                "admission": "rate_limit_event.status=allowed",
                "quota": "rate_limit_event.unifiedWindows",
            },
            "closure": None,
            "readings": provider_readings(RL_AXIOM),
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 1,
                "result_subtype": "error_during_execution",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 17 — the model refused (C-9.2 `content-filter`).
    sid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    refusal = "I can't help with that."
    rows = [
        init_event(sid, OPUS),
        assistant_event(sid, OPUS, refusal, stop_reason="refusal"),
        rate_limit_event(sid, RL_AXIOM),
        result_success(sid, refusal, stop_reason="refusal"),
    ]
    write_case(
        "content-filter",
        stdout=stream(rows),
        stderr="",
        rc=0,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. `stop_reason: \"refusal\"` is the signal Claude Code 2.1.260 "
                "documents as 'detecting stop_reason \"refusal\" on the assistant error "
                "frame'; the rate_limit_info is the verbatim experiment-0 max@axiom.org "
                "payload, so a refusal still reports capacity."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "content-filter",
            "detail_contains": "refusal",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=allowed",
                "quota": "rate_limit_event.unifiedWindows",
            },
            "closure": None,
            "readings": provider_readings(RL_AXIOM),
            "deliverable": refusal,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": True,
                "assistants": 1,
                "assistant_models": [OPUS],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 18 — the guardian could not spawn the provider at all (C-5.2, C-12.7).
    sid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    write_case(
        "spawn-failure",
        stdout="",
        stderr="env: claude: No such file or directory\n",
        rc=127,
        expected={
            "synthetic": True,
            "provenance": (
                "Synthetic. The guardian writes rc 127 with a spawn_error when the "
                "provider binary cannot be executed (C-5.2); the stderr line is the "
                "shell's own wording."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "unknown",
            "detail_contains": "127",
            "evidence": {
                "auth": "no system/init",
                "admission": "not reached",
                "quota": "not reached",
            },
            "closure": None,
            "readings": [],
            "deliverable": None,
            "attestation": {"status": "unattested", "served_model": None},
            "stream": {
                "init": False,
                "assistants": 0,
                "assistant_models": [],
                "rate_limit_events": 0,
                "result_subtype": None,
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )

    # 19 — a real v1 stderr line that must not change a successful classification.
    sid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    answer = "Done: the graph builds and the tests pass."
    rows = [
        init_event(sid, OPUS),
        assistant_event(sid, OPUS, answer),
        rate_limit_event(sid, RL_GMAIL),
        result_success(sid, answer),
    ]
    write_case(
        "ok-with-background-task-warning",
        stdout=stream(rows),
        stderr=TEXT_BG_TASKS + "\n",
        rc=0,
        transcript=transcript_rows(sid, [OPUS], answer),
        expected={
            "synthetic": True,
            "provenance": (
                "The stderr line is the real, byte-for-byte stderr of v1 run "
                "20260905-063243-us-housing-source-graph (rc 0, lane max@farness.ai). "
                "rate_limit_info is the verbatim experiment-0 max.ghenis@gmail.com "
                "payload. The stream frames and the deliverable text are assembled: the "
                "real run's output is the lane's own prose and is not fixture material."
            ),
            "requested_model": OPUS,
            "session_id": sid,
            "class": "ok",
            "detail_contains": "rate_limit_event",
            "evidence": {
                "auth": "system/init",
                "admission": "rate_limit_event.status=allowed",
                "quota": "rate_limit_event.unifiedWindows",
            },
            "closure": None,
            "readings": provider_readings(RL_GMAIL),
            "deliverable": answer,
            "attestation": {"status": "attested", "served_model": OPUS},
            "stream": {
                "init": True,
                "assistants": 1,
                "assistant_models": [OPUS],
                "rate_limit_events": 1,
                "result_subtype": "success",
                "truncated_tail": False,
                "bad_lines": 0,
            },
        },
    )


if __name__ == "__main__":
    build()
    cases = sorted(p.name for p in ROOT.iterdir() if p.is_dir())
    print(f"{len(cases)} fixture cases:")
    for case in cases:
        print("  ", case)
