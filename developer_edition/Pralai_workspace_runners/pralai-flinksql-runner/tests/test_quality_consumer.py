"""DQ-CONTRACT-005 — the consumer's topics/version come from the shared contract."""
from __future__ import annotations

import hashlib
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from app.core import quality_contract as qc
from app.core.config import Settings
from app.services import quality_consumer as c

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_settings_default_to_the_shared_contract():
    s = Settings()
    assert s.quality_contract_version == qc.CONTRACT_VERSION == "2.0"
    assert (s.quality_results_topic, s.quality_gate_topic, s.quality_quarantine_topic) == (
        qc.RESULTS_TOPIC, qc.GATE_TOPIC, qc.QUARANTINE_TOPIC)


def test_contract_file_hash_matches_module():
    raw = (ROOT / "app" / "contracts" / "quality_contract.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == qc.CONTRACT_SHA256


def _dq_checkout():
    env = os.environ.get("DQ_SERVICE_PATH")
    cands = [pathlib.Path(env)] if env else []
    cands.append(ROOT.parent.parent / "Pralai_dataquality_backend")
    return next((p for p in cands if (p / "app" / "contracts" / "quality_contract.json").exists()),
                None)


@pytest.mark.skipif(_dq_checkout() is None, reason="DQ service checkout not available "
                                                   "(set DQ_SERVICE_PATH)")
def test_contract_is_byte_identical_with_the_dq_service():
    dq = _dq_checkout()
    for rel in ("app/contracts/quality_contract.json", "app/core/quality_contract.py"):
        assert (dq / rel).read_bytes() == (ROOT / rel).read_bytes(), rel


def test_consumer_subscribes_to_contract_topics_with_manual_commit():
    s = Settings(kafka_bootstrap_servers="k:9092", kafka_security_protocol="PLAINTEXT",
                 quality_results_topic="custom.results")
    with patch.object(c, "KafkaConsumer") as k:
        k.return_value = MagicMock()
        c._make_consumer(s)
    assert k.call_args.args == ("custom.results", qc.GATE_TOPIC)
    assert k.call_args.kwargs["enable_auto_commit"] is False


def test_unimplemented_contract_version_is_misconfigured():
    s = Settings(quality_consumer_enabled=True, ucs_url="http://u", ucs_internal_token="t",
                 quality_contract_version="1.0")
    ok, detail = c.consumer_health(s)
    assert ok is False and detail["status"] == "misconfigured"
    assert "QUALITY_CONTRACT_VERSION" in detail["error"]
