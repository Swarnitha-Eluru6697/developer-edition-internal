"""
GAP-05: PipelineService.deploy() must verify a template's connector plugin —
and everything a specific pipeline's generated config actually asks the
Kafka Connect worker for (its connector class, SMTs, converters, and any
ConfigProvider indirection) — is really installed/configured on that live
worker BEFORE calling deploy_connector, not find out from a rejected PUT
(or, worse, a silently-accepted-but-broken task) after the fact.

GET /connector-plugins?connectorsOnly=false (KIP-769, Kafka Connect 3.5+) is
the one live introspection endpoint Connect exposes, and even it only
covers five plugin kinds: sink/source connectors, converters, header
converters, transformations and predicates — never ConfigProviders (there is
no REST endpoint that lists a worker's configured `config.providers`; it's a
worker-startup-only setting, not a discoverable plugin). So the six checks
below split across two different sources of truth:
  - connector class (and that it is installed as the right KIND, source or
    sink) / the INSTALLED PLUGIN'S VERSION against the version the template
    was qualified with / SMTs / converters / Kafka Connect version: checked
    against this live endpoint (app/services/runtime_orchestrator.py's
    KafkaConnectClient.list_connector_plugins/get_worker_info).
  - ConfigProvider: checked against settings.kafka_connect_config_providers,
    an operator-maintained mirror of the target cluster's own
    config.providers setting — the only source of truth this app can
    consult for that one (see app/core/config.py).
  - source mode: purely declarative — template["supported_modes"] (GAP-05's
    new ConnectorTemplate field) vs pipeline["mode"], no live check at all.

A worker older than 3.5 — or one that otherwise reports zero transformation/
converter plugin entries even with connectorsOnly=false — can't be asked
which SMTs/converters it has. Rather than treat "the worker didn't tell us"
as "none installed" and block every single deploy on such a cluster, those
two checks are skipped (with a warning logged) when the live response
reports no entries of that plugin type at all. The connector-class check has
no such escape hatch: connector plugins are always reported, on every
Connect version, with or without connectorsOnly=false.
"""

import re
import time
from typing import Any

import structlog

from app.core.config import settings
from app.core.errors import ConnectorCompatibilityError
from app.services.runtime_orchestrator import KafkaConnectClient
from app.services.transform_policy import transform_config_problems, transform_type_of_class

logger = structlog.get_logger(__name__)

# Ship with every Kafka Connect distribution (connect-api/connect-json) —
# always usable even on a worker whose connector-plugins response doesn't
# happen to enumerate them.
_BUILTIN_CONVERTERS = frozenset({
    "org.apache.kafka.connect.json.JsonConverter",
    "org.apache.kafka.connect.storage.StringConverter",
    "org.apache.kafka.connect.converters.ByteArrayConverter",
    "org.apache.kafka.connect.storage.SimpleHeaderConverter",
})

# Kafka Connect's own ${alias:path:key} ConfigProvider indirection syntax —
# see app/services/secrets.py's resolve_secret, the only place this app
# generates one today (alias is always "vault" there).
_PROVIDER_REF_RE = re.compile(r"\$\{([A-Za-z0-9_-]+):")


def _configured_transform_classes(config: dict[str, Any]) -> list[str]:
    names = [n for n in str(config.get("transforms") or "").split(",") if n]
    return [config[f"transforms.{name}.type"] for name in names if config.get(f"transforms.{name}.type")]


def transform_settings_problems(config: dict[str, Any]) -> list[str]:
    """Missing/invalid settings in the generated config's transforms — the ones Kafka Connect only rejects when the task starts
    (the pipeline then goes straight to FAILED after a deploy that reported success). Read from the generated config, not the
    policy, so a policy saved before transform_policy.validate_transform_settings existed is caught here too."""
    problems: list[str] = []
    for name in [n for n in str(config.get("transforms") or "").split(",") if n]:
        transform_type = transform_type_of_class(str(config.get(f"transforms.{name}.type") or ""))
        if transform_type is None:
            continue
        prefix = f"transforms.{name}."
        settings_of = {k[len(prefix):]: v for k, v in config.items() if k.startswith(prefix) and k != f"{prefix}type"}
        problems.extend(transform_config_problems(transform_type, settings_of))
    return problems


def _configured_converter_classes(config: dict[str, Any]) -> list[str]:
    return [config[key] for key in ("key.converter", "value.converter", "header.converter") if config.get(key)]


def _referenced_config_provider_aliases(config: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for value in config.values():
        if isinstance(value, str):
            aliases.update(_PROVIDER_REF_RE.findall(value))
    return aliases


def _numeric_version(version: str | None) -> tuple[int, ...] | None:
    """'2.7.3.Final' -> (2, 7, 3); '10.9.7' -> (10, 9, 7). Qualifier suffixes (.Final, -SNAPSHOT, -ccs) are
    ignored. None when there is no leading number to compare (e.g. Connect reports 'undefined' for some built-ins)."""
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", version or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def plugin_version_failure(template: dict[str, Any], installed: dict[str, Any]) -> dict[str, str] | None:
    """The template's `connector_version` is the version it was QUALIFIED against (its SMT notes, config defaults
    and migration notes describe that version). The installed plugin must be the same major version and not older:
    a newer minor/patch within the major is accepted (10.9.7 satisfies a 10.7.6 qualification), an older one or a
    different major is not — the template's behaviour may not exist there, or may have changed. Skipped (None) when
    either side has no comparable version, or when settings.deploy_preflight_enforce_plugin_versions is off."""
    if not settings.deploy_preflight_enforce_plugin_versions:
        return None
    # Connectors that ship INSIDE Apache Kafka (the FileStream connectors, MirrorMaker) have no version of their own: they
    # report the Connect RUNTIME's version (Confluent Platform's "7.9.0-ccs"), while a template pins the Kafka release its
    # connect-file jar came from ("3.6.1"). The two aren't comparable — what matters for these is the worker's own version,
    # which the template's supported_kafka_connect_versions check covers.
    if str(installed.get("class", "")).startswith("org.apache.kafka.connect."):
        return None
    pinned_text = template.get("connector_version")
    installed_text = installed.get("version")
    pinned, have = _numeric_version(pinned_text), _numeric_version(installed_text)
    if pinned is None or have is None:
        return None
    # "0.0.0.0" is what Connect reports for a plugin jar that carries no version metadata at all (the Redis sink),
    # not a real release older than everything: there is nothing to compare.
    if not any(have):
        return None
    name = installed.get("class", "the connector plugin")
    if have[0] != pinned[0]:
        return {
            "check": "connector_plugin_version",
            "reason": (
                f"Connector plugin '{name}' is version {installed_text} on the Kafka Connect worker, a different "
                f"major version from the {pinned_text} this template was qualified against."
            ),
        }
    if have < pinned:
        return {
            "check": "connector_plugin_version",
            "reason": (
                f"Connector plugin '{name}' is version {installed_text} on the Kafka Connect worker, older than "
                f"the {pinned_text} this template was qualified against — upgrade the plugin or use a template "
                "qualified for the installed version."
            ),
        }
    return None


class CachedPluginClient:
    """Wraps a KafkaConnectClient so the reconciler — which preflights every pipeline whose config is about to be
    applied, possibly many in one tick — asks the worker for its (large) plugin list once per `ttl` seconds
    instead of once per pipeline. Everything else passes straight through."""

    def __init__(self, client: Any, ttl_seconds: float = 30.0) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._plugins: tuple[float, list[dict[str, Any]]] | None = None
        self._worker: tuple[float, dict[str, Any]] | None = None

    async def list_connector_plugins(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._plugins is None or now - self._plugins[0] > self._ttl:
            self._plugins = (now, await self._client.list_connector_plugins())
        return self._plugins[1]

    async def get_worker_info(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._worker is None or now - self._worker[0] > self._ttl:
            self._worker = (now, await self._client.get_worker_info())
        return self._worker[1]


def _major_minor(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version


def connector_plugin_finding(template: dict[str, Any], plugins: list[dict[str, Any]]) -> tuple[str, str, str] | None:
    """Is the template's connector class installed on the worker, as the right kind, at an acceptable version? None when it is; otherwise
    (check, reason, code). The deploy preflight and the catalog's availability (NEW-09) BOTH ask this, so a connector the catalog calls available is
    one a deploy will not reject for its plugin, and the reverse."""
    by_type: dict[str, dict[str, dict]] = {}
    for p in plugins:
        by_type.setdefault(p.get("type", ""), {})[p.get("class", "")] = p
    connector_class = template.get("connector_class")
    expected_kind = (template.get("connector_type") or "").lower()  # "source" | "sink" | "" (unknown: accept either)
    if expected_kind in ("source", "sink"):
        installed = by_type.get(expected_kind, {}).get(connector_class)
        wrong_kind = next(
            (kind for kind in ("source", "sink") if kind != expected_kind and connector_class in by_type.get(kind, {})),
            None,
        )
    else:
        installed = by_type.get("source", {}).get(connector_class) or by_type.get("sink", {}).get(connector_class)
        wrong_kind = None
    if installed is None and wrong_kind is not None:
        return (
            "connector_class_installed",
            f"Connector class '{connector_class}' is installed on the Kafka Connect worker as a {wrong_kind} "
            f"connector, but this template is a {expected_kind} connector.",
            "PLUGIN_WRONG_KIND",
        )
    if installed is None:
        return (
            "connector_class_installed",
            f"Connector class '{connector_class}' is not installed on the Kafka Connect worker.",
            "PLUGIN_NOT_INSTALLED",
        )
    version_failure = plugin_version_failure(template, installed)
    if version_failure:
        return version_failure["check"], version_failure["reason"], "PLUGIN_VERSION_INCOMPATIBLE"
    return None


def worker_version_finding(template: dict[str, Any], worker_info: dict[str, Any]) -> tuple[str, str, str] | None:
    """Is the Kafka Connect worker's version one the template was qualified for? None when it is (or the template names none)."""
    supported_versions = template.get("supported_kafka_connect_versions") or []
    if not supported_versions:
        return None
    worker_version = _major_minor(str(worker_info.get("version", "")))
    if worker_version in {_major_minor(v) for v in supported_versions}:
        return None
    return (
        "kafka_connect_version_compatible",
        f"Kafka Connect worker version {worker_version} is not in this template's qualified versions {sorted(supported_versions)}.",
        "WORKER_VERSION_UNSUPPORTED",
    )


async def collect_preflight_failures(
    *, template: dict[str, Any], pipeline: dict[str, Any], config: dict[str, Any], client: KafkaConnectClient,
) -> list[dict[str, str]]:
    """The six checks themselves, factored out of run_deploy_preflight so
    FE-BUG-24's GET /pipelines/{id}/deploy-readiness (pipeline_service.py's
    check_deploy_readiness) can run them non-raising, alongside checks this
    module doesn't cover, without reimplementing them. A genuine failure to
    reach the Kafka Connect worker itself still propagates as
    KafkaConnectError (502, retryable, from _raise_for_connect/httpx) — a
    caller that wants that treated as just another failed check must catch
    it itself."""
    failures: list[dict[str, str]] = []

    plugins = await client.list_connector_plugins()
    by_type: dict[str, dict[str, dict]] = {}
    for p in plugins:
        by_type.setdefault(p.get("type", ""), {})[p.get("class", "")] = p

    if plugin_finding := connector_plugin_finding(template, plugins):
        failures.append({"check": plugin_finding[0], "reason": plugin_finding[1]})

    if template.get("supported_kafka_connect_versions"):
        if version_finding := worker_version_finding(template, await client.get_worker_info()):
            failures.append({"check": version_finding[0], "reason": version_finding[1]})

    transformation_plugins = by_type.get("transformation", {})
    if transformation_plugins:
        for smt_class in _configured_transform_classes(config):
            if smt_class not in transformation_plugins:
                failures.append({
                    "check": "smts_installed",
                    "reason": f"Transform class '{smt_class}' is not installed on the Kafka Connect worker.",
                })
    else:
        logger.warning("preflight.smt_check_skipped_worker_reports_none", pipeline_id=pipeline.get("id"))

    for problem in transform_settings_problems(config):
        failures.append({"check": "smt_settings_valid", "reason": problem})

    converter_plugins = by_type.get("converter", {})
    if converter_plugins:
        for conv_class in _configured_converter_classes(config):
            if conv_class in _BUILTIN_CONVERTERS:
                continue
            if conv_class not in converter_plugins:
                failures.append({
                    "check": "converters_available",
                    "reason": f"Converter class '{conv_class}' is not installed on the Kafka Connect worker.",
                })
    else:
        logger.warning("preflight.converter_check_skipped_worker_reports_none", pipeline_id=pipeline.get("id"))

    configured_providers = settings.kafka_connect_config_providers or {}
    for alias in sorted(_referenced_config_provider_aliases(config)):
        if alias not in configured_providers:
            failures.append({
                "check": "config_provider_available",
                "reason": (
                    f"Config provider '{alias}' referenced in the generated config is not declared as "
                    "configured on the Kafka Connect worker "
                    "(see DATACONNECT_KAFKA_CONNECT_CONFIG_PROVIDERS)."
                ),
            })

    supported_modes = template.get("supported_modes") or []
    if supported_modes and pipeline.get("mode") not in supported_modes:
        failures.append({
            "check": "source_mode_supported",
            "reason": (
                f"Pipeline mode '{pipeline.get('mode')}' is not supported by this connector "
                f"template (supports {sorted(supported_modes)})."
            ),
        })

    return failures


async def run_deploy_preflight(
    *, template: dict[str, Any], pipeline: dict[str, Any], config: dict[str, Any], client: KafkaConnectClient,
) -> None:
    """Raises ConnectorCompatibilityError (422, non-retryable, `details.failures`
    holds the structured per-check reasons) if any check fails. A genuine
    failure to reach the Kafka Connect worker itself propagates as
    KafkaConnectError (502, retryable, from _raise_for_connect/httpx) —
    deploy() must not proceed blind either way."""
    failures = await collect_preflight_failures(template=template, pipeline=pipeline, config=config, client=client)
    if failures:
        reasons = "; ".join(f["reason"] for f in failures)
        raise ConnectorCompatibilityError(
            f"Connector plugin compatibility preflight failed — deployment blocked: {reasons}",
            details={"failures": failures},
        )
