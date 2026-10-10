"""Usage: copy state.sqlite3 (and -wal) somewhere, set SUBFLEET_STORE_COPY to that directory, run with an ISO start.

Read-only analysis of a copy of the live Subfleet store plus retained streams.

Answers: how common 1h vs 5m Claude cache writes are; how often conversation
turns changed lane and what the first request then cost; how resume jobs
waited; what provider-limit closures looked like. Prints JSON.
"""
import json, sqlite3, sys, os
from collections import Counter, defaultdict
from datetime import datetime, timezone

LIVE = os.environ.get("SUBFLEET_STORE_COPY", os.path.dirname(os.path.abspath(__file__)))
JOBS = os.path.expanduser("~/.subfleet/jobs")


def t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def stream_usage(path):
    """First/last main-thread request usage and the result usage, or None."""
    try:
        f = open(path, errors="replace")
    except OSError:
        return None
    seen, reqs, results = set(), [], []
    with f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "assistant" and row.get("parent_tool_use_id") is None:
                msg = row.get("message") or {}
                mid, u = msg.get("id"), msg.get("usage")
                if u and mid not in seen:
                    seen.add(mid)
                    reqs.append((u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
                                 u.get("cache_creation_input_tokens") or 0,
                                 (u.get("cache_creation") or {})))
            elif row.get("type") == "result" and isinstance(row.get("usage"), dict):
                results.append(row["usage"])
    if not reqs and not results:
        return None
    out = {"requests": len(reqs)}
    if reqs:
        i, r, w, cc = reqs[0]
        out["first"] = {"input": i, "read": r, "write": w, "prompt": i + r + w}
        i, r, w, cc = reqs[-1]
        out["last"] = {"input": i, "read": r, "write": w, "prompt": i + r + w}
    if results:
        u = results[-1]
        cc = u.get("cache_creation") or {}
        out["result"] = {"input": u.get("input_tokens"), "read": u.get("cache_read_input_tokens"),
                         "write": u.get("cache_creation_input_tokens"), "output": u.get("output_tokens"),
                         "w1h": cc.get("ephemeral_1h_input_tokens"), "w5m": cc.get("ephemeral_5m_input_tokens"),
                         "n_results": len(results)}
    return out


def main(since="2026-09-27T00:00:00Z"):
    db = sqlite3.connect(LIVE + "/state.sqlite3")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT a.attempt_id, a.job_id, a.lane_id, a.state, a.reserved_at, a.started_at, a.finished_at, "
        "a.native_session_id, a.outcome_class, j.kind, j.name, j.created_at, j.pinned_lane "
        "FROM attempts a JOIN jobs j USING(job_id) WHERE a.lane_id LIKE 'claude-%' AND a.started_at >= ? "
        "ORDER BY a.started_at", (since,)).fetchall()
    ttl = Counter()
    share_by_kind = defaultdict(lambda: [0, 0, 0])  # read, prompt, n
    usage = {}
    for r in rows:
        u = stream_usage(f"{JOBS}/{r['attempt_id']}/stdout")
        if not u:
            continue
        usage[r["attempt_id"]] = u
        res = u.get("result")
        if res:
            w1, w5 = res.get("w1h") or 0, res.get("w5m") or 0
            ttl["1h-only" if w1 and not w5 else "5m-only" if w5 and not w1 else "both" if w1 and w5 else "no-writes"] += 1
            if res["read"] is not None:
                p = (res["input"] or 0) + (res["read"] or 0) + (res["write"] or 0)
                s = share_by_kind[r["kind"]]
                s[0] += res["read"]; s[1] += p; s[2] += 1
    # Turns: lane changes within a conversation.
    by_conv = defaultdict(list)
    for r in rows:
        if r["kind"] == "turn" and r["name"]:
            by_conv[r["name"]].append(r)
    closures = [dict(c) for c in db.execute("SELECT * FROM closures WHERE reason='provider-limit'")]

    def closed_at(lane, when):
        out = []
        for c in closures:
            if c["lane_id"] == lane and t(c["created_at"]) <= when and t(c["until_at"]) > when and (
                    not c["released_at"] or t(c["released_at"]) > when):
                out.append({"scope": c["scope"], "until_at": c["until_at"],
                            "reopen_in_s": (t(c["until_at"]) - when).total_seconds()})
        return out

    stays, moves = [], []
    for name, turns in by_conv.items():
        prev = None
        for r in turns:
            u = usage.get(r["attempt_id"])
            if prev is not None and u and "first" in u:
                rec = {"conv": name, "attempt": r["attempt_id"], "from": prev["lane_id"], "to": r["lane_id"],
                       "first": u["first"], "gap_s": (t(r["started_at"]) - t(prev["finished_at"] or prev["started_at"])).total_seconds(),
                       "prev_last_prompt": (usage.get(prev["attempt_id"]) or {}).get("last", {}).get("prompt")}
                if r["lane_id"] == prev["lane_id"]:
                    stays.append(rec)
                else:
                    rec["from_closed"] = closed_at(prev["lane_id"], t(r["reserved_at"]))
                    moves.append(rec)
            prev = r
    # Resumes: wait from creation to start, and the pinned lane's closures at creation.
    resumes = []
    for r in db.execute("SELECT a.attempt_id, a.lane_id, a.reserved_at, a.started_at, j.created_at, j.pinned_lane, a.outcome_class "
                        "FROM attempts a JOIN jobs j USING(job_id) WHERE j.kind='resume' ORDER BY a.started_at"):
        u = stream_usage(f"{JOBS}/{r['attempt_id']}/stdout")
        resumes.append({"attempt": r["attempt_id"], "lane": r["lane_id"],
                        "wait_s": (t(r["reserved_at"]) - t(r["created_at"])).total_seconds(),
                        "closed_at_submit": closed_at(r["lane_id"], t(r["created_at"])),
                        "outcome": r["outcome_class"], "first": (u or {}).get("first")})
    lim = [{"lane": c["lane_id"], "scope": c["scope"], "created": c["created_at"], "until": c["until_at"],
            "span_h": round((t(c["until_at"]) - t(c["created_at"])).total_seconds() / 3600, 2),
            "clock": c["clock_source"]} for c in closures]

    def summ(recs):
        if not recs:
            return {"n": 0}
        writes = sorted(x["first"]["write"] for x in recs)
        reads = sorted(x["first"]["read"] for x in recs)
        prompts = sorted(x["first"]["prompt"] for x in recs)
        med = lambda v: v[len(v) // 2]
        return {"n": len(recs), "first_write_median": med(writes), "first_read_median": med(reads),
                "first_prompt_median": med(prompts), "first_write_sum": sum(writes), "first_read_sum": sum(reads),
                "first_prompt_sum": sum(prompts)}

    print(json.dumps({
        "since": since, "claude_attempts": len(rows), "with_usage": len(usage),
        "ttl_split": ttl, "share_by_kind": {k: {"n": v[2], "read": v[0], "prompt": v[1],
                                                "share": round(v[0] / v[1], 4) if v[1] else None}
                                            for k, v in share_by_kind.items()},
        "turn_stays": summ(stays), "turn_moves": summ(moves),
        "turn_moves_detail": moves,
        "stays_gap_over_1h": summ([x for x in stays if x["gap_s"] > 3600]),
        "stays_gap_under_1h": summ([x for x in stays if x["gap_s"] <= 3600]),
        "resumes": resumes, "provider_limit_closures": lim,
    }, indent=1, default=str))


if __name__ == "__main__":
    main(*sys.argv[1:])
