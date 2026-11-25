"""
GAP-05: PipelineService.deploy() must verify a pipeline's connector class,
SMTs, converters, referenced ConfigProviders, the Kafka Connect worker's own
version, and the pipeline's mode are all actually supported by the live
Kafka Connect worker BEFORE ever calling deploy_connector — see
app/services/deploy_preflight.py.

These are direct unit tests of run_deploy_preflight() against a hand-rolled
fake KafkaConnectClient (no pipeline/connection/build_connector scaffolding
needed) — see test_deploy_preflight_integration.py for proof it's actually
wired into PipelineService.deploy() itself.
"""

import pytest

from app.core.config import settings
from app.core.errors import ConnectorCompatibilityError, KafkaConnectError
from app.services.deploy_preflight import run_deploy_preflight

JDBC_SOURCE_PLUGIN = {"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}
REROUTE_TRANSFORM_PLUGIN = {"class": "org.apache.kafka.connect.transforms.RegexRouter", "type": "transformation"}
OTHER_TRANSFORM_PLUGIN = {"class": "com.example.SomeOtherTransform", "type": "transformation"}
OTHER_CONVERTER_PLUGIN = {"class": "com.example.CustomConverter", "type": "converter"}


class _FakeClient:
    def __init__(self, plugins, worker_version: str = "3.7.0", worker_info_error: Exception | None = None):
        self._plugins = plugins
        self._worker_version = worker_version
        self._worker_info_error = worker_info_error

    async def list_connector_plugins(self):
        return self._plugins

    async def get_worker_info(self):
        if self._worker_info_error:
            raise self._worker_info_error
        return {"version": self._worker_version}


def _template(**overrides):
    return {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", **overrides}


def _pipeline(**overrides):
    return {"id": "pipe-1", "mode": "cdc", **overrides}


def _config(**overrides):
    return {
        "connector.class": "io.confluent.connect.jdbc.JdbcSourceConnector",
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        **overrides,
    }


def _failure_checks(exc_info) -> set[str]:
    return {f["check"] for f in exc_info.value.details["failures"]}


@pytest.mark.asyncio
async def test_passes_when_connector_class_installed_and_nothing_else_restricted() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=_config(), client=client)


@pytest.mark.asyncio
async def test_blocks_when_connector_class_not_installed() -> None:
    client = _FakeClient([])  # worker has nothing installed
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=_config(), client=client)
    assert "connector_class_installed" in _failure_checks(exc_info)
    assert exc_info.value.status_code == 422
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_sink_connector_class_matches_sink_type_entries_too() -> None:
    sink_plugin = {"class": "io.confluent.connect.jdbc.JdbcSinkConnector", "type": "sink", "version": "10.7.4"}
    client = _FakeClient([sink_plugin])
    template = _template(connector_class="io.confluent.connect.jdbc.JdbcSinkConnector")
    await run_deploy_preflight(template=template, pipeline=_pipeline(), config=_config(), client=client)


@pytest.mark.asyncio
async def test_blocks_when_worker_version_not_in_templates_qualified_list() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN], worker_version="3.5.2")
    template = _template(supported_kafka_connect_versions=["3.7", "3.8", "3.9"])
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=template, pipeline=_pipeline(), config=_config(), client=client)
    assert "kafka_connect_version_compatible" in _failure_checks(exc_info)


@pytest.mark.asyncio
async def test_allows_when_worker_version_major_minor_matches_qualified_list() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN], worker_version="3.9.1")  # patch version differs, major.minor matches
    template = _template(supported_kafka_connect_versions=["3.7", "3.8", "3.9"])
    await run_deploy_preflight(template=template, pipeline=_pipeline(), config=_config(), client=client)


@pytest.mark.asyncio
async def test_version_check_skipped_when_template_declares_no_supported_versions() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN], worker_version="0.0.1")  # would fail any real check
    template = _template()  # no supported_kafka_connect_versions
    await run_deploy_preflight(template=template, pipeline=_pipeline(), config=_config(), client=client)


@pytest.mark.asyncio
async def test_blocks_when_a_configured_smt_is_not_installed() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN, OTHER_TRANSFORM_PLUGIN])  # reports transforms, but not RegexRouter
    config = _config(transforms="reroute", **{"transforms.reroute.type": REROUTE_TRANSFORM_PLUGIN["class"]})
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)
    assert "smts_installed" in _failure_checks(exc_info)


@pytest.mark.asyncio
async def test_allows_when_configured_smt_is_installed() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN, REROUTE_TRANSFORM_PLUGIN])
    config = _config(transforms="reroute", **{"transforms.reroute.type": REROUTE_TRANSFORM_PLUGIN["class"],
                                              "transforms.reroute.regex": ".*", "transforms.reroute.replacement": "topic"})
    await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)


@pytest.mark.asyncio
async def test_smt_check_skipped_when_worker_reports_no_transformation_plugins_at_all() -> None:
    # Pre-3.5 Kafka Connect (or connectorsOnly=false simply not supported) —
    # the worker only ever reports connector plugins, never SMTs. Rather than
    # read that as "nothing is installed" and block every deploy, the check
    # is skipped entirely.
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    config = _config(transforms="reroute", **{"transforms.reroute.type": REROUTE_TRANSFORM_PLUGIN["class"],
                                              "transforms.reroute.regex": ".*", "transforms.reroute.replacement": "topic"})
    await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)


@pytest.mark.asyncio
async def test_blocks_when_a_configured_converter_is_not_installed() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN, OTHER_CONVERTER_PLUGIN])
    config = _config(**{"value.converter": "com.example.MissingConverter"})
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)
    assert "converters_available" in _failure_checks(exc_info)


@pytest.mark.asyncio
async def test_builtin_converters_are_never_blocked_even_when_worker_reports_other_converters() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN, OTHER_CONVERTER_PLUGIN])  # JsonConverter not in this list
    await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=_config(), client=client)


@pytest.mark.asyncio
async def test_converter_check_skipped_when_worker_reports_no_converter_plugins_at_all() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    config = _config(**{"value.converter": "com.example.MissingConverter"})
    await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)


@pytest.mark.asyncio
async def test_blocks_when_config_provider_referenced_is_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kafka_connect_config_providers", {})
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    config = _config(**{"connection.password": "${vault:secret/data/db:password}"})
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)
    assert "config_provider_available" in _failure_checks(exc_info)


@pytest.mark.asyncio
async def test_allows_config_provider_reference_when_declared_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "kafka_connect_config_providers",
        {"vault": "io.github.jaredpetersen.kafkaconnectvault.config.VaultConfigProvider"},
    )
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    config = _config(**{"connection.password": "${vault:secret/data/db:password}"})
    await run_deploy_preflight(template=_template(), pipeline=_pipeline(), config=config, client=client)


@pytest.mark.asyncio
async def test_blocks_when_pipeline_mode_not_in_templates_supported_modes() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    template = _template(supported_modes=["batch", "snapshot"])
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=template, pipeline=_pipeline(mode="cdc"), config=_config(), client=client)
    assert "source_mode_supported" in _failure_checks(exc_info)


@pytest.mark.asyncio
async def test_allows_pipeline_mode_declared_in_templates_supported_modes() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    template = _template(supported_modes=["batch", "cdc"])
    await run_deploy_preflight(template=template, pipeline=_pipeline(mode="cdc"), config=_config(), client=client)


@pytest.mark.asyncio
async def test_mode_check_skipped_when_template_declares_no_supported_modes() -> None:
    client = _FakeClient([JDBC_SOURCE_PLUGIN])
    template = _template()  # supported_modes undeclared — existing templates predate this field
    await run_deploy_preflight(template=template, pipeline=_pipeline(mode="anything"), config=_config(), client=client)


@pytest.mark.asyncio
async def test_every_failing_check_is_reported_together_not_just_the_first() -> None:
    client = _FakeClient([])  # connector class itself missing
    template = _template(supported_modes=["batch"])
    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(template=template, pipeline=_pipeline(mode="cdc"), config=_config(), client=client)
    checks = _failure_checks(exc_info)
    assert "connector_class_installed" in checks
    assert "source_mode_supported" in checks


@pytest.mark.asyncio
async def test_worker_unreachable_propagates_kafka_connect_error_not_swallowed() -> None:
    class _UnreachableClient:
        async def list_connector_plugins(self):
            raise KafkaConnectError("Kafka Connect error (502): connection refused")

    with pytest.raises(KafkaConnectError):
        await run_deploy_preflight(
            template=_template(), pipeline=_pipeline(), config=_config(), client=_UnreachableClient()
        )


# ── GAP-05 follow-up: the INSTALLED plugin's version, and installed-as-the-right-kind ───────────────────

from app.services.deploy_preflight import (  # noqa: E402
    CachedPluginClient,
    _numeric_version,
    plugin_version_failure,
)

JDBC_SOURCE = "io.confluent.connect.jdbc.JdbcSourceConnector"


def _plugin(version, kind="source", cls=JDBC_SOURCE):
    return {"class": cls, "type": kind, "version": version}


@pytest.mark.parametrize(("text", "parsed"), [
    ("10.9.7", (10, 9, 7)), ("2.7.3.Final", (2, 7, 3)), ("7.9.0-ccs", (7, 9, 0)), ("v3.1", (3, 1)),
    ("undefined", None), ("", None), (None, None),
])
def test_version_parsing_ignores_qualifiers_and_rejects_non_versions(text, parsed) -> None:
    assert _numeric_version(text) == parsed


@pytest.mark.parametrize(("installed", "ok"), [
    ("10.7.6", True),     # exactly what the template was qualified with
    ("10.9.7", True),     # newer minor/patch within the same major
    ("10.7.5", False),    # older patch
    ("10.6.0", False),    # older minor
    ("9.9.9", False),     # different (older) major
    ("11.0.0", False),    # different (newer) major: behaviour may have changed
])
def test_the_installed_plugin_must_be_the_qualified_major_and_not_older(installed, ok) -> None:
    failure = plugin_version_failure({"connector_version": "10.7.6"}, _plugin(installed))

    assert (failure is None) is ok
    if not ok:
        assert failure["check"] == "connector_plugin_version"
        assert installed in failure["reason"]
        assert "10.7.6" in failure["reason"]


def test_a_debezium_style_qualifier_does_not_defeat_the_comparison() -> None:
    template = {"connector_version": "2.7.3.Final"}

    assert plugin_version_failure(template, _plugin("2.5.4.Final")) is not None  # the live mismatch on a real stack
    assert plugin_version_failure(template, _plugin("2.7.3.Final")) is None
    assert plugin_version_failure(template, _plugin("2.8.0.Final")) is None


def test_no_comparison_is_made_when_either_side_has_no_usable_version() -> None:
    assert plugin_version_failure({}, _plugin("1.0")) is None                                  # template pins nothing
    assert plugin_version_failure({"connector_version": "10.7.6"}, _plugin("undefined")) is None  # worker can't say
    assert plugin_version_failure({"connector_version": "0.0.12"}, _plugin("0.0.0.0")) is None    # jar has no version metadata


def test_the_version_check_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deploy_preflight_enforce_plugin_versions", False)

    assert plugin_version_failure({"connector_version": "10.7.6"}, _plugin("1.0.0")) is None


@pytest.mark.asyncio
async def test_deploy_is_blocked_when_the_installed_plugin_is_older_than_the_template_was_qualified_with() -> None:
    client = _FakeClient([_plugin("10.6.0")])

    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(
            template=_template(connector_type="source", connector_version="10.7.6"),
            pipeline=_pipeline(), config=_config(), client=client,
        )

    assert "connector_plugin_version" in _failure_checks(exc_info)


@pytest.mark.asyncio
async def test_a_newer_patch_of_the_plugin_deploys() -> None:
    await run_deploy_preflight(
        template=_template(connector_type="source", connector_version="10.7.6"),
        pipeline=_pipeline(), config=_config(), client=_FakeClient([_plugin("10.9.7")]),
    )  # no exception


@pytest.mark.asyncio
async def test_a_source_template_whose_class_is_only_installed_as_a_sink_is_blocked_with_a_clear_reason() -> None:
    client = _FakeClient([_plugin("10.7.6", kind="sink")])

    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await run_deploy_preflight(
            template=_template(connector_type="source"), pipeline=_pipeline(), config=_config(), client=client,
        )

    failures = exc_info.value.details["failures"]
    assert [f["check"] for f in failures] == ["connector_class_installed"]
    assert "installed on the Kafka Connect worker as a sink connector" in failures[0]["reason"]


@pytest.mark.asyncio
async def test_a_template_without_a_connector_type_still_accepts_either_kind() -> None:
    await run_deploy_preflight(
        template=_template(), pipeline=_pipeline(), config=_config(), client=_FakeClient([_plugin("1.0", kind="sink")]),
    )


@pytest.mark.asyncio
async def test_the_plugin_list_cache_asks_the_worker_once_within_its_lifetime_and_again_after() -> None:
    class Counting:
        plugins = 0
        info = 0

        async def list_connector_plugins(self):
            Counting.plugins += 1
            return []

        async def get_worker_info(self):
            Counting.info += 1
            return {"version": "7.9.0"}

    cached = CachedPluginClient(Counting(), ttl_seconds=0.05)
    for _ in range(4):
        await cached.list_connector_plugins()
        await cached.get_worker_info()
    assert (Counting.plugins, Counting.info) == (1, 1)

    import asyncio

    await asyncio.sleep(0.08)
    await cached.list_connector_plugins()
    assert Counting.plugins == 2


def test_a_connector_bundled_with_kafka_reports_the_runtimes_version_and_is_not_compared_to_the_templates_pin() -> None:
    """FileStreamSourceConnector ships inside Apache Kafka, so Connect reports ITS version for it ("7.9.0-ccs" on Confluent
    Platform) while the template pins the connect-file jar's ("3.6.1"). Comparing the two blocked every file pipeline."""
    template = {"connector_version": "3.6.1"}
    installed = {"class": "org.apache.kafka.connect.file.FileStreamSourceConnector", "version": "7.9.0-ccs"}

    assert plugin_version_failure(template, installed) is None
    # ...but a connector with its own version is still checked
    assert plugin_version_failure({"connector_version": "10.7.6"}, {"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "version": "1.0.0"})
