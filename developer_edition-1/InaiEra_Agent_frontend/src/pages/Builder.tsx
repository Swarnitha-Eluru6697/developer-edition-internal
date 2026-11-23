import { useEffect, useRef, useState } from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { api, ApiError } from '../api';
import Icon from '../components/Icon';
import NodeConfigPanel from '../components/NodeConfigPanel';
import NodePicker, { type NodePickerTarget } from '../components/NodePicker';
import TryItModal from '../components/TryItModal';
import { PageHead, Banner } from '../components/ui';
import { NODE_TYPES, PALETTE_GROUPS } from '../constants';
import { getCommentColor, COMMENT_FONT_SIZES, renderCommentMarkdown } from '../commentFormatting';
import { validateFlow } from '../flowValidation';
import { useApp } from '../context/AppContext';
import { P } from '../permissions';
import type { Agent, FlowNode } from '../types';

const NODE_W = 128;
const NODE_H = 54; // matches the visual height assumed by the old wire offset (y+27)
const CLICK_DRAG_THRESHOLD = 6; // px of movement below which a port press counts as a click, not a drag

interface OutputPort { handle: string | null; x: number; y: number; label?: string; cls: string }

function outputPorts(n: FlowNode): OutputPort[] {
  if (n.type === 'respond' || n.type === 'comment' || n.type === 'exit_loop') return []; // terminal / non-executable, no output
  if (n.type === 'condition') {
    return [
      { handle: 'true', x: n.x + NODE_W, y: n.y + 14, label: 'T', cls: 'true' },
      { handle: 'false', x: n.x + NODE_W, y: n.y + NODE_H - 14, label: 'F', cls: 'false' },
    ];
  }
  // A code node's own second port — real, not decorative: only appears
  // once "Fail branch" is actually selected (NodeConfigPanel's code
  // case), matching handle_code/compute_next_node_id's real routing on
  // a genuine nonzero exit code (harness.py) — the same real two-port
  // mechanism `condition` uses above, not a separate one.
  if (n.type === 'code' && n.config?.error_handling === 'Fail branch') {
    return [
      { handle: 'success', x: n.x + NODE_W, y: n.y + 14, label: 'OK', cls: 'true' },
      { handle: 'error', x: n.x + NODE_W, y: n.y + NODE_H - 14, label: 'ERR', cls: 'false' },
    ];
  }
  // The LLM node's own real second port (§0.64, GAP_REPORT) — the
  // exact same real mechanism as the code node just above, now that
  // handle_llm/compute_next_node_id genuinely route a real provider
  // failure down a distinct port when "Fail branch" is configured,
  // instead of the config panel's own error_handling control being
  // inert.
  if (n.type === 'llm' && n.config?.error_handling === 'Fail branch') {
    return [
      { handle: 'success', x: n.x + NODE_W, y: n.y + 14, label: 'OK', cls: 'true' },
      { handle: 'error', x: n.x + NODE_W, y: n.y + NODE_H - 14, label: 'ERR', cls: 'false' },
    ];
  }
  // `loop` is a real container now — its body is whatever nodes have
  // parent_id === this loop's id, wired among THEMSELVES with their own
  // real edges (including their own condition branches, ending at a real
  // `exit_loop` node). The loop node itself just needs one regular
  // downstream edge, same as any other node, for "what runs after the
  // whole loop is done."
  return [{ handle: null, x: n.x + NODE_W, y: n.y + NODE_H / 2, cls: '' }];
}

function inputPoint(n: FlowNode) {
  return { x: n.x, y: n.y + NODE_H / 2 };
}

interface LoopBox { x: number; y: number; w: number; h: number }

function loopBoundingBox(loopNode: FlowNode, nodes: FlowNode[]): LoopBox {
  const children = nodes.filter((n) => n.parent_id === loopNode.id);
  const all = [loopNode, ...children];
  const minX = Math.min(...all.map((n) => n.x)) - 24;
  const minY = Math.min(...all.map((n) => n.y)) - 54; // extra top padding for the LOOP label
  const maxX = Math.max(...all.map((n) => n.x + NODE_W)) + 24;
  const maxY = Math.max(...all.map((n) => n.y + NODE_H)) + 24;
  // an empty (or near-empty) loop would otherwise render a box barely
  // bigger than the loop node itself — a target too small to reliably
  // hit with a drag-and-drop, which was a real contributor to loop
  // children silently landing outside the container
  const MIN_W = 320, MIN_H = 220;
  return { x: minX, y: minY, w: Math.max(maxX - minX, MIN_W), h: Math.max(maxY - minY, MIN_H) };
}

// A real, deliberately SEPARATE twin of loopBoundingBox for the
// Iteration node (§0.35, GAP_REPORT) — `loop` itself stays completely
// untouched, per explicit instruction, even though both container types
// share the same "bounding box around real parent_id children" idea.
function iterationBoundingBox(iterationNode: FlowNode, nodes: FlowNode[]): LoopBox {
  const children = nodes.filter((n) => n.parent_id === iterationNode.id);
  const all = [iterationNode, ...children];
  const minX = Math.min(...all.map((n) => n.x)) - 24;
  const minY = Math.min(...all.map((n) => n.y)) - 54;
  const maxX = Math.max(...all.map((n) => n.x + NODE_W)) + 24;
  const maxY = Math.max(...all.map((n) => n.y + NODE_H)) + 24;
  const MIN_W = 320, MIN_H = 220;
  return { x: minX, y: minY, w: Math.max(maxX - minX, MIN_W), h: Math.max(maxY - minY, MIN_H) };
}

function pointInBox(x: number, y: number, box: LoopBox) {
  return x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h;
}

function bezierPath(x1: number, y1: number, x2: number, y2: number) {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

const EDGE_COLOR: Record<string, string> = { '': '#8A97A8', true: '#1E8E6A', false: '#C0392B', body: '#0B6459', exit: '#5A6478' };

interface Connecting { sourceId: string; handle: string | null; x: number; y: number; startX: number; startY: number }

export default function Builder() {
  const [params] = useSearchParams();
  const agentId = params.get('agent') || 'a1';
  const { openModal, openDrawer, toast, dataVersion, can, permissionsStatus } = useApp();
  const mayRun = can(P.agentsRun);
  const mayPublish = can(P.agentsPublish);
  const [agent, setAgent] = useState<Agent | null>(null);
  // Real optimistic-locking conflict handling (§0.71, GAP_REPORT) — a
  // real 409 means another tab's write landed first; the achievable,
  // honest UX here is a real refresh to the current state plus a clear
  // toast, not a full merge UI (a real, separate, larger piece of work
  // this fix doesn't take on). The person's own just-attempted change
  // is genuinely lost (never applied — the whole point of a 409 is
  // that it wasn't), but they're never left looking at stale data
  // silently overwritten without their knowledge, which is the actual
  // reported bug.
  const handleMutationError = (err: unknown) => {
    if (err instanceof ApiError && err.status === 409 && agentId) {
      toast("Someone else's change landed first — refreshed to the latest version. Please reapply your edit.");
      api.getAgent(agentId).then(setAgent).catch(() => {});
      return;
    }
    toast(err instanceof Error ? err.message : String(err));
  };
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dropOk, setDropOk] = useState(false);
  const [connecting, setConnecting] = useState<Connecting | null>(null);
  const [picker, setPicker] = useState<NodePickerTarget | null>(null);
  const [tryItOpen, setTryItOpen] = useState(false);
  const [checklistOpen, setChecklistOpen] = useState(false);
  const [publishedSnippets, setPublishedSnippets] = useState<Agent[]>([]);
  const canvasRef = useRef<HTMLDivElement>(null);

  const load = () => api.getAgent(agentId).then((a) => {
    setAgent(a);
    if (!selectedId && a.flow.nodes.length) {
      const llm = a.flow.nodes.find((n) => n.type === 'llm');
      setSelectedId(llm ? llm.id : a.flow.nodes[0].id);
    }
  }).catch((e) => toast(e.message));

  // dataVersion as well as agentId: publishing happens in PublishModal,
  // which ModalHost renders rather than this page, so the status chip
  // here had no way to learn the agent stopped being a draft.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [agentId, dataVersion]);

  // Entering a published agent's playground leads with the real end-user
  // experience rather than the node canvas — but ONCE, and no longer
  // from inside load().
  //
  // It used to be set there, which meant every call reopened it: a page
  // refresh is a fresh mount, so the panel came back every single time,
  // and once load() started running on dataVersion it would also have
  // reopened the moment anything published. "Leads with the app the
  // first time you open it" and "reappears whenever anything refetches"
  // are very different behaviours, and only the first was intended.
  //
  // sessionStorage, not state: it has to survive a reload (that is the
  // complaint) while still leading with the app in a genuinely new
  // session. Wrapped because a private window or blocked site data
  // makes these throw, and a dead storage API must not take the
  // Builder down with it.
  const tryItSeenKey = `tryit-shown:${agentId}`;
  const tryItDecidedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!agent) return;                       // still loading
    if (permissionsStatus === 'loading') return; // decide once /api/me has answered
    // Decided once per agentId, on arrival. Keying this on the status
    // alone would also fire the moment the agent BECAME published —
    // which is publishing, not entering — and the panel then opened on
    // top of the publish dialog that had just been used.
    if (tryItDecidedFor.current === agentId) return;
    tryItDecidedFor.current = agentId;
    if (agent.status !== 'published') return;
    if (!mayRun) return;                      // no Try it without agent:agents:run
    let alreadyShown = false;
    try {
      alreadyShown = sessionStorage.getItem(tryItSeenKey) === '1';
    } catch { alreadyShown = false; }
    if (alreadyShown) return;
    try { sessionStorage.setItem(tryItSeenKey, '1'); } catch { /* not worth failing over */ }
    setTryItOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, agent, permissionsStatus]);
  // Real published snippets available to insert into THIS (non-
  // snippet) workflow — matching Dify's own documented "Only published
  // snippets appear here."
  useEffect(() => { api.listAgents('Snippet').then((all) => setPublishedSnippets(all.filter((s) => s.status === 'published'))).catch(() => {}); }, [agentId]);

  if (!agent) return <div className="wrap"><p className="muted">Loading…</p></div>;

  const nodes = agent.flow.nodes;
  const edges = agent.flow.edges || [];
  const nodesById: Record<string, FlowNode> = Object.fromEntries(nodes.map((n) => [n.id, n]));

  const canvasPoint = (clientX: number, clientY: number) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return { x: clientX - rect.left + canvasRef.current!.scrollLeft, y: clientY - rect.top + canvasRef.current!.scrollTop };
  };

  const onDropCanvas = (e: React.DragEvent) => {
    e.preventDefault();
    setDropOk(false);
    const raw = e.dataTransfer.getData('text/plain');
    if (!raw) return;
    const rect = canvasRef.current!.getBoundingClientRect();
    const x = Math.max(10, Math.round(e.clientX - rect.left + canvasRef.current!.scrollLeft - 64));
    const y = Math.max(10, Math.round(e.clientY - rect.top + canvasRef.current!.scrollTop - 28));

    if (raw.startsWith('new:')) {
      const type = raw.slice(4);
      // dropping a node inside an existing loop's rendered box makes it a
      // real body child (parent_id) — except loops can't nest inside
      // loops in this version, and comments never execute so they're
      // never containers or children either
      const containingLoop = type !== 'loop'
        ? nodes.find((n) => n.type === 'loop' && pointInBox(x + NODE_W / 2, y + NODE_H / 2, loopBoundingBox(n, nodes)))
        : undefined;
      // Same real containment idea, for the real, separate Iteration
      // container (§0.35, GAP_REPORT) — `loop`'s own check above is
      // untouched; this is an additive, parallel check, not a
      // modification of it.
      const containingIteration = type !== 'iteration' && !containingLoop
        ? nodes.find((n) => n.type === 'iteration' && pointInBox(x + NODE_W / 2, y + NODE_H / 2, iterationBoundingBox(n, nodes)))
        : undefined;
      const container = containingLoop || containingIteration;
      api.addNode(agent.id, { type, x, y, sub: NODE_TYPES[type]?.sub || '', parent_id: container?.id ?? null }, agent.draft_revision).then((updated) => {
        setAgent(updated);
        const added = updated.flow.nodes[updated.flow.nodes.length - 1];
        setSelectedId(added.id);
        toast(container
          ? `${NODE_TYPES[type]?.pal || type} node added inside ${container.sub || (containingLoop ? 'the loop' : 'the iteration')} — wire it into the body`
          : `${NODE_TYPES[type]?.pal || type} node added — drag from its dot to connect it`);
      }).catch(handleMutationError);
    } else if (raw.startsWith('move:')) {
      const nodeId = raw.slice(5);
      api.moveNode(agent.id, nodeId, x, y, agent.draft_revision).then(setAgent).catch(handleMutationError);
      setSelectedId(nodeId);
    } else if (raw.startsWith('snippet:')) {
      const snippetId = raw.slice(8);
      const snippet = publishedSnippets.find((s) => s.id === snippetId);
      api.insertSnippet(agent.id, snippetId, x, y, agent.draft_revision).then((updated) => {
        setAgent(updated);
        toast(`${snippet?.name || 'Snippet'} inserted — a real, independent copy; editing the original snippet won't change this`);
      }).catch(handleMutationError);
    }
  };

  const hasGovNode = nodes.some((n) => n.type === 'gov');
  // Absent `enabled` means on: a gov node placed before this switch
  // existed had no such field, and those flows were checking.
  const govEnabled = (n: FlowNode) => (n.config as any)?.enabled !== false;

  const onToggleGovNode = (n: FlowNode, enabled: boolean) => {
    api.updateNodeConfig(agent.id, n.id, { ...(n.config || {}), enabled }, agent.draft_revision)
      .then((updated) => {
        setAgent(updated);
        toast(enabled
          ? 'UCS check on — it runs as its own step, so a denial shows up in the trace'
          : 'UCS check stood down — entitlements are still checked before every run, just not as a step here');
      })
      .catch(handleMutationError);
  };

  // Adds the node to a flow that predates it being scaffolded.
  const onAddUcsCheck = () => {
    api.setUcsCheck(agent.id, true, agent.draft_revision)
      .then((updated) => { setAgent(updated); toast('UCS check added after the Trigger'); })
      .catch(handleMutationError);
  };

  const onDeleteNode = (nodeId: string) => {
    api.deleteNode(agent.id, nodeId, agent.draft_revision).then((updated) => { setAgent(updated); setSelectedId(null); toast('Node deleted'); })
      .catch(handleMutationError);
  };

  const onPortMouseDown = (e: React.MouseEvent, sourceId: string, handle: string | null, portX: number, portY: number) => {
    e.stopPropagation();
    e.preventDefault();
    setConnecting({ sourceId, handle, x: portX, y: portY, startX: portX, startY: portY });
  };

  const onCanvasMouseMove = (e: React.MouseEvent) => {
    if (!connecting) return;
    const p = canvasPoint(e.clientX, e.clientY);
    setConnecting((c) => (c ? { ...c, x: p.x, y: p.y } : c));
  };

  const onCanvasMouseUp = (e: React.MouseEvent) => {
    if (!connecting) return;
    const p = canvasPoint(e.clientX, e.clientY);
    const movedDist = Math.hypot(p.x - connecting.startX, p.y - connecting.startY);
    setConnecting(null);

    if (movedDist < CLICK_DRAG_THRESHOLD) {
      // a click, not a drag: open the node picker to spawn a NEW node
      // already wired to this port, at the cursor's on-screen position
      setPicker({ sourceId: connecting.sourceId, handle: connecting.handle, x: e.clientX, y: e.clientY });
      return;
    }

    const targetEl = (e.target as HTMLElement).closest('[data-nodeid]') as HTMLElement | null;
    const targetId = targetEl?.dataset.nodeid;
    if (targetId && targetId !== connecting.sourceId) {
      const targetNode = nodesById[targetId];
      if (targetNode.type === 'trigger') {
        toast("Trigger has no input — it's always the start of the flow");
      } else if (targetNode.type === 'comment') {
        toast("A comment is documentation, not a flow step — it can't be wired into the execution path");
      } else {
        api.addEdge(agent.id, connecting.sourceId, targetId, connecting.handle, agent.draft_revision).then(setAgent).catch(handleMutationError);
      }
    }
  };

  const onPickNodeType = (type: string) => {
    if (!picker || !agent) return;
    const sourceNode = nodesById[picker.sourceId];
    // place the new node to the right of the source, offset vertically for
    // a condition branch so True/False don't land on top of each other
    const yOffset = picker.handle === 'true' ? -70 : picker.handle === 'false' ? 70 : 0;
    // clicking a LOOP's own port (its single output) starts that loop's
    // real body — the new node becomes its first child. Clicking a port
    // on a node that's ITSELF already a loop child continues that same
    // body chain, inheriting the same parent. This was the actual gap:
    // node creation via the port-click picker never set parent_id at all,
    // so a loop's body never really formed no matter how it was grown —
    // only the palette drag-and-drop path had containment detection.
    // Same real idea, additively extended to the real, separate
    // Iteration container (§0.35, GAP_REPORT) — the loop branch above is
    // untouched.
    const parentId = sourceNode.type === 'loop' || sourceNode.type === 'iteration' ? sourceNode.id : (sourceNode.parent_id ?? null);
    const xGap = parentId ? 160 : 220; // tighter spacing keeps a loop's rendered box compact instead of sprawling
    const x = sourceNode.x + xGap;
    const y = Math.max(10, sourceNode.y + yOffset);

    api.addNode(agent.id, { type, x, y, sub: NODE_TYPES[type]?.sub || '', parent_id: parentId }, agent.draft_revision).then((updated) => {
      const added = updated.flow.nodes[updated.flow.nodes.length - 1];
      return api.addEdge(agent.id, picker.sourceId, added.id, picker.handle, updated.draft_revision).then((withEdge) => {
        setAgent(withEdge);
        setSelectedId(added.id);
        toast(parentId
          ? `${NODE_TYPES[type]?.pal || type} node added inside the loop and connected`
          : `${NODE_TYPES[type]?.pal || type} node added and connected`);
      });
    }).catch(handleMutationError).finally(() => setPicker(null));
  };

  const onDeleteEdge = (edgeId: string) => {
    api.deleteEdge(agent.id, edgeId, agent.draft_revision).then((updated) => { setAgent(updated); toast('Connection removed'); })
      .catch(handleMutationError);
  };

  const selectedNode = agent.flow.nodes.find((n) => n.id === selectedId);
  const connectedSourceHandles = new Set(edges.map((e) => `${e.source}::${e.source_handle ?? ''}`));
  const flowIssues = validateFlow(agent.flow.nodes, edges, agent.type === 'App' ? agent.app_type : null);

  return (
    <div className="wrap wide">
      <PageHead
        title={agent.name}
        eyebrow="Studio"
        // The parent is a real link back to the agent list — it used to be
        // plain text, so the obvious way back did nothing. Named after the
        // page it returns to (Studio), not the old "Agents".
        crumb={<><Link to="/agents" className="crumb crumb-link">Studio</Link><span className="crumb"> /</span> {agent.name}</>}
        subtitle="Drag a node from the palette to add it · drag a node to move it · drag from its dot to connect it to another · click a connection to remove it"
        actions={<>
          <span className={`chip ${agent.status === 'published' ? 'green' : agent.status === 'staged' ? 'amber' : 'slate'}`}>{agent.status}</span>
          <button className="btn ghost" title="Environment Variables" onClick={() => openDrawer('envVars', agent.id)}><Icon name="i-key" /> Env</button>
          {mayRun && <button className="btn ghost" onClick={() => setTryItOpen(true)}><Icon name="i-play" /> Try it</button>}
          <button className="btn ghost checklist-btn" title="Checklist" onClick={() => setChecklistOpen((v) => !v)}>
            <Icon name="i-check" />
            {flowIssues.length > 0 && <span className="checklist-badge">{flowIssues.length}</span>}
          </button>
          {mayPublish && <button className="btn" onClick={() => openModal('publish', agent.id)}><Icon name="i-check" /> Publish</button>}
        </>}
      />
      {checklistOpen && (
        <div className="checklist-panel">
          <div className="dh"><h3>Checklist({flowIssues.length})</h3><button className="x" onClick={() => setChecklistOpen(false)}><Icon name="i-x" /></button></div>
          {flowIssues.length === 0
            ? <p className="small muted" style={{ padding: '0 20px 16px' }}>No issues found — this flow is ready to publish.</p>
            : <>
                <p className="small muted" style={{ padding: '0 20px 4px' }}>Resolve the following issues before publishing</p>
                <div className="checklist-items">
                  {flowIssues.map((issue) => (
                    <div className="checklist-item" key={issue.nodeId} onClick={() => { setSelectedId(issue.nodeId); setChecklistOpen(false); }}>
                      <span className="checklist-item-icon" style={{ background: issue.dot }}><Icon name={issue.icon} /></span>
                      <div>
                        <b>{issue.label}</b>
                        {issue.messages.map((m, i) => <div className="checklist-item-msg" key={i}>{m}</div>)}
                      </div>
                    </div>
                  ))}
                </div>
              </>}
        </div>
      )}

      <div className="builder-shell">
        <div className="pal">
          <div className="cmd" style={{ marginBottom: 12 }}><Icon name="i-search" /><input placeholder="Search nodes…" /></div>
          {agent.plane !== 'Snippet' && (
            <div className="palnode" style={{ cursor: 'default', opacity: 0.6 }}>
              <span className="pal-icon" style={{ background: NODE_TYPES.trigger.dot }}><Icon name={NODE_TYPES.trigger.icon} /></span>Trigger <span className="small muted" style={{ marginLeft: 'auto' }}>always on canvas</span>
            </div>
          )}
          {PALETTE_GROUPS.map((g) => {
            // A real, deliberate restriction while editing a snippet
            // (§0.38, GAP_REPORT), matching Dify's own documented rule
            // exactly: "Knowledge Retrieval and Human Input aren't
            // supported yet either" — a snippet plugs into a workflow
            // instead of running on its own, so these don't apply.
            const types = agent.plane === 'Snippet' ? g.types.filter((t) => t !== 'retrieval' && t !== 'hitl') : g.types;
            if (types.length === 0) return null;
            return (
              <div key={g.lbl}>
                <div className="lbl">{g.lbl}</div>
                {types.map((t) => (
                  <div
                    key={t}
                    className="palnode"
                    draggable
                    onDragStart={(e) => e.dataTransfer.setData('text/plain', `new:${t}`)}
                  >
                    <span className="pal-icon" style={{ background: NODE_TYPES[t].dot }}><Icon name={NODE_TYPES[t].icon} /></span>{NODE_TYPES[t].pal}
                    <a
                      className="pal-doc-icon"
                      href={`#/docs/${t}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={`View documentation for ${NODE_TYPES[t].pal}`}
                      onClick={(e) => e.stopPropagation()}
                      draggable={false}
                    >
                      <Icon name="i-help" />
                    </a>
                  </div>
                ))}
              </div>
            );
          })}
          {agent.plane !== 'Snippet' && publishedSnippets.length > 0 && (
            <div>
              <div className="lbl">Snippets</div>
              {publishedSnippets.map((s) => (
                <div
                  key={s.id}
                  className="palnode"
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData('text/plain', `snippet:${s.id}`)}
                  title={`Insert a real, independent copy of ${s.name}`}
                >
                  <span className="pal-icon" style={{ background: 'var(--slate)' }}><Icon name="i-braces" /></span>{s.name}
                </div>
              ))}
            </div>
          )}
          <hr className="sep" />
          {agent.plane === 'Snippet' ? (
            <Banner kind="info">A snippet has no Start or End node of its own — declare Input Variables instead (select nothing on the canvas to edit them), then insert this into a workflow once published.</Banner>
          ) : (
            <div className="field" style={{ marginTop: 4 }}>
              {/* The switch lives on the node now, not here. This is only
                  the way back for a flow created before the node was
                  placed on every canvas. */}
              {hasGovNode ? (
                <div className="small muted" style={{ lineHeight: 1.45 }}>
                  The <b>UCS CHECK</b> node is on the canvas after the Trigger. Use the switch on the
                  node to stand it down — entitlements are still checked before every run either way.
                </div>
              ) : (
                <>
                  <button className="btn sm ghost" onClick={onAddUcsCheck}>
                    <Icon name="i-shield" /> Add UCS check
                  </button>
                  <div className="small muted" style={{ marginTop: 6, lineHeight: 1.45 }}>
                    This flow has no UCS check node. Entitlements are still checked before every run;
                    adding it makes a denial visible as its own step.
                  </div>
                </>
              )}
            </div>
          )}
        </div>

        <div
          ref={canvasRef}
          className={`canvas${dropOk ? ' dropok' : ''}`}
          style={connecting ? { cursor: 'crosshair' } : undefined}
          onDragOver={(e) => { e.preventDefault(); setDropOk(true); }}
          onDragLeave={() => setDropOk(false)}
          onDrop={onDropCanvas}
          onMouseMove={onCanvasMouseMove}
          onMouseUp={onCanvasMouseUp}
          onClick={(e) => { if (e.target === canvasRef.current) setSelectedId(null); }}
        >
          <svg className="canvas-svg">
            <defs>
              <marker id="arrow-default" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#8A97A8" />
              </marker>
            </defs>
            {edges.map((e) => {
              const source = nodesById[e.source];
              const target = nodesById[e.target];
              if (!source || !target) return null;
              const ports = outputPorts(source);
              const fromPort = ports.find((p) => p.handle === (e.source_handle ?? null)) || ports[0];
              if (!fromPort) return null;
              const to = inputPoint(target);
              const path = bezierPath(fromPort.x, fromPort.y, to.x, to.y);
              const color = EDGE_COLOR[e.source_handle || ''] || EDGE_COLOR[''];
              return (
                <g key={e.id}>
                  <path d={path} className="edge-hit" onClick={() => onDeleteEdge(e.id)} />
                  <path d={path} className={`edge-line ${e.source_handle || ''}`} style={{ stroke: color }} />
                </g>
              );
            })}
            {connecting && (() => {
              const source = nodesById[connecting.sourceId];
              const ports = source ? outputPorts(source) : [];
              const fromPort = ports.find((p) => p.handle === connecting.handle) || { x: connecting.x, y: connecting.y };
              return <path d={bezierPath(fromPort.x, fromPort.y, connecting.x, connecting.y)} className="connecting-line" />;
            })()}
          </svg>

          {/* Real containers — a loop's rendered box is its own bounding
              rect around its actual children, not decoration. Rendered
              behind the node divs so children visually sit inside it. */}
          {nodes.filter((n) => n.type === 'loop').map((loopNode) => {
            const box = loopBoundingBox(loopNode, nodes);
            return (
              <div key={`box-${loopNode.id}`} className="loop-container" style={{ left: box.x, top: box.y, width: box.w, height: box.h }}>
                <div className="loop-container-label"><Icon name="i-cog" /> {loopNode.sub || 'Loop'}</div>
              </div>
            );
          })}

          {/* Same real container idea for the real, separate Iteration
              node (§0.35, GAP_REPORT) — a fully separate block, the loop
              one above is untouched. */}
          {nodes.filter((n) => n.type === 'iteration').map((iterNode) => {
            const box = iterationBoundingBox(iterNode, nodes);
            return (
              <div key={`box-${iterNode.id}`} className="loop-container" style={{ left: box.x, top: box.y, width: box.w, height: box.h }}>
                <div className="loop-container-label"><Icon name="i-repeat" /> {iterNode.sub || 'Iteration'}</div>
              </div>
            );
          })}

          {nodes.map((n) => {
            const nt = NODE_TYPES[n.type];
            if (n.type === 'comment') {
              const color = getCommentColor(n.config.color);
              const fontSize = COMMENT_FONT_SIZES[n.config.font_size as string] || COMMENT_FONT_SIZES.small;
              return (
                <div
                  key={n.id}
                  data-nodeid={n.id}
                  className={`comment-note${selectedId === n.id ? ' sel' : ''}`}
                  style={{ left: n.x, top: n.y, background: color.bg, borderColor: color.border }}
                  draggable
                  onClick={() => setSelectedId(n.id)}
                  onDragStart={(e) => e.dataTransfer.setData('text/plain', `move:${n.id}`)}
                >
                  <div className="comment-title">{n.config.title || 'Comment'}</div>
                  {n.config.body ? (
                    <div className="comment-body" style={{ fontSize }} dangerouslySetInnerHTML={{ __html: renderCommentMarkdown(n.config.body) }} />
                  ) : (
                    <div className="comment-body" style={{ fontSize }}>Click to add notes explaining this part of the flow.</div>
                  )}
                </div>
              );
            }
            return (
              <div
                key={n.id}
                data-nodeid={n.id}
                className={`cnode ${nt.cls}${nt.locked ? ' locked' : ''}${selectedId === n.id ? ' sel' : ''}${n.type === 'gov' && !govEnabled(n) ? ' off' : ''}`}
                style={{ left: n.x, top: n.y }}
                // Not draggable for trigger/gov/respond, and that mirrors
                // the server rather than inventing a restriction: move_node
                // refuses those three outright ("This node is fixed and
                // cannot be moved (§4.4)"). Making them draggable here just
                // turns a dead drag into a 400 and an error toast.
                draggable={!nt.locked}
                onClick={() => setSelectedId(n.id)}
                onDragStart={(e) => { if (nt.locked) { e.preventDefault(); return; } e.dataTransfer.setData('text/plain', `move:${n.id}`); }}
              >
                <div className="cnode-top">
                  <span className="cnode-icon" style={{ background: nt.dot || 'var(--slate)' }}><Icon name={nt.icon} /></span>
                  <div className="tt">{nt.label}</div>
                </div>
                <div className="ds">{n.sub || nt.sub}</div>
                {/* The UCS check is placed on every canvas rather than
                    dragged in, so its switch belongs on the node: it is
                    stood down in place instead of deleted, which keeps the
                    graph intact and makes its state visible where the node
                    is. stopPropagation on both events because the node
                    itself is clickable (selects) and draggable. */}
                {n.type === 'gov' && (
                  <label
                    className="cnode-switch"
                    title={govEnabled(n)
                      ? 'On — the check runs here, as its own step'
                      : 'Off — the check still runs before the flow starts, just not as a step here'}
                    onClick={(e) => e.stopPropagation()}
                    onDragStart={(e) => { e.preventDefault(); e.stopPropagation(); }}
                  >
                    {/* The app's own switch control (.switch, as used
                        throughout the Features panel) rather than a bare
                        checkbox, in a size that fits a node card — a
                        toggle should look like the other toggles. */}
                    <span className="switch xs">
                      <input
                        type="checkbox"
                        checked={govEnabled(n)}
                        onChange={(e) => onToggleGovNode(n, e.target.checked)}
                      />
                      <span />
                    </span>
                    <span className="cnode-switch-label">{govEnabled(n) ? 'On' : 'Off'}</span>
                  </label>
                )}
              </div>
            );
          })}

          {nodes.flatMap((n) => outputPorts(n).map((p) => {
            const connected = connectedSourceHandles.has(`${n.id}::${p.handle ?? ''}`);
            const branchLabel = n.type === 'condition' ? `the ${p.label === 'T' ? 'True' : 'False'} branch` : null;
            // Real fan-out is now genuinely supported (a port can feed
            // more than one downstream node), so the hint stays visible
            // and discoverable even once connected — just worded
            // differently, since dragging again here now adds a real
            // second branch rather than replacing the first.
            const hint = connected
              ? (branchLabel ? `Drag to add another connection from ${branchLabel}` : 'Drag to add another connection')
              : (branchLabel ? `Drag to connect ${branchLabel}` : 'Drag to connect');
            return (
              <div key={`${n.id}-${p.handle ?? 'out'}`} className="port-wrap" style={{ position: 'absolute', left: p.x - 9, top: p.y - 9 }}>
                <div
                  className={`port ${p.cls}${connected ? ' connected' : ' unconnected'}`}
                  data-nodeid={n.id}
                  onMouseDown={(e) => onPortMouseDown(e, n.id, p.handle, p.x, p.y)}
                />
                {p.label && <div className="port-label" style={{ left: -14, top: -1 }}>{p.label}</div>}
                <div className="port-hint" style={{ left: 20, top: -6 }}>{hint}</div>
              </div>
            );
          }))}
        </div>

        <NodeConfigPanel agent={agent} node={selectedNode} onAgentUpdate={setAgent} onDelete={onDeleteNode} />
      </div>

      {picker && <NodePicker target={picker} onPick={onPickNodeType} onClose={() => setPicker(null)} />}
      {tryItOpen && mayRun && <TryItModal agent={agent} onClose={() => setTryItOpen(false)} />}
    </div>
  );
}
