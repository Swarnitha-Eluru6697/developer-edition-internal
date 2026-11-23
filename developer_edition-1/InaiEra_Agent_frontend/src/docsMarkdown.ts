// A real, deliberately focused markdown-to-HTML renderer for the node
// documentation pages — covers exactly what nodeDocs.ts actually uses
// (h2/h3, paragraphs, bold/italic, inline code, tables, bullet lists),
// not a full CommonMark implementation. Real HTML-entity escaping runs
// before any markdown transform, so doc content can never inject raw
// HTML.

import { isSafeLinkUrl } from './safeUrl';

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function inlineFormat(raw: string): string {
  let out = escapeHtml(raw);
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  // Same URL check as the comment renderer — see safeUrl.ts. These doc
  // bodies are static constants in this repo rather than anything a user
  // can store, so this is not the same live vulnerability the comment
  // renderer had; it is the identical line, and leaving one copy
  // unchecked is how the fixed one comes back.
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => (
    isSafeLinkUrl(url)
      ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : `<span class="comment-link-blocked" title="Blocked link (${escapeHtml(url)})">${label}</span>`
  ));
  return out;
}

function isTableSeparator(line: string): boolean {
  return /^\|?[\s:|-]+\|?$/.test(line) && line.includes('-');
}

export function renderDocsMarkdown(raw: string): string {
  const lines = raw.split('\n');
  const html: string[] = [];
  let i = 0;
  let inList = false;
  const closeList = () => { if (inList) { html.push('</ul>'); inList = false; } };

  while (i < lines.length) {
    const line = lines[i];

    const headingMatch = line.match(/^(#{2,4})\s+(.*)$/);
    if (headingMatch) {
      closeList();
      const level = headingMatch[1].length;
      html.push(`<h${level}>${inlineFormat(headingMatch[2])}</h${level}>`);
      i++;
      continue;
    }

    if (line.trim().startsWith('|') && lines[i + 1] && isTableSeparator(lines[i + 1].trim())) {
      closeList();
      const headerCells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        const cells = lines[i].trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
        rows.push(cells);
        i++;
      }
      html.push('<table class="docs-table"><thead><tr>' + headerCells.map((c) => `<th>${inlineFormat(c)}</th>`).join('') + '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + r.map((c) => `<td>${inlineFormat(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      if (!inList) { html.push('<ul>'); inList = true; }
      html.push(`<li>${inlineFormat(line.replace(/^[-*]\s+/, ''))}</li>`);
      i++;
      continue;
    }

    closeList();

    if (line.trim() === '') { i++; continue; }
    html.push(`<p>${inlineFormat(line)}</p>`);
    i++;
  }
  closeList();
  return html.join('\n');
}
