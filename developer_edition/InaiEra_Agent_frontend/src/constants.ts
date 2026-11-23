import { P, type Permission } from './permissions';

export interface NodeTypeDef {
  label: string;
  sub: string;
  cls: string;
  locked?: boolean;
  pal?: string;
  dot?: string;
  group?: string;
  icon: string;
}

export const NODE_TYPES: Record<string, NodeTypeDef> = {
  trigger: { label: 'TRIGGER', sub: 'user input', cls: 'trigger', locked: true, icon: 'i-user', dot: '#8A6D3B' },
  // Not in the palette — it is added and removed with the UCS check
  // toggle above the canvas, so there is exactly one or none. But no
  // longer `locked`: a node you chose to add has to be positionable.
  gov: { label: 'UCS CHECK', sub: 'entitlement gate', cls: 'gov', icon: 'i-shield', dot: 'var(--navy)' },
  // Draggable, and no longer `locked`. It was locked and absent from
  // the palette because agent creation always placed one — so the
  // palette showed a greyed-out "Output … on canvas" caption instead
  // of a real entry. Creation now places only the Trigger, which made
  // that caption describe a node that was not there and left no way
  // to get one at all.
  respond: { label: 'OUTPUT', sub: 'to user', cls: 'respond', pal: 'Output (to user)', dot: 'var(--teal-d)', icon: 'i-send' },
  answer: { label: 'ANSWER', sub: 'chatflow response', cls: 'c-teal', pal: 'Answer', dot: 'var(--teal-d)', group: 'Transform', icon: 'i-send' },
  llm: { label: 'LLM', sub: 'compose', cls: 'c-purple', pal: 'LLM', dot: 'var(--purple)', icon: 'i-brain' },
  retrieval: { label: 'KNOWLEDGE RETRIEVAL', sub: 'retrieve docs', cls: 'c-green', pal: 'Knowledge Retrieval', dot: 'var(--green)', icon: 'i-book' },
  agent: { label: 'AGENT', sub: 'agentic strategy', cls: 'c-purple', pal: 'Agent', dot: 'var(--purple)', icon: 'i-agent' },
  classifier: { label: 'QUESTION CLASSIFIER', sub: 'classify intent', cls: 'c-green', pal: 'Question Classifier', dot: 'var(--green)', group: 'Question Understand', icon: 'i-bars' },
  parameter_extractor: { label: 'PARAMETER EXTRACTOR', sub: 'LLM to structured data', cls: 'c-green', pal: 'Parameter Extractor', dot: 'var(--green)', group: 'Question Understand', icon: 'i-braces' },
  condition: { label: 'IF/ELSE', sub: 'branch', cls: 'c-teal', pal: 'IF/ELSE', dot: 'var(--teal-d)', group: 'Logic', icon: 'i-branch' },
  hitl: { label: 'HUMAN INPUT', sub: 'pause for input', cls: 'c-teal', pal: 'Human Input', dot: 'var(--teal-d)', group: 'Logic', icon: 'i-exchange' },
  iteration: { label: 'ITERATION', sub: 'loop over array', cls: 'c-teal', pal: 'Iteration', dot: 'var(--teal-d)', group: 'Logic', icon: 'i-repeat' },
  loop: { label: 'LOOP', sub: 'loop until condition', cls: 'c-teal', pal: 'Loop', dot: 'var(--teal-d)', group: 'Logic', icon: 'i-loop' },
  code: { label: 'CODE', sub: 'sandboxed', cls: 'c-blue', pal: 'Code', dot: 'var(--blue)', group: 'Transform', icon: 'i-code' },
  template: { label: 'TEMPLATE', sub: 'jinja2 transform', cls: 'c-blue', pal: 'Template', dot: 'var(--blue)', group: 'Transform', icon: 'i-split' },
  aggregator: { label: 'VARIABLE AGGREGATOR', sub: 'merge variables', cls: 'c-blue', pal: 'Variable Aggregator', dot: 'var(--blue)', group: 'Transform', icon: 'i-braces' },
  docextractor: { label: 'DOC EXTRACTOR', sub: 'parse file to text', cls: 'c-green', pal: 'Doc Extractor', dot: 'var(--green)', group: 'Transform', icon: 'i-file' },
  list_operator: { label: 'LIST OPERATOR', sub: 'filter, sort, select', cls: 'c-blue', pal: 'List Operator', dot: 'var(--blue)', group: 'Transform', icon: 'i-bars' },
  tool: { label: 'TOOL', sub: 'UCS-catalog connector', cls: 'c-amber', pal: 'Tool / Connector', dot: '#B5650A', group: 'Connect', icon: 'i-plug' },
  http: { label: 'HTTP REQUEST', sub: 'raw HTTP call', cls: 'c-purple', pal: 'HTTP Request', dot: 'var(--purple)', group: 'Connect', icon: 'i-globe' },
  subagent: { label: 'SUB-AGENT', sub: 'invoke platform agent', cls: 'c-slate', pal: 'Sub-Agent', dot: 'var(--slate)', group: 'InAiEra platform', icon: 'i-subagent' },
  memory: { label: 'MEMORY', sub: 'state', cls: 'c-slate', pal: 'Memory / State', dot: 'var(--slate)', group: 'InAiEra platform', icon: 'i-db' },
  variable_assigner: { label: 'VARIABLE ASSIGNER', sub: 'persist conversation vars', cls: 'c-slate', pal: 'Variable Assigner', dot: 'var(--slate)', group: 'InAiEra platform', icon: 'i-repeat' },
  comment: { label: 'COMMENT', sub: 'note', cls: 'c-comment', pal: 'Comment', dot: '#E0A62B', group: 'Annotate', icon: 'i-comment' },
  exit_loop: { label: 'EXIT LOOP', sub: 'stop iterating', cls: 'c-amber', pal: 'Exit Loop', dot: '#B5650A', group: 'Logic', icon: 'i-exit' },
  output: { label: 'OUTPUT VARS', sub: 'named output variables', cls: 'c-teal', pal: 'Output', dot: 'var(--teal-d)', group: 'Transform', icon: 'i-flag' },
};

export const PALETTE_GROUPS: { lbl: string; types: string[] }[] = [
  { lbl: 'Reasoning', types: ['llm', 'retrieval', 'agent'] },
  { lbl: 'Question Understand', types: ['classifier', 'parameter_extractor'] },
  { lbl: 'Logic', types: ['condition', 'hitl', 'iteration', 'loop', 'exit_loop'] },
  { lbl: 'Transform', types: ['code', 'template', 'aggregator', 'docextractor', 'list_operator', 'output', 'answer', 'respond'] },
  { lbl: 'Connect', types: ['tool', 'http'] },
  { lbl: 'InAiEra platform', types: ['subagent', 'memory', 'variable_assigner'] },
  { lbl: 'Annotate', types: ['comment'] },
];

export interface InputFieldTypeDef { value: string; label: string; icon: string; dataType: string }
export const INPUT_FIELD_TYPES: InputFieldTypeDef[] = [
  { value: 'short_text', label: 'Short Text', icon: 'i-text', dataType: 'string' },
  { value: 'paragraph', label: 'Paragraph', icon: 'i-scroll', dataType: 'string' },
  { value: 'select', label: 'Select', icon: 'i-chev', dataType: 'string' },
  { value: 'number', label: 'Number', icon: 'i-hash', dataType: 'number' },
  { value: 'checkbox', label: 'Checkbox', icon: 'i-checkbox', dataType: 'boolean' },
  { value: 'single_file', label: 'Single File', icon: 'i-file', dataType: 'file' },
  { value: 'file_list', label: 'File List', icon: 'i-file', dataType: 'array[file]' },
  { value: 'json_code', label: 'JSON Code', icon: 'i-braces', dataType: 'object' },
];

export interface NavItem { id: string; label: string; icon: string; permission: Permission; badge?: 'agents' | 'openAlerts' }
export interface NavGroup { group: string | null; items: NavItem[] }

// Each entry is shown to a user holding its permission (from GET /api/me),
// never to a named role.
export const NAV: NavGroup[] = [
  {
    group: 'Build', items: [
      { id: 'home', label: 'Overview', icon: 'i-home', permission: P.overviewView },
      { id: 'agents', label: 'Studio', icon: 'i-cube', permission: P.studioEdit, badge: 'agents' },
      { id: 'templates', label: 'Templates', icon: 'i-grid', permission: P.templatesView },
      { id: 'rag', label: 'RAG', icon: 'i-book', permission: P.ragManage },
      { id: 'integrations', label: 'Integrations', icon: 'i-plug', permission: P.integrationsManage },
    ],
  },
  {
    group: 'Operate', items: [
      { id: 'runs', label: 'Runs', icon: 'i-play', permission: P.runsView },
      { id: 'traces', label: 'Traces', icon: 'i-branch', permission: P.tracesView },
      { id: 'monitoring', label: 'Monitoring', icon: 'i-chart', permission: P.monitoringView },
      { id: 'guardrails', label: 'Guardrails', icon: 'i-shield', permission: P.guardrailsManage },
      { id: 'alerts', label: 'Alerts', icon: 'i-bell', permission: P.alertsManage, badge: 'openAlerts' },
    ],
  },
  {
    group: null, items: [
      { id: 'webapps', label: 'Web Apps', icon: 'i-flow', permission: P.webappsUse },
      { id: 'settings', label: 'Settings', icon: 'i-cog', permission: P.settingsEdit },
    ],
  },
];

/** Where a nav entry lives. */
export const navPath = (id: string): string => (id === 'home' ? '/' : `/${id}`);

export const PLATFORM_METRICS_LABELS: { key: string; label: string; icon: string; hint: string }[] = [
  { key: 'success', label: 'Success rate', icon: 'i-check', hint: 'p50/p95 shown alongside' },
  { key: 'costAvg', label: 'Cost per run (avg)', icon: 'i-bolt', hint: 'tokens: 1.2k in / 640 out' },
  { key: 'retries', label: 'Retries per run', icon: 'i-clock', hint: 'avg per run' },
];
