"""Second round: every panelist scores the pooled points.

The count says how many models thought of a point, not whether it is any good:
a point one model made may be the best idea in the run or a mistake. So each
panelist that answered gets the full list of points, with no model names and no
counts, and scores each from 1 to 5. A point's score is the mean from the
panelists who did not make it.

Each panelist rates in its own session (`nod run --session`, as the wrap-up
does), so it still knows what it found in the codebase. It writes its scores to
/workspace/ratings.json, next to its answer.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from nod_poll.panel import REPO_MOUNT, Answer, nod_argv, run_nod, session_usage
from nod_poll.tally import Disagreement, JudgeError, Point, Rating, Tally, parse_json

RATINGS_FILE = "ratings.json"
RATE_TURNS = 12
RATE_TIMEOUT = 600

RATE_PROMPT = """\
A second task, separate from your answer. Every panelist's answer to the
question was split into points, and points saying the same thing were merged.
All of them are listed below in random order. Some are yours.

Rate each point from 1 to 5:
5 = right and important (for a proposal: clearly worth doing)
3 = plausible but minor, or you can't tell
1 = wrong, or not worth doing

Judge each point on its merits, not on whether you made it.{repo_note}

Write your ratings to /workspace/ratings.json with cud_file, as JSON only:
{{"ratings": [{{"id": 1, "score": 4, "note": "one short sentence saying why"}}]}}
Include every id. Then reply with the single word "done".

Points:
{points}
"""

REPO_NOTE = f"""
You may check {REPO_MOUNT} for a point you doubt, but keep it brief."""

Scores = dict[int, tuple[int, str]]  # point index -> (score, note)


def prompt(points: list[Point], seed: str, has_repo: bool) -> str:
    """The rating task for one panelist. Ids are the points' positions (from 1);
    the listing order is shuffled per panelist so position doesn't favour the
    same points for everyone."""

    order = list(range(len(points)))
    random.Random(seed).shuffle(order)
    listing = "\n".join(f"[{i + 1}] {' '.join(points[i].point.split())}" for i in order)
    return RATE_PROMPT.format(repo_note=REPO_NOTE if has_repo else "", points=listing)


def parse(text: str, n: int) -> Scores:
    """The usable ratings in a panelist's JSON: known ids, scores from 1 to 5.
    Anything else is dropped; the first rating of an id wins."""

    try:
        data = parse_json(text)
    except JudgeError:
        return {}
    scores: Scores = {}
    for item in data.get("ratings") or []:
        if not isinstance(item, dict):
            continue
        m = re.search(r"\d+", str(item.get("id", "")))
        raw = item.get("score")
        if not m or isinstance(raw, bool):
            continue
        try:
            score = round(float(raw))
        except (TypeError, ValueError):
            continue
        i = int(m.group()) - 1
        if 0 <= i < n and 1 <= score <= 5 and i not in scores:
            scores[i] = (score, str(item.get("note") or "").strip())
    return scores


def rate_one(
    answer: Answer, points: list[Point], run_dir: Path, config: Path, repo: Path | None
) -> tuple[Scores, str]:
    """Run one panelist's rating round. Returns its scores and an error, if any,
    and adds what the round cost to `answer`."""

    workdir = run_dir / "answers" / answer.slug
    ratings_file = workdir / RATINGS_FILE
    ratings_file.unlink(missing_ok=True)  # a re-run must not read the last round's file
    task = run_dir / "ratings" / f"{answer.slug}.md"
    task.write_text(prompt(points, answer.slug, repo is not None), encoding="utf-8")
    session = run_dir / "sessions" / f"{answer.slug}.jsonl"
    before = session_usage(session)

    started = time.monotonic()
    argv = nod_argv(answer.model, config, workdir, answer.slug, task,
                    max_turns=RATE_TURNS, repo=repo)
    run = run_nod(argv, run_dir / "logs" / f"{answer.slug}.rate.log", RATE_TIMEOUT)
    answer.seconds = round(answer.seconds + time.monotonic() - started, 1)

    usage = run.usage
    if usage is None:
        # Stopped by the timeout: count what the round added to the session.
        after = session_usage(session)
        usage = {k: after[k] - before[k] for k in after}
    answer.calls += int(usage.get("calls") or 0)
    answer.cost = round(answer.cost + (usage.get("cost") or 0.0), 6)
    for key, name in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
        answer.tokens[key] = answer.tokens.get(key, 0) + int(usage.get(name) or 0)

    text = ratings_file.read_text(encoding="utf-8") if ratings_file.exists() else run.reply
    scores = parse(text, len(points))
    if scores and not ratings_file.exists():
        ratings_file.write_text(text.strip() + "\n", encoding="utf-8")
    return scores, "" if scores else (run.error or "no ratings written")


def rate(
    tally: Tally,
    answers: list[Answer],
    run_dir: Path,
    config: Path,
    repo: Path | None,
    on_done: Callable[[Answer, int, str], None] | None = None,
) -> Tally:
    """Have every panelist in `answers` rate the points, in parallel, and return
    the tally with the ratings attached and the points re-sorted by score."""

    (run_dir / "ratings").mkdir(exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    # The ids the panelists were shown; the report renumbers once it sorts by score.
    (run_dir / "ratings" / "points.json").write_text(
        json.dumps([{"id": i + 1, "point": p.point} for i, p in enumerate(tally.points)], indent=2)
        + "\n", encoding="utf-8")
    results: dict[str, Scores] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(answers))) as pool:
        futures = {pool.submit(rate_one, a, tally.points, run_dir, config, repo): a for a in answers}
        for f in as_completed(futures):
            a = futures[f]
            scores, error = f.result()
            results[a.model] = scores
            if on_done:
                on_done(a, len(scores), error)
    for p in tally.points:
        p.ratings = []
    for a in answers:  # panel order, so each point's ratings come out in the same order
        for i, (score, note) in sorted(results[a.model].items()):
            tally.points[i].ratings.append(Rating(panelist=a.model, score=score, note=note))
    return rank(tally)


def rank(tally: Tally) -> Tally:
    """Sort by score within the shared points and within the lone ones, best
    first, unrated last. Disagreements follow their points."""

    def key(i: int) -> tuple:
        p = tally.points[i]
        return (p.count == 1, p.score is None, -(p.score or 0), -p.count, i)

    order = sorted(range(len(tally.points)), key=key)
    new_pos = {old: new for new, old in enumerate(order)}
    return Tally(
        points=[tally.points[i] for i in order],
        disagreements=[Disagreement(topic=d.topic, points=sorted(new_pos[i] for i in d.points))
                       for d in tally.disagreements],
        panel_size=tally.panel_size,
    )
