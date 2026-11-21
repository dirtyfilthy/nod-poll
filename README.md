# nod-poll

Ask a panel of models the same question, optionally about a codebase, then
group the points they make and count how many models made each one.

Each model runs as its own [Nod](https://github.com/dirtyfilthy/nod) agent through OpenRouter. Its tools run in a
Docker sandbox with **no network**: the codebase is mounted read-only at `/repo`, and
the model writes its answer to `/workspace/answer.md`, which is that model's folder
in the run directory on the host. Nod's loop and your API key stay on the host.

```sh
export OPENROUTER_API_KEY=sk-or-...
uv run nod-poll -r ~/src/someproject "Where is auth enforced, and where could it be bypassed?"
uv run nod-poll -m anthropic/claude-sonnet-5.5,openai/gpt-6.2-sol "Tabs or spaces?"
echo "long question..." | uv run nod-poll -r . -
uv run nod-poll --retally ~/.nod-poll/runs/20261121-...   # re-tally without asking the panel again
```

## What a run leaves behind

```
<run>/
  report.md               points sorted by how many models made them, disagreements, cost table
  report.json             the same, plus every extracted claim and which group it landed in
  question.md, run.json
  answers/<model>/answer.md   each model's own answer (its /workspace)
  sessions/<model>/           Nod's transcript for that model
```

Runs go to `~/.nod-poll/runs/<time>-<question>` unless you pass `-o` or set
`NOD_POLL_DIR`.

## How the counting works

1. **Ask.** Each model runs in parallel with a panelist prompt that asks for one
   point per bullet with `path:line` citations.
2. **Extract.** A judge model (`-j`, default Sonnet 5.5) splits each answer on its
   own into self-contained claims.
3. **Group.** One judge call sees every claim with only an opaque id (`b3` = answer
   b, claim 3), never a model name, and groups claims that make the same point. It
   also flags groups that contradict each other.
4. **Count.** The counts are computed from the ids, not taken from the judge: a
   point's count is the number of distinct models with a claim in its group. Two
   claims from one model in the same group count once. Claims the judge drops come
   back as points of their own, and ids it invents are ignored.

## Options

| Flag | Default |
| --- | --- |
| `-m/--models` | `$NOD_POLL_MODELS`, else Sonnet 5.5, GPT-6.2 Sol, Gemini 4 Carbon, DeepSeek V4.1 Flash, Kimi K3.1 Pro |
| `-j/--judge` | `$NOD_POLL_JUDGE`, else `anthropic/claude-sonnet-5.5` |
| `-r/--repo` | none: no codebase is mounted |
| `--max-turns` | 40 provider round-trips per model |
| `--effort` | each model's own reasoning effort |

OpenRouter chooses the upstream provider for every model (no routing pin), unlike
Nod's own config.

## Install

```sh
uv tool install git+ssh://git@github.com/dirtyfilthy/nod-poll.git
```

Nod is pulled from its private GitHub repo over SSH, pinned in `uv.lock`
(`uv lock --upgrade-package nod` moves it to the latest `main`). To try changes
to a local Nod checkout, run `uv run --with-editable ../nod nod-poll ...`.

## Tests

```sh
uv run --extra dev pytest -q
```

They use a fake judge, so they need neither Docker nor an API key.
