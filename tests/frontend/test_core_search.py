"""The ⌘K palette's matching and ranking (C-29.12): app/Sources/SearchPalette.swift.

Example tests pin the behaviour Max asked for; the property tests state what
holds for every input and check it against a small Python reference:

- soundness and completeness: the results are exactly the candidates whose
  title, workspace, provider or messages hold every query word (folding case,
  diacritics and width), and `total` counts them;
- tiers: exact title, then every word in the title, then in title, workspace
  or provider, then in messages, then a fuzzy title: each word found nowhere
  has its letters in the title in order within three times its length;
- order: tier, then recent activity (undated last), then folded title, then id,
  whatever order the candidates came in, and the same with the fold cache;
- highlights: sorted, disjoint, in bounds, each on a query word, covering
  every occurrence, by the same folding the index matches with;
- snippets: every message match has one, from the newest message holding the
  most of the words only messages hold, as a cut of that message with its
  white space collapsed, "…" exactly where it was cut, and a highlight;
- monotonicity: another word never adds a result.

Differential tests hold the index's folding to the Python reference and its
ASCII fold to Foundation's, and its containment to Foundation's
`range(of:options:)` wherever folding keeps one character one. Where case
folding makes one character several ("ß" is "ss", "ﬁ" is "fi"), the index
matches part of them and Foundation does not: intended, and pinned below.
"""

from __future__ import annotations

import json
import unicodedata
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from tests.frontend.conftest import needs_swift, run_probe

pytestmark = needs_swift

TIERS = ["exact-title", "title", "details", "message", "fuzzy-title"]
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]


def probe(core_probe, tmp_path, command: str, payload: dict):
    path = tmp_path / f"{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(payload))
    return run_probe(core_probe, command, path)


def search(core_probe, tmp_path, candidates, *queries, limit=50, cache=False) -> dict:
    return probe(core_probe, tmp_path, "search", {"candidates": candidates, "queries": list(queries), "limit": limit,
                                                  "cache": cache})


def answers(core_probe, tmp_path, candidates, *queries, limit=50) -> list[dict]:
    return search(core_probe, tmp_path, candidates, *queries, limit=limit)["answers"]


def conv(cid, title, *, workspace="~/code/app", provider="claude", date=None, messages=(), kind="conversation"):
    return {"id": cid, "kind": kind, "provider": provider, "title": title, "workspace": workspace, "date": date,
            "messages": [{"item": f"{cid}/m{i}", "author": author, "text": text}
                         for i, (author, text) in enumerate(messages)]}


def ids(answer) -> list[str]:
    return [result["id"] for result in answer["results"]]


def spans(text: str, highlights) -> list[str]:
    return [text[start:start + length] for start, length in highlights]


# MARK: - Examples

def test_c29_12_exact_title_first_then_recent_activity(core_probe, tmp_path):
    candidates = [conv("old-exact", "Deploy", date=100), conv("new-title", "Deploy notes", date=300),
                  conv("mid-title", "Plan the deploy", date=200), conv("undated", "deploy checklist")]
    [answer] = answers(core_probe, tmp_path, candidates, "deploy")
    assert ids(answer) == ["old-exact", "new-title", "mid-title", "undated"]
    assert [r["tier"] for r in answer["results"]] == ["exact-title", "title", "title", "title"]
    # White space and case aside, the title is the query.
    [answer] = answers(core_probe, tmp_path, [conv("a", "Fix  the\tBUG", date=1), conv("b", "fix the bug now", date=2)],
                       "  fix THE bug ")
    assert [(r["id"], r["tier"]) for r in answer["results"]] == [("a", "exact-title"), ("b", "title")]


def test_c29_12_title_before_workspace_and_provider_before_messages(core_probe, tmp_path):
    candidates = [
        conv("message", "Unrelated", date=400, messages=[("You", "we should use the palette here")]),
        conv("workspace", "Something", workspace="~/code/palette", date=300),
        conv("title", "Palette polish", date=100),
        conv("provider", "Palette-free", provider="codex", date=500),
    ]
    [answer, codex] = answers(core_probe, tmp_path, candidates, "palette", "codex")
    assert [(r["id"], r["tier"]) for r in answer["results"]] == [
        ("provider", "title"), ("title", "title"), ("workspace", "details"), ("message", "message")]
    assert [(r["id"], r["tier"], r["provider_matched"]) for r in codex["results"]] == [("provider", "details", True)]


def test_c29_12_fuzzy_titles_come_last(core_probe, tmp_path):
    candidates = [conv("fuzzy", "Subfleet release", date=9), conv("spread", "s u b f l e e e e e e e e e e t", date=8),
                  conv("message", "Unrelated", date=1, messages=[("You", "the sbflt typo")]),
                  conv("two", "Search palette", date=5)]
    [sbflt, two, one_letter, with_a, with_provider] = answers(core_probe, tmp_path, candidates, "sbflt", "srch plt", "q",
                                                              "a sbflt", "claude sbflt")
    assert [(r["id"], r["tier"]) for r in sbflt["results"]] == [("message", "message"), ("fuzzy", "fuzzy-title")]
    # Adjacent letters are one highlight.
    assert spans("Subfleet release", sbflt["results"][1]["title_highlights"]) == ["S", "bfl", "t"]
    assert [(r["id"], r["tier"]) for r in two["results"]] == [("two", "fuzzy-title")]
    assert spans("Search palette", two["results"][0]["title_highlights"]) == ["S", "rch", "p", "l", "t"]
    assert one_letter["total"] == 0
    # A word found as it is (here "a" in "Subfleet release", "claude" the
    # provider) leaves the rest to match fuzzily.
    assert ("fuzzy", "fuzzy-title") in [(r["id"], r["tier"]) for r in with_a["results"]]
    fuzzy = next(r for r in with_a["results"] if r["id"] == "fuzzy")
    assert spans("Subfleet release", fuzzy["title_highlights"]) == ["S", "bfl", "t", "a"]
    assert [(r["id"], r["tier"]) for r in with_provider["results"]][-1] == ("fuzzy", "fuzzy-title")


def test_c29_12_a_long_line_costs_no_more_than_a_short_one(core_probe, tmp_path):
    """The snippet of a match far into a megabyte line with one accented
    letter: its place is found by folding a stretch around it, not the line
    (review of 158db058: two seconds for a 5 MB line)."""
    text = "é " + "lorem ipsum " * 250_000 + "the needle here" + " tail" * 10
    candidates = [conv("big", "Log", messages=[("Claude", text)])]
    path = tmp_path / "big.json"
    path.write_text(json.dumps({"candidates": candidates, "queries": ["needle"], "limit": 5}))
    out = run_probe(core_probe, "search", path, timeout=300)
    snippet = out["answers"][0]["results"][0]["snippet"]
    assert spans(snippet["text"], snippet["highlights"]) == ["needle"]
    # Processor time of the search alone (the unoptimized probe folded the line
    # a character at a time for about ten seconds before).
    [seconds] = out["cpu_seconds"]
    assert seconds < 2, seconds


def test_c29_12_every_word_must_match_across_fields(core_probe, tmp_path):
    candidates = [
        conv("split", "Release notes", workspace="~/code/subfleet", date=2,
             messages=[("You", "draft the changelog"), ("Claude", "Here is the palette section")]),
        conv("half", "Release notes", workspace="~/code/other", date=3),
    ]
    [both, missing] = answers(core_probe, tmp_path, candidates, "subfleet palette release", "subfleet zebra")
    assert ids(both) == ["split"] and both["results"][0]["tier"] == "message"
    assert both["results"][0]["snippet"]["author"] == "Claude"
    assert spans(both["results"][0]["snippet"]["text"], both["results"][0]["snippet"]["highlights"]) == ["palette"]
    assert missing["total"] == 0 and missing["results"] == []


def test_c29_12_case_diacritics_and_width_are_ignored(core_probe, tmp_path):
    candidates = [conv("a", "Résumé polish", date=1), conv("b", "CAFÉ opening", workspace="~/Straße", date=2),
                  conv("c", "ｄｅｐｌｏｙ ｗｉｄｅ", date=3)]
    found = {a["query"]: a for a in answers(core_probe, tmp_path, candidates, "resume", "RÉSUMÉ", "cafe", "strasse",
                                             "deploy", "Café")}
    assert ids(found["resume"]) == ["a"] and ids(found["RÉSUMÉ"]) == ["a"]
    assert spans("Résumé polish", found["resume"]["results"][0]["title_highlights"]) == ["Résumé"]
    assert ids(found["cafe"]) == ["b"] and ids(found["Café"]) == ["b"]
    assert ids(found["strasse"]) == ["b"] and found["strasse"]["results"][0]["tier"] == "details"
    assert spans("~/Straße", found["strasse"]["results"][0]["workspace_highlights"]) == ["Straße"]
    assert ids(found["deploy"]) == ["c"]


def test_c29_12_message_match_snippet_comes_from_the_best_newest_message(core_probe, tmp_path):
    candidates = [conv("c", "Quarterly", date=5, messages=[
        ("You", "the alpha plan"),                    # one word
        ("Claude", "alpha and beta, older"),          # both words
        ("You", "beta only"),                         # one word
        ("Claude", "newest: beta then alpha again"),  # both words, newest
        ("You", "nothing here"),
    ])]
    [answer] = answers(core_probe, tmp_path, candidates, "alpha beta")
    snippet = answer["results"][0]["snippet"]
    assert (snippet["item"], snippet["author"]) == ("c/m3", "Claude")
    assert snippet["text"] == "newest: beta then alpha again"
    assert spans(snippet["text"], snippet["highlights"]) == ["beta", "alpha"]


def test_c29_12_snippet_is_a_cut_on_one_line_marked_where_it_was_cut(core_probe, tmp_path):
    before = " ".join(f"word{i}" for i in range(40))
    after = " ".join(f"tail{i}" for i in range(60))
    text = f"{before}\n\n  the   needle\tis here\n{after}"
    [answer] = answers(core_probe, tmp_path, [conv("c", "Log", messages=[("Claude", text)])], "needle")
    snippet = answer["results"][0]["snippet"]["text"]
    assert snippet.startswith("…word") and snippet.endswith("…")
    assert "\n" not in snippet and "  " not in snippet and "\t" not in snippet
    assert "the needle is here" in snippet
    assert len(snippet) <= 2 + 160
    body = snippet.strip("…")
    assert body in " ".join(text.split())
    # Not cut at either end: no marks.
    [short] = answers(core_probe, tmp_path, [conv("c", "Log", messages=[("You", "  find the needle  ")])], "needle")
    assert short["results"][0]["snippet"]["text"] == "find the needle"


def test_c29_12_snippet_finds_the_match_after_non_ascii_lines(core_probe, tmp_path):
    # Folding changes byte lengths ("é", "ß", "ｆ"), so the snippet looks for the
    # match in the line the folded bytes found it on.
    text = "Café crème — résumé\nStraße ｆｕｌｌ\n" + "x " * 30 + "the target word\nlast line"
    [answer] = answers(core_probe, tmp_path, [conv("c", "Notes", messages=[("Claude", text)])], "target")
    snippet = answer["results"][0]["snippet"]
    assert spans(snippet["text"], snippet["highlights"]) == ["target"]
    [accented] = answers(core_probe, tmp_path, [conv("c", "Notes", messages=[("Claude", text)])], "strasse")
    snippet = accented["results"][0]["snippet"]
    assert spans(snippet["text"], snippet["highlights"]) == ["Straße"]


def test_c29_12_empty_query_lists_everything_by_recent_activity(core_probe, tmp_path):
    candidates = [conv("b", "Beta", date=2), conv("none", "Undated"), conv("c", "Gamma", date=3), conv("a", "Alpha", date=1)]
    for query in ("", "   ", "\n\t"):
        [answer] = answers(core_probe, tmp_path, candidates, query)
        assert ids(answer) == ["c", "b", "a", "none"]
        assert {r["tier"] for r in answer["results"]} == {"recent"} and answer["total"] == 4


def test_c29_12_results_are_limited_and_the_rest_counted(core_probe, tmp_path):
    candidates = [conv(f"c{i:02}", f"Task {i}", date=i) for i in range(60)]
    [answer] = answers(core_probe, tmp_path, candidates, "task", limit=50)
    assert answer["total"] == 60 and len(answer["results"]) == 50
    assert ids(answer) == [f"c{i:02}" for i in range(59, 9, -1)]


def test_c29_12_words_past_twelve_are_counted_not_searched(core_probe, tmp_path):
    words = [f"w{i}" for i in range(14)]
    candidates = [conv("c", " ".join(words[:12])), conv("d", "w0 only")]
    [answer, repeated, accent] = answers(core_probe, tmp_path, candidates, " ".join(words), "w0 W0 w0", "\u0301 w0")
    assert answer["ignored_words"] == 2 and ids(answer) == ["c"]
    # Repeats are one word; a word that folds to nothing is no word.
    assert repeated["ignored_words"] == 0 and set(ids(repeated)) == {"c", "d"}
    assert set(ids(accent)) == {"c", "d"}


def test_c29_12_fold_cache_gives_the_same_answers_and_keeps_only_what_it_used(core_probe, tmp_path):
    candidates = [conv("a", "One", date=1, messages=[("You", "alpha"), ("Claude", "beta")]),
                  conv("b", "Two", date=2, messages=[("You", "gamma")]), conv("c", "Three", date=3)]
    out = search(core_probe, tmp_path, candidates, "alpha", "gamma", "", "t", cache=True)
    assert out["cached"] == out["answers"] and out["recached"] == out["answers"]
    assert out["cache_size"] == 3 and out["cache_size_again"] == 3
    assert out["cache_size_after_fewer"] == 2   # "a" alone: its two messages
    assert out["cancelled"] is True


def test_c29_12_duplicate_candidates_keep_the_first(core_probe, tmp_path):
    candidates = [conv("a", "First copy", date=1), conv("a", "Second copy", date=9)]
    [answer] = answers(core_probe, tmp_path, candidates, "copy")
    assert [r["title"] for r in answer["results"]] == ["First copy"] and answer["total"] == 1


# MARK: - Candidates from the store

def _conversation(cid, title, *, provider="claude", native=None, updated="2026-09-27T10:00:00Z", workspace="/w/app"):
    return {"conversation_id": cid, "provider": provider, "native_session_id": native, "title": title,
            "workspace": workspace, "workspace_kind": "in-place", "worktree": None, "allow_main": False, "lane_id": None,
            "settings": {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True},
            "origin": "app", "handoff_from": None, "blocked_by": None, "created_at": updated, "updated_at": updated,
            "last_message": None, "pending_approvals": 0, "active": False}


def _item(sid, *, title=None, prompt=None, provider="claude", mtime=1_790_000_000.0, cwd="/w/old"):
    return {"provider": provider, "native_session_id": sid, "title": title, "first_prompt": prompt, "cwd": cwd,
            "mtime": mtime, "continuable": True}


def _event(seq, kind, message_id, **data):
    return {"seq": seq, "conversation_id": "cv1", "message_id": message_id, "kind": kind, "ts": f"2026-09-27T10:00:{seq:02}Z",
            "data": data}


def test_c29_12_candidates_come_from_every_conversation_and_session(core_probe, tmp_path):
    listing = {"conversations": [_conversation("cv1", "Palette work"), _conversation("cv2", None, provider="codex",
                                                                                    workspace="/w/tools"),
                                 _conversation("cv3", "Bound", native="ABC-1")],
               "catalog": {"complete": True, "items": [_item("s-1", prompt="  how do I deploy the palette?  "),
                                                        _item("s-2", title="Named session", prompt="first words")]}}
    receipts = {"conversation": _conversation("cv1", "Palette work"), "events_cursor": 0, "pending_approvals": [],
                "messages": [{"message_id": "m1", "conversation_id": "cv1", "seq": 1, "state": "complete",
                              "text": "Search the palette please"}]}
    events = {"events": [
        _event(1, "accepted", "m1"),
        _event(2, "thinking", "m1", text="private musing about zebras", block="t1"),
        _event(3, "tool.started", "m1", name="Bash", id="x1", summary="grep zebras"),
        _event(4, "text", "m1", text="Here is the **palette** answer", block="a1"),
    ], "next": 4, "reset": False}
    history = {"items": [{"role": "assistant", "kind": "text", "text": "An older answer about cats", "cursor": 10},
                         {"role": "user", "kind": "text", "text": "An older question", "cursor": 5}],
               "next_before": None}
    sessions = [_item("s-3", title="Beyond the page", prompt="palette elsewhere"),
                _item("abc-1", title="Bound elsewhere"),        # a conversation continues it (case aside)
                _item("s-1", prompt="a duplicate of a loaded one")]
    out = probe(core_probe, tmp_path, "search-state", {
        "steps": [{"list": listing}, {"open": receipts}, {"events": events, "conversation_id": "cv1"},
                  {"history": history, "conversation_id": "cv1"}],
        "search": "zzz-no-sidebar-match", "provider_filter": "codex", "sessions": sessions,
        "queries": ["palette", "zebras", "cats", "tools", "beyond"]})
    assert out["sidebar"] == [], "the sidebar's own filters are applied there"
    by_id = {c["id"]: c for c in out["candidates"]}
    assert set(by_id) == {"cv:cv1", "cv:cv2", "cv:cv3", "native:claude:s-1", "native:claude:s-2", "native:claude:s-3"}
    assert by_id["cv:cv2"]["title"] == "tools" and by_id["cv:cv2"]["workspace"] == "/w/tools"
    messages = by_id["cv:cv1"]["messages"]
    assert [(m["author"], m["text"]) for m in messages] == [
        ("You", "An older question"),
        ("Claude", "An older answer about cats"),
        ("You", "Search the palette please"),
        ("Claude", "Here is the **palette** answer"),
    ]
    assert all(m["item"] for m in messages)
    assert by_id["native:claude:s-1"]["messages"] == [{"item": None, "author": "First prompt",
                                                        "text": "how do I deploy the palette?"}]
    assert by_id["native:claude:s-3"]["messages"][0]["text"] == "palette elsewhere"
    found = {a["query"]: a for a in out["answers"]}
    assert ids(found["palette"])[0] == "cv:cv1" and set(ids(found["palette"])) == {
        "cv:cv1", "native:claude:s-1", "native:claude:s-3"}
    assert found["zebras"]["total"] == 0, "thoughts and tool calls are not searched"
    assert ids(found["cats"]) == ["cv:cv1"] and found["cats"]["results"][0]["snippet"]["item"].startswith("history:")
    assert ids(found["tools"]) == ["cv:cv2"] and found["tools"]["results"][0]["tier"] == "exact-title"
    assert ids(found["beyond"]) == ["native:claude:s-3"]


# MARK: - Properties

VOCABULARY = ["alpha", "Alpha", "beta", "GAMMA", "delta", "café", "CAFÉ", "cafe", "résumé", "resume", "naïve",
              "über", "UBER", "Ñandú", "façade", "a", "ab", "palette", "search", "Search", "x", "xx", "élan"]
SEPARATORS = [" ", "  ", "\n", "\t", " \n "]


def reference_fold(text: str) -> str:
    """Compatibility decomposition, marks dropped, case folded: over the test
    alphabets this is Foundation's case, diacritic and width folding."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def reference_words(query: str) -> list[str]:
    words, seen = [], []
    for word in query.split():
        folded = reference_fold(word)
        if not folded or folded in seen:
            continue
        seen.append(folded)
        words.append(folded)
    return words[:12]


def exact_form(text: str) -> str:
    return " ".join(f for f in (reference_fold(w) for w in text.split()) if f)


def reference_tier(candidate: dict, query: str) -> str | None:
    return tier_and_words(candidate, query)[0]


def tier_and_words(candidate: dict, query: str):
    """The tier, the words only messages hold (a snippet's), and the words
    found only as a fuzzy title match."""
    words = reference_words(query)
    title, workspace = reference_fold(candidate["title"]), reference_fold(candidate["workspace"])
    provider = reference_fold(candidate["provider"])
    if all(w in title for w in words):
        return ("exact-title" if exact_form(candidate["title"]) == exact_form(query) else "title"), [], []
    rest = [w for w in words if not (w in title or w in workspace or w in provider)]
    if not rest:
        return "details", [], []
    folded = [reference_fold(m["text"]) for m in candidate["messages"]]
    in_messages = [w for w in rest if any(w in text for text in folded)]
    if len(in_messages) == len(rest):
        return "message", rest, []
    fuzzy_words = [w for w in rest if w not in in_messages]
    if all(fuzzy(w, title) is not None for w in fuzzy_words):
        return "fuzzy-title", in_messages, fuzzy_words
    return None, [], []


def fuzzy(word: str, title: str) -> list[int] | None:
    """The positions of the shortest stretch of `title` holding `word`'s
    letters in order (the first of equals), within three times its length."""
    if len(word) < 2 or len(word) > len(title):
        return None
    best = None
    for start, character in enumerate(title):
        if character != word[0]:
            continue
        positions, at = [start], start + 1
        for letter in word[1:]:
            found = title.find(letter, at)
            if found < 0:
                break
            positions.append(found)
            at = found + 1
        if len(positions) < len(word):
            break
        if best is None or positions[-1] - start < best[-1] - best[0]:
            best = positions
    if best is None or best[-1] - best[0] + 1 > 3 * len(word):
        return None
    return best


def found_characters(text: str, words) -> set[int]:
    """The text's characters (by index) in occurrences of the words, found left to right."""
    folded, owner = "", []
    for index, character in enumerate(text):
        piece = reference_fold(character)
        folded += piece
        owner += [index] * len(piece)
    covered = set()
    for word in words:
        at = folded.find(word)
        while at >= 0:
            covered |= set(owner[at:at + len(word)])
            at = folded.find(word, at + len(word))
    return covered


def fuzzy_characters(title: str, words) -> set[int]:
    """The title's characters (by index) the words' fuzzy matches take."""
    folded, owner = "", []
    for index, character in enumerate(title):
        piece = reference_fold(character)
        folded += piece
        owner += [index] * len(piece)
    return {owner[p] for w in words for p in (fuzzy(w, folded) or [])}


def order_key(candidate: dict, tier: str):
    date = candidate["date"]
    return (TIERS.index(tier), 0 if date is not None else 1, -(date or 0),
            reference_fold(candidate["title"]).encode(), candidate["id"])


def phrase(draw, low, high):
    words = draw(st.lists(st.sampled_from(VOCABULARY), min_size=low, max_size=high))
    seps = draw(st.lists(st.sampled_from(SEPARATORS), min_size=len(words), max_size=len(words)))
    return "".join(w + s for w, s in zip(words, seps)).strip(" ") if words else ""


@st.composite
def corpora(draw):
    count = draw(st.integers(min_value=1, max_value=9))
    candidates = []
    for index in range(count):
        messages = [{"item": f"c{index}/m{m}", "author": draw(st.sampled_from(["You", "Claude", "Codex"])),
                     "text": phrase(draw, 1, 10)} for m in range(draw(st.integers(0, 4)))]
        candidates.append({"id": f"c{index}", "kind": draw(st.sampled_from(["conversation", "session"])),
                           "provider": draw(st.sampled_from(["claude", "codex"])),
                           "title": phrase(draw, 1, 3), "workspace": "~/" + phrase(draw, 0, 2).replace("\n", ""),
                           "date": draw(st.one_of(st.none(), st.sampled_from([1.0, 2.0, 3.0]))), "messages": messages})
    queries = [phrase(draw, 0, 3) for _ in range(draw(st.integers(1, 4)))]
    queries += [draw(st.sampled_from(["codex", "claude", "~", "é"]))]
    # Letters of titles in order, for fuzzy matches.
    queries += [draw(st.sampled_from(["plt", "srch", "cfe", "rsm", "abt", "gma", "pltt srch", "apa", "xx", "nd"]))]
    extra = draw(st.sampled_from(VOCABULARY))
    return candidates, queries, extra


def check_highlights(text: str, highlights, words):
    last_end = -1
    for start, length in highlights:
        assert length > 0 and 0 <= start and start + length <= len(text)
        assert start > last_end, "highlights are sorted and disjoint"
        last_end = start + length
        assert any(w in reference_fold(text[start:start + length]) for w in words)
    # Every occurrence of a word in the folded text, found left to right, lies in a highlight.
    folded, owner = "", []
    for index, character in enumerate(text):
        piece = reference_fold(character)
        folded += piece
        owner += [index] * len(piece)
    for word in words:
        at = folded.find(word)
        while at >= 0:
            first, last = owner[at], owner[at + len(word) - 1]
            assert any(s <= first and last < s + n for s, n in highlights), (text, word)
            at = folded.find(word, at + len(word))


@settings(max_examples=60, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(corpora())
def test_c29_12_search_properties(core_probe, tmp_path, corpus):
    candidates, queries, extra = corpus
    queries = queries + [f"{q} {extra}" for q in queries]
    out = search(core_probe, tmp_path, candidates, *queries, limit=100, cache=True)
    reversed_out = search(core_probe, tmp_path, list(reversed(candidates)), *queries, limit=100)
    # Determinism: input order, and building through the fold cache, change nothing.
    assert reversed_out["answers"] == out["answers"]
    assert out["cached"] == out["answers"] and out["recached"] == out["answers"]
    by_id = {c["id"]: c for c in candidates}
    results_for = {}
    for answer in out["answers"]:
        query = answer["query"]
        words = reference_words(query)
        if not words:
            assert [r["tier"] for r in answer["results"]] == ["recent"] * len(candidates)
            results_for[query] = set(by_id)
            continue
        expected = {cid: tier for cid, c in by_id.items() if (tier := reference_tier(c, query))}
        # Soundness, completeness, tiers.
        assert {r["id"]: r["tier"] for r in answer["results"]} == expected
        assert answer["total"] == len(expected)
        # Order.
        assert ids(answer) == sorted(expected, key=lambda cid: order_key(by_id[cid], expected[cid]))
        results_for[query] = set(expected)
        for result in answer["results"]:
            candidate = by_id[result["id"]]
            _, rest, fuzzy_words = tier_and_words(candidate, query)
            found = [w for w in words if w not in fuzzy_words]
            if fuzzy_words:
                covered = {i for start, length in result["title_highlights"] for i in range(start, start + length)}
                assert covered == found_characters(candidate["title"], found) | fuzzy_characters(
                    candidate["title"], fuzzy_words)
            else:
                check_highlights(candidate["title"], result["title_highlights"], words)
            check_highlights(candidate["workspace"], result["workspace_highlights"], words)
            assert result["provider_matched"] == any(w in reference_fold(candidate["provider"]) for w in words)
            snippet = result["snippet"]
            if not rest:
                assert snippet is None
                continue
            counts = [sum(w in reference_fold(m["text"]) for w in rest) for m in candidate["messages"]]
            best = max(i for i, c in enumerate(counts) if c == max(counts))
            message = candidate["messages"][best]
            assert (snippet["item"], snippet["author"]) == (message["item"], message["author"])
            body = snippet["text"].removeprefix("…").removesuffix("…")
            assert body in " ".join(message["text"].split())
            assert snippet["text"].startswith("…") == (not " ".join(message["text"].split()).startswith(body))
            assert snippet["text"].endswith("…") == (not " ".join(message["text"].split()).endswith(body))
            assert snippet["highlights"], "the word the snippet was cut around is highlighted"
            check_highlights(snippet["text"], snippet["highlights"], words)
    # Another word never adds a result.
    for query in queries[: len(queries) // 2]:
        assert results_for[f"{query} {extra}"] <= results_for[query]


# MARK: - Folding, against Foundation and the reference

@settings(max_examples=200, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(st.lists(st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=127), max_size=40), min_size=1,
                max_size=20))
def test_c29_12_ascii_fold_is_foundations_fold(core_probe, tmp_path, texts):
    out = probe(core_probe, tmp_path, "search-fold", {"texts": texts, "pairs": []})
    for text, fold in zip(texts, out["folds"]):
        assert fold["ascii"] is True
        assert fold["bytes"] == fold["foundation"] == list(text.lower().encode())


#: Characters case folding makes several of.
EXPANDING = ["ß", "ẞ", "ﬁ", "ﬀ", "ﬆ", "ŉ", "İ"]
KEPT = list("abcAZ xyé") + ["É", "ss", "SS", "st", "ñ", "n\u0303", "ü", "ｆ", "Ｆ", "fi", "f", "Å", "Ω", "ω", "中",
                            "😀", "\n", "i", "ı", "æ", "e\u0301"]


def texts_of(letters, low, high):
    return st.lists(st.sampled_from(letters), min_size=low, max_size=high).map("".join)


@settings(max_examples=200, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(st.lists(texts_of(KEPT + EXPANDING, 0, 12), min_size=1, max_size=20))
def test_c29_12_folding_is_the_reference_folding(core_probe, tmp_path, texts):
    """Case, diacritics and width folded as Unicode's case folding and canonical
    decomposition say, expansions included."""
    out = probe(core_probe, tmp_path, "search-fold", {"texts": texts, "pairs": []})
    for text, fold in zip(texts, out["folds"]):
        assert bytes(fold["bytes"]) == reference_fold(text).encode(), text


#: A query's words hold no white space: the query is split at it.
WORD_LETTERS = [letter for letter in KEPT if not any(c.isspace() for c in letter)]


@settings(max_examples=200, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(st.lists(st.tuples(texts_of(KEPT + ["\u0301"], 0, 12), texts_of(WORD_LETTERS, 1, 3)), min_size=1, max_size=15))
def test_c29_12_containment_is_foundations_where_folding_keeps_characters_one(core_probe, tmp_path, pairs):
    """Words as the query makes them. (A word holding a newline would differ: a
    lone accent after a control character folds away, "\\n\u0301a" holds "\\na"
    folded, and Foundation's search does not find it there.)"""
    out = probe(core_probe, tmp_path, "search-fold", {"texts": [], "pairs": [list(p) for p in pairs]})
    for (text, word), pair in zip(pairs, out["pairs"]):
        assert pair["folded"] == pair["foundation"], (text, word)


def test_c29_12_expansions_match_by_folding_not_by_foundation(core_probe, tmp_path):
    """Intended: the index matches part of an expansion, which Foundation's
    search does not, and never what folding does not hold, which Foundation's
    sometimes does ("ß" in "ast"). Highlights follow the index."""
    pairs = [["Maße", "s"], ["Straße", "strasse"], ["aﬁ", "f"], ["ast", "ß"], ["afi", "ﬀ"]]
    out = probe(core_probe, tmp_path, "search-fold", {"texts": [], "pairs": pairs})
    assert [p["folded"] for p in out["pairs"]] == [True, True, True, False, False]
    assert [p["foundation"] for p in out["pairs"]] == [False, True, False, True, True]
    [answer] = answers(core_probe, tmp_path, [conv("a", "Maße", date=1), conv("b", "Office ﬁle", date=2)], "masse")
    assert ids(answer) == ["a"] and spans("Maße", answer["results"][0]["title_highlights"]) == ["Maße"]
    [answer] = answers(core_probe, tmp_path, [conv("b", "Office ﬁle", date=2)], "file")
    assert spans("Office ﬁle", answer["results"][0]["title_highlights"]) == ["ﬁle"]
