import { describe, it, expect } from 'vitest';
import { formatApiDetail } from '../api';

/**
 * "[object Object]" on a blocked publish.
 *
 * `body.detail` was handed straight to `Error`, whose message is a
 * string — so every endpoint answering with a STRUCTURED detail
 * rendered in the UI as the literal text "[object Object]". Publish is
 * where people met it: flow validation answers with
 * `{message, issues: [{message, node_id}]}`, so a blocked publish
 * reported nothing about what was actually wrong.
 */
describe('formatApiDetail', () => {
  it('renders a flow-validation detail as the message plus its issues', () => {
    const out = formatApiDetail({
      message: 'Publish blocked — this flow has real, unresolved problems.',
      issues: [
        { severity: 'error', code: 'unknown_knowledge_base', message: "References knowledge base 'x', which doesn't exist", node_id: 'n3' },
        { severity: 'error', code: 'missing_output_node', message: 'Workflow apps must have an Output node' },
      ],
    }, 'fallback');

    expect(out).toContain('Publish blocked');
    expect(out).toContain("References knowledge base 'x'");
    expect(out).toContain('(node n3)');
    expect(out).toContain('Workflow apps must have an Output node');
    expect(out).not.toContain('[object Object]');
  });

  it('passes a plain string detail through unchanged', () => {
    const message = 'Publish blocked — 2 object(s) this agent reads are not granted to it';
    expect(formatApiDetail(message, 'fallback')).toBe(message);
  });

  it('renders a FastAPI request-validation list', () => {
    const out = formatApiDetail([
      { type: 'missing', loc: ['body', 'group'], msg: 'Field required' },
    ], 'fallback');
    expect(out).toContain('group: Field required');
    expect(out).not.toContain('[object Object]');
  });

  it('falls back for an empty detail', () => {
    expect(formatApiDetail(undefined, 'Request failed: 500')).toBe('Request failed: 500');
    expect(formatApiDetail(null, 'Request failed: 500')).toBe('Request failed: 500');
  });

  it('never returns the literal "[object Object]" for any object shape', () => {
    for (const detail of [{}, { anything: 1 }, { nested: { deep: true } }, [1, 2, 3]]) {
      expect(formatApiDetail(detail, 'fallback')).not.toContain('[object Object]');
    }
  });

  it('is what String(detail) would have produced, for contrast', () => {
    // Guards the diagnosis: this is exactly what the UI used to show.
    expect(String({ message: 'x', issues: [] })).toBe('[object Object]');
  });
});
