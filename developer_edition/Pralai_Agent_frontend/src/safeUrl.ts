// Whether a URL is safe to put in an href, judged on what a browser
// would actually do with it rather than on its literal text.
//
// Shared by both markdown renderers in this app. They had the same link
// substitution — a regex straight into `href="$2"` with no look at the
// URL — and fixing one of the two would have left the defect a
// copy-paste away from coming back.

/** Schemes a comment link may use. An allowlist, not a denylist of the
 *  dangerous ones: `javascript:`, `data:`, `vbscript:` and `file:` are
 *  the ones people think of, but the set of URL schemes a browser (or an
 *  embedded webview, or a desktop shell) will act on is open-ended —
 *  `intent:`, `ms-msdt:`, `search-ms:` and friends have all been used
 *  this way. Naming what IS allowed is the only version of this check
 *  that stays correct as that list grows. */
const ALLOWED_LINK_SCHEMES = ['http:', 'https:', 'mailto:'];

/** Everything a browser throws away before parsing a scheme, and which
 *  therefore has to be thrown away before CHECKING one: leading and
 *  trailing whitespace, and the C0 control characters that can be
 *  written inside a scheme (`java&#9;script:` is a live URL in more
 *  parsers than one would hope). */
const IGNORED_IN_SCHEME = /[\u0000-\u0020\u007f-\u009f]/g;

/** Named and numeric entities that resolve to a colon or a slash —
 *  the characters that turn a word into a scheme. Decoded before the
 *  check so `javascript&#58;alert(1)` is judged as what it could become,
 *  not as the literal text it currently is. */
function decodeEntitiesOnce(value: string): string {
  return value
    .replace(/&#x([0-9a-f]+);?/gi, (_m, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/&#(\d+);?/g, (_m, dec) => String.fromCharCode(parseInt(dec, 10)))
    .replace(/&colon;?/gi, ':')
    .replace(/&sol;?/gi, '/')
    .replace(/&tab;?/gi, '\t')
    .replace(/&newline;?/gi, '\n')
    .replace(/&amp;?/gi, '&');
}

function decodeEntities(value: string): string {
  // Repeatedly, because escapeHtml has already run by the time a URL
  // reaches this check: a comment containing `javascript&#58;alert`
  // arrives here as `javascript&amp;#58;alert`, which needs two passes
  // before the colon is visible. Bounded, so a string of nested `&amp;`
  // cannot turn the check into a loop.
  let current = value;
  for (let pass = 0; pass < 4; pass += 1) {
    const next = decodeEntitiesOnce(current);
    if (next === current) break;
    current = next;
  }
  return current;
}

/** Whether this URL is safe to put in an href, judged on what a browser
 *  would actually do with it.
 *
 *  The link syntax used to be a single regex substitution straight into
 *  `href="$2"`, with no look at the URL at all. Escaping had already run,
 *  so quotes could not break out of the attribute — but that was never
 *  the risk: `[Open report](javascript:location='//evil')` produced a
 *  perfectly well-formed `<a href="javascript:…">` that ran on click,
 *  from a comment stored in the flow and rendered for everyone who opens
 *  that agent.
 *
 *  Relative links are allowed and deliberately: a link to another page
 *  of this app cannot name a scheme at all, and resolving against the
 *  app's own origin is exactly what should happen. A protocol-relative
 *  URL (`//host/x`) is treated as absolute — it inherits the page
 *  scheme, so it leaves the app — but it carries no scheme of its own to
 *  reject, and http/https are allowed anyway.
 */
export function isSafeLinkUrl(rawUrl: string): boolean {
  const candidate = decodeEntities(String(rawUrl || '')).replace(IGNORED_IN_SCHEME, '');
  if (!candidate) return false;
  // A colon before any /, ? or # means the text up to it is a scheme.
  // Without one there is nothing to reject: the browser resolves it
  // against this page.
  const schemeMatch = /^([a-z][a-z0-9+.-]*):/i.exec(candidate);
  if (!schemeMatch) {
    // `foo:bar` with no valid scheme shape is relative, but a bare
    // backslash-prefixed path is a Windows-style path some shells act
    // on — treat anything that is not plainly a path or fragment as
    // unsafe rather than guessing.
    return !candidate.startsWith('\\');
  }
  return ALLOWED_LINK_SCHEMES.includes(schemeMatch[1].toLowerCase() + ':');
}

