"""Ask one model the question by running `nod run` in a no-network sandbox.

Each panelist is its own `nod` process (the same Nod this package is installed
with), so it runs Nod's real CLI and wiring, and a stuck one can be stopped
with a timeout. Its tools run in a Docker container with no network. The
codebase, if any, is mounted read-only at /repo. The panelist's own directory on
the host is the container's /workspace, and the agent is told to write its
answer to /workspace/answer.md, so every answer lands in a known file.

All panelists in a run share one generated config (run_dir/nod/nod.toml): the
panelist prompt as the soul, no provider pin, sessions under run_dir/sessions,
one session per panelist named after it. The model and turn limit go on the
command line.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_MOUNT = "/repo"
ANSWER_FILE = "answer.md"
# Compact well before any panelist's window fills; the smallest common one is ~128k.
CONTEXT_WINDOW = 128_000
# Time a panelist gets to stop after SIGTERM (Nod removes its container on exit).
STOP_GRACE_SECONDS = 30

PANELIST_SOUL = """\
You are answering one question on your own. Other models are answering the same
question independently, and the points you make will be compared with theirs, so
say what you actually think and be specific.

Your working directory is /workspace.
{repo_note}

When you are done, write your answer as markdown to /workspace/answer.md with
cud_file. Make each distinct point its own bullet that opens with the claim in
one sentence, then gives the support for it. Put the most important points
first. Cite code as repo-relative `path:line`. Then reply with the single word
"done".
"""

REPO_NOTE = """\
The codebase the question is about is mounted read-only at /repo. Investigate it
with list_dir, read_file and run_shell (grep, find, git log, ...) before
answering, and base your claims on what you read there, not on guesses."""

WRAP_UP = """\
Stop investigating. Write your answer to /workspace/answer.md now with cud_file,
in the format asked for, using what you have found so far. Say where you are
unsure. Then reply "done"."""
WRAP_UP_TURNS = 3
WRAP_UP_TIMEOUT = 300


@dataclass
class Answer:
    model: str
    slug: str
    text: str = ""
    ok: bool = False
    # Where the text came from: "file" (answer.md), "reply" (the agent's last
    # message, when it never wrote the file) or "" when there is no answer.
    source: str = ""
    error: str = ""
    seconds: float = 0.0
    calls: int = 0
    cost: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return f"answers/{self.slug}/{ANSWER_FILE}"


def slugify(model: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", model.lower()).strip("-")


def soul_for(has_repo: bool) -> str:
    return PANELIST_SOUL.format(repo_note=REPO_NOTE if has_repo else "")


def write_config(
    run_dir: Path, *, has_repo: bool, reasoning: dict[str, Any] | None = None
) -> Path:
    """Write the run's Nod config and panelist prompt; return the config's path."""

    nod_dir = run_dir / "nod"
    nod_dir.mkdir(parents=True, exist_ok=True)
    (nod_dir / "soul.md").write_text(soul_for(has_repo), encoding="utf-8")
    path = nod_dir / "nod.toml"
    path.write_text(config_toml(run_dir, reasoning), encoding="utf-8")
    return path


def config_toml(run_dir: Path, reasoning: dict[str, Any] | None) -> str:
    s = json.dumps  # a JSON string is a valid TOML basic string
    lines = [
        "# Written by nod-poll for this run.",
        f"soul_path = {s(str(run_dir / 'nod' / 'soul.md'))}",
        f"memory_dir = {s(str(run_dir / 'sessions'))}",
        f"jobs_dir = {s(str(run_dir / 'nod' / 'jobs'))}",
        "skills_dirs = []",
        # Nod's default pins deepseek/ models to DeepSeek's own deployment, which
        # not every OpenRouter account can use. Let OpenRouter route everything.
        "provider_routing = {}",
    ]
    if reasoning:
        pairs = ", ".join(f"{k} = {s(v)}" for k, v in reasoning.items())
        lines.append(f"reasoning = {{ {pairs} }}")
    lines += ["", "[compaction]", f"context_window = {CONTEXT_WINDOW}", ""]
    return "\n".join(lines)


def nod_argv(
    model: str,
    config: Path,
    workdir: Path,
    session: str,
    instructions: Path | None = None,
    text: str | None = None,
    *,
    max_turns: int,
    repo: Path | None = None,
) -> list[str]:
    argv = [
        sys.executable, "-m", "nod", "-c", str(config), "--model", model,
        "--max-turns", str(max_turns), "--workspace", str(workdir), "--no-network",
    ]
    if repo:
        argv += ["--mount", f"{repo}:{REPO_MOUNT}:ro"]
    argv += ["run", "--session", session]
    if instructions:
        argv += ["-f", str(instructions)]
    if text:
        argv.append(text)
    return argv


def ask(
    model: str,
    question_file: Path,
    run_dir: Path,
    config: Path,
    *,
    max_turns: int,
    repo: Path | None = None,
    timeout: float = 1200,
) -> Answer:
    """Run one panelist to the end and collect its answer. Never raises."""

    slug = slugify(model)
    answer = Answer(model=model, slug=slug)
    workdir = run_dir / "answers" / slug
    workdir.mkdir(parents=True, exist_ok=True)
    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)
    started = time.monotonic()

    argv = nod_argv(model, config, workdir, slug, question_file, max_turns=max_turns, repo=repo)
    runs = [run_nod(argv, logs / f"{slug}.log", timeout)]
    if not (workdir / ANSWER_FILE).exists() and not _fatal(runs[0].error):
        # Out of turns or time, or it forgot: a few more turns to write what it has.
        argv = nod_argv(model, config, workdir, slug, text=WRAP_UP,
                        max_turns=WRAP_UP_TURNS, repo=repo)
        runs.append(run_nod(argv, logs / f"{slug}.wrap-up.log", WRAP_UP_TIMEOUT))
    answer.error = runs[-1].error or runs[0].error

    answer.seconds = round(time.monotonic() - started, 1)
    if all(r.usage is not None for r in runs):
        # Nod's own totals, which include compaction calls.
        usage = {k: sum(r.usage.get(k) or 0 for r in runs)
                 for k in ("calls", "cost", "prompt_tokens", "completion_tokens")}
    else:
        # A stopped run prints no [usage] line; count what the session recorded.
        usage = session_usage(run_dir / "sessions" / f"{slug}.jsonl")
    answer.calls, answer.cost = int(usage["calls"]), round(usage["cost"], 6)
    answer.tokens = {"prompt": int(usage["prompt_tokens"]),
                     "completion": int(usage["completion_tokens"])}
    _collect(answer, workdir / ANSWER_FILE, runs[-1].reply)
    return answer


def _fatal(error: str) -> bool:
    # A config or sandbox failure won't go away on a second try.
    return error.startswith("nod exited 2")


@dataclass
class NodRun:
    reply: str
    error: str = ""
    # From the [usage {...}] line nod run ends stderr with; None if it printed
    # none (stopped by a signal).
    usage: dict[str, Any] | None = None


def run_nod(argv: list[str], log: Path, timeout: float) -> NodRun:
    """Run one `nod run`, its stderr appended to `log`."""

    with log.open("a", encoding="utf-8") as err:
        err.write(f"$ {shlex.join(argv)}\n")
        err.flush()
        start = err.tell()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(proc)
            out, timed_out = "", True
        else:
            timed_out = False
    stderr = log.read_text(encoding="utf-8", errors="replace")[start:]
    result = NodRun(reply=out.strip(), usage=parse_usage(stderr))
    if timed_out:
        result.error = f"timed out after {timeout:.0f}s"
    elif proc.returncode != 0:
        result.error = f"nod exited {proc.returncode}: {_tail(stderr)}"
    return result


def parse_usage(stderr: str) -> dict[str, Any] | None:
    for line in reversed(stderr.splitlines()):
        if line.startswith("[usage ") and line.endswith("]"):
            try:
                return json.loads(line[len("[usage "):-1])
            except json.JSONDecodeError:
                return None
    return None


def stop(proc: subprocess.Popen) -> None:
    """SIGTERM, which Nod turns into a clean exit that removes its container;
    SIGKILL if it won't go, and then remove the container ourselves."""

    proc.terminate()
    try:
        proc.communicate(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    # DockerSandbox names its container nod-sandbox-<pid>-<random>.
    found = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=nod.sandbox",
         "--filter", f"name=nod-sandbox-{proc.pid}-"],
        capture_output=True, text=True,
    )
    ids = found.stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)


def session_usage(path: Path) -> dict[str, Any]:
    """Provider calls, cost and tokens from a Nod session file.

    Counts every assistant reply plus the empty replies Nod dropped and asked
    again for. Compaction summaries aren't stored as replies, so a compacted
    session slightly undercounts.
    """

    total = {"calls": 0, "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    if not path.exists():
        return total
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("role") != "assistant":
            continue
        meta = entry.get("meta") or {}
        for m in [meta, *(meta.get("empty_replies") or [])]:
            usage = m.get("usage")
            if usage is None:
                continue
            total["calls"] += 1
            total["cost"] += usage.get("cost") or 0.0
            total["prompt_tokens"] += usage.get("prompt_tokens") or 0
            total["completion_tokens"] += usage.get("completion_tokens") or 0
    return total


def _tail(stderr: str, n: int = 300) -> str:
    lines = [l for l in stderr.strip().splitlines()
             if not l.startswith(("$ ", "[session ", "[usage "))]
    return " ".join(lines)[-n:]


def _collect(answer: Answer, answer_file: Path, reply: str) -> None:
    """Prefer the answer file; fall back to the final reply if it's substantive."""

    text = answer_file.read_text(encoding="utf-8").strip() if answer_file.exists() else ""
    if text:
        answer.text, answer.source = text, "file"
    elif len(reply.strip()) > 40 and not reply.startswith("(stopped:"):
        answer.text, answer.source = reply.strip(), "reply"
        answer_file.write_text(answer.text + "\n", encoding="utf-8")
    answer.ok = bool(answer.text)
    if answer.ok:
        answer.error = ""
    elif not answer.error:
        answer.error = reply.strip()[:300] or "no answer written"
