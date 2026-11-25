"""
A knowledge base is a FILESET in UCS: its role names the fileset tier's
privileges explicitly, a 409 on registration means "exists", and a real
failure reports UCS's status and message.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import ucs_provisioning
from app.ucs_sync import UCSSyncError


def test_the_role_secures_the_fileset_with_read_fileset(monkeypatch):
    calls = []
    monkeypatch.setattr(ucs_provisioning, "_admin_post", lambda path, body: calls.append((path, body)) or {})
    out = ucs_provisioning.grant_kb_access("acme_bot", "agent-builders")
    path, role = calls[0]
    assert path == "/api/v1/admin/rbac/roles"
    assert role["object_type"] == "fileset"
    assert role["access_level"] == "select"
    assert role["privileges"] == ["READ_FILESET"]
    assert "SELECT_TABLE" not in str(role)
    assert calls[1][1] == {"role_name": "kb-acme_bot-select", "group_name": "agent-builders",
                           "group_source": "custom"}
    assert out["role_status"] == "created" and out["bind_status"] == "bound"


def test_a_409_on_registration_is_exists(monkeypatch):
    def conflict(*a, **k):
        raise UCSSyncError("POST /api/v1/admin/filesets -> 409: {\"detail\":\"conflict\"}")
    monkeypatch.setattr(ucs_provisioning, "_create_schema", conflict)
    monkeypatch.setattr(ucs_provisioning, "_create_fileset", conflict)
    r = ucs_provisioning.ensure_kb_catalog_object("acme_bot")
    assert r["schema"] == "exists" and r["fileset"] == "exists"


def test_a_real_failure_carries_ucs_status_and_message(monkeypatch):
    def refused(*a, **k):
        raise UCSSyncError("POST /api/v1/admin/schemas -> 403: missing permission ucs:catalogs:manage")
    monkeypatch.setattr(ucs_provisioning, "_create_schema", refused)
    monkeypatch.setattr(ucs_provisioning, "_create_fileset", lambda *a, **k: {})
    r = ucs_provisioning.ensure_kb_catalog_object("acme_bot")
    assert r["schema"].startswith("error: UCSSyncError: POST /api/v1/admin/schemas -> 403")
    assert "ucs:catalogs:manage" in r["schema"]
    assert r["fileset"] == "created"
