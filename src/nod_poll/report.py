"""Write the poll's results: report.md to read, report.json to keep."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from nod_poll.panel import Answer
from nod_poll.tally import Claim, Tally


def short(model: str) -> str:
    return model.split("/", 1)[-1]


def render(
    question: str,
    repo: Path | None,
    judge: str,
    answers: list[Answer],
    tally: Tally | None,
    judge_cost: float,
) -> str:
    out = [f"# Poll\n\n> {question.strip()}\n"]
    if repo:
        out.append(f"Codebase: `{repo}`  ")
    answered = [a for a in answers if a.ok]
    total = sum(a.cost for a in answers) + judge_cost
    out.append(
        f"Panel: {len(answered)} of {len(answers)} answered · judge `{short(judge)}` · "
        f"total cost ${total:.4f}\n"
    )

    if tally and tally.points:
        n = tally.panel_size
        shared = [p for p in tally.points if p.count > 1]
        lone = [p for p in tally.points if p.count == 1]
        num = 0
        if shared:
            out.append("## Points more than one model made\n")
            for p in shared:
                num += 1
                out.append(_point(num, p, n))
        if lone:
            out.append("## Points only one model made\n")
            for p in lone:
                num += 1
                out.append(_point(num, p, n))
        if tally.disagreements:
            out.append("## Disagreements\n")
            for d in tally.disagreements:
                refs = " vs ".join(f"#{i + 1}" for i in d.points)
                out.append(f"- {d.topic or 'Conflicting points'}: {refs}")
            out.append("")
    elif answered:
        out.append("_No points were extracted; read the answers below._\n")

    out.append("## Panel\n")
    out.append("| Model | Answer | Calls | Cost | Time |")
    out.append("| --- | --- | ---: | ---: | ---: |")
    for a in answers:
        if a.ok:
            status = f"[answer]({a.path})" + (" (from reply)" if a.source == "reply" else "")
        else:
            status = "failed: " + a.error.replace("|", "\\|").replace("\n", " ")[:120]
        out.append(f"| `{a.model}` | {status} | {a.calls} | ${a.cost:.4f} | {a.seconds:.0f}s |")
    out.append(f"| judge `{judge}` | | | ${judge_cost:.4f} | |")
    return "\n".join(out) + "\n"


def _point(num: int, p, n: int) -> str:
    who = ", ".join(short(m) for m in p.panelists)
    return f"{num}. **[{p.count}/{n}]** {p.point}  \n   _{who}_\n"


def write(
    run_dir: Path,
    question: str,
    repo: Path | None,
    judge: str,
    answers: list[Answer],
    tally: Tally | None,
    claims: list[Claim],
    judge_cost: float,
    judge_error: str = "",
) -> Path:
    md = render(question, repo, judge, answers, tally, judge_cost)
    if judge_error:
        md += f"\n**Tally failed:** {judge_error}\n"
    (run_dir / "report.md").write_text(md, encoding="utf-8")
    data = {
        "question": question,
        "repo": str(repo) if repo else None,
        "judge": judge,
        "judge_cost": judge_cost,
        "judge_error": judge_error or None,
        "answers": [{k: v for k, v in asdict(a).items() if k != "text"} | {"path": a.path} for a in answers],
        "points": [
            {
                "point": p.point,
                "count": p.count,
                "panelists": p.panelists,
                "claims": [asdict(c) for c in p.claims],
            }
            for p in (tally.points if tally else [])
        ],
        "disagreements": [asdict(d) for d in (tally.disagreements if tally else [])],
        "claims": [asdict(c) for c in claims],
    }
    (run_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return run_dir / "report.md"
