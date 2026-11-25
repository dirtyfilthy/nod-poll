"""Turn the panel's answers into a list of points with a count for each.

Two judge calls, kept apart so the counts can't be made up:

1. extract: each answer, on its own, becomes a list of self-contained claims.
2. group: every claim, labelled only with an opaque id (no model names), goes to
   one call that groups the claims making the same point.

The counts are then worked out here from the ids, never taken from the judge:
a point's count is the number of distinct panelists with a claim in its group.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from nod.messages import assistant, system, user

EXTRACT_PROMPT = """\
You split one answer into its distinct substantive points.

Rules:
- One entry per distinct claim, recommendation or finding. Split bullets that
  make two points; merge sentences that make one.
- Write each claim so it stands alone: someone who hasn't read the answer must
  understand it. Keep the specifics (names, file paths, numbers).
- Put the answer's support for the claim (cited paths, reasoning) in "support",
  briefly.
- Leave out filler, restatements of the question, and generic caveats.

Reply with JSON only, no prose:
{"points": [{"claim": "...", "support": "..."}]}"""

GROUP_PROMPT = """\
Several people answered the same question independently. Their answers were
split into claims, each with an id like "b3" (the letter is the answer, the
number the claim). You group claims that make the same point.

Rules:
- Group claims that assert the same thing, even if worded differently or at a
  different level of detail. Do not group claims that only share a topic but
  assert different things.
- Every claim id appears in exactly one group. A claim nobody else made is a
  group of one.
- For each group write "point": one or two sentences stating what its claims
  share, keeping the specifics they agree on (file paths, names).
- In "disagreements", list pairs or sets of groups that directly contradict each
  other, by their index in your "groups" list, with a short "topic".

Reply with JSON only, no prose:
{"groups": [{"point": "...", "claims": ["a1", "c2"]}],
 "disagreements": [{"topic": "...", "groups": [0, 3]}]}"""

Complete = Callable[[list], str]


class JudgeError(RuntimeError):
    pass


@dataclass
class Claim:
    id: str
    panelist: str
    claim: str
    support: str = ""


@dataclass
class Rating:
    panelist: str
    score: int  # 1 (wrong or not worth doing) to 5 (right and important)
    note: str = ""


@dataclass
class Point:
    point: str
    panelists: list[str]
    claims: list[Claim] = field(default_factory=list)
    # Filled by the rating round (rating.py), empty without it.
    ratings: list[Rating] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.panelists)

    @property
    def peer_ratings(self) -> list[Rating]:
        """Ratings from the panelists who didn't make this point."""

        return [r for r in self.ratings if r.panelist not in self.panelists]

    @property
    def scored_by_peers(self) -> bool:
        return bool(self.peer_ratings)

    @property
    def score(self) -> float | None:
        """Mean peer rating. A point every rater made has no peers, so it falls
        back to their ratings of their own point."""

        rs = self.peer_ratings or self.ratings
        return sum(r.score for r in rs) / len(rs) if rs else None


@dataclass
class Disagreement:
    topic: str
    points: list[int]  # indexes into Tally.points


@dataclass
class Tally:
    points: list[Point]
    disagreements: list[Disagreement]
    panel_size: int


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model reply, tolerating code fences and chatter."""

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise JudgeError(f"no JSON object in judge reply: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise JudgeError(f"bad JSON from judge ({exc}): {text[:200]!r}") from exc


def ask_json(complete: Complete, prompt: str, body: str, tries: int = 2) -> dict[str, Any]:
    messages = [system(prompt), user(body)]
    for attempt in range(tries):
        reply = complete(messages)
        try:
            return parse_json(reply)
        except JudgeError as exc:
            if attempt == tries - 1:
                raise
            messages += [
                assistant(reply),
                user(f"That wasn't valid JSON ({exc}). Reply with the JSON object only."),
            ]
    raise AssertionError("unreachable")


def extract(complete: Complete, question: str, panelist: str, letter: str, answer: str) -> list[Claim]:
    data = ask_json(complete, EXTRACT_PROMPT, f"Question:\n{question}\n\nAnswer:\n{answer}")
    claims = []
    for item in data.get("points") or []:
        if isinstance(item, dict) and str(item.get("claim", "")).strip():
            claims.append(
                Claim(
                    id=f"{letter}{len(claims) + 1}",
                    panelist=panelist,
                    claim=str(item["claim"]).strip(),
                    support=str(item.get("support") or "").strip(),
                )
            )
    return claims


def group(complete: Complete, question: str, claims: list[Claim]) -> dict[str, Any]:
    listing = "\n".join(f"{c.id}: {c.claim}" for c in claims)
    return ask_json(complete, GROUP_PROMPT, f"Question:\n{question}\n\nClaims:\n{listing}")


def merge(claims: list[Claim], grouping: dict[str, Any], panel_size: int) -> Tally:
    """Build the points from the judge's grouping, trusting only the ids.

    Unknown ids are dropped, a claim the judge put in two groups stays in the
    first, and a claim it left out becomes a point of its own.
    """

    by_id = {c.id: c for c in claims}
    placed: set[str] = set()
    points: list[Point] = []
    index_map: dict[int, int] = {}  # judge's group index -> our point index
    for i, g in enumerate(grouping.get("groups") or []):
        if not isinstance(g, dict):
            continue
        members = []
        for cid in g.get("claims") or []:
            cid = str(cid).strip()
            if cid in by_id and cid not in placed:
                placed.add(cid)
                members.append(by_id[cid])
        if not members:
            continue
        index_map[i] = len(points)
        text = str(g.get("point") or "").strip() or members[0].claim
        points.append(Point(point=text, panelists=_panelists(members), claims=members))

    for c in claims:
        if c.id not in placed:
            points.append(Point(point=c.claim, panelists=[c.panelist], claims=[c]))

    order = sorted(range(len(points)), key=lambda i: (-points[i].count, i))
    new_pos = {old: new for new, old in enumerate(order)}
    disagreements = []
    for d in grouping.get("disagreements") or []:
        if not isinstance(d, dict):
            continue
        idx = sorted(
            {new_pos[index_map[g]] for g in d.get("groups") or [] if isinstance(g, int) and g in index_map}
        )
        if len(idx) >= 2:
            disagreements.append(Disagreement(topic=str(d.get("topic") or "").strip(), points=idx))
    return Tally(points=[points[i] for i in order], disagreements=disagreements, panel_size=panel_size)


def _panelists(members: list[Claim]) -> list[str]:
    seen: list[str] = []
    for c in members:
        if c.panelist not in seen:
            seen.append(c.panelist)
    return seen


def tally(complete: Complete, question: str, answers: list[tuple[str, str]]) -> tuple[Tally, list[Claim]]:
    """answers: (panelist, answer text) for each panelist that answered."""

    letters = _letters(len(answers))
    with ThreadPoolExecutor(max_workers=max(1, len(answers))) as pool:
        per_answer = list(
            pool.map(
                lambda a: extract(complete, question, a[1][0], a[0], a[1][1]),
                zip(letters, answers),
            )
        )
    claims = [c for cs in per_answer for c in cs]
    grouping = group(complete, question, claims) if claims else {}
    return merge(claims, grouping, panel_size=len(answers)), claims


def _letters(n: int) -> list[str]:
    alphabet = string.ascii_lowercase
    if n <= 26:
        return list(alphabet[:n])
    return [alphabet[i // 26 - 1] + alphabet[i % 26] if i >= 26 else alphabet[i] for i in range(n)]
