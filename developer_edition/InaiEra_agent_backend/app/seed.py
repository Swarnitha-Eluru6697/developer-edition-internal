"""Seeds the database on startup, in two clearly separate parts.

REFERENCE data is the platform's own capability catalog — model
providers, the chat/embedding models they serve, the tool-connector
catalog, the template gallery. A fresh install needs it to function at
all, so it is installed unconditionally and idempotently.

DEMO data is the sample agents and the synthetic operational history
around them (runs, traces, alerts, roles, logs, "connected" data
sources). It is fiction, and it is off unless SEED_DEMO_DATA is
explicitly set — and refused outright under ENVIRONMENT=production.

Those two used to be one all-or-nothing block guarded only by "does an
Agent row exist", so a fresh production database received the whole lot
on first startup: runs and traces that skew every monitoring average,
alerts nobody raised, and integrations reported as connected that were
never connected.
"""
import logging
import os
from copy import deepcopy

from sqlalchemy import func
from sqlmodel import Session, select
from .db import engine
from .models import (
    Agent, AgentLog, Run, Trace, Alert, Role, ModelProvider, ProviderModel,
    Tool, ToolAction, MCPServer, DataSource, Template, TemplateVersion,
)

logger = logging.getLogger(__name__)

# ---- default flow graph used for agents that don't need a bespoke one ----
# Deliberately NO knowledge base selected. These referenced
# "lakehouse.core.customer_events" and "app_datasets.docs.support_kb",
# neither of which exists in any deployment: there is no `lakehouse`
# catalog and no `docs` schema was ever created, so retrieval returned
# a placeholder chunk at run time and publish refused the flow outright
# ("References knowledge base ..., which doesn't exist"). A seeded
# example cannot name a real KB either, because the seeder does not
# create knowledge bases — they are made by whoever uses the product.
# An empty selection is the honest default: the node is there to be
# pointed at a real KB in the Builder.

DEFAULT_FLOW = {
    "nodes": [
        {"id": "n1", "type": "trigger", "x": 40, "y": 140, "sub": "user input", "config": {}},
        {"id": "n2", "type": "gov", "x": 200, "y": 140, "sub": "entitlement gate", "config": {}},
        {"id": "n3", "type": "retrieval", "x": 360, "y": 140, "sub": "knowledge base",
         # see the note below
         "config": {"knowledge_bases": [], "top_k": 5}},
        {"id": "n4", "type": "llm", "x": 520, "y": 140, "sub": "compose",
         "config": {"mode": "agentic", "model": "Claude Sonnet 5", "step_limit": 12,
                    "timeout_s": 30, "guardrail_profile": "Standard (input + output)"}},
        {"id": "n5", "type": "tool", "x": 680, "y": 140, "sub": "connector",
         "config": {"catalog_object": "pg_sales.public.orders"}},
        {"id": "n6", "type": "respond", "x": 840, "y": 140, "sub": "to user",
         "config": {"format": "markdown", "streaming": True}},
    ],
    # Explicit edges — the flow graph is a real directed graph now, not an
    # implicit ordering derived from node x-position. This chain matches
    # what the old x-sort would have produced, so existing seeded agents
    # look identical on load.
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
        {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        {"id": "e3", "source": "n3", "target": "n4", "source_handle": None},
        {"id": "e4", "source": "n4", "target": "n5", "source_handle": None},
        {"id": "e5", "source": "n5", "target": "n6", "source_handle": None},
    ],
}


AGENTS = [
    dict(id="a1", name="Revenue Analysis Agent", type="Data", subtype="Analysis", plane="Data",
         owner="team-data", status="published", version="v4", health_score=91, health_status="pass",
         model="Claude Sonnet 5", mode="agentic", runs_7d=214, cost_7d="$28.40", first_party=True,
         tags=["finance", "sql", "reporting"], description="Generates SQL/pandas analysis and charts against the revenue lakehouse."),
    dict(id="a2", name="Schema Migration Designer", type="Data", subtype="ERD Designer", plane="Data",
         owner="team-platform", status="published", version="v2", health_score=88, health_status="pass",
         model="Claude Sonnet 5", mode="fixed", runs_7d=41, cost_7d="$9.10", first_party=True,
         tags=["platform", "schema"], description="Designs and validates ERD changes before a migration ships."),
    dict(id="a3", name="Pipeline Engineer Agent", type="Data", subtype="Engineer", plane="Data",
         owner="team-platform", status="published", version="v6", health_score=79, health_status="warn",
         model="Claude Sonnet 5", mode="agentic", runs_7d=156, cost_7d="$44.80", first_party=True,
         tags=["platform", "etl"], description="Builds and debugs data pipeline steps end to end."),
    dict(id="a4", name="Churn Model Scientist", type="Data", subtype="Scientist", plane="Data",
         owner="team-ml", status="staged", version="v1", health_score=64, health_status="warn",
         model="Claude Opus 4.8", mode="agentic", runs_7d=12, cost_7d="$6.20", first_party=True,
         tags=["ml", "churn"], description="Explores churn drivers and drafts model feature candidates."),
    dict(id="a5", name="Support Knowledge Bot", type="App", subtype="Conversational", plane="App",
         owner="team-support", status="published", version="v9", health_score=95, health_status="pass",
         model="Claude Sonnet 5", mode="agentic", runs_7d=3820, cost_7d="$112.60", first_party=False,
         app_type="chatbot", tags=["support", "customer-facing"], description="Answers customer support questions from the real knowledge base.",
         features={"conversation_opener": {"enabled": True, "text": "Hi! I can help with account, billing, and product questions. What do you need?"},
                   "follow_up": {"enabled": True}, "citations": {"enabled": True}}),
    dict(id="a6", name="Invoice Approval Flow", type="App", subtype="Task-Automation", plane="App",
         owner="team-finance", status="published", version="v3", health_score=97, health_status="pass",
         model="Claude Haiku 4.5", mode="fixed", runs_7d=390, cost_7d="$14.30", first_party=False,
         app_type="workflow", tags=["finance", "internal"], description="Routes an invoice through a real approval workflow."),
    dict(id="a7", name="Onboarding Concierge", type="App", subtype="Conversational", plane="App",
         owner="team-hr", status="draft", version="v1", health_score=None, health_status="—",
         model="Claude Sonnet 5", mode="agentic", runs_7d=0, cost_7d="$0.00", first_party=False,
         app_type="chatflow", tags=["hr", "internal"], description="Guides a new hire through onboarding steps.",
         features={"conversation_opener": {"enabled": True, "text": "Welcome aboard! I can walk you through your first-week checklist."}}),
    dict(id="a8", name="Vendor Onboarding Automation", type="App", subtype="Task-Automation", plane="App",
         owner="team-procurement", status="published", version="v2", health_score=58, health_status="fail",
         model="Claude Sonnet 5", mode="agentic", runs_7d=64, cost_7d="$21.90", first_party=False,
         app_type="agent", tags=["procurement", "internal"], description="Plans and executes multi-step vendor onboarding tasks.",
         features={"moderation": {"enabled": True, "blocked_words": ["bypass-approval"]}}),
]

AGENT_METRICS = {
    "a1": dict(success="96.8%", p50="1.2s", p95="5.9s", costAvg="$0.077", retries="0.09", loopIter="2.1",
               stepLimit="0.2%", guardrail="0.4%", entPass="99.6%", chainDepth="1.0", cacheHit="95%"),
    "a2": dict(success="98.1%", p50="2.0s", p95="7.4s", costAvg="$0.220", retries="0.05", loopIter="—",
               stepLimit="—", guardrail="0.2%", entPass="99.9%", chainDepth="1.0", cacheHit="97%"),
    "a3": dict(success="88.4%", p50="3.1s", p95="14.2s", costAvg="$0.287", retries="0.61", loopIter="4.8",
               stepLimit="3.1%", guardrail="2.0%", entPass="98.9%", chainDepth="1.0", cacheHit="91%"),
    "a4": dict(success="71.2%", p50="4.4s", p95="19.0s", costAvg="$0.517", retries="0.88", loopIter="5.6",
               stepLimit="6.4%", guardrail="4.1%", entPass="97.2%", chainDepth="1.0", cacheHit="88%"),
    "a5": dict(success="98.9%", p50="1.1s", p95="2.4s", costAvg="$0.029", retries="0.03", loopIter="1.4",
               stepLimit="0.1%", guardrail="0.6%", entPass="99.8%", chainDepth="1.0", cacheHit="96%"),
    "a6": dict(success="99.2%", p50="8.0s", p95="46.0s", costAvg="$0.084", retries="0.02", loopIter="—",
               stepLimit="—", guardrail="0.1%", entPass="99.9%", chainDepth="2.0", cacheHit="98%"),
    "a7": dict(success="—", p50="—", p95="—", costAvg="—", retries="—", loopIter="—",
               stepLimit="—", guardrail="—", entPass="—", chainDepth="—", cacheHit="—"),
    "a8": dict(success="82.0%", p50="0.6s", p95="2.1s", costAvg="$0.342", retries="0.34", loopIter="3.2",
               stepLimit="1.8%", guardrail="1.2%", entPass="93.4%", chainDepth="3.0", cacheHit="82%"),
}

RUNS = [
    dict(id="r1", agent_id="a5", agent_name="Support Knowledge Bot", status="success", duration="1.8s", cost="$0.031", hops=1, ts="14:22:07", fail_class=None),
    dict(id="r2", agent_id="a6", agent_name="Invoice Approval Flow", status="success", duration="41.2s", cost="$0.084", hops=2, ts="14:19:55", fail_class=None),
    dict(id="r3", agent_id="a8", agent_name="Vendor Onboarding Automation", status="denied", duration="0.4s", cost="$0.002", hops=3, ts="14:18:31", fail_class="ucs"),
    dict(id="r4", agent_id="a3", agent_name="Pipeline Engineer Agent", status="failed", duration="12.1s", cost="$0.190", hops=1, ts="14:12:09", fail_class="harness"),
    dict(id="r5", agent_id="a1", agent_name="Revenue Analysis Agent", status="success", duration="6.3s", cost="$0.077", hops=1, ts="14:08:44", fail_class=None),
    dict(id="r6", agent_id="a4", agent_name="Churn Model Scientist", status="failed", duration="2.0s", cost="$0.008", hops=1, ts="13:58:20", fail_class="orchestration"),
    dict(id="r7", agent_id="a5", agent_name="Support Knowledge Bot", status="success", duration="2.2s", cost="$0.028", hops=1, ts="13:40:02", fail_class=None),
]

TRACES = [
    dict(id="tr-8f2a1c", agent_id="a5", agent_name="Support Knowledge Bot", version="v9", principal="agent:svc-support-bot / user:priya", plane="App", spans=6, duration="1.8s", ts="2026-08-02 14:22:07", status="ok"),
    dict(id="tr-19bd44", agent_id="a6", agent_name="Invoice Approval Flow", version="v3", principal="agent:svc-invoice-flow / user:dana", plane="App", spans=9, duration="41.2s", ts="2026-08-02 14:19:55", status="ok"),
    dict(id="tr-cc0091", agent_id="a8", agent_name="Vendor Onboarding Automation", version="v2", principal="user:sam → agent:svc-vendor-onb → agent:svc-doc-extract", plane="App", spans=5, duration="0.4s", ts="2026-08-02 14:18:31", status="ucs_denial"),
    dict(id="tr-77e2b0", agent_id="a3", agent_name="Pipeline Engineer Agent", version="v6", principal="agent:svc-pipeline-eng / user:noah", plane="Data", spans=14, duration="12.1s", ts="2026-08-02 14:12:09", status="harness_fail"),
    dict(id="tr-3a90fd", agent_id="a1", agent_name="Revenue Analysis Agent", version="v4", principal="agent:svc-revenue-analysis / user:maria", plane="Data", spans=7, duration="6.3s", ts="2026-08-02 14:08:44", status="ok"),
    dict(id="tr-50c112", agent_id="a4", agent_name="Churn Model Scientist", version="v1", principal="agent:svc-churn-scientist / user:carol", plane="Data", spans=3, duration="2.0s", ts="2026-08-02 13:58:20", status="orch_fail"),
]

ALERTS = [
    dict(id="al1", sev="high", rule="quality-breach", agent_id="a4", agent_name="Churn Model Scientist", state="open", ts="2026-08-02 13:58",
         summary="Source object customer_events dropped to fail (nulls dimension) — Scientist Agent run blocked.", history=[]),
    dict(id="al2", sev="high", rule="denied-burst", agent_id="a8", agent_name="Vendor Onboarding Automation", state="open", ts="2026-08-02 14:18",
         summary="Agent principal hit 4 ACCESS_DENIED in 8m at hop 2 of a 3-hop chain — possible misconfigured sub-agent.", history=[]),
    dict(id="al3", sev="medium", rule="harness — step-limit", agent_id="a3", agent_name="Pipeline Engineer Agent", state="open", ts="2026-08-02 14:12",
         summary="Reasoning loop hit the 12-step cap before producing a final answer.", history=[]),
    dict(id="al4", sev="high", rule="priv-escalation", agent_id="a5", agent_name="Support Knowledge Bot", state="resolved", ts="2026-08-01 09:02",
         summary="Owner-tier entitlement briefly granted to the agent service principal — reverted.",
         history=[{"action": "Acknowledged", "by": "secadmin", "ts": "2026-08-01 09:05"},
                  {"action": "Resolved", "by": "secadmin", "ts": "2026-08-01 09:15"}]),
]

# Group names here are spelled the way the REAL UCS spells them, and
# that is load-bearing rather than cosmetic. check_entitlement sends an
# agent's bound groups to UCS verbatim, and UCS matches them literally:
# "data-engineers" is granted public_reader on pg_sales.public.orders,
# while "data_engineers" — the underscore spelling this list used to
# carry — matches nothing and is answered "no matching grant". The
# seeded agents read that object, so every one of them was blocked at
# publish on the agent-principal half of its own entitlement check, and
# the reason was a hyphen. The former names (analysts, support_team,
# finance_admins) existed in no IdP at all and behaved the same way.
ROLES = [
    dict(agent_id="a1", agent_name="Revenue Analysis Agent", tier="select", group="data-analysts"),
    dict(agent_id="a1", agent_name="Revenue Analysis Agent", tier="writer", group="data-engineers"),
    dict(agent_id="a5", agent_name="Support Knowledge Bot", tier="select", group="acme-docs-readers"),
    dict(agent_id="a6", agent_name="Invoice Approval Flow", tier="owner", group="finance"),
    dict(agent_id="a3", agent_name="Pipeline Engineer Agent", tier="writer", group="data-engineers"),
]

MODEL_PROVIDERS = [
    dict(name="OpenAI", version="1.0.3", status="credits", types=["LLM", "Text Embedding", "Speech2Text", "Moderation", "TTS"]),
    dict(name="Anthropic", version="0.3.26", status="credits", types=["LLM"]),
    # needs_key, not "credits": nothing is seeded with a Gemini
    # credential, and "credits" renders on the Model Providers page as
    # though the provider were ready to use. Every other provider
    # seeded "credits" here is one whose key arrives from the
    # environment; Gemini's never did, so the badge was simply untrue.
    dict(name="Gemini", version="0.9.4", status="needs_key", types=["LLM", "Text Embedding"]),
    dict(name="Amazon Bedrock", version="0.0.77", status="needs_key", types=["LLM", "Text Embedding", "Rerank"]),
    dict(name="Cohere", version="0.0.16", status="needs_key", types=["LLM", "Text Embedding", "Rerank"]),
    dict(name="Azure OpenAI", version="1.2.0", status="needs_key", types=["LLM", "Text Embedding"]),
    dict(name="Azure AI Studio", version="0.4.1", status="needs_key", types=["LLM"]),
    # HF serves feature-extraction (embeddings) as well as text generation;
    # seeded LLM-only, it under-declared what it actually supports.
    dict(name="Hugging Face Hub", version="0.1.9", status="needs_key", types=["LLM", "Text Embedding"]),
    dict(name="Replicate", version="0.0.5", status="needs_key", types=["LLM"]),
    dict(name="DeepSeek", version="0.2.3", status="needs_key", types=["LLM"]),
]

# Known embedding models per provider, with each model's REAL output
# dimension. The dimension matters more than any other field here: the
# pgvector column is created with this width at first ingest and cannot
# be widened afterwards without re-embedding everything, so a wrong
# number here is not a cosmetic error. Every value below is the
# provider's documented default output size.
#
# Deliberately absent, because neither vendor ships an embeddings API at
# all — Anthropic/Claude serves only messages, and DeepSeek only
# chat/completions and its reasoner. A row for either would be listed as
# selectable and then fail on the first ingest with a 404 from the
# vendor, which is worse than not offering it.
#
# Azure OpenAI is absent for a different reason: it addresses models by
# the DEPLOYMENT name a customer chose in their own resource, which is
# not knowable here. Add those by hand once the deployment exists.
#
# Amazon Bedrock and Replicate entries are included as catalog facts but
# will show as unavailable — neither has an embedding runtime wired up
# (SigV4 and Replicate's predictions API respectively), and
# /embedding-models says so rather than pretending otherwise.
PROVIDER_EMBEDDING_MODELS = [
    # OpenAI — /v1/embeddings. The v3 models support shortening via the
    # `dimensions` parameter; the full default width is used here.
    dict(name="text-embedding-3-small", provider_name="OpenAI",
         api_model_id="text-embedding-3-small", embedding_dimension=1536,
         input_price_per_mtok=0.02),
    dict(name="text-embedding-3-large", provider_name="OpenAI",
         api_model_id="text-embedding-3-large", embedding_dimension=3072,
         input_price_per_mtok=0.13),
    dict(name="text-embedding-ada-002", provider_name="OpenAI",
         api_model_id="text-embedding-ada-002", embedding_dimension=1536,
         input_price_per_mtok=0.10),

    # Hugging Face Hub — the feature-extraction pipeline over the
    # Inference API. These are the sentence-transformers style models
    # this platform's own HF embedding path already handles.
    dict(name="all-MiniLM-L6-v2", provider_name="Hugging Face Hub",
         api_model_id="sentence-transformers/all-MiniLM-L6-v2", embedding_dimension=384),
    dict(name="all-mpnet-base-v2", provider_name="Hugging Face Hub",
         api_model_id="sentence-transformers/all-mpnet-base-v2", embedding_dimension=768),
    dict(name="bge-base-en-v1.5", provider_name="Hugging Face Hub",
         api_model_id="BAAI/bge-base-en-v1.5", embedding_dimension=768),
    dict(name="bge-large-en-v1.5", provider_name="Hugging Face Hub",
         api_model_id="BAAI/bge-large-en-v1.5", embedding_dimension=1024),
    dict(name="multilingual-e5-large", provider_name="Hugging Face Hub",
         api_model_id="intfloat/multilingual-e5-large", embedding_dimension=1024),
    dict(name="e5-base-v2", provider_name="Hugging Face Hub",
         api_model_id="intfloat/e5-base-v2", embedding_dimension=768),
    dict(name="mxbai-embed-large-v1", provider_name="Hugging Face Hub",
         api_model_id="mixedbread-ai/mxbai-embed-large-v1", embedding_dimension=1024),

    # Cohere — reachable through its OpenAI-compatibility endpoint.
    dict(name="embed-english-v3.0", provider_name="Cohere",
         api_model_id="embed-english-v3.0", embedding_dimension=1024),
    dict(name="embed-multilingual-v3.0", provider_name="Cohere",
         api_model_id="embed-multilingual-v3.0", embedding_dimension=1024),
    dict(name="embed-english-light-v3.0", provider_name="Cohere",
         api_model_id="embed-english-light-v3.0", embedding_dimension=384),

    # Gemini — via the OpenAI-compatible surface.
    dict(name="gemini-embedding-001", provider_name="Gemini",
         api_model_id="gemini-embedding-001", embedding_dimension=3072),
    dict(name="text-embedding-004", provider_name="Gemini",
         api_model_id="text-embedding-004", embedding_dimension=768),

    # Amazon Bedrock — catalogued, but no runtime behind it yet.
    dict(name="titan-embed-text-v2", provider_name="Amazon Bedrock",
         api_model_id="amazon.titan-embed-text-v2:0", embedding_dimension=1024),
]


# Chat models per provider, so a provider with a key actually yields
# something selectable. Cohere and Gemini previously contributed nothing
# to the Rerank Model dropdown despite being configurable: only their
# embedding models were catalogued, and an embedding model cannot score
# relevance in prose.
#
# Same exclusions as the embedding catalog: Azure OpenAI addresses models
# by a customer-chosen DEPLOYMENT name that is not knowable here, and
# Amazon Bedrock / Replicate have no execution path (SigV4 and their own
# predictions API respectively).
#
# Prices are per million tokens, left out rather than guessed at.
PROVIDER_CHAT_MODELS = [
    dict(name="command-r-plus", provider_name="Cohere", api_model_id="command-r-plus"),
    dict(name="command-r", provider_name="Cohere", api_model_id="command-r"),

    dict(name="gemini-2.5-pro", provider_name="Gemini", api_model_id="gemini-2.5-pro"),
    dict(name="gemini-2.5-flash", provider_name="Gemini", api_model_id="gemini-2.5-flash"),

    dict(name="deepseek-chat", provider_name="DeepSeek", api_model_id="deepseek-chat"),
    dict(name="deepseek-reasoner", provider_name="DeepSeek", api_model_id="deepseek-reasoner"),

    # Replicate addresses models as owner/name (optionally :version).
    # These are official Replicate models, so the bare owner/name form
    # works without pinning a version hash.
    dict(name="llama-3-8b-instruct", provider_name="Replicate",
         api_model_id="meta/meta-llama-3-8b-instruct"),
    dict(name="llama-3-70b-instruct", provider_name="Replicate",
         api_model_id="meta/meta-llama-3-70b-instruct"),
    dict(name="mistral-7b-instruct", provider_name="Replicate",
         api_model_id="mistralai/mistral-7b-instruct-v0.2"),
]


TOOLS = [
    dict(name="JSON Process", org="langgenius/json_process", version="0.0.7", desc="Tools for processing JSON content using jsonpath_ng.", source="marketplace"),
    dict(name="markitdown", org="yevanchen/markitdown", version="0.0.4", desc="Python tool for converting files and office documents to Markdown.", source="marketplace"),
    dict(name="MinerU", org="langgenius/mineru", version="0.4.3", desc="Converts files into machine-readable formats.", source="marketplace"),
    dict(name="Jina AI", org="langgenius/jina_tool", version="0.0.13", desc="Your search foundation, supercharged.", source="marketplace"),
    dict(name="Audio", org="hjlarry/audio", version=None, desc="A tool for TTS and ASR.", source="builtin"),
    dict(name="Code Interpreter", org="InAiEra/code", version=None, desc="Run a piece of code and get the result back — same sandbox as the Code Node (§8).", source="builtin"),
    dict(name="CurrentTime", org="InAiEra/time", version=None, desc="A tool for getting the current time.", source="builtin"),
    dict(name="WebScraper", org="InAiEra/webscraper", version=None, desc="Scrape a web page into structured text.", source="builtin"),
]

TOOL_ACTIONS = {
    "JSON Process": [
        ("JSON Parse", "A tool for extracting JSON objects", [
            ("json_data", "string", True, "JSON data"),
            ("json_filter", "string", True, "JSON fields to be parsed")]),
        ("JSON Delete", "A tool for deleting JSON content", [
            ("json_data", "string", True, "JSON data"),
            ("json_filter", "string", True, "JSON fields to delete")]),
        ("JSON Replace", "A tool for replacing JSON content", [
            ("json_data", "string", True, "JSON data"),
            ("json_filter", "string", True, "JSON field to replace"),
            ("replace_value", "string", True, "New value to insert")]),
        ("JSON Insert", "A tool for inserting JSON content", [
            ("json_data", "string", True, "JSON data"),
            ("new_field", "string", True, "Field path to insert at"),
            ("new_value", "string", True, "Value to insert")]),
    ],
    "markitdown": [
        ("Convert to Markdown", "Converts a file or office document into Markdown text", [
            ("file", "file", True, "Source document (docx, pptx, xlsx, pdf, html…)")]),
    ],
    "MinerU": [
        ("Convert File", "Converts a file into a machine-readable format", [
            ("file", "file", True, "Source file to convert"),
            ("output_format", "string", False, "markdown | json | html")]),
    ],
    "Jina AI": [
        ("Search", "Search the web via Jina's search foundation", [
            ("query", "string", True, "Search query")]),
        ("Read Page", "Fetch and clean a URL into readable text", [
            ("url", "string", True, "Target page URL")]),
    ],
    "Audio": [
        ("Text to Speech", "Synthesize speech audio from text", [
            ("text", "string", True, "Text to synthesize"),
            ("voice", "string", False, "Voice preset name")]),
        ("Speech to Text", "Transcribe audio into text", [
            ("audio", "file", True, "Audio file to transcribe")]),
    ],
    "Code Interpreter": [
        ("Run Code", "Execute a code snippet and return the result", [
            ("language", "string", True, "python3 | nodejs20"),
            ("code", "string", True, "Source code to execute")]),
    ],
    "CurrentTime": [
        ("Get Current Time", "Returns the current time in a given timezone", [
            ("timezone", "string", False, "IANA timezone, defaults to UTC")]),
    ],
    "WebScraper": [
        ("Scrape Page", "Scrape a web page into structured text", [
            ("url", "string", True, "Target page URL"),
            ("selector", "string", False, "Optional CSS selector to scope extraction")]),
    ],
}

DATA_SOURCES = [
    dict(name="Google Drive", version="0.1.13", status="needs_auth", auth="oauth"),
    dict(name="OneDrive", version="1.0.0", status="needs_auth", auth="oauth"),
    dict(name="SharePoint", version="1.0.0", status="needs_auth", auth="oauth"),
    dict(name="Google Cloud Storage", version="0.2.13", status="needs_auth", auth="apikey"),
    dict(name="Notion", version="0.3.4", status="connected", auth="apikey"),
    dict(name="GitHub", version="1.1.0", status="connected", auth="apikey"),
]

MCP_SERVERS = [
    dict(id="mcp1", name="Figma MCP", identifier="figma-mcp", url="https://mcp.figma.com/v1", status="connected", actions=6),
    dict(id="mcp2", name="Linear MCP", identifier="linear-mcp", url="https://mcp.linear.app/http", status="connected", actions=4),
]

AGENT_LOGS = {
    "a1": [
        ("14:08:44.102", "INFO", "Run started · principal chain resolved (1 hop) · cache hit", "tr-3a90fd"),
        ("14:08:44.310", "INFO", "Retrieval node: lakehouse.sales.revenue_rollup · 214 rows", "tr-3a90fd"),
        ("14:08:49.881", "INFO", "LLM node: Claude Sonnet 5 · 1.1k in / 640 out tokens", "tr-3a90fd"),
        ("14:08:50.402", "INFO", "Run completed · 6.3s · $0.077", "tr-3a90fd"),
    ],
    "a3": [
        ("14:12:09.001", "INFO", "Run started · agentic mode · step limit 12", "tr-77e2b0"),
        ("14:12:11.220", "WARN", "Harness loop at step 9/12 — no final answer yet", "tr-77e2b0"),
        ("14:12:13.005", "ERROR", "Harness step-limit reached before final answer — run failed", "tr-77e2b0"),
    ],
    "a4": [
        ("13:58:20.011", "INFO", "Run started · reading lakehouse.core.customer_events", "tr-50c112"),
        ("13:58:20.240", "ERROR", "Quality gate: customer_events at FAIL (nulls dimension) — run blocked", "tr-50c112"),
    ],
    "a5": [
        ("14:22:07.010", "INFO", "Run started · conversational, sync path (no Temporal)", "tr-8f2a1c"),
        ("14:22:07.640", "INFO", "Retrieval: app_datasets.docs.support_kb · 4 chunks", "tr-8f2a1c"),
        ("14:22:08.812", "INFO", "Run completed · 1.8s · $0.031", "tr-8f2a1c"),
    ],
    "a8": [
        ("14:18:31.002", "INFO", "Run started · chain: user → svc-vendor-onb → svc-doc-extract", "tr-cc0091"),
        ("14:18:31.180", "WARN", "Entitlement check hop 2/3: agent principal not entitled on pg_procurement.vendors", "tr-cc0091"),
        ("14:18:31.401", "ERROR", "UCS DENIED at hop 2 — run terminated, no data accessed", "tr-cc0091"),
    ],
}


def sync_model_catalog():
    """Insert any catalogued embedding model that isn't registered yet,
    and add the "Text Embedding" type to providers that genuinely serve
    embeddings.

    seed() returns early on an already-seeded database, so anything
    added to a catalog list after the first run would never reach an
    existing install. This runs every startup instead, and is
    deliberately insert-only:

      * A model is skipped if its name is taken, OR if the same
        (provider, api_model_id) pair is already registered under a
        different name. Someone who added "MiniLM L6 v2" by hand keeps
        exactly that row rather than gaining a near-duplicate.
      * Nothing existing is ever rewritten — a dimension or price
        someone corrected by hand stays corrected.
      * Provider types are only ever added to, never replaced, so a
        provider's other declared types survive.
    """
    with Session(engine) as s:
        providers = {p.name: p for p in s.exec(select(ModelProvider)).all()}
        if not providers:
            return  # nothing seeded yet; seed() will handle it

        existing = s.exec(select(ProviderModel)).all()
        taken_names = {r.name for r in existing}
        taken_ids = {(r.provider_name, r.api_model_id) for r in existing}

        added = 0
        for catalog, model_type in ((PROVIDER_EMBEDDING_MODELS, "Text Embedding"),
                                    (PROVIDER_CHAT_MODELS, "LLM")):
            for m in catalog:
                if m["provider_name"] not in providers:
                    continue  # a provider this install doesn't carry
                if m["name"] in taken_names:
                    continue
                if (m["provider_name"], m["api_model_id"]) in taken_ids:
                    continue
                s.add(ProviderModel(**m, types=[model_type]))
                taken_names.add(m["name"])
                taken_ids.add((m["provider_name"], m["api_model_id"]))
                added += 1

        for name in ("Hugging Face Hub", "Azure OpenAI"):
            mp = providers.get(name)
            if mp and "Text Embedding" not in (mp.types or []):
                mp.types = [*(mp.types or []), "Text Embedding"]
                s.add(mp)

        # A provider with no credential must not display as configured.
        # Only ever tightens: a row that really has a key is untouched,
        # so this cannot undo someone's own configuration.
        for mp in providers.values():
            if mp.status == "credits" and not mp.api_key_configured and not mp.api_key:
                mp.status = "needs_key"
                s.add(mp)

        s.commit()
        return added


def demo_seeding_enabled() -> bool:
    """Whether to create the sample agents, runs, traces, alerts, roles
    and logs.

    OFF unless asked for. It used to be on for any database with no
    Agent row, which meant a fresh PRODUCTION database was seeded with
    synthetic operational records on first startup: runs and traces that
    distort every monitoring average, alerts nobody raised, "connected"
    data sources that are not connected, and role bindings to agents
    that only exist as examples. Nothing announced it and nothing
    distinguished it afterwards from real activity.

    Refused outright in production, not merely defaulted off: an
    ENVIRONMENT=production deployment that sets the flag has almost
    certainly inherited it from a dev template, and quietly writing demo
    rows because a config file said so is the failure this guards.
    """
    from .error_responses import is_production

    asked = os.getenv("SEED_DEMO_DATA", "").strip().lower() in ("1", "true", "yes", "on")
    if asked and is_production():
        logger.warning(
            "SEED_DEMO_DATA is set but ENVIRONMENT=production — refusing to seed demo data. "
            "Reference data (model providers, catalogued models, tool connectors, templates) "
            "is still installed; the sample agents/runs/traces/alerts/roles/logs are not."
        )
        return False
    return asked


def _bump_patch_version(value: str) -> str:
    try:
        major, minor, patch = (int(x) for x in value.split("."))
    except Exception:
        major, minor, patch = (1, 0, 0)
    return f"{major}.{minor}.{patch + 1}"


def _free_private_slot(s, *, name: str, key: str) -> None:
    """Make room for a release-managed Platform template without destroying
    locally authored Private or organization-managed templates.

    Active display names are case-insensitively unique. If a newly shipped
    Platform package claims a local template's name, the local draft is
    renamed. If that local template already had a published version, create a
    system patch snapshot with only the renamed published metadata so the
    gallery cannot show two cards with the same name. Existing unpublished
    draft content remains unpublished.
    """
    from datetime import datetime
    from uuid import uuid4

    clash = s.exec(
        select(Template).where(
            (func.lower(Template.name) == name.lower()) | (Template.key == key)
        )
    ).all()
    for row in clash:
        if row.source == "platform" and row.release_managed:
            continue

        old_draft_revision = row.draft_revision
        had_unpublished_changes = (
            row.current_version_id is not None
            and row.published_revision != row.draft_revision
        )
        renamed = False

        if row.status != "archived" and row.name.lower() == name.lower():
            suffix = "private" if row.source == "private" else "organization"
            n, candidate = 1, f"{name} ({suffix})"
            while s.exec(
                select(Template).where(
                    func.lower(Template.name) == candidate.lower(),
                    Template.status != "archived",
                    Template.id != row.id,
                )
            ).first():
                n += 1
                candidate = f"{name} ({suffix} {n})"
            row.name = candidate
            row.draft_revision += 1
            renamed = True

        if row.key == key:
            prefix = "private" if row.source == "private" else "platform-custom"
            row.key = f"{prefix}-{uuid4().hex[:12]}"

        # Keep the published gallery consistent with the forced rename without
        # publishing any unrelated draft edits the owner may be working on.
        if renamed and row.current_version_id:
            current = s.get(TemplateVersion, row.current_version_id)
            if current:
                now = datetime.utcnow()
                new_version = _bump_patch_version(row.version)
                snap = TemplateVersion(
                    id=f"tplv-{uuid4().hex[:24]}",
                    template_id=row.id,
                    version=new_version,
                    name=row.name,
                    type=current.type,
                    app_type=current.app_type,
                    desc=current.desc,
                    first_party=current.first_party,
                    flow=deepcopy(current.flow),
                    category=current.category,
                    vertical=current.vertical,
                    tags=list(current.tags or []),
                    requirements=deepcopy(current.requirements or {}),
                    published_at=now,
                    published_by="release-conflict-resolution",
                )
                s.add(snap)
                s.flush()
                row.version = new_version
                row.current_version_id = snap.id
                row.published_at = now
                row.published_by = "release-conflict-resolution"
                # If the draft was clean before the forced rename, the new
                # snapshot represents that whole revision. Otherwise preserve
                # the dirty marker so unrelated draft edits stay visibly
                # unpublished.
                if not had_unpublished_changes and row.published_revision == old_draft_revision:
                    row.published_revision = row.draft_revision

        s.add(row)
    s.flush()


def _semver_tuple(value: str):
    return tuple(int(x) for x in value.split("."))


def sync_platform_templates(s, packages=None) -> dict:
    """Synchronize release-managed Platform packages into immutable published
    template versions. Studio-authored Platform templates are never touched.

    Package versions are immutable. Startup is availability-safe: rollback
    packages and same-version content mutations are logged and skipped rather
    than taking down the data plane. The promotion command and CI package lock
    are the fail-closed release gates that reject those mistakes before deploy.
    """
    from datetime import datetime
    from uuid import uuid4
    from .template_packages import load_platform_packages

    packages = load_platform_packages() if packages is None else packages
    shipped = {p["key"] for p in packages}
    fields = (
        "name", "desc", "version", "type", "app_type", "category", "vertical",
        "tags", "flow", "requirements", "first_party",
    )
    content_fields = tuple(k for k in fields if k != "version")
    stats = {"added": 0, "updated": 0, "hidden": 0, "skipped": 0}

    existing = {
        r.key: r
        for r in s.exec(
            select(Template).where(
                Template.source == "platform", Template.release_managed == True  # noqa: E712
            )
        ).all()
    }

    # Preflight immutable-version mistakes before touching rows. A bad release
    # package must never take a data plane down at startup: promotion/CI are the
    # enforcement boundary, while runtime sync logs and skips the offending
    # package. This also makes intentional application rollbacks safe.
    skipped: set[str] = set()
    for pkg in packages:
        row = existing.get(pkg["key"])
        if row is None:
            continue
        package_changed = any(getattr(row, k) != pkg.get(k) for k in content_fields)
        version_changed = row.version != pkg["version"]
        if package_changed and not version_changed:
            logger.error(
                "Skipping Platform template %s: package content changed without a version bump (%s). "
                "Run promote_template so CI records a new immutable version.",
                pkg["key"], pkg["version"],
            )
            skipped.add(pkg["key"])
        elif version_changed and _semver_tuple(pkg["version"]) <= _semver_tuple(row.version):
            logger.warning(
                "Skipping Platform template %s during release rollback: database is at %s, package is %s.",
                pkg["key"], row.version, pkg["version"],
            )
            skipped.add(pkg["key"])

    stats["skipped"] = len(skipped)

    # Park only rows that will actually be updated. This allows two valid
    # packages to swap names without violating the active-name unique index,
    # while skipped rollback/bad packages keep their current names untouched.
    for p in packages:
        if p["key"] in skipped:
            continue
        row = existing.get(p["key"])
        if row is not None and row.name != p["name"]:
            row.name = f"__renaming__{row.id}"
            s.add(row)
    s.flush()

    for pkg in packages:
        if pkg["key"] in skipped:
            continue
        row = existing.get(pkg["key"])
        now = datetime.utcnow()
        if row is None:
            _free_private_slot(s, name=pkg["name"], key=pkg["key"])
            row = Template(
                id=f"tpl-{pkg['key']}", key=pkg["key"], source="platform",
                status="published", release_managed=True, hidden=False, owner="release",
                draft_revision=1, published_revision=1, published_at=now, published_by="release",
                **{k: pkg.get(k) for k in fields},
            )
            version = TemplateVersion(
                id=f"tplv-{uuid4().hex[:24]}", template_id=row.id, version=pkg["version"],
                name=pkg["name"], type=pkg["type"], app_type=pkg.get("app_type"),
                desc=pkg["desc"], first_party=pkg["first_party"], flow=pkg["flow"],
                category=pkg["category"], vertical=pkg["vertical"], tags=pkg["tags"],
                requirements=pkg["requirements"], published_at=now, published_by="release",
            )
            s.add(row); s.add(version); s.flush()
            row.current_version_id = version.id
            s.add(row)
            stats["added"] += 1
            continue

        package_changed = any(getattr(row, k) != pkg.get(k) for k in content_fields)
        version_changed = row.version != pkg["version"]

        changed = row.hidden or row.status != "published" or package_changed or version_changed
        if changed:
            if row.name != pkg["name"]:
                _free_private_slot(s, name=pkg["name"], key=pkg["key"])
            if package_changed or version_changed:
                row.draft_revision += 1
            for k in fields:
                setattr(row, k, pkg.get(k))
            row.release_managed = True
            row.source = "platform"
            row.status = "published"
            row.hidden = False
            row.updated_at = now
            row.published_at = now
            row.published_by = "release"

            if version_changed:
                version = TemplateVersion(
                    id=f"tplv-{uuid4().hex[:24]}", template_id=row.id, version=pkg["version"],
                    name=pkg["name"], type=pkg["type"], app_type=pkg.get("app_type"),
                    desc=pkg["desc"], first_party=pkg["first_party"], flow=pkg["flow"],
                    category=pkg["category"], vertical=pkg["vertical"], tags=pkg["tags"],
                    requirements=pkg["requirements"], published_at=now, published_by="release",
                )
                s.add(version); s.flush()
                row.current_version_id = version.id
            row.published_revision = row.draft_revision
            s.add(row)
            stats["updated"] += 1

    for key, row in existing.items():
        if key not in shipped and not row.hidden:
            row.hidden = True
            row.status = "archived"
            row.updated_at = datetime.utcnow()
            s.add(row)
            stats["hidden"] += 1
    s.flush()
    return stats


def seed_reference_data():
    """The platform's own capability catalog, which is NOT demo content
    and must exist for a fresh install to function: the model providers
    the Integrations page lists (without them there is nothing to attach
    an API key to), the catalogued chat and embedding models those
    providers serve, the tool/connector catalog a Tool node and the
    function-calling loop resolve against, and the shipped template
    gallery.

    Insert-only and idempotent per row, so it can run on every startup
    without duplicating anything or overwriting a value someone has
    edited — which is what makes it safe to separate from the
    once-only demo seeding below. Anything added to these lists later
    therefore reaches an EXISTING install too, which the old
    all-or-nothing seeder could not do (and which sync_model_catalog
    existed to work around for one of the four lists)."""
    with Session(engine) as s:
        for mp in MODEL_PROVIDERS:
            if not s.get(ModelProvider, mp["name"]):
                s.add(ModelProvider(**mp))
        for pm in PROVIDER_EMBEDDING_MODELS:
            if not s.get(ProviderModel, pm["name"]):
                s.add(ProviderModel(**pm, types=["Text Embedding"]))
        for cm in PROVIDER_CHAT_MODELS:
            if not s.get(ProviderModel, cm["name"]):
                s.add(ProviderModel(**cm, types=["LLM"]))
        for t in TOOLS:
            if not s.get(Tool, t["name"]):
                s.add(Tool(**t))
        for tool_name, actions in TOOL_ACTIONS.items():
            for name, desc, params in actions:
                exists = s.exec(
                    select(ToolAction).where(ToolAction.tool_name == tool_name, ToolAction.name == name)
                ).first()
                if not exists:
                    s.add(ToolAction(tool_name=tool_name, name=name, desc=desc, params=[
                        {"name": p[0], "type": p[1], "required": p[2], "desc": p[3]} for p in params
                    ]))
        sync_platform_templates(s)
        s.commit()


def seed_demo_data():
    """Sample agents and the synthetic operational history around them.

    Everything here is fiction: the runs never ran, the traces trace
    nothing, the alerts fired for nobody, the data sources marked
    "connected" are not connected, and the role bindings point at agents
    that exist only as examples. The group names, however, are real: see
    the comment on ROLES for why a fictional group name is not a harmless
    detail but the thing that blocks publish.

    Useful for a demo or a fresh dev environment; actively harmful in a
    real one, which is why demo_seeding_enabled() gates it."""
    with Session(engine) as s:
        if s.exec(select(Agent)).first():
            return  # already seeded

        for a in AGENTS:
            flow = dict(DEFAULT_FLOW)
            s.add(Agent(**a, flow=flow, metrics=AGENT_METRICS.get(a["id"], {})))
        for r in RUNS:
            s.add(Run(**r))
        for tr in TRACES:
            s.add(Trace(**tr))
        for al in ALERTS:
            s.add(Alert(**al))
        for ro in ROLES:
            s.add(Role(**ro))
        for ds in DATA_SOURCES:
            s.add(DataSource(**ds))
        for m in MCP_SERVERS:
            s.add(MCPServer(**m))
        for agent_id, logs in AGENT_LOGS.items():
            for ts, level, msg, trace_id in logs:
                s.add(AgentLog(agent_id=agent_id, ts=ts, level=level, msg=msg, trace_id=trace_id))
        s.commit()


def seed():
    """Startup entry point (app/main.py). Reference data always;
    demo data only when explicitly asked for and never in production."""
    seed_reference_data()
    if demo_seeding_enabled():
        logger.warning("SEED_DEMO_DATA is on — installing sample agents, runs, traces, "
                       "alerts, roles and logs. These are synthetic records.")
        seed_demo_data()
