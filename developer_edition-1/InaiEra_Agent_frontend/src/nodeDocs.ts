// Real per-node documentation — written to accurately describe THIS
// platform's own actual, verified behavior (not copied from Dify or
// any other source). Every node type in constants.ts's NODE_TYPES has
// an entry here. Markdown is rendered by docsMarkdown.ts.

export interface NodeDoc {
  title: string;
  summary: string;
  body: string; // Markdown
}

export const NODE_DOCS: Record<string, NodeDoc> = {
  trigger: {
    title: 'Trigger',
    summary: 'The entry point of every flow — where a run begins.',
    body: `
Every flow has exactly one Trigger node, automatically placed when the flow is created. It can't be deleted or added a second time.

## Trigger types

- **User Input** — the most common case. The person (or a calling API) provides a single input string, or a set of structured fields you define.
- **Webhook** — the flow starts when an external system POSTs to this agent's real webhook URL. The raw JSON body is exposed to downstream nodes as \`{{webhook_payload}}\`.
- **Scheduled** — runs on a real cron-style schedule you configure.

## Structured input fields

If you define input fields on the Trigger (name, type, whether it's required, a default value), each one becomes a real, independently referenceable variable downstream — \`{{field_name}}\` — instead of everything being collapsed into a single input string. This is optional; a flow with no fields defined behaves exactly like a plain single-input call.

## Output variables

| Variable | Type | Description |
|---|---|---|
| \`{{input}}\` | string | The raw original input, always available everywhere downstream |
| \`{{trigger_input}}\` | string | Same as \`{{input}}\`, under an explicit name |
| \`{{trigger_type}}\` | string | Which trigger type actually fired: \`user_input\`, \`webhook\`, or \`scheduled\` |
| \`{{webhook_payload}}\` | object | Only present on a webhook-triggered run, if the body was valid JSON |
| \`{{field_name}}\` | varies | One per configured structured input field |
`.trim(),
  },

  gov: {
    title: 'UCS Check',
    summary: 'The automatic entitlement gate — not a node you drag in.',
    body: `
A UCS Check is inserted automatically right after the Trigger on every flow. You never add one manually, and it never appears in the node palette.

## What it does

Before any node that touches a real catalog object (a Tool/Connector or a Knowledge Retrieval) executes, this check resolves whether the invoking user is actually entitled to use every object the flow could reach. It calls UCS's real \`/api/v1/authz/check\` — this is a genuine external authorization decision, not a local table lookup.

## Soft vs. hard denial

By default, a denial here is a hard stop — the run fails with a clear \`UCSDenied\` error naming exactly which object was denied. If your deployment has soft-check mode enabled, a denial is recorded in \`{{ucs_soft_denials}}\` instead, and the flow continues (later nodes may still fail on their own if they genuinely need the denied object).

## Nothing to configure

There's no config panel for this node beyond a read-only note — its behavior is entirely determined by what the rest of the flow actually references.
`.trim(),
  },

  respond: {
    title: 'Output',
    summary: "Delivers a Workflow app's final result. Automatically placed; can't be added manually.",
    body: `
Every Workflow-type flow ends with an Output node. Like the Trigger, it's placed automatically and can't be added a second time — this is the Workflow-app counterpart to the Answer node's role in Chatflow apps.

## Format

Choose how the final result should be interpreted by the end-user UI:

- **Plain text** — the raw text, no markdown parsing.
- **Markdown** — rendered as real formatted markdown (headings, lists, bold, links, etc.).
- **JSON** — the result is treated as structured data and shown accordingly.

## Streaming

When enabled, the response streams to the end user as it's produced rather than arriving all at once — matching the real per-node \`node_start\`/\`node_done\` events already emitted during execution.

## What actually gets sent

The Output node sends whatever the last node before it set as \`ctx["output"]\` — typically an LLM's generated text, a Template node's rendered string, or an Output Vars node's structured fields. If nothing upstream ever set an output, this node has nothing real to send.
`.trim(),
  },

  answer: {
    title: 'Answer',
    summary: 'Defines what content streams to the user in Chatflow apps.',
    body: `
The Answer node is the Chatflow-app equivalent of the Output node — Workflow apps use Output, Chatflow apps use Answer. Unlike Output, a flow can contain **more than one** Answer node.

## Content

A plain text editor where you write the response, referencing any upstream variable with \`{{var}}\` — including \`{{input}}\` for the original message. Real interpolation: whatever you write is genuinely substituted against the flow's actual variables at run time.

## Multiple Answer nodes

Each Answer node's resolved content is appended to a running transcript, not overwritten — so several Answer nodes at different points in a flow build up one coherent reply delivered to the user in the order they actually execute.

## Multimodal references

If a referenced variable resolves to an uploaded file (the same shape produced by a trigger's file field or a Doc Extractor result), it renders as a real clickable link rather than a raw object dump.

## A stated scope boundary

Streaming here is real, but node-granular: a whole Answer node's content becomes visible once it's fully resolved, not streamed character-by-character mid-string the way some chat UIs render token-by-token.
`.trim(),
  },

  llm: {
    title: 'LLM',
    summary: 'Calls a language model with your own System/User prompts.',
    body: `
The core reasoning node. Makes a real call to the configured model — a genuine API call when \`ANTHROPIC_API_KEY\` is set, an honestly-labeled mock response (\`"mock": true\`) otherwise, never silently pretending to be real.

## Prompt configuration

- **System prompt** and **User prompt** — both support real \`{{var}}\` interpolation against every upstream variable, plus \`{{input}}\` for the original trigger input.
- **Context variable** — explicitly wire a Knowledge Retrieval node's chunks into the prompt instead of relying on the older always-append behavior.

## Mode

- **Agentic** — counts against the flow's real step limit, for use inside multi-step reasoning flows.
- **Fixed** — a single, unconditional call.

## Memory

When enabled, real prior conversation turns (for this agent and invoking user) are threaded into the system prompt as a transcript, and the new turn is appended after the call completes — genuinely persisted via Postgres, not just held in this one run's memory.

## Output variables

| Variable | Description |
|---|---|
| \`{{llm_output}}\` | The full result object: \`{text, model, mock, usage}\` |
| \`ctx.output\` | The generated text, if no earlier node already set one |
`.trim(),
  },

  retrieval: {
    title: 'Knowledge Retrieval',
    summary: 'Real semantic search against one or more Knowledge Bases.',
    body: `
Retrieves the most relevant chunks from your configured Knowledge Bases for a given query — genuine vector similarity search against pgvector, not a keyword match stand-in.

## Query

Reference any upstream variable as the search query, or leave it to default to the original input.

## Knowledge Base selection

Pick one or more real, already-ingested Knowledge Bases to search across.

## Metadata filtering

Optionally restrict results using real metadata conditions (equals, contains, greater/less than) against fields your documents were actually ingested with. Filtering happens at the database level — a genuine \`WHERE\` clause, not a post-hoc filter on already-fetched results.

## Output variables

| Variable | Description |
|---|---|
| \`{{retrieved_chunks}}\` | The real matched chunks, ready to feed into a downstream LLM node's context |

An LLM node's own **Context variable** field can reference this explicitly, or it falls back to using it automatically if left unset.
`.trim(),
  },

  agent: {
    title: 'Agent',
    summary: 'Gives an LLM autonomous, iterative control over which tools to call.',
    body: `
A genuinely different execution *pattern*, not just a different prompt — the number and structure of real API calls actually differs by strategy.

## Strategies

- **ReAct** — loops up to Max Iterations, alternating Thought/Action, stopping the moment the model itself produces a real \`Final Answer:\`.
- **Function calling** — a real, iterative multi-round tool-use loop using genuine model tool-calling (not text-parsed), stopping the moment the model stops requesting a tool.
- **Plan & execute** — this platform's own addition beyond the two strategies above: always exactly two real calls, a plan then an execution pass genuinely conditioned on that plan.

## Query & Instructions

**Query** points the agent at a specific variable (defaults to the prior node's output). **Instructions** are genuinely prepended to the strategy's own system prompt, giving the agent a real role, persona, or constraints.

## Tools

For Function calling mode, select which real catalog tools the agent may call. Each real tool call still returns an honest placeholder result (no live external connector is configured in this reference platform), but the *decision* of which tool and when is genuine model reasoning.

## Memory

Reuses the exact same real conversation-history mechanism the LLM node's own memory toggle uses.

## Output variables

| Variable | Description |
|---|---|
| \`{{agent_final_answer}}\` | The agent's real final text |
| \`{{agent_tool_outputs}}\` | One real entry per tool call: \`{tool, input, output}\` |
| \`{{agent_reasoning_trace}}\` | Step-by-step real trace |
| \`{{agent_iteration_count}}\` | Real number of rounds taken |
| \`{{agent_success}}\` | Real boolean — did it produce a genuine answer within the cap |

Every run also writes real, persisted Agent Log entries — visible in this agent's own log history, not just returned in the response.
`.trim(),
  },

  classifier: {
    title: 'Question Classifier',
    summary: 'A real model call that picks one of your defined classes.',
    body: `
Routes a flow down different branches based on what the input actually is — a real LLM call asked to choose exactly one of your configured classes, not a keyword match.

## Configuration

- **Model** — defaults to a fast, low-cost model since classification rarely needs a large one.
- **Classes** — define two or more named categories.
- **Instruction** — an optional custom system prompt; if set, it's genuinely used instead of the default classification instruction.

## Routing

Each class gets its own real output port — wire different downstream logic to each one.

## Output variables

| Variable | Description |
|---|---|
| \`{{class_name}}\` | The real class the model picked |
| \`{{class_label}}\` | Same value, kept for compatibility with older flows |
`.trim(),
  },

  parameter_extractor: {
    title: 'Parameter Extractor',
    summary: 'Converts unstructured text into structured data using an LLM.',
    body: `
Bridges natural language and the structured parameters downstream nodes need — a real LLM call that returns real structured JSON, not a regex guess.

## Input & parameters

Point **Input variable** at the text to extract from. Define each parameter with a name, type (string/number/boolean/array/object), description, and whether it's required — the description genuinely shapes what the model looks for.

## Inference mode

- **Function Call / Tool Call** — uses the model's real tool-use API for reliable, type-compliant structured output.
- **Prompt-based** — a real, pure-prompt instruction to respond with only JSON, with real parsing of the response (including stripping a \`\`\`json code fence if the model adds one).

## Memory

When enabled, includes real conversation history for context — the same real transcript the LLM node's own memory toggle reads.

## Output variables

Each configured parameter becomes its own real variable, plus two built-in status variables kept in Dify's own exact naming since matching a documented spec is the point of these two:

| Variable | Description |
|---|---|
| \`__is_success\` | \`1\` on real success, \`0\` on real failure |
| \`__reason\` | A real error description when extraction fails |
`.trim(),
  },

  condition: {
    title: 'IF/ELSE',
    summary: 'Branches the flow based on a real expression.',
    body: `
Evaluates a real boolean expression against your flow's actual variables and routes execution down one of two real output ports — **True** or **False** — never both, never neither.

## Expression

Write a real comparison against any upstream variable, e.g. \`{{quality_score}} >= 70\`. Supports the usual comparison and logical operators.

## Output ports

Wire different downstream logic to each branch. Only the port matching the real evaluated result actually executes; the other branch's nodes are genuinely skipped for this run, not silently run and discarded.
`.trim(),
  },

  hitl: {
    title: 'Human Input',
    summary: 'Genuinely pauses the flow to request human input, then resumes.',
    body: `
A real pause-and-resume on this platform's sync execution path — the run's state is persisted, execution actually stops, and a separate resume call continues it later with whatever was submitted.

## Delivery method

Choose at least one real way the request reaches a person (e.g. the web app). A Human Input node with no delivery method configured can never actually be triggered.

## Fields

Define the real form fields a person fills in — paragraph text, a select dropdown, single file, or file list.

## Actions

Define named response actions (e.g. Approve / Reject) a person can choose between, if the form is a decision rather than free input.

## Timeout

An optional real timeout, after which the run either fails or proceeds down a default path, your choice.

## Output variables

Whatever the person submits becomes real, referenceable variables downstream, named after your configured fields.
`.trim(),
  },

  iteration: {
    title: 'Iteration',
    summary: 'Real array-based mapping — runs its body once per real element.',
    body: `
A genuine sibling of Loop, not a replacement — Iteration maps over a real array, Loop repeats until a real condition is met. Pick whichever matches your actual data shape.

## Input array

Point at any upstream variable that resolves to a real array.

## Body

Drag nodes inside the Iteration's boundary to define what runs per element. Inside the body, \`{{item}}\` and \`{{index}}\` are real, genuinely bound per-iteration variables.

## Execution mode

- **Sequential** — one element at a time, in real order.
- **Parallel** — up to 10 concurrent real threads, each with its own isolated context so elements never interfere with each other.

## Error handling

- **Terminated** — the whole run fails on the first real error.
- **Continue on error** — a failed element's slot gets a real \`null\`, the rest continue.
- **Remove abnormal output** — a failed element is skipped entirely from the collected results.

## Flatten

When results are themselves arrays, choose whether to splice them into one flat array or keep them nested per element.
`.trim(),
  },

  loop: {
    title: 'Loop',
    summary: 'Repeats its body until a real condition is met.',
    body: `
Condition-driven repetition — distinct from Iteration's array-mapping. Use Loop when you're repeating based on a real evaluated condition, not iterating a known array.

## Condition

A real boolean expression, evaluated fresh before each real repetition.

## Max loops

A real safety cap — the loop stops once this many repetitions have run, regardless of the condition, so a mistaken condition can never spin forever.

## Body & Exit Loop

Drag nodes inside the Loop's boundary. An Exit Loop node inside the body can end the loop early based on its own real condition, independent of the outer Max loops cap.
`.trim(),
  },

  code: {
    title: 'Code',
    summary: 'Runs real, sandboxed Python — genuine execution, not a template.',
    body: `
Executes your actual Python code in a real, network-isolated sandbox with a genuine timeout — not a simulated result.

## Input variables

Bind up to two named arguments (\`arg1\`, \`arg2\`) to real upstream variables; they're genuinely passed into your \`main()\` function's parameters.

## Output naming

Your \`main()\` function's return value is stored under a real, configurable output name (defaults to \`result\`).

## Error handling

- **None** — a real error genuinely fails the run.
- **Default value** — on a real error, falls back to a value you configure instead.
- **Fail branch** — routes to a real, separate error output port so downstream nodes can handle the failure explicitly.

## Retry on failure

When enabled, a real second sandbox attempt runs before giving up — genuinely useful for transient failures, not just a label.

## Real, verified isolation

No network access, no filesystem access beyond the sandbox, and a real timeout on infinite loops — all independently verified, not just documented as intended.
`.trim(),
  },

  template: {
    title: 'Template',
    summary: 'Real Jinja2-style {{var}} substitution.',
    body: `
Renders a text template with real variable interpolation against every upstream variable — variable substitution only, not full Jinja2 (no loops or conditionals inside the template text itself).

## Template

Write any text, referencing variables as \`{{var_name}}\`. Each reference is genuinely replaced with that variable's real current value at run time.

## Output variables

| Variable | Description |
|---|---|
| \`{{template_output}}\` | The real rendered string |
| \`ctx.output\` | Set to the same value, if no earlier node already produced one |
`.trim(),
  },

  aggregator: {
    title: 'Variable Aggregator',
    summary: 'Merges several branch-specific variables into one real name.',
    body: `
Solves the common "which branch actually ran" problem: several upstream branches might each produce a similarly-shaped result under different variable names, and you want one single downstream reference regardless of which branch fired.

## Aggregation Groups

Define one or more groups, each with an **output name** and a real, ordered list of source variable references. For each group, the first real non-empty variable in the list is genuinely selected — type-preserved, not stringified — and stored under the group's output name.

## Real, type-preserving behavior

If the winning source variable was a number, array, or object, it stays that type downstream — not coerced into a string the way a naive template-merge would.
`.trim(),
  },

  docextractor: {
    title: 'Doc Extractor',
    summary: 'Real fetch + real text extraction from an uploaded or remote file.',
    body: `
Genuinely fetches file bytes and extracts real text — not a placeholder. Accepts a literal cloud-storage URL (public or presigned), this platform's own real upload URL, or a \`{{var}}\` reference to either.

## Mode

- **Single File** — one input, one real extracted text output.
- **Multiple Files** — a real array of files in, a real array of extracted strings out, one per file in the same order; a single file's failure doesn't abort the others.

## Real format coverage

pdf, docx, doc, xlsx, xls, csv, pptx, ppt, epub, eml, msg, vtt, html, txt, md, json, xml, yaml, and other text-like formats — with real encoding detection rather than an always-UTF-8 assumption. Tables become real Markdown tables; consecutive same-speaker VTT lines are genuinely merged.

## Output variables

| Variable | Description |
|---|---|
| \`{{docextractor_text}}\` | A real string (Single File mode) or a real array of strings (Multiple Files mode) |
`.trim(),
  },

  list_operator: {
    title: 'List Operator',
    summary: 'Real filtering, sorting, and selection over an array.',
    body: `
Processes arrays by filtering, sorting, and selecting elements — the piece that makes splitting a mixed upload into "images here, documents there" genuinely possible.

## Input array

Point at any upstream array — plain values (strings/numbers/booleans) or real file-shaped objects.

## Filters

Every filter must match (real AND semantics). For file arrays, filter by real Type (image/document/audio/video, inferred from extension/content-type), MIME Type, Extension, Size, Name, or Transfer Method (this platform's own uploads vs. a remote URL).

## Sort

Sort by any of the same properties, ascending or descending. Items genuinely missing the sort property sort last, regardless of direction — not silently reordered unpredictably.

## Take First N

An optional real limit, 1–20.

## Output variables

| Variable | Description |
|---|---|
| \`{{result}}\` | The full real filtered/sorted/limited array |
| \`{{first_record}}\` | The real first element, or \`null\` if empty |
| \`{{last_record}}\` | The real last element, or \`null\` if empty |
`.trim(),
  },

  tool: {
    title: 'Tool / Connector',
    summary: 'Invokes a catalog-registered data source or tool action.',
    body: `
Wires the flow to a real UCS-catalog object — a data source or a specific tool action — gated by the same real entitlement resolution the UCS Check node performs upfront.

## Catalog object

Pick from real, currently-registered data sources or tool actions in your workspace's catalog.

## Honest scope boundary

The catalog integration and entitlement checking are real. The actual external call a selected tool makes is an honest placeholder in this reference platform — no live third-party connector credentials are configured — matching the same stated boundary the Agent node's own "Function calling" strategy relies on.
`.trim(),
  },

  http: {
    title: 'HTTP Request',
    summary: 'A raw, real outbound HTTP call.',
    body: `
Makes a genuine HTTP request to a URL you configure — real network I/O, not a stub.

## Configuration

- **Method** — GET, POST, PUT, or DELETE.
- **URL, headers, and body** — all support real \`{{var}}\` interpolation.

## Output variables

The real response status, headers, and body are all made available downstream for later nodes to reference.
`.trim(),
  },

  subagent: {
    title: 'Sub-Agent',
    summary: "Genuinely invokes another agent's entire real flow.",
    body: `
Distinct from the Agent node (which is about how *one* node reasons) — Sub-Agent calls a whole other agent's real flow graph, including its own real Trigger and UCS Check, and bubbles its real output back.

## Target agent

Select which other agent to invoke. Snippets don't appear here — they're never run standalone.

## Input

Optionally override what the target agent receives as input; defaults to whatever this flow has produced so far.

## Real cycle detection

An agent already in this call's chain is refused before ever being invoked — a cycle can't happen no matter how many hops deep it's buried. A real hop cap (5) matches the same limit UCS's own entitlement resolution enforces.

## Principal threading

The same invoking user is threaded through every hop — a sub-agent's own UCS Check evaluates entitlement for the real original caller, not a synthetic system identity.
`.trim(),
  },

  memory: {
    title: 'Memory / State',
    summary: 'A real, persistent key-value store — genuinely survives across runs.',
    body: `
Unlike a plain workflow variable (which resets every run), a value written here is genuinely still there the next time this flow runs — a real Postgres-backed UPSERT, not held only in this execution's own memory.

## Scope

- **Agent** (default) — one shared value across every run of this agent, regardless of who invoked it.
- **Conversation** — scoped to the specific invoking user.

## Mode

- **Read** — loads the prior value, available downstream as \`{{memory_<key>}}\`.
- **Write** — saves a new value, either a literal string or \`{{output}}\` to capture whatever the most recent node produced.
- **Read, then write** — both, in that order, in one node.

## Key

Names which real memory slot this node reads or writes — pick the same key across multiple nodes/runs to share the same real persisted value.
`.trim(),
  },

  variable_assigner: {
    title: 'Variable Assigner',
    summary: 'Writes to real, persistent conversation variables.',
    body: `
Genuinely different from a plain workflow variable: a value set here is readable by a **later, separate run** for the same invoking user — real conversational continuity, not just this one execution's own state.

## Assignments

Define one or more real assignments, each targeting a named conversation variable with one of these real operations:

| Operation | Applies to | What it does |
|---|---|---|
| Overwrite | any type | Replace with a real value from another variable |
| Set | any type | Replace with a real fixed literal value |
| Clear | any type | Empty array (for an array) or real \`null\` |
| Arithmetic | number | Add, subtract, multiply, or divide by a real variable or a literal number |
| Append | array | Add one real element to the end |
| Extend | array | Add every real element from another array |
| Remove First / Remove Last | array | Drop the real first or last element |

## Reading a conversation variable elsewhere

Any node in a later run can reference \`{{conv_<name>}}\` — every known conversation variable for the current user is genuinely loaded before that run's traversal even starts.
`.trim(),
  },

  comment: {
    title: 'Comment',
    summary: 'Documentation only — never executes, never connects into the flow.',
    body: `
A sticky note for explaining part of your flow to yourself or teammates. It never runs, and nothing can be wired into or out of it.

## Background color

Pick from six real preset colors — genuinely applied to the note on the canvas, not just a swatch picker with nothing behind it.

## Formatting

A real toolbar: font size (Small/Medium/Large), Bold, Italic, Strikethrough, Link, Bulleted list, Heading, and Quote. Every button genuinely wraps your current text selection with real Markdown, rendered on the note itself — headings, bold text, links, and lists all display as real formatted content, not raw \`**asterisks**\`.
`.trim(),
  },

  exit_loop: {
    title: 'Exit Loop',
    summary: "Stops a Loop early, based on its own real condition.",
    body: `
Placed inside a Loop's body. When its own condition evaluates true, the enclosing Loop ends immediately for this run — independent of the Loop's own Max loops cap.

## Condition

A real boolean expression, evaluated against whatever variables are available inside the loop body at that point.
`.trim(),
  },

  output: {
    title: 'Output Vars',
    summary: 'Collects one or more named, structured output fields.',
    body: `
Distinct from the Output (\`respond\`) node: this collects real, individually-named fields into a structured result, rather than a single text blob.

## Variables

Define any number of named fields, each with a real expression referencing upstream variables via \`{{var}}\`.

## Multiple Output Vars nodes

A flow can have more than one — useful when different branches each contribute their own fields. Their variables genuinely merge into one structured result rather than overwriting each other, so two branches that both eventually reach the final Output node don't clobber each other's fields.
`.trim(),
  },
};
