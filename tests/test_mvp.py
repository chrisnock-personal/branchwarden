from collections import Counter
from datetime import datetime, timezone

import mvp


def test_rank_highest_first_ties_by_name_and_top_five():
    c = Counter({"b": 3, "a": 3, "c": 9, "d": 1, "e": 2, "f": 2, "z": 0})
    assert mvp.rank(c) == [("c", 9), ("a", 3), ("b", 3), ("e", 2), ("f", 2)]


def test_window():
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    start, end = mvp.window("this", now)
    assert (end - start).days == 7 and end == now
    pstart, pend = mvp.window("prev", now)
    assert pend == start


def test_bots_are_not_people():
    assert not mvp._human("renovate[bot]") and not mvp._human(None) and mvp._human("dana")


def test_post_message():
    result = {"start": datetime(2026, 11, 3, tzinfo=timezone.utc), "end": datetime(2026, 11, 10, tzinfo=timezone.utc),
              "boards": [{"title": "Most reviews", "unit": ("review", "reviews"), "top": ("sasha", 14),
                          "rest": [{"login": "morgan", "v": 11}, {"login": "jules", "v": 9}, {"login": "x", "v": 1}]},
                         {"title": "Most merges", "unit": ("merge", "merges"), "top": None, "rest": []}]}
    msg = mvp.post_message(result, "https://bw/mvp")
    assert msg["heading"] == "This week's MVPs (03 Nov to 09 Nov)"
    assert len(msg["sections"]) == 1
    assert msg["sections"][0]["lines"] == [{"text": "@{sasha}: 14 reviews"}, {"text": "morgan 11 · jules 9"}]
    assert mvp.post_message({**result, "boards": [result["boards"][1]]}, None) is None


def test_amount_singular():
    assert mvp.amount(1, ("branch deleted", "branches deleted")) == "1 branch deleted"
    assert mvp.amount(3, ("review", "reviews")) == "3 reviews"
