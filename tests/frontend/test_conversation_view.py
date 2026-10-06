"""Host the conversation window's real views (C-27.5, C-29.7; design §12): the
pinned strip carries a Review button exactly while cards wait, the sidebar's
hand badge is a button that shows them (2026-09-27), and the queue tray above
the composer offers Withdraw on each message it holds, Steer where the daemon
offers it, and a bounded list for a long queue (2026-09-28).

The probe hosts `QueueTray` offscreen with rows built directly. SwiftUI builds
no accessibility tree without an assistive client and a link button's AppKit
title is empty, so a tray control is named by what its click fired (the closures
log "withdraw:<id>" and "steer:<id>") and by its width, matched against link
buttons the probe draws with each label the tray uses. The same limit hides the
buttons' VoiceOver labels (`.accessibilityLabel`) and tooltips (`.help`); no test
here checks them.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="module")
def views(tmp_path_factory) -> dict:
    probe = compile_probe(tmp_path_factory.mktemp("subfleet-conversation-view") / "probe",
                          ROOT / "tests/frontend/ConversationViewProbe.swift", "SUBFLEET_VIEW_TEST")
    result = subprocess.run([str(probe)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def test_c27_5_the_strip_shows_review_only_while_a_card_waits(views):
    """C-27.5: no card, no Review; the label counts the cards."""
    strips = {strip["pending"]: strip for strip in views["strips"]}
    assert [strips[n]["label"] for n in (0, 1, 2, 12)] == [None, "Review", "Review (2)", "Review (12)"]
    # The same turn in every strip: only the Review button changes the room it takes.
    assert strips[0]["width"] < strips[1]["width"] < strips[2]["width"] <= strips[12]["width"]
    # A bordered button is taller than the caption line it joins.
    assert strips[0]["height"] < strips[1]["height"]
    assert strips[1]["height"] == strips[2]["height"] == strips[12]["height"]
    assert views["visible_windows"] == 0


def test_c27_5_the_strip_buttons_do_what_they_say(views):
    """C-27.5: Stop stops and never opens a request; where AppKit draws Review
    (before macOS 26) clicking it opens one, and only while a card waits."""
    for strip in views["strips"]:
        clicks = strip["clicks"]
        assert clicks.count("stop") == 1 and "none" not in clicks
        assert clicks.count("review") == (0 if strip["pending"] == 0 else len(clicks) - 1)
        assert clicks.count("review") <= 1


def test_c27_5_the_sidebar_hand_badge_is_a_button_that_shows_the_cards(views):
    """C-27.5: the badge opens its conversation at the oldest card."""
    rows = {row["pending"]: row for row in views["rows"]}
    assert rows[0]["clicks"] == []
    assert rows[2]["clicks"] == ["badge"]
    assert rows[2]["width"] > rows[0]["width"]
    assert rows[2]["spoken"] == "2 approvals waiting"


# The queue tray (C-29.7).

@pytest.fixture(scope="module")
def trays(views) -> dict:
    return {tray["name"]: tray for tray in views["trays"]}


@pytest.fixture(scope="module")
def tray_overhead(trays, views) -> float:
    """The tray's header, spacing and padding: a one-row tray less its row."""
    return trays["one"]["passes"][0]["height"] - views["tray_rows"]["one_line"]


def fired(clicks: list[list[str]]) -> list[str]:
    """Every event the clicks of one pass fired, in click order."""
    return [event for click in clicks for event in click]


def ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{n:02d}" for n in range(1, count + 1)]


def withdrawn_top_down(shown: dict) -> list[str]:
    """The messages one pass's Withdraw buttons withdraw, from the top of the
    tray down. Each row sits below the one before it, so no two Withdraw
    buttons share a top."""
    pairs = [(top, click[0].removeprefix("withdraw:")) for top, click in zip(shown["tops"], shown["clicks"])
             if len(click) == 1 and click[0].startswith("withdraw:")]
    assert len({top for top, _ in pairs}) == len(pairs)
    return [mid for _, mid in sorted(pairs)]


def test_c29_7_a_lone_queued_message_has_one_withdraw_that_withdraws_it(trays, views):
    """C-29.7: one queued message shows one AppKit button, Withdraw; clicking it
    withdraws that message once and fires nothing else. The tray holds no rows of
    its own, so the click changes nothing until the store's next layout."""
    one = trays["one"]
    assert one["title"] == "1 message queued; it goes when this turn ends"
    for shown in one["passes"]:
        assert shown["buttons"] == 1
        assert shown["clicks"] == [["withdraw:q1"]]
        assert shown["widths"] == [views["label_widths"]["Withdraw"]]
        assert shown["width"] == one["width"] == 480
    assert one["passes"][0] == one["passes"][1] == one["passes"][2]


def test_c29_7_an_unblock_note_has_no_withdraw(trays):
    """C-29.7: in a tray of an unblock note, a queued message and one still
    sending, the note (withdraw .none) has no Withdraw; each message's Withdraw
    withdraws it once, and no click ever withdraws the note."""
    note, one = trays["note"], trays["one"]
    assert note["rows"] == ["note", "q1", "q2"]
    for shown in note["passes"]:
        assert shown["buttons"] == 2
        assert all(len(click) == 1 for click in shown["clicks"])
        assert sorted(fired(shown["clicks"])) == ["withdraw:q1", "withdraw:q2"]
        assert "withdraw:note" not in fired(shown["clicks"])
        assert withdrawn_top_down(shown) == ["q1", "q2"]
        # No button sits in the note's row, the first, where a lone row's Withdraw sits.
        assert one["passes"][0]["tops"][0] not in shown["tops"]


def test_c29_7_a_row_being_withdrawn_offers_no_second_withdraw(trays, views):
    """C-29.7: a row whose Withdraw the daemon has not answered (`withdrawing`)
    has no Withdraw button; the row after it keeps its own, in its own row. The
    busy row shows a progress indicator instead: each busy tray draws as many
    AppKit progress indicators as the row's `ProgressView().controlSize(.mini)`
    hosted alone does (one on macOS 26.6), on every pass, and no tray without a
    busy row draws one."""
    busy, alone, pair = trays["busy"], trays["busy-only"], trays["steer-no-closure"]
    for shown in busy["passes"]:
        assert shown["buttons"] == 1
        assert shown["clicks"] == [["withdraw:q2"]]
        assert shown["tops"] == [pair["passes"][0]["tops"][1]]
    for shown in alone["passes"]:
        assert shown["buttons"] == 0 and shown["clicks"] == []
    reference = views["spinner_reference"]
    assert reference <= 1
    assert {name for name, tray in trays.items() if tray["withdrawing"]} == {"busy", "busy-only", "steer-busy"}
    for name, tray in trays.items():
        assert all(shown["spinners"] == (reference if tray["withdrawing"] else 0) for shown in tray["passes"]), name


def test_c29_7_a_row_being_withdrawn_cannot_be_steered(trays):
    """C-29.7: the head row the daemon offers to steer (canSteer, with a steer
    closure) has neither Steer nor Withdraw while its Withdraw is under way, so
    no click steers a message on its way out; the only button is the next
    row's Withdraw, in that row."""
    busy, pair = trays["steer-busy"], trays["steer-no-closure"]
    assert busy["rows"] == ["q1", "q2"] and busy["withdrawing"] == ["q1"]
    for shown in busy["passes"]:
        assert shown["buttons"] == 1
        assert shown["clicks"] == [["withdraw:q2"]]
        assert shown["tops"] == [pair["passes"][0]["tops"][1]]


def test_c29_7_twelve_queued_show_three_and_show_nine_more(trays, views):
    """C-29.7: twelve queued messages collapse to the first three in the order
    the daemon sends them, each with Withdraw, and a Show 9 more link below them:
    four AppKit buttons. Clicking Show 9 more withdraws nothing. The link's words
    are not readable from AppKit; it is as wide as a link labelled "Show 9 more",
    and forty queued messages give one as wide as "Show 37 more"."""
    labels = views["label_widths"]
    assert labels["Show 9 more"] != labels["Show 37 more"] and labels["Show fewer"] not in (
        labels["Show 9 more"], labels["Show 37 more"], labels["Withdraw"])
    for name, hidden, label in (("twelve", 9, "Show 9 more"), ("forty", 37, "Show 37 more")):
        tray = trays[name]
        assert (tray["shown"], tray["hidden"]) == (3, hidden)
        collapsed = tray["passes"][0]
        assert collapsed["buttons"] == 4
        assert sorted(fired(collapsed["clicks"])) == [f"withdraw:{mid}" for mid in ids("q", 3)]
        assert withdrawn_top_down(collapsed) == ids("q", 3)
        assert all(len(click) == 1 for click in collapsed["clicks"] if click)
        more = [index for index, click in enumerate(collapsed["clicks"]) if not click]
        assert len(more) == 1
        assert collapsed["widths"][more[0]] == labels[label]
        assert all(width == labels["Withdraw"] for index, width in enumerate(collapsed["widths"])
                   if index != more[0])
        # Below the three rows.
        assert collapsed["tops"][more[0]] == max(collapsed["tops"])
    assert trays["twelve"]["passes"][0]["height"] == trays["forty"]["passes"][0]["height"]


def test_c29_7_show_more_expands_and_a_long_queue_scrolls_inside_the_tray(trays, views, tray_overhead):
    """C-29.7: after Show 9 more, all twelve rows have their Withdraw (each
    withdraws its row once), top to bottom in the order the daemon sends them,
    and the header has Show fewer. The tray is at most
    200 pt of rows plus its header: the rows scroll inside it, so twelve and
    forty queued messages take the same room. Show fewer collapses it again."""
    labels = views["label_widths"]
    for name, count in (("twelve", 12), ("forty", 40)):
        tray = trays[name]
        collapsed, expanded, again = tray["passes"]
        assert expanded["buttons"] == count + 1
        assert sorted(fired(expanded["clicks"])) == [f"withdraw:{mid}" for mid in ids("q", count)]
        assert withdrawn_top_down(expanded) == ids("q", count)
        fewer = [index for index, click in enumerate(expanded["clicks"]) if not click]
        assert len(fewer) == 1 and expanded["widths"][fewer[0]] == labels["Show fewer"]
        # In the header, above every row.
        assert expanded["tops"][fewer[0]] == min(expanded["tops"])
        assert expanded["height"] <= 200 + tray_overhead + 4
        [scroll] = expanded["scrolls"]
        assert scroll["visible"] == 200 < scroll["document"]
        # Show fewer: back to three rows and Show N more.
        assert (again["buttons"], again["height"], again["tops"]) == (4, collapsed["height"], collapsed["tops"])
        assert withdrawn_top_down(again) == ids("q", 3)
    twelve, forty = trays["twelve"]["passes"][1], trays["forty"]["passes"][1]
    assert twelve["height"] == forty["height"]
    assert forty["scrolls"][0]["document"] > twelve["scrolls"][0]["document"]


def test_c29_7_the_collapsed_tray_shows_what_queue_tray_visible_says(trays, views):
    """C-29.7: for 1 to 9, 12, 13 and 40 queued messages the collapsed tray
    shows every row up to four; past four, the first three and one Show N more
    link (`queueTrayVisible`, limit 4), N the rows it hides. Show N more expands
    to every row's Withdraw; a tray of four or fewer has no link and the same
    rows on every pass. Rows run top to bottom in the order the daemon sends
    them, collapsed and expanded.

    The link's N is read from its width, which equals a link labelled with the
    hidden count. Digits are not all as wide as each other, so the test also
    checks that some tray would draw a different width with N one short (13
    hides 10, "Show 9 more" is a digit narrower) and some with N one over (12
    hides 9, "Show 10 more" is a digit wider)."""
    labels = views["label_widths"]
    names = [f"count-{n}" for n in (*range(1, 10), 13)] + ["twelve", "forty"]
    heights = {}
    one_short_differs = one_over_differs = False
    for name in names:
        tray = trays[name]
        count = len(tray["rows"])
        shown = count if count <= 4 else 3
        assert (tray["shown"], tray["hidden"]) == (shown, count - shown)
        collapsed, expanded, again = tray["passes"]
        link = 1 if count > shown else 0
        assert collapsed["buttons"] == shown + link
        assert sorted(fired(collapsed["clicks"])) == [f"withdraw:{mid}" for mid in tray["rows"][:shown]]
        assert withdrawn_top_down(collapsed) == withdrawn_top_down(again) == tray["rows"][:shown]
        assert sum(1 for click in collapsed["clicks"] if not click) == link
        if link:
            hidden = count - shown
            [more] = [index for index, click in enumerate(collapsed["clicks"]) if not click]
            assert collapsed["widths"][more] == labels[f"Show {hidden} more"], name
            one_short_differs |= labels[f"Show {hidden - 1} more"] != labels[f"Show {hidden} more"]
            one_over_differs |= labels[f"Show {hidden + 1} more"] != labels[f"Show {hidden} more"]
            assert expanded["buttons"] == count + 1
            assert sorted(fired(expanded["clicks"])) == [f"withdraw:{mid}" for mid in tray["rows"]]
            assert withdrawn_top_down(expanded) == tray["rows"]
        else:
            assert collapsed == expanded == again
        assert (again["buttons"], again["widths"]) == (collapsed["buttons"], collapsed["widths"])
        heights[count] = collapsed["height"]
    assert one_short_differs and one_over_differs
    assert labels["Show 9 more"] < labels["Show 10 more"]
    # Each row up to four takes room; past four the collapsed tray stays the same size.
    assert heights[1] < heights[2] < heights[3] < heights[4]
    assert len({heights[n] for n in (5, 6, 7, 8, 9, 12, 13, 40)}) == 1


EXPANDED_FRAME_BUG = (
    "app/Sources/UIQueueTray.swift:37: the expanded list's `.frame(maxHeight: 200)` takes the height "
    "proposed to it, up to 200 pt, not the height its rows need. Alone, an expanded tray of 5 to 9 short "
    "rows is 235 pt with the rows centred in it (expected 126 to 202 pt with the rows under the header); "
    "in a 700 pt column like the conversation view's, expanding 5 rows takes 120 pt from the timeline "
    "instead of 19 pt. In a scratch copy of the sources, `.fixedSize(horizontal: false, vertical: true)` "
    "after that frame gives the expected layout and keeps the 200 pt scrolling list for 12 and 40 rows.")


@pytest.mark.xfail(strict=True, reason=EXPANDED_FRAME_BUG)
def test_c29_7_an_expanded_tray_whose_rows_fit_is_as_tall_as_its_rows(trays):
    """C-29.7: Show N more on five to nine short rows shows every row directly
    under the header: each row adds as much height as the fourth row of a
    collapsed tray does, and the first row sits as far below Show fewer as it
    does in a twelve-row tray, whose rows scroll. In a 700 pt column laid out
    like the conversation view's (a stand-in: the real view needs a live model),
    where the tray is proposed more room than its rows need, the same holds, and
    expanding five rows takes one row's height from the timeline."""
    step = trays["count-4"]["passes"][0]["height"] - trays["count-3"]["passes"][0]["height"]
    header_to_row = sorted(trays["twelve"]["passes"][1]["tops"])
    first_row = header_to_row[1] - header_to_row[0]
    observed, expected = {}, {}
    for n in range(5, 10):
        tops = sorted(trays[f"count-{n}"]["passes"][1]["tops"])
        observed[n] = (trays[f"count-{n}"]["passes"][1]["height"], tops[1] - tops[0])
        expected[n] = (trays["count-4"]["passes"][0]["height"] + (n - 4) * step, first_row)

    def column(name: str, index: int) -> tuple[float, float]:
        shown = trays[name]["passes"][index]
        tops = sorted(shown["tops"])
        [timeline] = [scroll["visible"] for scroll in shown["scrolls"] if scroll["document"] == 2000]
        return tops[1] - tops[0], timeline

    gap, timeline = column("column-5", 1)
    _, collapsed_timeline = column("column-5", 0)
    assert (observed, gap, collapsed_timeline - timeline) == (expected, column("column-12", 1)[0], step)


@pytest.mark.xfail(strict=True, reason=(
    "app/Sources/UIQueueTray.swift:37, the same frame: an expanded tray withdrawn down to four rows stays "
    "235 pt tall with its four rows centred in it, and has neither Show fewer nor Show N more (line 27: "
    "four rows are all shown collapsed), so the person cannot shrink it; a collapsed four-row tray is "
    "107 pt with its rows under the header."))
def test_c29_7_an_expanded_tray_withdrawn_to_four_rows_is_as_tall_as_its_rows(trays):
    """C-29.7: twelve rows expanded with Show 9 more, then the store's next
    layout leaves four (the tray keeps its expanded state): the tray shows the
    four rows as a collapsed four-row tray does, same height and same places."""
    shrunk, four = trays["twelve-then-four"]["passes"][1], trays["count-4"]["passes"][0]
    assert trays["twelve-then-four"]["passes"][0]["buttons"] == 4
    assert shrunk["buttons"] == 4
    assert sorted(fired(shrunk["clicks"])) == [f"withdraw:{mid}" for mid in ids("q", 4)]
    assert (shrunk["height"], shrunk["tops"]) == (four["height"], four["tops"])


def test_c29_7_a_long_message_takes_two_lines_at_most(views, trays):
    """C-29.7: at the tray's width a row whose text is 5,000 characters, the
    same text as `queuePreview` cuts it (280 characters), or forty short lines is
    no taller than a row whose text is exactly two lines long (the fewest words
    that wrap), and taller than a one-line row: two lines, then an ellipsis. The
    long row stays the tray's width and its Withdraw keeps its width."""
    rows = views["tray_rows"]
    assert rows["one_word_short_of_two_lines"] == rows["one_line"] < rows["two_line"]
    assert rows["long_raw_length"] == 5000 and rows["long_preview_length"] == 280
    for name in ("long_raw", "long_preview", "many_lines"):
        assert rows["one_line"] < rows[name] <= rows["two_line"], name
    assert rows["long_raw_width"] == rows["width"]
    long, one = trays["long"]["passes"][0], trays["one"]["passes"][0]
    assert long["width"] == one["width"]
    assert long["height"] <= one["height"] + rows["two_line"] - rows["one_line"]
    assert long["widths"] == one["widths"] == [views["label_widths"]["Withdraw"]]
    assert long["clicks"] == [["withdraw:q1"]]


def test_c29_7_a_status_caption_adds_a_line(views):
    """C-29.7: a row with a status caption ("Deferred: provider busy") is taller
    than the same row without one, at one line and at two; a 5,000-character row
    with a caption is no taller than a two-line row with one."""
    rows = views["tray_rows"]
    assert rows["status"] > rows["one_line"]
    assert rows["status_two_line"] > rows["two_line"]
    assert rows["long_status"] <= rows["status_two_line"]


def test_c29_7_steer_shows_only_where_the_daemon_offers_it(trays, views):
    """C-29.7: a row with canSteer, given a steer closure, has a Steer button in
    its own row whose click steers that row only; its Withdraw and the next row's
    still withdraw. Without the closure, or with canSteer false, no row has Steer."""
    labels = views["label_widths"]
    steer = trays["steer"]
    for shown in steer["passes"]:
        assert shown["buttons"] == 3
        assert all(len(click) == 1 for click in shown["clicks"])
        assert sorted(fired(shown["clicks"])) == ["steer:q1", "withdraw:q1", "withdraw:q2"]
        [index] = [i for i, click in enumerate(shown["clicks"]) if click == ["steer:q1"]]
        [head] = [i for i, click in enumerate(shown["clicks"]) if click == ["withdraw:q1"]]
        assert shown["widths"][index] == labels["Steer"]
        assert shown["tops"][index] == shown["tops"][head]
        assert withdrawn_top_down(shown) == ["q1", "q2"]
    for name in ("steer-no-closure", "steer-not-offered"):
        for shown in trays[name]["passes"]:
            assert shown["buttons"] == 2
            assert sorted(fired(shown["clicks"])) == ["withdraw:q1", "withdraw:q2"], name
            assert labels["Steer"] not in shown["widths"]


def test_c29_7_a_narrow_held_tray_cuts_its_title_not_its_controls(trays):
    """C-29.7: at 300 pt, with the longer heading of a held conversation, the
    tray's links are as wide as at 480 pt and it is as tall: the one-line heading
    is cut, not the controls."""
    narrow, wide = trays["twelve-held-narrow"], trays["twelve"]
    assert narrow["title"] == "12 messages queued; they wait until the conversation can continue"
    assert narrow["width"] == 300
    for thin, full in zip(narrow["passes"], wide["passes"]):
        assert (thin["widths"], thin["height"], thin["buttons"]) == (full["widths"], full["height"], full["buttons"])


def test_c29_7_the_tray_never_orders_a_window_front(trays, views):
    """C-29.7: hosting and clicking the tray, including Show N more and Show
    fewer, shows no window."""
    assert trays and all(tray["visible_windows"] == 0 for tray in trays.values())
    assert views["visible_windows"] == 0
