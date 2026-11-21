import json

import pytest

from nod_poll.tally import Claim, JudgeError, merge, parse_json, tally


def claims():
    return [
        Claim("a1", "m/one", "X leaks memory"),
        Claim("a2", "m/one", "Y is slow"),
        Claim("b1", "m/two", "X has a memory leak"),
        Claim("b2", "m/two", "X leaks via the cache too"),
        Claim("c1", "m/three", "Y is fast"),
        Claim("c2", "m/three", "Z is unused"),
    ]


def test_counts_distinct_panelists_from_ids():
    grouping = {"groups": [
        {"point": "Y is slow", "claims": ["a2"]},
        {"point": "X leaks memory", "claims": ["a1", "b1", "b2"]},
        {"point": "Y is fast", "claims": ["c1"]},
    ], "disagreements": [{"topic": "speed of Y", "groups": [0, 2]}]}
    t = merge(claims(), grouping, panel_size=3)
    # Two claims from m/two count once; most-agreed point sorts first.
    assert t.points[0].point == "X leaks memory"
    assert t.points[0].count == 2
    assert t.points[0].panelists == ["m/one", "m/two"]
    # c2 was left out by the judge and becomes its own point.
    assert any(p.point == "Z is unused" and p.count == 1 for p in t.points)
    # Disagreement indexes follow the re-sorted points.
    (d,) = t.disagreements
    assert {t.points[i].point for i in d.points} == {"Y is slow", "Y is fast"}


def test_ignores_unknown_and_duplicate_ids():
    grouping = {"groups": [
        {"point": "X", "claims": ["a1", "zz9", "b1"]},
        {"point": "X again", "claims": ["b1", "a1"]},  # all already placed -> dropped
    ]}
    t = merge(claims(), grouping, panel_size=3)
    assert [p.point for p in t.points].count("X again") == 0
    assert sum(len(p.claims) for p in t.points) == len(claims())


def test_parse_json_tolerates_fences_and_chatter():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    with pytest.raises(JudgeError):
        parse_json("no json here")


def test_tally_end_to_end_with_fake_judge():
    def complete(messages):
        body = messages[-1].content
        if "Claims:" in body:
            ids = [line.split(":")[0] for line in body.split("Claims:\n")[1].splitlines()]
            return json.dumps({"groups": [{"point": "shared", "claims": ids}]})
        return '{"points": [{"claim": "it works", "support": "a.py:1"}]}'

    t, cs = tally(complete, "does it work?", [("m/one", "yes"), ("m/two", "yep")])
    assert [c.id for c in cs] == ["a1", "b1"]
    assert len(t.points) == 1 and t.points[0].count == 2 and t.panel_size == 2


def test_judge_gets_a_retry_on_bad_json():
    replies = iter(["oops", '{"points": [{"claim": "ok"}]}', '{"groups": []}'])
    seen = []

    def complete(messages):
        seen.append(len(messages))
        return next(replies)

    t, cs = tally(complete, "q", [("m/one", "answer")])
    assert seen[:2] == [2, 4]  # second try carries the bad reply and a correction
    assert t.points[0].point == "ok"
