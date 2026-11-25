"""Template packages — the one format a template travels in.

A package is a single JSON document:

    {
      "package_version": "1",
      "manifest": {key, name, version, type, app_type, category, vertical, tags, desc, first_party},
      "flow": {nodes: [...], edges: [...]},
      "requirements": {...}          # derived, see compute_requirements
    }

It is used in three places, and deliberately only one format:

  1. Platform templates ship as files in `app/template_packages/` and the
     seeder (seed.py) upserts them on every deploy and upgrade.
  2. "Save as template" in Studio stores an Organization-managed or Private draft in this
     data plane's own database, built with the same sanitiser and the
     same requirements derivation.
  3. Export / import — how the InAiEra team moves a template out of the
     authoring instance into the repo, and how a package file comes back
     in as a Private template.

Nothing in a package is tenant-specific: secret-looking literals are
blanked on the way out, and knowledge bases / catalog objects are
recorded as *requirements* for the installer to satisfy, not as
bindings that would be wrong in another data plane.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PACKAGE_VERSION = "1"

# Fixed vocabulary for the gallery's grouping. "Data" and "General" exist
# because the first-party Data templates and the blank canvas don't belong
# to a business function; they are not a place to put arbitrary values.
CATEGORIES: Tuple[str, ...] = ("Sales", "Service", "Operations", "Marketing", "Data", "General")

PACKAGES_DIR = Path(__file__).resolve().parent / "template_packages"

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ENV_REF_RE = re.compile(r"\{\{\s*env\.([A-Za-z0-9_]+)\s*\}\}")
_VAR_REF_RE = re.compile(r"^\s*\{\{.*\}\}\s*$")
# Config KEYS whose literal value is a credential. A {{reference}} is
# fine (it is resolved per data plane); a typed-in value is not portable
# and must not leave the instance it was typed into.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|authorization|bearer|private[_-]?key|credential)", re.I
)
_MAX_FLOW_BYTES = 2 * 1024 * 1024


class PackageError(ValueError):
    """The package is malformed or unsafe. The message is shown to the
    person who tried to import it, so it states what is wrong."""


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:80] or "template"


# ---------------------------------------------------------------------------
# Sanitising and requirements
# ---------------------------------------------------------------------------

def sanitize_flow(flow: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Deep copy of `flow` with literal credentials blanked.

    Returns (clean_flow, redactions) where each redaction is a readable
    "<node id>.<path>" so the person saving a template can see exactly
    what was removed. Values that are `{{...}}` references are kept."""
    clean = copy.deepcopy(flow or {})
    redactions: List[str] = []

    def walk(value: Any, path: str, node_id: str) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                here = f"{path}.{k}" if path else str(k)
                if (
                    isinstance(v, str)
                    and v.strip()
                    and _SECRET_KEY_RE.search(str(k))
                    and not _VAR_REF_RE.match(v)
                ):
                    out[k] = ""
                    redactions.append(f"{node_id}.{here}")
                else:
                    out[k] = walk(v, here, node_id)
            return out
        if isinstance(value, list):
            return [walk(v, f"{path}[{i}]", node_id) for i, v in enumerate(value)]
        return value

    for node in clean.get("nodes", []) or []:
        if isinstance(node, dict) and isinstance(node.get("config"), dict):
            node["config"] = walk(node["config"], "", str(node.get("id", "?")))
            if node.get("type") == "agent":
                cfg = node["config"]
                # Skills and files belong to the source agent and would
                # dangle in another one; env rows hold secrets under
                # arbitrary key names, so every value is blanked.
                for k in ("skills", "files"):
                    if cfg.pop(k, None):
                        redactions.append(f"{node.get('id', '?')}.{k}")
                if isinstance(cfg.get("env"), list):
                    rows = []
                    for r in cfg["env"]:
                        if isinstance(r, dict):
                            if r.get("value"):
                                redactions.append(f"{node.get('id', '?')}.env.{r.get('key', '')}")
                            rows.append({"key": r.get("key", ""), "value": ""})
                    cfg["env"] = rows
    return clean, redactions


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


from .agent_assets import template_asset_summary  # noqa: E402


def compute_requirements(flow: Dict[str, Any]) -> Dict[str, Any]:
    """What an installer has to provide for this flow to run.

    Derived from the flow itself so it can never disagree with it. The
    `review` list names every node that executes code or reaches out over
    the network — what a person should look at before trusting a template
    that someone else wrote."""
    nodes = [n for n in (flow or {}).get("nodes", []) or [] if isinstance(n, dict)]
    node_types = sorted({str(n.get("type")) for n in nodes if n.get("type")})
    kbs: set = set()
    catalog_objects: set = set()
    tools: set = set()
    models: set = set()
    env_vars: set = set()
    review: List[Dict[str, str]] = []

    for n in nodes:
        cfg = n.get("config") or {}
        t = n.get("type")
        if t == "retrieval":
            for kb in (cfg.get("knowledge_bases") or []):
                if kb:
                    kbs.add(str(kb))
            if cfg.get("knowledge_base"):
                kbs.add(str(cfg["knowledge_base"]))
        if t == "tool" and cfg.get("catalog_object"):
            obj = str(cfg["catalog_object"])
            (tools if obj.startswith("tool:") else catalog_objects).add(obj)
        for a in (cfg.get("allowed_tools") or []):
            if a:
                tools.add(str(a))
        if t in ("llm", "agent") and cfg.get("model"):
            models.add(str(cfg["model"]))
        if t == "agent":
            for a in (cfg.get("tools") or []):
                if a:
                    tools.add(str(a))
            if cfg.get("skills_embedded"):
                review.append({"node": str(n.get("id", "?")), "type": t,
                               "why": f"embeds {len(cfg['skills_embedded'])} skill(s): instructions the agent will follow"})
        for s in _strings(cfg):
            env_vars.update(_ENV_REF_RE.findall(s))
        if t in ("code", "http"):
            review.append({"node": str(n.get("id", "?")), "type": t,
                           "why": "runs custom code" if t == "code" else "makes outbound HTTP requests"})

    out = {
        "node_types": node_types,
        "knowledge_bases": sorted(kbs),
        "catalog_objects": sorted(catalog_objects),
        "tools": sorted(tools),
        "models": sorted(models),
        "env_vars": sorted(env_vars),
        "review": review,
    }
    # Skills/reference files the template carries, and files the installer must
    # upload. Only present when there is something to say, so packages that
    # predate this keep their content hash.
    assets = template_asset_summary(flow)
    if any(assets.values()):
        out["assets"] = assets
    return out


# ---------------------------------------------------------------------------
# Building and parsing packages
# ---------------------------------------------------------------------------

def build_package(t: Any) -> Dict[str, Any]:
    """Package for a Template row (or anything with the same attributes)."""
    return {
        "package_version": PACKAGE_VERSION,
        "manifest": {
            "key": t.key, "name": t.name, "version": t.version, "type": t.type,
            "app_type": getattr(t, "app_type", None),
            "category": t.category, "vertical": t.vertical, "tags": list(t.tags or []),
            "desc": t.desc, "first_party": bool(t.first_party),
        },
        "flow": copy.deepcopy(t.flow),
        "requirements": compute_requirements(t.flow),
    }


def _package_from_agent_dsl(dsl: Dict[str, Any], source_label: str) -> Dict[str, Any]:
    """Wrap an agent's DSL export (GET /api/agents/{id}/dsl) as a package.

    People export an agent and expect to import that file as a template;
    the DSL carries everything a manifest needs except the gallery
    fields, which take their defaults."""
    if str(dsl.get("dsl_version")) != "1":
        raise PackageError(
            f"{source_label}: unsupported dsl_version {dsl.get('dsl_version')!r} "
            "(this platform reads agent exports of version '1')"
        )
    ttype = dsl.get("type") if dsl.get("type") in ("App", "Data") else "App"
    return {
        "package_version": PACKAGE_VERSION,
        "manifest": {
            "name": dsl.get("name"), "type": ttype, "app_type": dsl.get("app_type"),
            "category": "Data" if ttype == "Data" else "General",
            "desc": dsl.get("description") or "", "first_party": False,
        },
        "flow": dsl.get("flow"),
    }


def parse_package(doc: Any, *, source_label: str = "package") -> Dict[str, Any]:
    """Validate a package document and return flat Template fields.

    Raises PackageError with a specific message. `requirements` in the
    document is ignored and recomputed: a package must not be able to
    claim it needs less than its flow actually uses."""
    if not isinstance(doc, dict):
        raise PackageError(f"{source_label}: not a JSON object")
    if "package_version" not in doc and "dsl_version" in doc:
        doc = _package_from_agent_dsl(doc, source_label)
    if str(doc.get("package_version")) != PACKAGE_VERSION:
        raise PackageError(
            f"{source_label}: unsupported package_version {doc.get('package_version')!r} "
            f"(this platform reads version {PACKAGE_VERSION!r})"
        )
    m = doc.get("manifest")
    if not isinstance(m, dict):
        raise PackageError(f"{source_label}: missing manifest")
    flow = doc.get("flow")
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), list) or not isinstance(flow.get("edges"), list):
        raise PackageError(f"{source_label}: flow must contain 'nodes' and 'edges' lists")
    if len(json.dumps(flow)) > _MAX_FLOW_BYTES:
        raise PackageError(f"{source_label}: flow is larger than {_MAX_FLOW_BYTES // (1024 * 1024)} MB")
    ids = [n.get("id") for n in flow["nodes"] if isinstance(n, dict)]
    if len(ids) != len(flow["nodes"]) or any(not i for i in ids):
        raise PackageError(f"{source_label}: every node needs an id")
    if len(set(ids)) != len(ids):
        raise PackageError(f"{source_label}: duplicate node ids")

    name = str(m.get("name") or "").strip()
    if not name or len(name) > 120:
        raise PackageError(f"{source_label}: name is required (max 120 characters)")
    key = str(m.get("key") or slugify(name))
    if not _KEY_RE.match(key):
        raise PackageError(f"{source_label}: key {key!r} must be lowercase letters, digits and hyphens")
    version = str(m.get("version") or "1.0.0")
    if not _SEMVER_RE.match(version):
        raise PackageError(f"{source_label}: version {version!r} must look like 1.2.3")
    ttype = m.get("type") or "App"
    if ttype not in ("App", "Data"):
        raise PackageError(f"{source_label}: type must be 'App' or 'Data'")
    app_type = m.get("app_type") or None
    if app_type is not None and app_type not in ("workflow", "chatflow", "chatbot", "agent", "text_generator"):
        raise PackageError(
            f"{source_label}: app_type must be workflow, chatflow, chatbot, agent or text_generator"
        )
    category = m.get("category") or "General"
    if category not in CATEGORIES:
        raise PackageError(f"{source_label}: category must be one of {', '.join(CATEGORIES)}")
    tags = m.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(x, str) for x in tags) or len(tags) > 20:
        raise PackageError(f"{source_label}: tags must be a list of up to 20 strings")
    vertical = (m.get("vertical") or None)
    if vertical is not None and (not isinstance(vertical, str) or len(vertical) > 60):
        raise PackageError(f"{source_label}: vertical must be a short string")

    return {
        "key": key, "name": name, "version": version, "type": ttype, "app_type": app_type,
        "category": category, "vertical": vertical, "tags": [t.strip() for t in tags if t.strip()],
        "desc": str(m.get("desc") or ""), "first_party": bool(m.get("first_party", True)),
        "flow": flow, "requirements": compute_requirements(flow),
    }


def load_platform_packages(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every package file shipped in this release, validated.

    A malformed shipped file is a build defect, so this raises rather
    than skipping it: a release that silently dropped a template would
    ship with the template quietly missing on every customer."""
    d = Path(directory) if directory else PACKAGES_DIR
    out: List[Dict[str, Any]] = []
    seen_keys: Dict[str, str] = {}
    seen_names: Dict[str, str] = {}
    for f in sorted(d.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise PackageError(f"{f.name}: invalid JSON ({e})") from e
        p = parse_package(doc, source_label=f.name)
        if p["key"].startswith("private-"):
            raise PackageError(f"{f.name}: key {p['key']!r} uses the 'private-' prefix, which is reserved for customer templates")
        if p["key"] in seen_keys:
            raise PackageError(f"{f.name}: key {p['key']!r} already used by {seen_keys[p['key']]}")
        if p["name"] in seen_names:
            raise PackageError(f"{f.name}: name {p['name']!r} already used by {seen_names[p['name']]}")
        seen_keys[p["key"]] = f.name
        seen_names[p["name"]] = f.name
        out.append(p)
    return out
