"""Review round-1 fixes: text that could hide data, exact approval text, unknown owners, safe logins, publishing."""

import httpx
import pytest
from test_social import THREADS, approve, ask, make_auth, run, social  # noqa: F401 (social is a fixture)

from ampi_tools import meta
from ampi_tools.meta import MetaAuth, check_text, text_extras
from ampi_tools.ops import Policy
from ampi_tools.todoist import ToolError


@pytest.mark.parametrize("text", [
    "a b", "two  spaces", "trailing ", " leading", "a\n\n\nb", "\U0001d406reat", "ＡBC", "x⠀y",
    "obj￼", "thin space", "\nstart", "end\n",
])
def test_text_that_could_hide_data_is_refused(text):
    with pytest.raises(ToolError):
        check_text(text, 500, True)


@pytest.mark.parametrize("text", [
    "Καλημέρα, κόσμε! 1️⃣",
    "flag \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f ok",
    "Line one\n\nLine two … ™ \U0001f469‍\U0001f4bb",
])
def test_ordinary_text_is_shown_exactly_as_posted(text):
    assert check_text(text, 500, True) == text
    shown = meta.exact_block(text)
    assert "\n".join(line[2:] for line in shown.split("\n")) == text


def test_instagram_counts_characters_not_bytes():
    check_text("\U0001f389" * 600, 2200, False)


def test_mixed_alphabets_are_flagged():
    assert any("mixed alphabets" in e for e in text_extras("Greаt talk"))
    assert not any("mixed alphabets" in e for e in text_extras("Καλή μέρα"))
    assert any("mixed alphabets" in e for e in text_extras("Καλή talk και μέρα"))


def test_an_unknown_owner_never_counts_as_the_owner(tmp_path):
    auth = make_auth(THREADS, tmp_path, httpx.AsyncClient(), [], ("", ""))
    with pytest.raises(ToolError, match="say which account"):
        auth.user


def test_logins_are_saved_only_once_the_account_is_known(tmp_path):
    def handler(request):
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "SHORT_TOKEN_123456"})
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "LONG_TOKEN_1234567", "expires_in": 5184000})
        return httpx.Response(200, json={"id": "", "username": ""})  # /me without an account

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = MetaAuth(THREADS, "appid", "APP_SECRET_abcdefgh", tmp_path / "t.json", client)
    _, state = auth.login_url(write=False)
    with pytest.raises(ToolError, match="say which account"):
        run(auth.finish_login(f"https://localhost:8765/?code=abc&state={state}", state, False))
    assert not (tmp_path / "t.json").exists()


def test_redaction_covers_json_and_dict_forms():
    text = "{'access_token': 'UNREGISTERED123', \"client_secret\": \"zzz999\"}"
    out = meta.redact(text)
    assert "UNREGISTERED123" not in out and "zzz999" not in out


def test_a_post_that_was_only_prepared_is_published_once(social):
    daemon, graph = social(policy=Policy())
    orig = graph.threads
    published = []

    def threads(method, path, q):
        if path == "/501" and q.get("fields", "").startswith("status"):
            return httpx.Response(200, json={"status": "PUBLISHED" if published else "FINISHED"})
        if path == "/501" and not published:  # a container that isn't published has no permalink yet
            return httpx.Response(400, json={"error": {"message": "x", "code": 100}})
        if path == "/111/threads_publish":
            published.append(1)
            return httpx.Response(200, json={"id": "501"})
        return orig(method, path, q)

    graph.threads = threads
    ask(daemon, ("threads.publish", {"text": "hello world"}))
    assert approve(daemon)["status"] == "done"
    posts = [p for _, m, p, *_ in graph.calls if m == "POST"]
    assert posts == ["/v1.0/111/threads", "/v1.0/111/threads_publish"]


def test_an_unconfirmed_publish_says_so(social):
    daemon, graph = social(policy=Policy())
    orig = graph.threads

    def threads(method, path, q):
        if path == "/501":
            return httpx.Response(400, json={"error": {"message": "x", "code": 100}})
        return orig(method, path, q)

    graph.threads = threads
    ask(daemon, ("threads.publish", {"text": "hello world"}))
    done = approve(daemon)
    assert done["status"] == "failed" and "may have been posted" in done["text"]
