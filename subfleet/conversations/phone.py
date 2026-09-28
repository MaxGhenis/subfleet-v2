"""Owner-chat actions; the daemon remains the sole conversation-store writer.

The gateway supplies Telegram update IDs, not approval IDs or provider decisions.
All decisions come from the durable card and use the normal person-only response
path. No transport, credentials, or Telegram polling belongs here (C-31.1).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from ..phone import enabled
from .store import ConversationError, message_digest, utcnow


def _check(service, peer, *, mutate=True):
    service._person(peer, "using the phone gateway")
    if mutate and not enabled(service.daemon.policy):
        raise ConversationError("phone-disabled", "phone.telegram is disabled", code=7)


def _message_id(args):
    value = args.get("telegram_message_id")
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ConversationError("bad-phone-message", "Telegram message ID must be a positive integer")
    value = int(value)
    if value > 2**63 - 1:
        raise ConversationError("bad-phone-message", "Telegram message ID is too large")
    return value


def owns(service, args, peer):
    # Even with delivery disabled, the poller must not reinterpret old Subfleet
    # replies as chief-of-staff commands or decisions.
    _check(service, peer, mutate=False)
    mid = _message_id(args)
    row = service.store.one("SELECT token FROM phone_cards WHERE telegram_message_id=?", (mid,))
    return {"owned": row is not None}


def _card(service, *, token=None, mid=None):
    column, value = ("token", token) if token is not None else ("telegram_message_id", mid)
    row = service.store.one(f"SELECT * FROM phone_cards WHERE {column}=?", (value,))
    if row is None or row["telegram_message_id"] is None:
        raise ConversationError("unknown-phone-card", "this is not a delivered Subfleet card")
    row["actions"] = json.loads(row["actions_json"])
    row["progress"] = json.loads(row["progress_json"])
    return row


def _event(tx, card, kind, data, *, position, message_id=None):
    # append_events also advances provider replay marks. Phone actions are local
    # events and must never change a runner's stdout offset or stdin sequence.
    tx.execute("INSERT OR IGNORE INTO events(conversation_id,message_id,attempt_id,source,position,ordinal,"
               "kind,data_json,ts) VALUES (?,?,?,'phone',?,0,?,?,?)",
               (card["conversation_id"], message_id or card.get("message_id"),
                f"phone:{card['token']}", position, kind, json.dumps({**data, "source": "phone"}), utcnow()))


def _response(service, card, peer, *, decision, answers=None):
    approval = service.store.approval(card["approval_id"])
    if approval["state"] != "pending":
        previous = approval.get("decision") or {}
        if (approval["state"] != "answered" or previous.get("decision") != decision
                or previous.get("answers") != answers or previous.get("message") is not None):
            raise ConversationError("not-pending", "the approval has a different answer or was withdrawn")
    result = service.op_approval_respond({"approval_id": approval["approval_id"],
        "nonce": approval["nonce"], "request_sha256": approval["request_sha256"],
        "decision": decision, "answers": answers, "source": "phone"}, peer)
    result.update(source="phone", conversation_id=card["conversation_id"],
                  route="answer" if decision == "answer" else "approval")
    with service.store.transaction() as tx:
        _event(tx, card, "phone.approval.responded", {"approval_id": approval["approval_id"], "decision": decision},
               position=f"approval:{approval['approval_id']}")
    return result


def _questions(approval):
    questions = approval["display"].get("questions")
    if (not isinstance(questions, list) or not questions
            or any(not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"]
                   for q in questions)
            or len({q["question"] for q in questions}) != len(questions)):
        raise ConversationError("bad-phone-question", "this question cannot be answered from this card")
    return questions


def _receipt(tx, card, request, result):
    if request is None:
        return
    update_id, digest = request
    stored = {**result, "_request_sha256": digest}
    tx.execute("INSERT INTO phone_replies(update_id,telegram_message_id,conversation_id,message_id,result_json,"
               "created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(update_id) DO UPDATE SET result_json=excluded.result_json",
               (update_id, card["telegram_message_id"], card["conversation_id"],
                result.get("message_id", card.get("message_id")), json.dumps(stored), utcnow()))


def _question(service, card, peer, *, action_key=None, action=None, text=None, request=None):
    approval = service.store.approval(card["approval_id"])
    questions, progress = _questions(approval), card["progress"]
    index = progress.get("index", 0)
    consumed = progress.setdefault("consumed", [])
    if action_key is not None and action_key in consumed:
        if index >= len(questions):
            return _response(service, card, peer, decision="answer", answers=progress["answers"])
        if approval["state"] != "pending":
            raise ConversationError("not-pending", "the question was already answered or withdrawn")
        return {"source": "phone", "route": "answer", "conversation_id": card["conversation_id"],
                "pending": True, "question_index": index, "duplicate": True}
    if approval["state"] != "pending":
        raise ConversationError("not-pending", "the question was already answered or withdrawn")
    if index >= len(questions):
        # Final progress is durable even for a direct CLI reply without a
        # Telegram update ID. A failed response may retry the identical final
        # text, but must not replace the committed answer or queue new work.
        answers = progress.get("answers", {})
        if text is None or answers.get(questions[-1]["question"]) != text:
            raise ConversationError("stale-phone-action", "the final answer is already recorded; retry that same answer")
        with service.store.transaction() as tx:
            _receipt(tx, card, request, {"_pending_answer": {"token": card["token"], "answers": answers}})
        result = _response(service, card, peer, decision="answer", answers=answers)
        with service.store.transaction() as tx:
            _receipt(tx, card, request, result)
        return result
    if action is not None and action.get("question") != index:
        raise ConversationError("stale-phone-action", "answer the question currently shown on the card")
    question = questions[index]
    selected = progress.setdefault("selected", [])
    answer = text
    if action is not None:
        if action["kind"] == "option":
            option = action["option"]
            if question.get("multiSelect"):
                if option not in selected:
                    selected.append(option)
            else:
                answer = option
        elif action["kind"] == "finish":
            if not selected:
                raise ConversationError("no-phone-selection", "choose an option or reply with an answer first")
            answer = ", ".join(selected)
        else:
            raise ConversationError("bad-phone-action", "this action cannot answer a question")
    if action_key is not None:
        consumed.append(action_key)
    if answer is not None:
        progress.setdefault("answers", {})[question["question"]] = answer
        progress["index"], progress["selected"] = index + 1, []
    finished = progress.get("index", 0) >= len(questions)
    result = {"source": "phone", "route": "answer", "conversation_id": card["conversation_id"],
              "pending": not finished, "question_index": progress.get("index", 0)}
    if finished:
        # A reply receipt and question progress commit together. If the daemon
        # dies before responding, replay resumes this exact answer, never the
        # next question and never an unintended queued message.
        result["_pending_answer"] = {"token": card["token"], "answers": progress["answers"]}
    with service.store.transaction() as tx:
        state = tx.execute("SELECT state FROM approvals WHERE approval_id=?", (approval["approval_id"],)).fetchone()
        if state is None or state["state"] != "pending":
            raise ConversationError("not-pending", "the question was already answered or withdrawn")
        tx.execute("UPDATE phone_cards SET progress_json=?,updated_at=? WHERE token=?",
                   (json.dumps(progress), utcnow(), card["token"]))
        _event(tx, card, "phone.question.progress", {"approval_id": approval["approval_id"],
               "question_index": progress.get("index", 0)},
               position=f"question:{action_key or (request[0] if request else uuid.uuid4())}")
        _receipt(tx, card, request, result)
    if finished:
        result = _response(service, card, peer, decision="answer", answers=progress["answers"])
        with service.store.transaction() as tx:
            _receipt(tx, card, request, result)
    return result


def tap(service, args, peer):
    _check(service, peer)
    data = args.get("data")
    if not isinstance(data, str) or len(data.encode()) > 64 or not re.fullmatch(r"sf:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", data):
        raise ConversationError("bad-phone-action", "invalid Subfleet callback")
    _, token, key = data.split(":")
    with service._phone_actions:
        card = _card(service, token=token)
        action = card["actions"].get(key)
        if not isinstance(action, dict) or not card.get("approval_id"):
            raise ConversationError("bad-phone-action", "this action was not offered on the card")
        if action.get("kind") == "decision":
            return _response(service, card, peer, decision=action["decision"])
        return _question(service, card, peer, action_key=key, action=action)


def reply(service, args, peer):
    _check(service, peer)
    mid, text = _message_id(args), args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 1_048_576:
        raise ConversationError("bad-phone-reply", "reply must contain text within the message size limit")
    update_id = args.get("update_id")
    if update_id is not None and (isinstance(update_id, bool) or not re.fullmatch(r"[0-9]{1,20}", str(update_id))):
        raise ConversationError("bad-phone-update", "update ID must be a nonnegative integer")
    request = None if update_id is None else (str(update_id), hashlib.sha256(
        json.dumps([mid, text], separators=(",", ":")).encode()).hexdigest())
    with service._phone_actions:
        card = _card(service, mid=mid)
        if request:
            prior = service.store.one("SELECT result_json FROM phone_replies WHERE update_id=?", (request[0],))
            if prior:
                result = json.loads(prior["result_json"])
                if result.pop("_request_sha256", None) != request[1]:
                    raise ConversationError("phone-update-conflict", "this Telegram update was recorded with different text")
                pending = result.pop("_pending_answer", None)
                if pending:
                    result = _response(service, _card(service, token=pending["token"]), peer,
                                       decision="answer", answers=pending["answers"])
                    with service.store.transaction() as tx:
                        _receipt(tx, card, request, result)
                return {**result, "duplicate": True}
        if card.get("approval_id"):
            approval = service.store.approval(card["approval_id"])
            if approval["kind"] == "question" and approval["state"] == "pending":
                return _question(service, card, peer, text=text, request=request)
        # This checkout has no live steer operation. Queue through the normal
        # submit path; its deterministic ID survives a crash before the receipt.
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"subfleet:phone:reply:{request[0]}")) if request else str(uuid.uuid4())
        existing = service.store.find_message(message_id) if request else None
        if existing is not None:
            # A previous process committed this message before recording its
            # phone receipt. Compare against its original settings, regardless
            # of later conversation changes; never submit or widen anything.
            if (existing["conversation_id"] != card["conversation_id"] or existing["digest"] !=
                    message_digest(card["conversation_id"], text, [], existing["settings"])):
                raise ConversationError("phone-update-conflict", "this Telegram update was accepted with different content")
            result = {**service._receipt(existing, created=False), "duplicate": True}
        else:
            for attempt in range(3):
                predecessor = service.store.one("SELECT message_id FROM messages WHERE conversation_id=? "
                                                 "AND origin='person' ORDER BY seq DESC LIMIT 1", (card["conversation_id"],))
                try:
                    result = service.op_message_submit({"conversation_id": card["conversation_id"],
                        "message_id": message_id, "text": text,
                        "after_message_id": predecessor["message_id"] if predecessor else None}, peer)
                    break
                except ConversationError as exc:
                    if exc.reason != "out-of-order" or attempt == 2:
                        raise
        conversation = service.store.conversation(card["conversation_id"])
        result.update(source="phone", route="queued", blocked_by=conversation.get("blocked_by") or conversation.get("legacy_hold"))
        with service.store.transaction() as tx:
            _event(tx, card, "phone.message.submitted", {"telegram_message_id": mid, "route": "queued"},
                   position=f"reply:{request[0] if request else message_id}", message_id=message_id)
            _receipt(tx, card, request, result)
        return result


def notify(service, args, peer):
    _check(service, peer)
    active = args.get("enabled", not args.get("off", False))
    if not isinstance(active, bool):
        raise ConversationError("bad-phone-notify", "enabled must be true or false")
    policy = (service.daemon.policy.get("phone") or {}).get("telegram") or {}
    if active and "done" not in policy.get("events", ["approvals", "questions", "blocks"]):
        raise ConversationError("phone-event-disabled", "enable the done event in phone.telegram before subscribing", code=7)
    conversation = service.store.conversation(args["conversation_id"])
    cid = conversation["conversation_id"]
    with service._phone_actions, service.store.transaction() as tx:
        if active:
            after = tx.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE conversation_id=? AND state='complete'", (cid,)).fetchone()[0]
            tx.execute("INSERT INTO phone_notify(conversation_id,after_seq,created_at) VALUES (?,?,?) "
                       "ON CONFLICT(conversation_id) DO UPDATE SET after_seq=excluded.after_seq,created_at=excluded.created_at",
                       (cid, after, utcnow()))
        else:
            tx.execute("DELETE FROM phone_notify WHERE conversation_id=?", (cid,))
        _event(tx, {"conversation_id": cid, "token": f"notify:{cid}"}, "phone.notify", {"enabled": active},
               position=str(uuid.uuid4()))
    return {"conversation_id": cid, "enabled": active, "source": "phone"}
