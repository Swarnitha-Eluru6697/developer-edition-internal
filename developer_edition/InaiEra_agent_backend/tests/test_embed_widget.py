"""
Tests for /embed.js — the script behind Publish → Embed → "Chat Bubble".

The endpoint did not exist before: both proxies routed /embed.js to this
service and the Publish modal printed a snippet pointing at it, but the
route was missing, so every copied Chat Bubble snippet fetched a 404 and
no button appeared. These tests pin the contract the modal advertises to
users, since that copy is the only spec an embedder reads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.routers.embed import BUTTON_ID, CONFIG_KEY, WINDOW_ID

client = TestClient(app)


def test_embed_js_is_served_as_javascript():
    """The regression this fixes: the snippet's <script src> was a 404."""
    r = client.get("/embed.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.text.strip()


def test_served_cross_origin_and_briefly_cached():
    """It runs on someone else's domain, and a widget fix has to reach
    existing embeds without site owners editing their snippet."""
    r = client.get("/embed.js")
    assert r.headers["access-control-allow-origin"] == "*"
    cache = r.headers["cache-control"]
    assert "max-age=300" in cache
    assert "immutable" not in cache


def test_honours_the_identifiers_the_publish_modal_documents():
    """The modal tells users to style `#inaiera-chatbot-bubble-button`
    with `--inaiera-chatbot-bubble-button-bg-color`, and to configure via
    `window.inaieraChatbotConfig`. All three must really be what the
    script uses, or that instruction is fiction."""
    body = client.get("/embed.js").text
    assert BUTTON_ID == "inaiera-chatbot-bubble-button"
    assert CONFIG_KEY == "inaieraChatbotConfig"
    assert f"#{BUTTON_ID}{{" in body
    assert f"button.id = '{BUTTON_ID}'" in body
    assert "window.inaieraChatbotConfig" in body
    assert "var(--inaiera-chatbot-bubble-button-bg-color," in body
    for prop in ("bottom", "right", "left", "top", "width", "height", "border-radius", "box-shadow"):
        assert f"var(--inaiera-chatbot-bubble-button-{prop}," in body, prop
    assert "hover-transform" in body


def test_points_the_panel_at_the_real_hash_route():
    """The app uses HashRouter — '/app/<id>' without the '#' loads the
    studio, not the agent's web app."""
    body = client.get("/embed.js").text
    assert "'/#/app/' + encodeURIComponent(cfg.token)" in body
    assert WINDOW_ID in body


def test_reuses_the_app_in_an_iframe_rather_than_reimplementing_chat():
    """One runner implementation, so Web App Settings and the
    published/draft gate apply inside the bubble for free."""
    body = client.get("/embed.js").text
    assert "createElement('iframe')" in body
    # No second chat client calling the run API from the widget itself.
    assert "/api/agents/" not in body


def test_defends_against_the_ways_a_pasted_snippet_goes_wrong():
    body = client.get("/embed.js").text
    # Snippet included twice by a CMS partial -> one button, not two.
    assert "__inaieraChatbotEmbedLoaded" in body
    # Config missing or script tags ordered wrongly -> a real message.
    assert "console.error" in body
    # Trailing slash in baseUrl would give '…//#/app/x'.
    assert "replace(/\\/+$/, '')" in body
    # Snippet pasted into <head> runs before <body> exists.
    assert "DOMContentLoaded" in body
    # The app should not boot for visitors who never open the bubble.
    assert "!frame.getAttribute('src')" in body


def test_author_css_can_win_on_specificity_ties():
    """Injecting the widget stylesheet last would beat the page's own
    rules on a tie and break the documented customisation."""
    body = client.get("/embed.js").text
    assert "insertBefore(style, head.firstChild)" in body


def test_keyboard_and_assistive_technology_support():
    body = client.get("/embed.js").text
    assert "aria-expanded" in body
    assert "aria-controls" in body
    assert "'Escape'" in body
    assert "prefers-reduced-motion" in body


def test_exposes_a_programmatic_handle():
    """So an embedding page can open the bubble from its own CTA."""
    body = client.get("/embed.js").text
    assert "window.inaieraChatbot = {" in body
    for fn in ("open:", "close:", "toggle:"):
        assert fn in body, fn


def test_not_in_the_public_api_schema():
    """It is a static asset, not part of the product API surface."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/embed.js" not in paths


def test_served_script_is_syntactically_valid_javascript():
    """Every other test here matches substrings, which a broken script
    would still satisfy. The script is assembled by %-formatting a
    Python template, so a stray '%' or quote would ship a file the
    browser refuses to parse while all the assertions above stayed
    green."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not available to parse the script")

    body = client.get("/embed.js").text
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(body)
        path = fh.name
    proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    # The CSS goes through the same %-formatting: '50%%' must land as
    # '50%', not '50%%', or the border-radius is simply invalid.
    assert "border-radius,50%)" in body and "50%%" not in body
