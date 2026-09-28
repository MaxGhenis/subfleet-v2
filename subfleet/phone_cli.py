"""Thin phone commands over the existing daemon socket (C-31.1)."""

from __future__ import annotations

import argparse
import json
import sys

from .client import Client, DaemonError, DaemonUnavailable
from .contracts import Exit
from .protocol import ProtocolError


def add_verbs(subparsers) -> None:
    phone = subparsers.add_parser("phone", help="answer Subfleet through the owner Telegram gateway")
    commands = phone.add_subparsers(dest="phone_command", required=True)
    owns = commands.add_parser("owns", help="exit 0 for an owned Telegram card, 1 for unknown")
    owns.add_argument("telegram_message_id", type=int)
    tap = commands.add_parser("tap", help="apply a recorded sf: button action")
    tap.add_argument("data")
    reply = commands.add_parser("reply", help="answer a question or send to the card's conversation")
    reply.add_argument("telegram_message_id", type=int)
    reply.add_argument("--update-id", help="Telegram update id, for safe redelivery")
    reply.add_argument("text", help="one text argument; use -- before text beginning with a dash")
    notify = commands.add_parser("notify", help="notify once when this conversation next completes")
    notify.add_argument("conversation_id")
    notify.add_argument("--off", action="store_true")
    for parser in (owns, tap, reply, notify):
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    command = args.phone_command
    if command == "owns":
        payload = {"telegram_message_id": args.telegram_message_id}
    elif command == "tap":
        payload = {"data": args.data}
    elif command == "reply":
        payload = {"telegram_message_id": args.telegram_message_id, "text": args.text}
        if args.update_id is not None:
            payload["update_id"] = args.update_id
    else:
        payload = {"conversation_id": args.conversation_id, "enabled": not args.off}
    try:
        result = Client().call("phone." + command, payload)
    except (DaemonError, DaemonUnavailable, ProtocolError, OSError) as exc:
        print(f"subfleet phone: {exc}", file=sys.stderr)
        # owns reserves 1 for an authoritative negative ownership answer.
        # Errors must never send an owned reply into the CoS decision parser.
        return int(Exit.DAEMON_UNAVAILABLE if command == "owns" else
                   getattr(exc, "code", Exit.OPERATIONAL))
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif command == "owns":
        print("owned" if result.get("owned") else "unknown")
    elif result.get("duplicate"):
        print("Already recorded.")
    elif command == "notify":
        print("Completion notification " + ("disabled." if args.off else "requested."))
    else:
        route = result.get("route", "recorded")
        note = {"approval": "Decision recorded.", "answer": "Answer recorded.",
                "queued": "Reply queued.", "steer": "Reply sent to the live turn."}.get(route, "Recorded.")
        if result.get("blocked_by"):
            note += f" Waiting on {result['blocked_by']}."
        print(note)
    return int(Exit.OK) if command != "owns" or result.get("owned") else 1
