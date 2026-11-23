"""
The real script behind the Publish → Embed → "Chat Bubble" snippet.

This endpoint did not exist. Both proxies already routed `/embed.js` to
this service (`nginx.conf`'s `location /embed.js`, and vite.config.ts's
dev proxy), and the Publish modal printed a `<script
src="…/embed.js">` snippet plus a note documenting
`--inaiera-chatbot-bubble-button-bg-color` on
`#inaiera-chatbot-bubble-button` — but the route was never implemented,
so every copied Chat Bubble snippet fetched a 404 and no button ever
appeared on the embedding page.

Scope: the widget is a floating button that toggles a panel containing
an iframe pointing at the agent's own web app (`#/app/<token>`). It
deliberately does NOT reimplement the chat UI or call /api itself. That
keeps exactly one runner implementation — the same one Try It and the
public link use — so an agent's Web App Settings (name, icon, theme,
accent, placeholder, legal footer) and its published/draft gating apply
inside the bubble without this file knowing anything about them.
"""
from fastapi import APIRouter, Response

router = APIRouter(tags=["embed"])

# Kept in one place so the served script and the tests agree on the
# contract the Publish modal advertises to users.
BUTTON_ID = "inaiera-chatbot-bubble-button"
WINDOW_ID = "inaiera-chatbot-bubble-window"
CONFIG_KEY = "inaieraChatbotConfig"

_SCRIPT = """/* InAiEra chat bubble embed widget. Served by the platform at /embed.js. */
(function () {
  'use strict';

  // The snippet can legitimately end up on a page twice (a CMS partial
  // included in both a layout and a template). Running twice would
  // stack two buttons, so the second run is a no-op.
  if (window.__inaieraChatbotEmbedLoaded) return;
  window.__inaieraChatbotEmbedLoaded = true;

  var cfg = window.%(CONFIG_KEY)s;
  if (!cfg || !cfg.token) {
    console.error(
      '[inaiera] /embed.js loaded but window.%(CONFIG_KEY)s.token is missing. ' +
      'Copy the snippet from Publish \\u2192 Embed \\u2192 Chat Bubble, and keep the ' +
      'config <script> BEFORE the <script src=".../embed.js"> tag.'
    );
    return;
  }

  // A trailing slash in baseUrl would produce '…//#/app/x'. Falling back
  // to the script's own origin means a snippet that omits baseUrl still
  // works, rather than silently pointing the iframe at the host site.
  var base = String(cfg.baseUrl || '').replace(/\\/+$/, '');
  if (!base) {
    var self = document.currentScript;
    base = self ? new URL(self.src, window.location.href).origin : window.location.origin;
  }
  var appUrl = base + '/#/app/' + encodeURIComponent(cfg.token);

  var style = document.createElement('style');
  style.id = 'inaiera-chatbot-embed-styles';
  style.textContent = [
    '#%(BUTTON_ID)s{',
    '  position:fixed;',
    '  bottom:var(--inaiera-chatbot-bubble-button-bottom,1.25rem);',
    '  right:var(--inaiera-chatbot-bubble-button-right,1.25rem);',
    '  left:var(--inaiera-chatbot-bubble-button-left,auto);',
    '  top:var(--inaiera-chatbot-bubble-button-top,auto);',
    '  width:var(--inaiera-chatbot-bubble-button-width,3.5rem);',
    '  height:var(--inaiera-chatbot-bubble-button-height,3.5rem);',
    '  border-radius:var(--inaiera-chatbot-bubble-button-border-radius,50%%);',
    '  background:var(--inaiera-chatbot-bubble-button-bg-color,#0f766e);',
    '  color:var(--inaiera-chatbot-bubble-button-color,#fff);',
    '  box-shadow:var(--inaiera-chatbot-bubble-button-box-shadow,0 6px 20px rgba(0,0,0,.22));',
    '  border:0;padding:0;cursor:pointer;z-index:2147483000;',
    '  display:flex;align-items:center;justify-content:center;',
    '  transition:transform .18s ease,box-shadow .18s ease;',
    '}',
    '#%(BUTTON_ID)s:hover{transform:var(--inaiera-chatbot-bubble-button-hover-transform,scale(1.06))}',
    '#%(BUTTON_ID)s:focus-visible{outline:2px solid currentColor;outline-offset:3px}',
    '#%(BUTTON_ID)s svg{width:55%%;height:55%%;display:block;pointer-events:none}',
    '#%(WINDOW_ID)s{',
    '  position:fixed;',
    '  bottom:calc(var(--inaiera-chatbot-bubble-button-bottom,1.25rem) + var(--inaiera-chatbot-bubble-button-height,3.5rem) + .75rem);',
    '  right:var(--inaiera-chatbot-bubble-button-right,1.25rem);',
    '  width:var(--inaiera-chatbot-bubble-window-width,26rem);',
    '  height:var(--inaiera-chatbot-bubble-window-height,40rem);',
    '  max-width:calc(100vw - 2rem);max-height:calc(100vh - 2rem);',
    '  border-radius:var(--inaiera-chatbot-bubble-window-border-radius,14px);',
    '  box-shadow:0 12px 40px rgba(0,0,0,.24);',
    '  background:#fff;overflow:hidden;z-index:2147483000;',
    '  display:none;',
    '}',
    '#%(WINDOW_ID)s[data-open="true"]{display:block}',
    '#%(WINDOW_ID)s iframe{width:100%%;height:100%%;border:0;display:block}',
    /* On a phone the panel is the page — a 26rem card positioned off a
       1.25rem offset would otherwise hang off the side. */
    '@media (max-width:640px){',
    '  #%(WINDOW_ID)s{top:0;right:0;bottom:0;left:0;width:100%%;height:100%%;',
    '    max-width:none;max-height:none;border-radius:0}',
    '}',
    '@media (prefers-reduced-motion:reduce){',
    '  #%(BUTTON_ID)s{transition:none}',
    '  #%(BUTTON_ID)s:hover{transform:none}',
    '}'
  ].join('\\n');

  var ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  var ICON_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  var panel = document.createElement('div');
  panel.id = '%(WINDOW_ID)s';
  panel.setAttribute('data-open', 'false');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'false');
  panel.setAttribute('aria-label', cfg.title || 'Chat');

  var frame = document.createElement('iframe');
  frame.title = cfg.title || 'Chat';
  frame.setAttribute('allow', 'clipboard-write');
  frame.setAttribute('loading', 'lazy');
  panel.appendChild(frame);

  var button = document.createElement('button');
  button.id = '%(BUTTON_ID)s';
  button.type = 'button';
  button.setAttribute('aria-label', cfg.title || 'Open chat');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', '%(WINDOW_ID)s');
  button.innerHTML = ICON_CHAT;

  function setOpen(open) {
    // The app is not loaded until the visitor actually asks for it —
    // an embed on every page of a site should not cost every visitor a
    // full app boot they never use.
    if (open && !frame.getAttribute('src')) frame.setAttribute('src', appUrl);
    panel.setAttribute('data-open', open ? 'true' : 'false');
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.setAttribute('aria-label', open ? 'Close chat' : (cfg.title || 'Open chat'));
    button.innerHTML = open ? ICON_CLOSE : ICON_CHAT;
    if (open) frame.focus();
  }

  button.addEventListener('click', function () {
    setOpen(panel.getAttribute('data-open') !== 'true');
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && panel.getAttribute('data-open') === 'true') {
      setOpen(false);
      button.focus();
    }
  });

  function mount() {
    // Injected ahead of the page's own stylesheets so that author CSS on
    // #%(BUTTON_ID)s wins ties on specificity, which is what the
    // Publish modal's customisation note promises.
    var head = document.head || document.documentElement;
    head.insertBefore(style, head.firstChild);
    document.body.appendChild(panel);
    document.body.appendChild(button);
    if (cfg.autoOpen) setOpen(true);
  }

  // A snippet pasted into <head> runs before <body> exists.
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);

  window.inaieraChatbot = {
    open: function () { setOpen(true); },
    close: function () { setOpen(false); },
    toggle: function () { setOpen(panel.getAttribute('data-open') !== 'true'); }
  };
})();
""" % {"CONFIG_KEY": CONFIG_KEY, "BUTTON_ID": BUTTON_ID, "WINDOW_ID": WINDOW_ID}


@router.get("/embed.js", include_in_schema=False)
def embed_js() -> Response:
    """Serve the chat bubble widget.

    Public and unauthenticated on purpose: it is a static asset fetched
    by anonymous visitors on a third-party site, and it carries no agent
    data of its own — the agent id travels in the embedding page's own
    config, and every access decision still happens in the iframe's app
    and the /api calls behind it.
    """
    return Response(
        content=_SCRIPT,
        media_type="application/javascript; charset=utf-8",
        headers={
            # Not content-hashed like /assets/*, so it cannot be cached
            # immutably — a widget fix has to reach existing embeds
            # without asking every site owner to change their snippet.
            "Cache-Control": "public, max-age=300",
            # Fetched cross-origin by construction: the whole point is
            # that it runs on someone else's domain.
            "Access-Control-Allow-Origin": "*",
        },
    )
