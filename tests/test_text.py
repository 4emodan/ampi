from assistant_bridge.text import markdown_to_html, split_markdown


def test_short_text_is_one_chunk():
    assert split_markdown("hello") == ["hello"]
    assert split_markdown("   ") == []


def test_long_text_splits_on_lines_and_keeps_content():
    lines = [f"line {i} " + "x" * 50 for i in range(300)]
    text = "\n".join(lines)
    chunks = split_markdown(text, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines


def test_very_long_line_is_cut():
    chunks = split_markdown("word " * 1000, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks).replace(" ", "") == "word" * 1000


def test_code_fence_is_closed_and_reopened_across_chunks():
    code = "\n".join(f"print({i})" for i in range(200))
    chunks = split_markdown(f"Intro\n```python\n{code}\n```\nOutro", limit=600)
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk
    assert chunks[1].startswith("```python")


def test_markdown_to_html_basics():
    html = markdown_to_html("# Title\n**bold** and *it* and `a<b`\n- item\n[link](https://x.y/?a=1&b=2)")
    assert "<b>Title</b>" in html
    assert "<b>bold</b>" in html
    assert "<i>it</i>" in html
    assert "<code>a&lt;b</code>" in html
    assert "• item" in html
    assert '<a href="https://x.y/?a=1&amp;b=2">link</a>' in html


def test_markdown_to_html_escapes_and_code_blocks():
    html = markdown_to_html("1 < 2 & 3 > 2\n```js\nif (a<b) {}\n```")
    assert "1 &lt; 2 &amp; 3 &gt; 2" in html
    assert '<pre><code class="language-js">if (a&lt;b) {}</code></pre>' in html


def test_snake_case_is_not_italic():
    assert "<i>" not in markdown_to_html("use my_var_name here")
