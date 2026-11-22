from nod_poll.panel import Answer, _collect, slugify, soul_for


def test_slugify():
    assert slugify("anthropic/claude-sonnet-5.5") == "anthropic-claude-sonnet-5.5"


def test_collect_prefers_answer_file(tmp_path):
    f = tmp_path / "answer.md"
    f.write_text("- the answer\n")
    a = Answer(model="m", slug="m")
    _collect(a, f, "done")
    assert a.ok and a.source == "file" and a.text == "- the answer"


def test_collect_falls_back_to_a_substantive_reply(tmp_path):
    f = tmp_path / "answer.md"
    a = Answer(model="m", slug="m")
    _collect(a, f, "Here is my full answer, which I forgot to write to the file.")
    assert a.ok and a.source == "reply" and f.exists()


def test_collect_fails_on_done_without_file(tmp_path):
    a = Answer(model="m", slug="m")
    _collect(a, tmp_path / "answer.md", "done")
    assert not a.ok and a.error == "done"


def test_soul_mentions_repo_only_when_given():
    assert "/repo" in soul_for(True) and "/repo" not in soul_for(False)



import json
import sys

from nod import config as nod_config

from nod_poll.panel import nod_argv, parse_usage, run_nod, session_usage, write_config


def test_config_loads_in_nod_with_no_provider_pin(tmp_path):
    path = write_config(tmp_path, has_repo=True, reasoning={"effort": "high"})
    cfg = nod_config.load(path)
    assert cfg.provider_routing == {}  # deepseek/ models would be pinned to DeepSeek's own host
    assert cfg.reasoning == {"effort": "high"}
    assert cfg.skills_dirs == []
    assert cfg.context_window == 128_000
    assert "/repo" in cfg.soul()
    assert cfg.resolve(cfg.memory_dir) == tmp_path / "sessions"


def test_nod_argv_puts_global_flags_before_run(tmp_path):
    argv = nod_argv("x/y", tmp_path / "c.toml", tmp_path / "w", "x-y",
                    tmp_path / "q.md", max_turns=7, repo=tmp_path / "repo")
    run = argv.index("run")
    for flag in ("-c", "--model", "--max-turns", "--workspace", "--no-network", "--mount"):
        assert argv.index(flag) < run
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--mount") + 1] == f"{tmp_path / 'repo'}:/repo:ro"
    assert argv[run:] == ["run", "--session", "x-y", "-f", str(tmp_path / "q.md")]


def test_session_usage_counts_replies_and_dropped_empties(tmp_path):
    f = tmp_path / "s.jsonl"
    rows = [
        {"role": "user", "content": "q", "meta": {}},
        {"role": "assistant", "meta": {"usage": {"cost": 0.01, "prompt_tokens": 100,
                                                 "completion_tokens": 10},
                                       "empty_replies": [{"usage": {"cost": 0.002}}]}},
        {"role": "tool", "content": "r", "meta": {}},
        {"role": "assistant", "meta": {"usage": {"cost": 0.03, "prompt_tokens": 200}}},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    u = session_usage(f)
    assert u["calls"] == 3 and round(u["cost"], 6) == 0.042
    assert u["prompt_tokens"] == 300 and u["completion_tokens"] == 10
    assert session_usage(tmp_path / "missing.jsonl")["calls"] == 0


def test_parse_usage_takes_the_last_usage_line():
    stderr = '[usage {"calls": 1}]\nnoise\n[usage {"calls": 4, "cost": 0.5}]\n[session s]\n'
    assert parse_usage(stderr) == {"calls": 4, "cost": 0.5}
    assert parse_usage("[session s]\n") is None
    assert parse_usage("[usage {not json}]") is None


FAKE_NOD = """
import json, sys
print("the reply")
print('[usage ' + json.dumps({"calls": 2, "cost": 0.01}) + ']', file=sys.stderr)
print("[session s]", file=sys.stderr)
sys.exit(int(sys.argv[1]))
"""


def test_run_nod_reads_reply_and_usage(tmp_path):
    log = tmp_path / "log"
    log.write_text('$ earlier run\n[usage {"calls": 99}]\n')  # an earlier run's lines are ignored
    r = run_nod([sys.executable, "-c", FAKE_NOD, "0"], log, timeout=30)
    assert r.reply == "the reply" and r.error == "" and r.usage == {"calls": 2, "cost": 0.01}


def test_run_nod_reports_exit_code_without_footer_noise(tmp_path):
    r = run_nod([sys.executable, "-c", "import sys; sys.exit('[error] boom')"], tmp_path / "log", 30)
    assert r.error == "nod exited 1: [error] boom" and r.usage is None


def test_run_nod_times_out_without_usage(tmp_path):
    r = run_nod([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path / "log", timeout=1)
    assert r.reply == "" and r.error == "timed out after 1s" and r.usage is None
