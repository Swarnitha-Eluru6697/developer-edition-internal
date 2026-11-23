import { NODE_TYPES } from './constants';
import type { FlowNode, FlowEdge } from './types';

export interface FlowIssue {
  nodeId: string;
  nodeType: string;
  label: string;
  icon: string;
  dot?: string;
  messages: string[];
}

// Nodes that are always structurally present/connected as part of the
// base skeleton, or that are genuinely never meant to be wired into the
// execution graph (comments are documentation-only, per the existing
// harness — see harness.py's node dispatch table comment).
const SKIP_CONNECTIVITY_CHECK = new Set(['comment']);

function hasAnyEdge(nodeId: string, edges: FlowEdge[]): boolean {
  return edges.some((e) => e.source === nodeId || e.target === nodeId);
}

/** Whether this node genuinely needs an edge to be reachable.
 *
 * A node inside a loop or iteration container is reached by the
 * container running its body, not by an edge from the outer graph. When
 * the body holds a single node there is nothing to connect it TO, so
 * demanding an edge reports a working flow as broken — which is what
 * Showcase 3 hit: its iteration body is one template, correctly wired
 * to nothing, and the Builder refused to run it.
 *
 * With two or more children the body has its own internal order, so
 * each one is still expected to be joined to its siblings.
 */
function needsAnEdge(node: FlowNode, nodes: FlowNode[]): boolean {
  if (!node.parent_id) return true;
  const siblings = nodes.filter((n) => n.parent_id === node.parent_id);
  return siblings.length > 1;
}

/** Real, type-specific "is this node's own config actually usable"
 * checks — not a generic "config is non-empty" check, since an empty
 * config can be valid for some node types (e.g. tool) and invalid for
 * others. Mirrors the real config fields NodeConfigPanel actually reads
 * and harness.py actually consumes, so a flagged issue here is a real
 * issue a run would actually hit, not a cosmetic nag. */
function configIssues(node: FlowNode): string[] {
  const cfg = node.config || {};
  const issues: string[] = [];
  switch (node.type) {
    case 'output': {
      const vars = cfg.variables || [];
      if (!vars.length || !vars.some((v: any) => v?.name?.trim())) issues.push('output variable is required');
      break;
    }
    case 'classifier':
      if (!cfg.input_var?.trim()) issues.push('Input Variables is required');
      if (!cfg.class1?.trim() && !cfg.class2?.trim()) issues.push('At least one class is required');
      break;
    case 'hitl':
      if (!cfg.delivery_methods?.length) issues.push('Please select at least one delivery method');
      break;
    case 'condition':
      if (!cfg.expression?.trim()) issues.push('Expression is required');
      break;
    case 'template':
      if (!cfg.template?.trim()) issues.push('Jinja2 template is required');
      break;
    case 'code':
      if (!cfg.code?.trim()) issues.push('Code is required');
      break;
    case 'http':
      if (!cfg.url?.trim()) issues.push('URL is required');
      break;
    case 'subagent':
      if (!cfg.target_agent_id) issues.push('Target agent is required');
      break;
    case 'docextractor':
      if (!cfg.input_var?.trim()) issues.push('Input URL is required');
      break;
  }
  return issues;
}

/** Node types that can genuinely set the final response text via
 * handle_respond (harness.py) — mirrors that function's real fallback
 * chain exactly. */
const RESPONSE_PRODUCING_TYPES = new Set(['llm', 'agent', 'subagent', 'code', 'template', 'http', 'tool']);

// Container types whose own children form a SEPARATE sub-graph, walked
// by their own real runtime mechanism — mirrors flow_validator.py's own
// CONTAINER_TYPES exactly (§0.103, GAP_REPORT).
const CONTAINER_TYPES = new Set(['loop', 'iteration']);

/** Real, iterative (no recursion) three-color DFS cycle detection,
 * scoped to just one subset of node ids (and only edges where both
 * ends are in it) — mirrors flow_validator.py's own _find_cycle
 * exactly (§0.103, GAP_REPORT), so the Builder's own UI can flag the
 * same real cycle the backend would otherwise only reject at publish
 * time. Returns the real node-id path forming a cycle, or null if this
 * subset is genuinely acyclic. */
function findCycle(candidateIds: Set<string>, adjacency: Map<string, string[]>): string[] | null {
  const WHITE = 0, GRAY = 1, BLACK = 2;
  const color = new Map<string, number>();
  for (const id of candidateIds) color.set(id, WHITE);
  const sortedIds = Array.from(candidateIds).sort();
  for (const start of sortedIds) {
    if (color.get(start) !== WHITE) continue;
    const stack: [string, number][] = [[start, 0]];
    const path: string[] = [start];
    color.set(start, GRAY);
    while (stack.length) {
      const [nid, idx] = stack[stack.length - 1];
      const neighbors = (adjacency.get(nid) || []).filter((t) => candidateIds.has(t));
      if (idx < neighbors.length) {
        stack[stack.length - 1] = [nid, idx + 1];
        const nxt = neighbors[idx];
        if (color.get(nxt) === GRAY) {
          return path.slice(path.indexOf(nxt)).concat([nxt]);
        }
        if (color.get(nxt) === WHITE) {
          color.set(nxt, GRAY);
          path.push(nxt);
          stack.push([nxt, 0]);
        }
      } else {
        color.set(nid, BLACK);
        path.pop();
        stack.pop();
      }
    }
  }
  return null;
}

/** Real graph-edge cycle detection (§0.103, GAP_REPORT) — the actual
 * fix for a real, reported gap: this validator, and flow_validator.py
 * before this fix, only ever checked structural/config issues — never
 * a genuine cycle in the real execution edges (A waits on B, B waits
 * on A). Confirmed live, before this fix, that an unbroken cycle let
 * BOTH real schedulers reach their own normal, successful completion
 * with the cycle's own nodes never having run at all. Checked
 * separately over the top-level graph and each container's own
 * children, matching flow_validator.py's own real logic exactly, so
 * the Builder's own UI flags this before a publish attempt would even
 * need to reject it. Returns one FlowIssue per node actually IN the
 * cycle (not just the first one found), so the Builder highlights the
 * whole cycle, not a single arbitrary node. */
function cycleIssues(nodes: FlowNode[], edges: FlowEdge[]): FlowIssue[] {
  const adjacency = new Map<string, string[]>();
  for (const e of edges) {
    if (!adjacency.has(e.source)) adjacency.set(e.source, []);
    adjacency.get(e.source)!.push(e.target);
  }
  const issues: FlowIssue[] = [];
  const addCycleIssue = (cycle: string[], contextLabel: string) => {
    const cycleNodeIds = new Set(cycle.slice(0, -1));
    for (const node of nodes) {
      if (!cycleNodeIds.has(node.id)) continue;
      const def = NODE_TYPES[node.type];
      issues.push({
        nodeId: node.id, nodeType: node.type,
        label: def?.label ? def.label.charAt(0) + def.label.slice(1).toLowerCase() : node.type,
        icon: def?.icon || 'i-cog', dot: def?.dot,
        messages: [`${contextLabel}: ${cycle.join(' → ')} — none of these can ever become ready to run.`],
      });
    }
  };

  const topLevelIds = new Set(nodes.filter((n) => !n.parent_id).map((n) => n.id));
  const topCycle = findCycle(topLevelIds, adjacency);
  if (topCycle) addCycleIssue(topCycle, 'This step is part of a cycle in the flow');

  for (const container of nodes) {
    if (!CONTAINER_TYPES.has(container.type)) continue;
    const childIds = new Set(nodes.filter((n) => n.parent_id === container.id).map((n) => n.id));
    if (!childIds.size) continue;
    const childCycle = findCycle(childIds, adjacency);
    if (childCycle) addCycleIssue(childCycle, `This step is part of a cycle inside the ${container.type} container`);
  }
  return issues;
}

/** Real validation across the actual current flow — walks every real
 * node and edge, not a static/canned list. Returns one FlowIssue per
 * node that has at least one real problem, in flow order. `appType`,
 * when given, adds a real structural check on top of the per-node ones:
 * a workflow app with no Output node, or a chat-style app (chatbot/
 * chatflow/agent) with no node that could ever produce a response —
 * both genuinely reachable dead ends a person could otherwise publish
 * without warning. */
export function validateFlow(nodes: FlowNode[], edges: FlowEdge[], appType?: string | null): FlowIssue[] {
  const issues: FlowIssue[] = [];
  const seenOfType: Record<string, number> = {};
  for (const node of nodes) {
    seenOfType[node.type] = (seenOfType[node.type] || 0) + 1;
    const messages = configIssues(node);
    if (!SKIP_CONNECTIVITY_CHECK.has(node.type) && needsAnEdge(node, nodes)
        && !hasAnyEdge(node.id, edges)) {
      messages.push('This step is not connected to anything');
    }
    if (messages.length) {
      const def = NODE_TYPES[node.type];
      const baseLabel = def?.label ? def.label.charAt(0) + def.label.slice(1).toLowerCase() : node.type;
      issues.push({
        nodeId: node.id, nodeType: node.type,
        label: `${baseLabel} ${seenOfType[node.type]}`,
        icon: def?.icon || 'i-cog', dot: def?.dot,
        messages,
      });
    }
  }
  issues.push(...cycleIssues(nodes, edges));
  if (appType) {
    const trigger = nodes.find((n) => n.type === 'trigger');
    const isChatApp = appType === 'chatbot' || appType === 'chatflow' || appType === 'agent';
    if (trigger) {
      if (!isChatApp && !nodes.some((n) => n.type === 'output')) {
        issues.push({
          nodeId: `${trigger.id}::flow-end`, nodeType: 'trigger', label: 'Flow structure',
          icon: 'i-flag', dot: NODE_TYPES.output?.dot,
          messages: ['Workflow apps should end with an Output node — add one to define the structured result end users will see.'],
        });
      }
      if (isChatApp && !nodes.some((n) => RESPONSE_PRODUCING_TYPES.has(n.type))) {
        issues.push({
          nodeId: `${trigger.id}::flow-end`, nodeType: 'trigger', label: 'Flow structure',
          icon: 'i-comment', dot: NODE_TYPES.llm?.dot,
          messages: ['This chat app has no node that can produce a response (LLM, Agent, Sub-Agent, Code, Template, HTTP, or Tool) — it would answer with silence.'],
        });
      }
    }
  }
  return issues;
}
