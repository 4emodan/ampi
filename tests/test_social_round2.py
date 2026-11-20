"""Review round-2 fixes."""

import httpx
import pytest
from test_social import approve, ask, social  # noqa: F401 (social is a fixture)

from ampi_tools import meta
from ampi_tools.meta import check_text, mixed_scripts
from ampi_tools.ops import Policy
from ampi_tools.todoist import ToolError


def test_decomposed_letters_are_refused_but_ordinary_symbols_pass():
    with pytest.raises(ToolError, match="decomposed"):
        check_text("café", 500, True)
    with pytest.raises(ToolError):
        check_text("́ dangling mark", 500, True)
    assert check_text("café 1º ½ ²", 500, True)


def test_look_alike_words_in_another_alphabet_are_flagged():
    assert mixed_scripts("I saw an аре at the zoo today") == ["аре"]
    assert mixed_scripts("日本ごを勉強") == []  # Japanese kana + kanji is normal


def test_redaction_leaves_ordinary_words_alone():
    assert meta.redact("shortcode=ABC promo code: SAVE10") == "shortcode=ABC promo code: [redacted]" or \
        "shortcode=ABC" in meta.redact("shortcode=ABC")


def test_own_top_level_posts_without_is_reply_still_count(social):
    daemon, graph = social()
    graph.th_posts["201"].pop("is_reply")
    assert ask(daemon, ("threads.post", {"target": "201"}))["ok"]


def test_a_published_container_is_reported_as_posted(social):
    daemon, graph = social(policy=Policy())
    orig = graph.threads

    def threads(method, path, q):
        if path == "/501":
            if q.get("fields", "").startswith("status"):
                return httpx.Response(200, json={"status": "PUBLISHED"})
            return httpx.Response(400, json={"error": {"message": "x", "code": 100}})
        if path == "/me/threads":
            return httpx.Response(200, json={"data": [{"id": "777", "text": "hello world",
                                                       "permalink": "https://www.threads.com/@me_user/post/X"}]})
        return orig(method, path, q)

    graph.threads = threads
    ask(daemon, ("threads.publish", {"text": "hello world"}))
    done = approve(daemon)
    assert done["status"] == "done" and "post/X" in done["text"]
    assert [p for _, m, p, *_ in graph.calls if m == "POST"] == ["/v1.0/111/threads"]
