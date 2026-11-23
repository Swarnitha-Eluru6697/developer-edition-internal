// Real, deliberately small markdown-lite support for the Comment node —
// exactly the constructs its own real formatting toolbar produces
// (bold/italic/strikethrough/links/bullet lists/headings/quotes), not a
// full CommonMark implementation. A comment's content is authored by the
// person on their own canvas, but real HTML entities are still escaped
// before any markdown transform runs, so a literal `<script>` typed into
// a note can never execute as real HTML.
//
// Escaping alone is not enough for LINKS, which is a separate question
// from breaking out of an attribute: `[x](javascript:…)` needs no quote
// to be dangerous, and a comment is stored in the flow and rendered for
// everyone who opens that agent. Link targets go through isSafeLinkUrl
// below, which allows an explicit list of schemes and treats everything
// else as text.

import { isSafeLinkUrl } from './safeUrl';

export interface CommentColor {
  id: string;
  bg: string;
  border: string;
  swatch: string;
}

// Six real presets matching the Dify-style pastel note palette — a
// light background paired with a slightly more saturated border/accent,
// genuinely applied to the canvas note, not just decorative swatches.
export const COMMENT_COLORS: CommentColor[] = [
  { id: 'blue', bg: '#EAF3FE', border: '#8EC3F5', swatch: '#8EC3F5' },
  { id: 'cyan', bg: '#E7FBFC', border: '#7FE0E6', swatch: '#7FE0E6' },
  { id: 'green', bg: '#EAFBEE', border: '#8FE0A8', swatch: '#8FE0A8' },
  { id: 'yellow', bg: '#FEFAE6', border: '#F3DD7A', swatch: '#F3DD7A' },
  { id: 'pink', bg: '#FDEDF6', border: '#F3A9D4', swatch: '#F3A9D4' },
  { id: 'purple', bg: '#F1EDFE', border: '#BDA6F5', swatch: '#BDA6F5' },
];

export const DEFAULT_COMMENT_COLOR = 'blue';

export function getCommentColor(id?: string): CommentColor {
  return COMMENT_COLORS.find((c) => c.id === id) || COMMENT_COLORS[0];
}

export const COMMENT_FONT_SIZES: Record<string, number> = { small: 11, medium: 13, large: 16 };

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Real markdown-lite -> HTML for exactly what the comment toolbar can
 * produce. Headings/quotes/lists are handled a real line at a time;
 * inline bold/italic/strikethrough/links are applied within each line
 * after that. Escapes real HTML entities first — safe for
 * dangerouslySetInnerHTML. */
export function renderCommentMarkdown(raw: string): string {
  const lines = (raw || '').split('\n');
  const htmlLines: string[] = [];
  let inList = false;

  const closeList = () => {
    if (inList) {
      htmlLines.push('</ul>');
      inList = false;
    }
  };

  const inlineFormat = (rawLine: string): string => {
    let out = escapeHtml(rawLine);
    out = out.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    out = out.replace(/~~(.+?)~~/g, '<del>$1</del>');
    // The URL is inspected, not just interpolated — see isSafeLinkUrl.
    // A rejected link still shows its own text, with the URL it would
    // have pointed at in the tooltip: silently dropping it would hide
    // from the reader that something was there at all, and rendering it
    // as a link is the bug.
    out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
      if (!isSafeLinkUrl(url)) {
        return `<span class="comment-link-blocked" title="Blocked link (${escapeHtml(url)})">${label}</span>`;
      }
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    return out;
  };

  for (const line of lines) {
    // Real block-marker detection runs on the RAW (unescaped) line —
    // escaping first would turn a literal '>' into '&gt;' and silently
    // break every block-level check below.
    if (/^#{1,3}\s+/.test(line)) {
      closeList();
      const level = line.match(/^(#{1,3})/)?.[1].length || 1;
      const text = line.replace(/^#{1,3}\s+/, '');
      htmlLines.push(`<h${level + 3} style="margin:4px 0">${inlineFormat(text)}</h${level + 3}>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      closeList();
      const text = line.replace(/^>\s?/, '');
      htmlLines.push(`<blockquote style="margin:4px 0;padding-left:8px;border-left:2px solid currentColor;opacity:.8">${inlineFormat(text)}</blockquote>`);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      if (!inList) {
        htmlLines.push('<ul style="margin:4px 0;padding-left:18px">');
        inList = true;
      }
      const text = line.replace(/^[-*]\s+/, '');
      htmlLines.push(`<li>${inlineFormat(text)}</li>`);
      continue;
    }
    closeList();
    if (line.trim() === '') {
      htmlLines.push('<br>');
    } else {
      htmlLines.push(`<div>${inlineFormat(line)}</div>`);
    }
  }
  closeList();
  return htmlLines.join('');
}

export type FormatAction = 'bold' | 'italic' | 'strike' | 'link' | 'bullet' | 'heading' | 'quote';

/** Applies a real formatting action to a real textarea selection,
 * returning the new full text plus the new real selection range so the
 * caller can restore focus/cursor position — genuine text manipulation,
 * not a decorative toolbar. */
export function applyCommentFormat(
  text: string, selStart: number, selEnd: number, action: FormatAction,
): { text: string; selStart: number; selEnd: number } {
  const before = text.slice(0, selStart);
  const selected = text.slice(selStart, selEnd);
  const after = text.slice(selEnd);

  if (action === 'bullet' || action === 'heading' || action === 'quote') {
    const prefix = action === 'bullet' ? '- ' : action === 'heading' ? '## ' : '> ';
    // Real per-line application over the current selection (or the
    // current line, if nothing is selected).
    const lineStart = before.lastIndexOf('\n') + 1;
    const affected = text.slice(lineStart, selEnd || lineStart);
    const already = affected.split('\n').every((l) => l.startsWith(prefix) || l.trim() === '');
    const transformed = affected
      .split('\n')
      .map((l) => (l.trim() === '' ? l : already ? l.slice(prefix.length) : prefix + l))
      .join('\n');
    const newText = text.slice(0, lineStart) + transformed + text.slice(selEnd || lineStart);
    return { text: newText, selStart: lineStart, selEnd: lineStart + transformed.length };
  }

  const wrap: Record<'bold' | 'italic' | 'strike', string> = { bold: '**', italic: '*', strike: '~~' };
  if (action === 'bold' || action === 'italic' || action === 'strike') {
    const marker = wrap[action];
    const alreadyWrapped = selected.startsWith(marker) && selected.endsWith(marker) && selected.length >= marker.length * 2;
    const newSelected = alreadyWrapped ? selected.slice(marker.length, -marker.length) : `${marker}${selected || 'text'}${marker}`;
    const newText = before + newSelected + after;
    return { text: newText, selStart: before.length, selEnd: before.length + newSelected.length };
  }

  if (action === 'link') {
    const label = selected || 'link text';
    const newSelected = `[${label}](https://)`;
    const newText = before + newSelected + after;
    const urlStart = before.length + label.length + 3; // position the cursor inside the URL placeholder
    return { text: newText, selStart: urlStart, selEnd: urlStart + 8 };
  }

  return { text, selStart, selEnd };
}
