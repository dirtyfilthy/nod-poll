# nod-poll

Ask a panel of models the same question, optionally about a codebase, then
group the points they make and count how many models made each one.

Each model is its own [Nod](https://github.com/dirtyfilthy/nod) one-shot run
(`nod --model <id> --no-network ... run`) through OpenRouter, using the Nod this
package is installed with. Its tools run in a Docker sandbox with **no network**
(and Nod tells the model so): the codebase is mounted read-only at `/repo`, and the
model writes its answer to `/workspace/answer.md`, which is that model's folder in
the run directory on the host. Nod's loop and your API key stay on the host.

```sh
export OPENROUTER_API_KEY=sk-or-...
uv run nod-poll -r ~/src/someproject "Where is auth enforced, and where could it be bypassed?"
uv run nod-poll -m anthropic/claude-sonnet-5.5,openai/gpt-6.2-sol "Tabs or spaces?"
echo "long question..." | uv run nod-poll -r . -
uv run nod-poll --rate -r ~/src/someproject "What modules is this missing?"   # panel also scores every point
uv run nod-poll --retally ~/.nod-poll/runs/20261121-...   # re-tally without asking the panel again
uv run nod-poll --retally ~/.nod-poll/runs/20261121-... --rate   # ...and have the panel rate the result
```

## What a run leaves behind

```
<run>/
  report.md               points sorted by how many models made them, disagreements, cost table
  report.json             the same, plus every extracted claim and which group it landed in
  question.md, run.json
  answers/<model>/answer.md   each model's own answer (its /workspace)
  answers/<model>/ratings.json   with --rate: that model's score for each point
  ratings/                    with --rate: the task each model was sent, and points.json (the ids it used)
  sessions/<model>.jsonl      Nod's transcript for that model (`nod chat --session` can resume it)
  logs/<model>.log            the nod command line and its stderr
  nod/                        the Nod config and panelist prompt generated for the run
```

Runs go to `~/.nod-poll/runs/<time>-<question>` unless you pass `-o` or set
`NOD_POLL_DIR`.

## How the counting works

1. **Ask.** Each model runs in parallel with a panelist prompt (the run's Nod soul)
   that asks for one point per bullet with `path:line` citations. A model that ends
   without writing `answer.md` (out of turns or time) gets one short `nod run
   --session` follow-up telling it to write what it has.
2. **Extract.** A judge model (`-j`, default Sonnet 5.5) splits each answer on its
   own into self-contained claims.
3. **Group.** One judge call sees every claim with only an opaque id (`b3` = answer
   b, claim 3), never a model name, and groups claims that make the same point. It
   also flags groups that contradict each other.
4. **Count.** The counts are computed from the ids, not taken from the judge: a
   point's count is the number of distinct models with a claim in its group. Two
   claims from one model in the same group count once. Claims the judge drops come
   back as points of their own, and ids it invents are ignored.

## Rating the points (`--rate`)

The count says how many models thought of a point, not whether it is any good. A
point only one model made may be the best idea in the run or a mistake, which
matters most when the question is a brainstorm. With `--rate`, after the tally
every model that answered gets the full list of points, with no model names and
no counts, in an order shuffled per model, and scores each from 1 (wrong, or not
worth doing) to 5 (right and important) with a one-line reason.

- Each model rates in its own session, so it still knows what it found in the
  codebase, and may check `/repo` again. The round is limited to 12 turns and 600 s
  per model, and its cost is added to that model's row in the report.
- A point's score is the mean rating from the models that did **not** make it.
  A point every rater made has no such ratings, so it shows their own and says so.
- The report keeps its two sections (points several models made, points one model
  made) and sorts each by score. Under a point it lists who gave what, and the
  reason from any model that gave it 1 or 2.
- A model that fails to rate is left out of the scores; the run still succeeds.
- `--retally` on its own regroups the points, so it drops earlier ratings. Add
  `--rate` to rate the new points; that needs Docker and the run's `sessions/`.

## Options

| Flag | Default |
| --- | --- |
| `-m/--models` | `$NOD_POLL_MODELS`, else Sonnet 5.5, GPT-6.2 Sol, Gemini 4 Carbon, DeepSeek V4.1 Flash, Kimi K3.1 Pro |
| `-j/--judge` | `$NOD_POLL_JUDGE`, else `anthropic/claude-sonnet-5.5` |
| `-r/--repo` | none: no codebase is mounted |
| `--max-turns` | 40 provider round-trips per model |
| `--timeout` | 1200 s per model, then SIGTERM (Nod removes its container) |
| `--effort` | each model's own reasoning effort |
| `--rate` | off: no rating round |

The generated config sets `provider_routing = {}`, so OpenRouter chooses the
upstream provider for every model (Nod's default pins `deepseek/` models to
DeepSeek's own deployment, which not every account can use). The model and turn
limit go on the command line (`--model`, `--max-turns`). Cost per model comes from
the `[usage {...}]` line `nod run` ends stderr with, compaction calls included; a
model stopped by `--timeout` prints none, so its cost is summed from its session
file instead.

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
