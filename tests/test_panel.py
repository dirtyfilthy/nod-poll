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
