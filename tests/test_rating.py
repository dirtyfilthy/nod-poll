import json
import re

from nod_poll import rating, report
from nod_poll.panel import Answer, NodRun
from nod_poll.tally import Disagreement, Point, Rating, Tally


def tally():
    return Tally(
        points=[
            Point("everyone's idea", ["m/one", "m/two"]),
            Point("shared and better", ["m/one", "m/two"]),
            Point("lone and weak", ["m/one"]),
            Point("lone and strong", ["m/two"]),
            Point("lone and unrated", ["m/three"]),
        ],
        disagreements=[Disagreement("strength", [2, 3])],
        panel_size=3,
    )


def test_score_is_the_mean_from_panelists_who_did_not_make_the_point():
    p = Point("x", ["m/one"], ratings=[Rating("m/one", 5), Rating("m/two", 2), Rating("m/three", 3)])
    assert p.score == 2.5 and p.scored_by_peers
    # Everyone who rated it also made it: their own ratings are all there is.
    own = Point("x", ["m/one", "m/two"], ratings=[Rating("m/one", 5), Rating("m/two", 4)])
    assert own.score == 4.5 and not own.scored_by_peers
    assert Point("x", ["m/one"]).score is None


def test_prompt_lists_every_point_once_with_no_names_or_counts():
    t = tally()
    text = rating.prompt(t.points, "m-one", has_repo=False)
    ids = [int(i) for i in re.findall(r"^\[(\d+)\] ", text, re.M)]
    assert sorted(ids) == [1, 2, 3, 4, 5]
    assert "[3] lone and weak" in text
    assert "m/one" not in text and "/repo" not in text
    assert "/repo" in rating.prompt(t.points, "m-one", has_repo=True)
    # Same panelist, same order; the order is shuffled, not the report's.
    assert text == rating.prompt(t.points, "m-one", has_repo=False)
    orders = {tuple(re.findall(r"^\[(\d+)\] ", rating.prompt(t.points, s, False), re.M))
              for s in ("a", "b", "c", "d", "e", "f")}
    assert len(orders) > 1


def test_parse_keeps_only_known_ids_and_scores_in_range():
    text = "```json\n" + json.dumps({"ratings": [
        {"id": 1, "score": 4, "note": " fine "},
        {"id": "[2]", "score": "5"},
        {"id": 3, "score": 9},        # out of range
        {"id": 99, "score": 3},       # no such point
        {"id": 1, "score": 1},        # second rating of the same id
        {"id": 4, "score": True},
        {"id": 5, "score": "high"},
        "junk",
    ]}) + "\n```"
    assert rating.parse(text, 5) == {0: (4, "fine"), 1: (5, "")}
    assert rating.parse("done", 5) == {}


def test_rank_sorts_each_section_by_score_and_moves_disagreements():
    t = tally()
    for i, score in enumerate([3, 5, 1, 4]):
        t.points[i].ratings = [Rating("m/three", score)]
    r = rating.rank(t)
    assert [p.point for p in r.points] == [
        "shared and better", "everyone's idea", "lone and strong", "lone and weak", "lone and unrated"]
    (d,) = r.disagreements
    assert {r.points[i].point for i in d.points} == {"lone and weak", "lone and strong"}


def fake_nod(replies):
    """A run_nod that writes ratings.json as the panelist named in the argv would."""

    def run(argv, log, timeout):
        workdir = argv[argv.index("--workspace") + 1]
        model = argv[argv.index("--model") + 1]
        assert argv[argv.index("--max-turns") + 1] == str(rating.RATE_TURNS)
        reply = replies[model]
        if isinstance(reply, NodRun):
            return reply
        (rating.Path(workdir) / rating.RATINGS_FILE).write_text(json.dumps({"ratings": reply}))
        return NodRun(reply="done", usage={"calls": 2, "cost": 0.01, "prompt_tokens": 100,
                                           "completion_tokens": 10})

    return run


def answers(tmp_path):
    out = []
    for m in ("m/one", "m/two", "m/three"):
        a = Answer(model=m, slug=m.replace("/", "-"), ok=True, calls=5, cost=0.1,
                   tokens={"prompt": 1000, "completion": 50})
        (tmp_path / "answers" / a.slug).mkdir(parents=True)
        out.append(a)
    return out


def test_rate_attaches_ratings_adds_cost_and_reports_who_failed(tmp_path, monkeypatch):
    panel = answers(tmp_path)
    stale = tmp_path / "answers" / "m-three" / rating.RATINGS_FILE
    stale.write_text(json.dumps({"ratings": [{"id": 1, "score": 5}]}))
    monkeypatch.setattr(rating, "run_nod", fake_nod({
        "m/one": [{"id": i, "score": 2, "note": "meh"} for i in range(1, 5)],
        "m/two": [{"id": 3, "score": 1, "note": "already exists"}, {"id": 4, "score": 5}],
        "m/three": NodRun(reply="", error="timed out after 600s"),
    }))
    done = {}
    t = rating.rate(tally(), panel, tmp_path, tmp_path / "nod.toml", None,
                    on_done=lambda a, n, err: done.update({a.model: (n, err)}))

    assert done == {"m/one": (4, ""), "m/two": (2, ""), "m/three": (0, "timed out after 600s")}
    assert not stale.exists()  # the earlier round's file was not read as this round's
    by_point = {p.point: p for p in t.points}
    weak = by_point["lone and weak"]  # made by m/one, so only m/two's 1 counts
    assert [(r.panelist, r.score) for r in weak.ratings] == [("m/one", 2), ("m/two", 1)]
    assert weak.score == 1
    assert by_point["lone and strong"].score == 2  # made by m/two; m/one gave it 2
    assert [p.point for p in t.points][-2:] == ["lone and weak", "lone and unrated"]
    one, two, three = panel
    assert (one.calls, one.cost, one.tokens) == (7, 0.11, {"prompt": 1100, "completion": 60})
    assert (three.calls, three.cost) == (5, 0.1)  # no usage line and nothing new in its session
    assert "[1] everyone's idea" in (tmp_path / "ratings" / "m-one.md").read_text()
    assert json.loads((tmp_path / "ratings" / "points.json").read_text())[2] == {
        "id": 3, "point": "lone and weak"}

    md = report.render("q?", None, "j/judge", panel, t, 0.0)
    assert "Rated by 2 of 3 models" in md
    assert "**[1/3 · 1.0]** lone and weak" in md
    assert "   - two (1): already exists" in md
    assert "**[1/3]** lone and unrated" in md
    data = json.loads((report.write(tmp_path, "q?", None, "j/judge", panel, t, [], 0.0)
                       .with_name("report.json")).read_text())
    scored = {p["point"]: p for p in data["points"]}
    assert scored["lone and weak"]["score"] == 1.0
    assert scored["lone and weak"]["ratings"][1] == {
        "panelist": "m/two", "score": 1, "note": "already exists"}
    assert scored["lone and unrated"]["score"] is None


def test_rate_reads_ratings_from_the_reply_when_no_file_was_written(tmp_path, monkeypatch):
    panel = answers(tmp_path)[:1]
    reply = json.dumps({"ratings": [{"id": 2, "score": 3}]})
    monkeypatch.setattr(rating, "run_nod", fake_nod({"m/one": NodRun(reply=reply)}))
    scores, error = rating.rate_one(panel[0], tally().points, _dirs(tmp_path), tmp_path / "c", None)
    assert scores == {1: (3, "")} and error == ""
    assert (tmp_path / "answers" / "m-one" / rating.RATINGS_FILE).exists()


def _dirs(run_dir):
    (run_dir / "ratings").mkdir(exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    return run_dir
