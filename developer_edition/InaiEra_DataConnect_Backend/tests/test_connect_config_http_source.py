"""The REST API source (castorm kafka-connect-http) config builder. Found by the release matrix: the template had no builder at all, and the connector's
own default turns one HTTP 429 or 503 into a FAILED task that stays failed until someone restarts it."""
import pytest

from app.services.connect_config import build_connector
from app.services.connect_config_t1 import (
    HTTP_DEFAULT_POLL_SECONDS,
    HTTP_RETRY_STATUS_CODES,
    HTTP_SOURCE,
    T1_CLASSES,
)

TEMPLATE = {"connector_class": HTTP_SOURCE, "connector_type": "source"}
TOPIC = "raw-rm-rest-events-api-v1"


def _pipeline(**cc) -> dict:
    return {
        "id": "p1", "org_id": "org_r", "domain": "rm", "source_system": "rest", "entity": "events", "version": "v1", "mode": "api",
        "topic_name": TOPIC, "connector_name": "rm--rest--events--api--v1", "connector_config": cc,
    }


async def _build(cc: dict, connection: dict | None = None):
    return await build_connector(_pipeline(**cc), {"connection_config": {"base_url": "http://api.example:8080"} if connection is None else connection}, TEMPLATE, schema=None)


def test_the_class_is_registered_so_the_dispatcher_finds_a_builder() -> None:
    assert HTTP_SOURCE in T1_CLASSES


@pytest.mark.asyncio
async def test_it_builds_a_complete_config_and_produces_to_the_pipeline_topic(fake_postgres) -> None:
    config, produced = await _build({"path": "/events", "params": "cursor=${offset.cursor}", "list_pointer": "/data",
                                     "offset_pointer": "cursor=/id", "initial_offset": "cursor=0"})
    assert produced == TOPIC
    assert config["connector.class"] == HTTP_SOURCE
    assert config["kafka.topic"] == TOPIC
    assert config["http.request.url"] == "http://api.example:8080/events"
    assert config["http.request.method"] == "GET"
    assert config["http.request.params"] == "cursor=${offset.cursor}"
    assert config["http.response.list.pointer"] == "/data"
    assert config["http.response.record.offset.pointer"] == "cursor=/id"
    assert config["http.offset.initial"] == "cursor=0"
    assert config["value.converter"] == "org.apache.kafka.connect.json.JsonConverter"
    assert config["value.converter.schemas.enable"] == "false"
    assert config["tasks.max"] == "1"


@pytest.mark.asyncio
async def test_a_429_and_server_errors_are_retried_not_fatal(fake_postgres) -> None:
    """The release-matrix findings: with the connector's default policy a 429 or 503 FAILED the task. They must be skipped (retried at the next poll)."""
    config, _ = await _build({"path": "/events"})
    skip = config["http.response.policy.codes.skip"]
    assert skip == f"300..399, {HTTP_RETRY_STATUS_CODES}"
    assert "429" in skip
    assert "500..599" in skip
    assert "process" not in "".join(k for k in config if k.endswith("codes.process")), "success stays the connector's default 200..299"


@pytest.mark.asyncio
async def test_the_retried_status_codes_can_be_changed(fake_postgres) -> None:
    config, _ = await _build({"path": "/events", "retry_status_codes": "429, 502..504"})
    assert config["http.response.policy.codes.skip"] == "300..399, 429, 502..504"


@pytest.mark.asyncio
async def test_the_default_poll_interval_is_longer_than_the_offset_flush_so_pages_are_not_republished(fake_postgres) -> None:
    config, _ = await _build({"path": "/events"})
    assert HTTP_DEFAULT_POLL_SECONDS > 60
    assert config["http.timer.interval.millis"] == str(HTTP_DEFAULT_POLL_SECONDS * 1000)
    assert config["http.timer"].endswith("FixedIntervalTimer")


@pytest.mark.asyncio
async def test_the_poll_interval_is_taken_in_seconds(fake_postgres) -> None:
    config, _ = await _build({"path": "/events", "poll_interval_seconds": 90})
    assert config["http.timer.interval.millis"] == "90000"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -5, "soon"])
async def test_a_bad_poll_interval_is_refused(fake_postgres, bad) -> None:
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        await _build({"path": "/events", "poll_interval_seconds": bad})


@pytest.mark.asyncio
async def test_a_timestamp_offset_gets_an_initial_timestamp_too(fake_postgres) -> None:
    config, _ = await _build({"path": "/events", "offset_pointer": "key=/id, timestamp=/ts", "initial_offset": "key=0"})
    assert config["http.offset.initial"] == "key=0, timestamp=1970-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_an_explicit_initial_timestamp_is_kept(fake_postgres) -> None:
    config, _ = await _build({"path": "/events", "offset_pointer": "timestamp=/ts", "initial_offset": "timestamp=2024-01-01T00:00:00Z"})
    assert config["http.offset.initial"] == "timestamp=2024-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_an_absolute_url_needs_no_base_url(fake_postgres) -> None:
    config, _ = await _build({"url": "https://api.example/v2/items"}, connection={})
    assert config["http.request.url"] == "https://api.example/v2/items"


@pytest.mark.asyncio
async def test_slashes_are_joined_once(fake_postgres) -> None:
    config, _ = await _build({"path": "events"}, connection={"base_url": "http://api.example:8080/"})
    assert config["http.request.url"] == "http://api.example:8080/events"


@pytest.mark.asyncio
async def test_raw_castorm_options_win_over_the_friendly_ones(fake_postgres) -> None:
    config, _ = await _build({"path": "/events", "http.request.url": "http://other/x", "http.response.policy.codes.skip": "300..399"})
    assert config["http.request.url"] == "http://other/x"
    assert config["http.response.policy.codes.skip"] == "300..399"


@pytest.mark.asyncio
async def test_method_headers_and_body_are_passed_on(fake_postgres) -> None:
    config, _ = await _build({"path": "/q", "method": "post", "headers": "X-Key: abc", "body": '{"a":1}'})
    assert (config["http.request.method"], config["http.request.headers"], config["http.request.body"]) == ("POST", "X-Key: abc", '{"a":1}')


@pytest.mark.asyncio
async def test_without_a_path_or_url_it_says_what_is_missing(fake_postgres) -> None:
    with pytest.raises(ValueError, match="'path'"):
        await _build({})


@pytest.mark.asyncio
async def test_without_any_base_url_a_relative_path_is_refused(fake_postgres) -> None:
    with pytest.raises(ValueError, match="base_url"):
        await _build({"path": "/events"}, connection={})
