import { describe, it, expect } from 'vitest';
import { validateFlow } from '../flowValidation';
import type { FlowNode, FlowEdge } from '../types';

function node(overrides: Partial<FlowNode>): FlowNode {
  return { id: 'n1', type: 'llm', x: 0, y: 0, sub: '', config: {}, ...overrides } as FlowNode;
}

describe('validateFlow — real checks against real node config, not a canned list', () => {
  it('flags an Output node with no variables, matching the reference message exactly', () => {
    const nodes = [node({ id: 'out1', type: 'output', config: { variables: [] } })];
    const edges: FlowEdge[] = [{ id: 'e1', source: 'x', target: 'out1' }];
    const issues = validateFlow(nodes, edges);
    expect(issues).toHaveLength(1);
    expect(issues[0].messages).toContain('output variable is required');
  });

  it('does NOT flag an Output node that has at least one real named variable', () => {
    const nodes = [node({ id: 'out1', type: 'output', config: { variables: [{ name: 'summary', expr: '{{x}}' }] } })];
    const edges: FlowEdge[] = [{ id: 'e1', source: 'x', target: 'out1' }];
    expect(validateFlow(nodes, edges)).toHaveLength(0);
  });

  it('flags a Question Classifier missing real input variables', () => {
    const nodes = [node({ id: 'q1', type: 'classifier', config: { class1: 'billing' } })];
    const edges: FlowEdge[] = [{ id: 'e1', source: 'x', target: 'q1' }];
    const issues = validateFlow(nodes, edges);
    expect(issues[0].messages).toContain('Input Variables is required');
  });

  it('flags a Human Input node with no real delivery method selected', () => {
    const nodes = [node({ id: 'h1', type: 'hitl', config: {} })];
    const edges: FlowEdge[] = [{ id: 'e1', source: 'x', target: 'h1' }];
    const issues = validateFlow(nodes, edges);
    expect(issues[0].messages).toContain('Please select at least one delivery method');
  });

  it('does NOT flag Human Input once a real delivery method is selected', () => {
    const nodes = [node({ id: 'h1', type: 'hitl', config: { delivery_methods: ['email'] } })];
    const edges: FlowEdge[] = [{ id: 'e1', source: 'x', target: 'h1' }];
    expect(validateFlow(nodes, edges)).toHaveLength(0);
  });

  it('flags any node with real zero edges as "not connected to anything"', () => {
    const nodes = [node({ id: 'isolated', type: 'llm', config: { model: 'Claude Sonnet 5' } })];
    const issues = validateFlow(nodes, []);
    expect(issues[0].messages).toContain('This step is not connected to anything');
  });

  it('never flags comment nodes for connectivity — documentation-only, never part of execution', () => {
    const nodes = [node({ id: 'c1', type: 'comment', config: { text: 'a note' } })];
    expect(validateFlow(nodes, [])).toHaveLength(0);
  });

  it('a node can have BOTH a real config issue and a real connectivity issue simultaneously', () => {
    const nodes = [node({ id: 'out1', type: 'output', config: { variables: [] } })];
    const issues = validateFlow(nodes, []);
    expect(issues[0].messages).toEqual(['output variable is required', 'This step is not connected to anything']);
  });

  it('produces a real numbered label matching the reference ("Output 3" style) based on position among same-type nodes', () => {
    const nodes = [
      node({ id: 'out1', type: 'output', config: { variables: [{ name: 'a', expr: '{{x}}' }] } }),
      node({ id: 'out2', type: 'output', config: { variables: [{ name: 'b', expr: '{{y}}' }] } }),
      node({ id: 'out3', type: 'output', config: { variables: [] } }),
    ];
    const edges: FlowEdge[] = [
      { id: 'e1', source: 'x', target: 'out1' }, { id: 'e2', source: 'x', target: 'out2' }, { id: 'e3', source: 'x', target: 'out3' },
    ];
    const issues = validateFlow(nodes, edges);
    expect(issues).toHaveLength(1);
    expect(issues[0].label).toBe('Output vars 3');
  });

  it('a fully valid flow produces zero real issues', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'g1', type: 'gov', config: {} }),
      node({ id: 'l1', type: 'llm', config: { model: 'Claude Sonnet 5' } }),
      node({ id: 'r1', type: 'respond', config: {} }),
    ];
    const edges: FlowEdge[] = [
      { id: 'e1', source: 't1', target: 'g1' }, { id: 'e2', source: 'g1', target: 'l1' }, { id: 'e3', source: 'l1', target: 'r1' },
    ];
    expect(validateFlow(nodes, edges)).toHaveLength(0);
  });

  it('flags a workflow app with no real Output node — a genuine dead end for structured results', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'l1', type: 'llm', config: { model: 'Claude Sonnet 5' } }),
      node({ id: 'r1', type: 'respond', config: {} }),
    ];
    const edges: FlowEdge[] = [{ id: 'e1', source: 't1', target: 'l1' }, { id: 'e2', source: 'l1', target: 'r1' }];
    const issues = validateFlow(nodes, edges, 'workflow');
    expect(issues.some((i) => i.messages.some((m) => m.includes('Workflow apps should end with an Output node')))).toBe(true);
  });

  it('does NOT flag a workflow app that has a real Output node', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'l1', type: 'llm', config: { model: 'Claude Sonnet 5' } }),
      node({ id: 'o1', type: 'output', config: { variables: [{ name: 'x', expr: '{{y}}' }] } }),
    ];
    const edges: FlowEdge[] = [{ id: 'e1', source: 't1', target: 'l1' }, { id: 'e2', source: 'l1', target: 'o1' }];
    expect(validateFlow(nodes, edges, 'workflow')).toHaveLength(0);
  });

  it('flags a chat app with no real response-producing node — it would answer with silence', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'g1', type: 'gov', config: {} }),
    ];
    const edges: FlowEdge[] = [{ id: 'e1', source: 't1', target: 'g1' }];
    const issues = validateFlow(nodes, edges, 'chatbot');
    expect(issues.some((i) => i.messages.some((m) => m.includes('has no node that can produce a response')))).toBe(true);
  });

  it('does NOT flag a chat app that has a real LLM node', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'l1', type: 'llm', config: { model: 'Claude Sonnet 5' } }),
      node({ id: 'r1', type: 'respond', config: {} }),
    ];
    const edges: FlowEdge[] = [{ id: 'e1', source: 't1', target: 'l1' }, { id: 'e2', source: 'l1', target: 'r1' }];
    expect(validateFlow(nodes, edges, 'chatbot')).toHaveLength(0);
  });

  it('does not apply the appType structural checks when no appType is passed (backward compatible)', () => {
    const nodes = [node({ id: 't1', type: 'trigger', config: {} }), node({ id: 'g1', type: 'gov', config: {} })];
    const edges: FlowEdge[] = [{ id: 'e1', source: 't1', target: 'g1' }];
    expect(validateFlow(nodes, edges)).toHaveLength(0);
  });

  // §0.103, GAP_REPORT — the actual fix for a real, reported gap: this
  // validator never checked for a genuine cycle in the real execution
  // edges (mirrors flow_validator.py's own new check exactly).
  it('flags every node in a real cycle: Trigger -> A <-> B (the exact reported scenario)', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'a', type: 'template', config: { template: 'a' } }),
      node({ id: 'b', type: 'template', config: { template: 'b' } }),
    ];
    const edges: FlowEdge[] = [
      { id: 'e1', source: 't1', target: 'a' },
      { id: 'e2', source: 'a', target: 'b' },
      { id: 'e3', source: 'b', target: 'a' },
    ];
    const issues = validateFlow(nodes, edges);
    const flaggedIds = new Set(issues.map((i) => i.nodeId));
    expect(flaggedIds.has('a')).toBe(true);
    expect(flaggedIds.has('b')).toBe(true);
    expect(issues.some((i) => i.messages.some((m) => m.includes('cycle')))).toBe(true);
  });

  it('flags a cycle inside a loop container\'s own body', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'loop1', type: 'loop', config: { max_loops: 5 } }),
      node({ id: 'c1', type: 'template', config: { template: 'c1' }, parent_id: 'loop1' }),
      node({ id: 'c2', type: 'template', config: { template: 'c2' }, parent_id: 'loop1' }),
    ];
    const edges: FlowEdge[] = [
      { id: 'e1', source: 't1', target: 'loop1' },
      { id: 'e2', source: 'c1', target: 'c2' },
      { id: 'e3', source: 'c2', target: 'c1' },
    ];
    const issues = validateFlow(nodes, edges);
    const flaggedIds = new Set(issues.map((i) => i.nodeId));
    expect(flaggedIds.has('c1')).toBe(true);
    expect(flaggedIds.has('c2')).toBe(true);
  });

  it('does NOT flag a legitimate diamond fan-out/fan-in as a false-positive cycle', () => {
    const nodes = [
      node({ id: 't1', type: 'trigger', config: {} }),
      node({ id: 'a', type: 'template', config: { template: 'a' } }),
      node({ id: 'b', type: 'template', config: { template: 'b' } }),
      node({ id: 'c', type: 'template', config: { template: 'c' } }),
      node({ id: 'out1', type: 'output', config: { variables: [{ name: 'x', expr: '{{a}}' }] } }),
    ];
    const edges: FlowEdge[] = [
      { id: 'e1', source: 't1', target: 'a' },
      { id: 'e2', source: 'a', target: 'b' },
      { id: 'e3', source: 'a', target: 'c' },
      { id: 'e4', source: 'b', target: 'out1' },
      { id: 'e5', source: 'c', target: 'out1' },
    ];
    const issues = validateFlow(nodes, edges);
    expect(issues.some((i) => i.messages.some((m) => m.includes('cycle')))).toBe(false);
  });
});

describe('nodes inside a loop or iteration container', () => {
  // A container child is reached by the container running its body, not
  // by an edge from the outer graph. Showcase 3's iteration body is a
  // single template, correctly wired to nothing — and the Builder
  // refused to run the agent, reporting "Template 1 is not connected to
  // anything".
  const container = { id: 'it', type: 'iteration', x: 0, y: 0, config: {} } as never;

  it('does not demand an edge from the only child of a container', () => {
    const nodes = [
      container,
      { id: 'it1', type: 'template', parent_id: 'it', x: 0, y: 0,
        config: { template: 'Item {{item}}' } },
    ] as never[];
    const issues = validateFlow(nodes, [{ id: 'e', source: 'n0', target: 'it' }] as never[]);
    expect(issues.find((i) => i.nodeId === 'it1')).toBeUndefined();
  });

  it('still demands one when the body has siblings to join', () => {
    // Two nodes in a body DO have an internal order, so an unattached
    // one really is unreachable.
    const nodes = [
      container,
      { id: 'a', type: 'template', parent_id: 'it', x: 0, y: 0, config: { template: 'x' } },
      { id: 'b', type: 'template', parent_id: 'it', x: 0, y: 0, config: { template: 'y' } },
    ] as never[];
    // The container itself is wired in; only its children are loose.
    const issues = validateFlow(nodes, [{ id: 'e0', source: 'n0', target: 'it' }] as never[]);
    const flagged = issues.filter((i) => i.messages.includes('This step is not connected to anything'));
    expect(flagged.map((i) => i.nodeId).sort()).toEqual(['a', 'b']);
  });

  it('a body of two that IS joined is accepted', () => {
    const nodes = [
      container,
      { id: 'a', type: 'template', parent_id: 'it', x: 0, y: 0, config: { template: 'x' } },
      { id: 'b', type: 'template', parent_id: 'it', x: 0, y: 0, config: { template: 'y' } },
    ] as never[];
    const issues = validateFlow(nodes, [
      { id: 'e0', source: 'n0', target: 'it' },
      { id: 'e1', source: 'a', target: 'b' },
    ] as never[]);
    expect(issues.filter((i) => i.messages.includes('This step is not connected to anything')))
      .toHaveLength(0);
  });

  it('a top-level node with no edges is still flagged', () => {
    // The exemption must not leak out of containers.
    const nodes = [
      { id: 'lonely', type: 'template', x: 0, y: 0, config: { template: 'x' } },
    ] as never[];
    const issues = validateFlow(nodes, [] as never[]);
    expect(issues[0].messages).toContain('This step is not connected to anything');
  });
});
