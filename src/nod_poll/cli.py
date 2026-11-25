"""nod-poll: ask a panel of models one question, then tally their points."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from nod.messages import Message
from nod.provider import OpenAICompatibleProvider, ProviderError

from nod_poll import rating, report
from nod_poll.panel import ANSWER_FILE, Answer, ask, write_config
from nod_poll.tally import JudgeError, tally

DEFAULT_MODELS = [
    "anthropic/claude-sonnet-5.5",
    "openai/gpt-6.2-sol",
    "google/gemini-4-carbon",
    "deepseek/deepseek-v4.1-flash",
    "moonshotai/kimi-k3.1-pro",
]
DEFAULT_JUDGE = "anthropic/claude-sonnet-5.5"
DEFAULT_RUNS_DIR = "~/.nod-poll/runs"


class CostMeter:
    """Wraps the judge's provider and adds up what its calls cost."""

    def __init__(self, inner: OpenAICompatibleProvider) -> None:
        self.inner = inner
        self.cost = 0.0
        self._lock = threading.Lock()

    def complete(self, messages, tools=None, on_token=None) -> Message:
        reply = self.inner.complete(messages, tools, on_token)
        with self._lock:
            self.cost += (reply.meta.get("usage") or {}).get("cost") or 0.0
        return reply


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="nod-poll",
        description="Ask several models the same question through Nod (tools sandboxed, "
        "no network), then group their points and count how many models made each.",
    )
    p.add_argument("question", nargs="?", help="The question, '-' to read stdin, or omit with --retally.")
    p.add_argument("-r", "--repo", help="Codebase to ask about, mounted read-only at /repo.")
    p.add_argument(
        "-m", "--models",
        help="Comma-separated OpenRouter model ids (default: $NOD_POLL_MODELS or a built-in panel).",
    )
    p.add_argument("-j", "--judge", default=os.environ.get("NOD_POLL_JUDGE", DEFAULT_JUDGE),
                   help=f"Model that extracts and groups the points (default: {DEFAULT_JUDGE}).")
    p.add_argument("-o", "--out", help=f"Run directory (default: {DEFAULT_RUNS_DIR}/<time>-<question>).")
    p.add_argument("--max-turns", type=int, default=40, help="Agent loop limit per model (default: 40).")
    p.add_argument("--timeout", type=float, default=1200,
                   help="Seconds each model gets before it is stopped (default: 1200).")
    p.add_argument("--effort", choices=["low", "medium", "high"],
                   help="Reasoning effort sent to every panelist (default: each model's own).")
    p.add_argument("--rate", action="store_true",
                   help="After the tally, have every panelist score every point from 1 to 5.")
    p.add_argument("--retally", metavar="RUN_DIR",
                   help="Skip the panel: re-run the tally on an earlier run's answers "
                   "(with --rate, the panelists then rate the new points).")
    args = p.parse_args(argv)

    try:
        if args.retally:
            return _retally(Path(args.retally).expanduser().resolve(), args.judge, args.rate)
        return _poll(args)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _poll(args: argparse.Namespace) -> int:
    question = sys.stdin.read() if args.question == "-" else args.question
    if not question or not question.strip():
        print("error: no question given", file=sys.stderr)
        return 2
    repo = None
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        if not repo.is_dir():
            print(f"error: {repo} is not a directory", file=sys.stderr)
            return 2
    models = _models(args.models)
    run_dir = _run_dir(args.out, question)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "question.md").write_text(question.strip() + "\n", encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps(
        {"question": question, "repo": str(repo) if repo else None, "models": models,
         "max_turns": args.max_turns, "timeout": args.timeout, "effort": args.effort},
        indent=2) + "\n", encoding="utf-8")
    # Fail now on a missing key rather than once per panelist.
    OpenAICompatibleProvider(model=args.judge)

    print(f"run: {run_dir}")
    print(f"asking {len(models)} models" + (f" about {repo}" if repo else "") + "...")
    reasoning = {"effort": args.effort} if args.effort else None
    config = write_config(run_dir, has_repo=repo is not None, reasoning=reasoning)
    answers: dict[str, Answer] = {}
    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {
            pool.submit(ask, m, run_dir / "question.md", run_dir, config, repo=repo,
                        max_turns=args.max_turns, timeout=args.timeout): m
            for m in models
        }
        for f in as_completed(futures):
            a = f.result()
            answers[a.model] = a
            mark = "ok  " if a.ok else "FAIL"
            note = "" if a.ok else f"  {a.error[:100]}"
            print(f"  {mark} {a.model:40} {a.calls:3} calls  ${a.cost:.4f}  {a.seconds:5.0f}s{note}",
                  flush=True)
    ordered = [answers[m] for m in models]
    return _finish(run_dir, question, repo, args.judge, ordered, config if args.rate else None)


def _retally(run_dir: Path, judge: str, rate: bool) -> int:
    data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    answers = []
    for raw in data["answers"]:
        raw = {k: v for k, v in raw.items() if k != "path"}
        a = Answer(**raw)
        f = run_dir / "answers" / a.slug / ANSWER_FILE
        a.text = f.read_text(encoding="utf-8").strip() if f.exists() else ""
        answers.append(a)
    repo = Path(data["repo"]) if data.get("repo") else None
    config = run_dir / "nod" / "nod.toml"
    if rate and not config.exists():
        print(f"error: {config} is missing, so the panelists can't be asked to rate", file=sys.stderr)
        return 2
    print(f"re-tallying {run_dir}")
    return _finish(run_dir, data["question"], repo, judge, answers, config if rate else None)


def _finish(run_dir: Path, question: str, repo: Path | None, judge: str, answers: list[Answer],
            rate_config: Path | None = None) -> int:
    """Tally the answers and write the report. With `rate_config` (the run's Nod
    config), the panelists that answered then rate the points."""

    answered = [(a.model, a.text) for a in answers if a.ok]
    meter = CostMeter(OpenAICompatibleProvider(model=judge, provider_routing={}))
    result, claims, error = None, [], ""
    if answered:
        print(f"tallying {len(answered)} answers with {judge}...", flush=True)
        try:
            result, claims = tally(lambda msgs: meter.complete(msgs).text, question, answered)
        except (JudgeError, ProviderError) as exc:
            error = str(exc)
            print(f"tally failed: {error}", file=sys.stderr)
    raters = [a for a in answers if a.ok]
    if rate_config and result and result.points:
        if len(raters) < 2:
            print("not rating: it takes at least two models that answered")
        else:
            print(f"rating {len(result.points)} points with {len(raters)} models...", flush=True)
            result = rating.rate(result, raters, run_dir, rate_config, repo, on_done=_rated)
    path = report.write(run_dir, question, repo, judge, answers, result, claims,
                        round(meter.cost, 6), error)
    if result:
        shared = sum(1 for p in result.points if p.count > 1)
        print(f"{len(result.points)} points, {shared} made by more than one model")
    print(f"report: {path}")
    return 0 if answered and not error else 1


def _rated(a: Answer, n: int, error: str) -> None:
    mark = "ok  " if n else "FAIL"
    note = f"  {error[:100]}" if error else ""
    print(f"  {mark} {a.model:40} rated {n:3} points{note}", flush=True)


def _models(flag: str | None) -> list[str]:
    raw = flag or os.environ.get("NOD_POLL_MODELS", "")
    models = [m.strip() for m in raw.split(",") if m.strip()] or list(DEFAULT_MODELS)
    return list(dict.fromkeys(models))


def _run_dir(out: str | None, question: str) -> Path:
    if out:
        return Path(out).expanduser().resolve()
    base = Path(os.environ.get("NOD_POLL_DIR", DEFAULT_RUNS_DIR)).expanduser()
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40].rstrip("-") or "poll"
    return (base / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}").resolve()


if __name__ == "__main__":
    raise SystemExit(main())
