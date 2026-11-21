"""Ask one model the question: a Nod agent whose tools run in a sandbox.

Each panelist gets its own Docker container with no network. The codebase, if
any, is mounted read-only at /repo. The panelist's own directory on the host is
the container's /workspace, and the agent is told to write its answer to
/workspace/answer.md, so every answer lands in a known file on the host.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nod.agent import Agent
from nod.compaction import CompactionConfig
from nod.memory import JSONFileMemory
from nod.messages import Message
from nod.provider import OpenAICompatibleProvider
from nod.sandbox import DockerSandbox
from nod.tools import ToolRegistry

REPO_MOUNT = "/repo"
ANSWER_FILE = "answer.md"
SESSION = "poll"
# Compact well before any panelist's window fills; the smallest common one is ~128k.
COMPACTION = CompactionConfig(enabled=True, context_window=128_000)

PANELIST_SOUL = """\
You are answering one question on your own. Other models are answering the same
question independently, and the points you make will be compared with theirs, so
say what you actually think and be specific.

Your tools run in a sandbox with no network. Your working directory is /workspace.
{repo_note}

When you are done, write your answer as markdown to /workspace/answer.md with
cud_file. Make each distinct point its own bullet that opens with the claim in
one sentence, then gives the support for it. Put the most important points
first. Cite code as repo-relative `path:line`. Then reply with the single word
"done".
"""

WRAP_UP = """\
Stop investigating. Write your answer to /workspace/answer.md now with cud_file,
in the format asked for, using what you have found so far. Say where you are
unsure. Then reply "done"."""
WRAP_UP_TURNS = 3

REPO_NOTE = """\
The codebase the question is about is mounted read-only at /repo. Investigate it
with list_dir, read_file and run_shell (grep, find, git log, ...) before
answering, and base your claims on what you read there, not on guesses."""


class CostMeter:
    """Wraps a provider and adds up what its calls cost."""

    def __init__(self, inner: OpenAICompatibleProvider) -> None:
        self.inner = inner
        self.calls = 0
        self.cost = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._lock = threading.Lock()

    def complete(self, messages, tools=None, on_token=None) -> Message:
        reply = self.inner.complete(messages, tools, on_token)
        usage = reply.meta.get("usage") or {}
        with self._lock:
            self.calls += 1
            self.cost += usage.get("cost") or 0.0
            self.prompt_tokens += usage.get("prompt_tokens") or 0
            self.completion_tokens += usage.get("completion_tokens") or 0
        return reply


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


def ask(
    model: str,
    question: str,
    run_dir: Path,
    *,
    repo: Path | None = None,
    max_turns: int = 40,
    reasoning: dict[str, Any] | None = None,
) -> Answer:
    """Run one panelist to the end and collect its answer. Never raises."""

    slug = slugify(model)
    answer = Answer(model=model, slug=slug)
    workdir = run_dir / "answers" / slug
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    meter: CostMeter | None = None
    sandbox: DockerSandbox | None = None
    reply = ""
    try:
        meter = CostMeter(
            OpenAICompatibleProvider(model=model, provider_routing={}, reasoning=reasoning or {})
        )
        sandbox = DockerSandbox(
            workspace=workdir,
            mounts=[f"{repo}:{REPO_MOUNT}:ro"] if repo else [],
            network=False,
        )
        # http_get is no use without a network; leave it out rather than let
        # the model spend turns finding that out.
        tools = [t for t in sandbox.tools() if t.name != "http_get"]
        agent = Agent(
            provider=meter,
            registry=ToolRegistry(tools),
            memory=JSONFileMemory(run_dir / "sessions" / slug),
            soul=soul_for(repo is not None),
            max_turns=max_turns,
            compaction=COMPACTION,
        )
        sandbox.start()
        reply = agent.send(question, session_id=SESSION)
        if not (workdir / ANSWER_FILE).exists():
            # Out of turns (or it forgot): give it a few more to write what it has.
            agent.max_turns = WRAP_UP_TURNS
            reply = agent.send(WRAP_UP, session_id=SESSION)
    except Exception as exc:  # noqa: BLE001 - one panelist failing shouldn't sink the poll
        answer.error = f"{type(exc).__name__}: {exc}"
    finally:
        if sandbox is not None:
            sandbox.stop()
        answer.seconds = round(time.monotonic() - started, 1)
        if meter is not None:
            answer.calls = meter.calls
            answer.cost = round(meter.cost, 6)
            answer.tokens = {"prompt": meter.prompt_tokens, "completion": meter.completion_tokens}

    _collect(answer, workdir / ANSWER_FILE, reply)
    return answer


def _collect(answer: Answer, answer_file: Path, reply: str) -> None:
    """Prefer the answer file; fall back to the final reply if it's substantive."""

    text = answer_file.read_text(encoding="utf-8").strip() if answer_file.exists() else ""
    if text:
        answer.text, answer.source = text, "file"
    elif len(reply.strip()) > 40 and not reply.startswith("(stopped:"):
        answer.text, answer.source = reply.strip(), "reply"
        answer_file.write_text(answer.text + "\n", encoding="utf-8")
    answer.ok = bool(answer.text)
    if not answer.ok and not answer.error:
        answer.error = reply.strip()[:300] or "no answer written"
