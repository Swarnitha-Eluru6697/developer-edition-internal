"""Template package, authoring, publishing, and release-management contract.

The product has one explicit lifecycle for both Studio-authored Platform and
Private templates: mutable draft -> immutable published TemplateVersion.
Release-managed Platform packages are the exception: the release pipeline owns
them and Studio treats them as read-only.
"""
import copy
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.db import engine
from app.models import Agent, Template, TemplateVersion
from app.seed import sync_platform_templates
from app.promote_template import check as check_promoted_packages, promote
from app.template_packages import (
    PackageError, compute_requirements, load_platform_packages, parse_package, sanitize_flow,
)

client = TestClient(app)
TAG = f"zz{uuid.uuid4().hex[:6]}"


def _flow(extra_cfg=None):
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "config": {}},
            {"id": "n2", "type": "http", "x": 1, "y": 0, "config": {
                "url": "https://example.com/api",
                "headers": {"Authorization": "Bearer sk-live-abc123", "X-Trace": "1"},
                "api_key": "{{env.PARTNER_KEY}}", **(extra_cfg or {})}},
            {"id": "n3", "type": "retrieval", "x": 2, "y": 0,
             "config": {"knowledge_bases": ["support_kb"]}},
            {"id": "n4", "type": "llm", "x": 3, "y": 0,
             "config": {"model": "Claude Sonnet 5", "system_prompt": "use {{env.BRAND}}"}},
            {"id": "n5", "type": "respond", "x": 4, "y": 0, "config": {}},
        ],
        "edges": [
            {"id": f"e{i}", "source": f"n{i}", "target": f"n{i + 1}", "source_handle": None}
            for i in range(1, 5)
        ],
    }


def _simple_flow():
    # Valid Workflow fixture: Workflow apps require an Output node.
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "config": {}},
            {"id": "n2", "type": "output", "x": 1, "y": 0,
             "config": {"variables": [{"name": "result", "expr": "{{input}}"}]}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
    }


def _chatflow():
    # Valid Chatflow fixture: a Respond node renders the response, but the
    # validator intentionally requires at least one node that can PRODUCE it.
    # Keep an LLM between Trigger and Respond so this fixture exercises the
    # same structural contract as a real chat template.
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "config": {}},
            {"id": "n2", "type": "llm", "x": 1, "y": 0,
             "config": {"model": "Claude Sonnet 5", "user_prompt": "{{input}}"}},
            {"id": "n3", "type": "respond", "x": 2, "y": 0, "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def _pkg(key, name, version="1.0.0", category="Sales", flow=None, app_type="workflow"):
    return {
        "package_version": "1",
        "manifest": {
            "key": key, "name": name, "version": version, "type": "App",
            "app_type": app_type, "category": category, "vertical": None,
            "tags": [], "desc": "d", "first_party": True,
        },
        "flow": flow or _simple_flow(), "requirements": {},
    }


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with Session(engine) as s:
        template_ids = [
            t.id for t in s.exec(select(Template)).all()
            if TAG in t.name or TAG in t.key or (t.owner or "").startswith(TAG)
        ]
        # Agents reference exact template versions, so remove tagged agents first.
        for a in s.exec(select(Agent)).all():
            if TAG in a.name or a.template_id in template_ids:
                s.delete(a)
        s.flush()
        for template_id in template_ids:
            t = s.get(Template, template_id)
            if t:
                # break the circular current-version reference before deleting snapshots
                t.current_version_id = None
                s.add(t)
        s.flush()
        for v in s.exec(select(TemplateVersion)).all():
            if v.template_id in template_ids:
                s.delete(v)
        s.flush()
        for template_id in template_ids:
            t = s.get(Template, template_id)
            if t:
                s.delete(t)
        s.commit()


def _make_agent(name=None, flow=None, app_type="workflow"):
    with Session(engine) as s:
        a = Agent(
            id=f"a{uuid.uuid4().hex[:10]}", name=name or f"{TAG} agent",
            type="App", subtype="Workflow", plane="App", owner="tests",
            status="draft", version="v1", health_status="—", app_type=app_type,
            flow=flow or _simple_flow(), metrics={},
        )
        s.add(a); s.commit()
        return a.id


def _save(name=None, *, source="private", flow=None, app_type="workflow"):
    r = client.post("/api/templates", json={
        "agent_id": _make_agent(flow=flow, app_type=app_type),
        "name": name or f"{TAG} template", "source": source,
        "category": "General", "owner": f"{TAG}-owner",
    })
    assert r.status_code == 200, r.text
    return r.json()


def _publish(t):
    r = client.post(
        f"/api/templates/{t['id']}/publish",
        headers={"If-Match": str(t["draft_revision"])}, json={},
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestPackageFormat:
    def test_sanitize_blanks_literal_credentials_but_keeps_references(self):
        clean, redactions = sanitize_flow(_flow())
        http = next(n for n in clean["nodes"] if n["id"] == "n2")["config"]
        assert http["headers"]["Authorization"] == ""
        assert http["headers"]["X-Trace"] == "1"
        assert http["api_key"] == "{{env.PARTNER_KEY}}"
        assert redactions == ["n2.headers.Authorization"]

    def test_sanitize_does_not_mutate_input(self):
        original = _flow(); before = copy.deepcopy(original)
        sanitize_flow(original)
        assert original == before

    def test_requirements_are_derived_from_flow(self):
        r = compute_requirements(_flow())
        assert r["knowledge_bases"] == ["support_kb"]
        assert r["models"] == ["Claude Sonnet 5"]
        assert r["env_vars"] == ["BRAND", "PARTNER_KEY"]

    def test_package_preserves_app_type(self):
        p = parse_package(_pkg(f"{TAG}-pkg", f"{TAG} package", app_type="chatflow"))
        assert p["app_type"] == "chatflow"

    @pytest.mark.parametrize("mutate,fragment", [
        (lambda d: d.update(package_version="2"), "unsupported package_version"),
        (lambda d: d["manifest"].update(category="Finance"), "category must be one of"),
        (lambda d: d["manifest"].update(version="1.0"), "must look like 1.2.3"),
        (lambda d: d["manifest"].update(key="Bad Key"), "lowercase letters"),
        (lambda d: d["manifest"].update(name=""), "name is required"),
    ])
    def test_malformed_packages_are_refused(self, mutate, fragment):
        doc = _pkg("ok", "Ok"); mutate(doc)
        with pytest.raises(PackageError, match=fragment):
            parse_package(doc)

    def test_shipped_packages_load(self):
        pkgs = load_platform_packages()
        assert len(pkgs) >= 7
        assert len({p["key"] for p in pkgs}) == len(pkgs)

    def test_shipped_packages_pass_publish_validation_and_immutable_lock(self):
        assert check_promoted_packages() == []

    def test_ci_check_detects_manual_content_change_without_version_bump(self, tmp_path):
        src = tmp_path / "source.json"
        src.write_text(json.dumps(_pkg(f"{TAG}-locked", f"{TAG} Locked", "1.0.0")))
        package_dir = tmp_path / "packages"
        promote(src, key=f"{TAG}-locked", version="1.0.0", dest_dir=package_dir)
        target = package_dir / f"{TAG}-locked.json"
        doc = json.loads(target.read_text())
        doc["manifest"]["desc"] = "manual edit without bump"
        target.write_text(json.dumps(doc))
        problems = check_promoted_packages(package_dir)
        assert any("content changed without a version bump" in p for p in problems)

    def test_malformed_shipped_file_fails_loudly(self, tmp_path):
        (tmp_path / "bad.json").write_text(json.dumps({"package_version": "1", "manifest": {}}))
        with pytest.raises(PackageError):
            load_platform_packages(tmp_path)


class TestDraftPublishLifecycle:
    def test_save_creates_private_draft_not_gallery_entry(self):
        saved = _save(flow=_flow())["template"]
        assert saved["source"] == "private"
        assert saved["status"] == "draft"
        assert saved["version"] == "0.0.0"
        assert saved["current_version_id"] is None
        assert saved["first_party"] is False
        assert all(x["id"] != saved["id"] for x in client.get("/api/templates").json())
        assert any(x["id"] == saved["id"] for x in client.get("/api/templates/manage").json())

    def test_platform_authored_template_uses_same_draft_publish_lifecycle(self):
        saved = _save(name=f"{TAG} platform", source="platform")["template"]
        assert (saved["source"], saved["status"], saved["release_managed"]) == ("platform", "draft", False)
        published = _publish(saved)
        assert published["template"]["status"] == "published"
        assert published["version"]["version"] == "1.0.0"
        assert any(x["id"] == saved["id"] for x in client.get("/api/templates").json())

    def test_publish_creates_immutable_version_and_gallery_uses_snapshot(self):
        saved = _save(name=f"{TAG} immutable")["template"]
        pub1 = _publish(saved)
        assert pub1["version"]["version"] == "1.0.0"
        rev = pub1["template"]["draft_revision"]

        # Edit draft metadata. Gallery stays on v1 snapshot.
        r = client.put(
            f"/api/templates/{saved['id']}", headers={"If-Match": str(rev)},
            json={"desc": "new draft description"},
        )
        assert r.status_code == 200, r.text
        draft = r.json()["template"]
        assert draft["has_unpublished_changes"] is True
        gallery = next(x for x in client.get("/api/templates").json() if x["id"] == saved["id"])
        assert gallery["desc"] != "new draft description"

        pub2 = _publish(draft)
        assert pub2["version"]["version"] == "1.0.1"
        versions = client.get(f"/api/templates/{saved['id']}/versions").json()
        assert [v["version"] for v in versions[:2]] == ["1.0.1", "1.0.0"]

    def test_publish_is_idempotent_for_same_revision(self):
        saved = _save(name=f"{TAG} idem")["template"]
        first = _publish(saved)
        second = client.post(
            f"/api/templates/{saved['id']}/publish",
            headers={"If-Match": str(first["template"]["draft_revision"])}, json={},
        )
        assert second.status_code == 200
        assert second.json()["version"]["id"] == first["version"]["id"]

    def test_unfinished_flow_can_be_saved_but_not_published(self):
        bad = {"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "config": {}}], "edges": []}
        saved = _save(name=f"{TAG} unfinished", flow=bad)
        assert isinstance(saved["issues"], list)
        r = client.post(
            f"/api/templates/{saved['template']['id']}/publish",
            headers={"If-Match": str(saved["template"]["draft_revision"])}, json={},
        )
        assert r.status_code == 422
        assert next(x for x in client.get("/api/templates/manage").json() if x["id"] == saved["template"]["id"])["status"] == "draft"

    def test_stale_edit_revision_is_rejected(self):
        saved = _save(name=f"{TAG} cas")["template"]
        r1 = client.put(
            f"/api/templates/{saved['id']}", headers={"If-Match": str(saved["draft_revision"])},
            json={"desc": "first"},
        )
        assert r1.status_code == 200
        r2 = client.put(
            f"/api/templates/{saved['id']}", headers={"If-Match": str(saved["draft_revision"])},
            json={"desc": "stale"},
        )
        assert r2.status_code == 409

    def test_refresh_from_agent_changes_only_draft_until_republish(self):
        saved = _publish(_save(name=f"{TAG} refresh")["template"])["template"]
        newer_agent = _make_agent(flow=_flow())
        r = client.put(
            f"/api/templates/{saved['id']}", headers={"If-Match": str(saved["draft_revision"])},
            json={"refresh_from_agent_id": newer_agent},
        )
        assert r.status_code == 200
        draft = r.json()["template"]
        assert len(draft["flow"]["nodes"]) == 5
        gallery = next(x for x in client.get("/api/templates").json() if x["id"] == saved["id"])
        assert len(gallery["flow"]["nodes"]) == 2

    def test_archive_removes_from_active_surfaces_but_keeps_history(self):
        saved = _publish(_save(name=f"{TAG} archive")["template"])["template"]
        versions_before = client.get(f"/api/templates/{saved['id']}/versions").json()
        r = client.delete(f"/api/templates/{saved['id']}")
        assert r.status_code == 200 and r.json()["archived"] is True
        assert all(x["id"] != saved["id"] for x in client.get("/api/templates").json())
        versions_after = client.get(f"/api/templates/{saved['id']}/versions").json()
        assert versions_after == versions_before

    def test_facets_use_shared_gallery_query_and_are_not_empty(self):
        data = client.get("/api/templates/facets").json()
        gallery = client.get("/api/templates").json()
        assert sum(data["sources"].values()) == len(gallery)
        assert sum(data["origins"].values()) == len(gallery)
        assert data["sources"]["platform"] >= 1
        assert data["origins"]["release"] >= 1

    def test_archived_name_can_be_reused(self):
        name = f"{TAG} reusable"
        first = _save(name=name)["template"]
        assert client.delete(f"/api/templates/{first['id']}").status_code == 200
        second = _save(name=name)["template"]
        assert second["id"] != first["id"]

    def test_literal_underscore_and_percent_are_not_sql_wildcards(self):
        assert _save(name=f"{TAG} abc")["template"]["name"] == f"{TAG} abc"
        assert _save(name=f"{TAG} a_c")["template"]["name"] == f"{TAG} a_c"
        assert _save(name=f"{TAG} a%c")["template"]["name"] == f"{TAG} a%c"

    def test_quoted_if_match_header_is_accepted(self):
        saved = _save(name=f"{TAG} quoted-etag")["template"]
        r = client.put(
            f"/api/templates/{saved['id']}",
            headers={"If-Match": f'"{saved["draft_revision"]}"'},
            json={"desc": "quoted works"},
        )
        assert r.status_code == 200, r.text
        edited = r.json()["template"]
        p = client.post(
            f"/api/templates/{saved['id']}/publish",
            headers={"If-Match": f'W/"{edited["draft_revision"]}"'},
            json={},
        )
        assert p.status_code == 200, p.text

    def test_identical_save_is_a_noop_and_does_not_create_a_new_version(self):
        pub = _publish(_save(name=f"{TAG} no-op")["template"])
        t = pub["template"]
        versions_before = client.get(f"/api/templates/{t['id']}/versions").json()
        r = client.put(
            f"/api/templates/{t['id']}",
            headers={"If-Match": str(t["draft_revision"])},
            json={"name": t["name"], "desc": t["desc"], "category": t["category"],
                  "vertical": t["vertical"], "tags": t["tags"]},
        )
        assert r.status_code == 200, r.text
        unchanged = r.json()["template"]
        assert unchanged["draft_revision"] == t["draft_revision"]
        again = _publish(unchanged)
        assert again["version"]["id"] == pub["version"]["id"]
        assert client.get(f"/api/templates/{t['id']}/versions").json() == versions_before



class TestPlatformReleaseManagement:
    def _sync(self, packages):
        with Session(engine) as s:
            stats = sync_platform_templates(s, packages); s.commit(); return stats

    def _row(self, key):
        with Session(engine) as s:
            return s.exec(select(Template).where(Template.key == key)).first()

    def test_same_release_is_idempotent_and_new_version_adds_snapshot(self):
        shipped = load_platform_packages()
        p1 = parse_package(_pkg(f"{TAG}-release", f"{TAG} Release", "1.0.0"))
        self._sync(shipped + [p1])
        assert self._sync(shipped + [p1]) == {"added": 0, "updated": 0, "hidden": 0, "skipped": 0}
        p2 = parse_package(_pkg(f"{TAG}-release", f"{TAG} Release", "1.1.0", category="Marketing"))
        self._sync(shipped + [p2])
        row = self._row(f"{TAG}-release")
        assert row.release_managed is True and row.version == "1.1.0" and row.category == "Marketing"
        with Session(engine) as s:
            versions = s.exec(select(TemplateVersion).where(TemplateVersion.template_id == row.id)).all()
            assert {v.version for v in versions} == {"1.0.0", "1.1.0"}

    def test_release_content_change_without_version_bump_is_logged_and_skipped_not_startup_fatal(self):
        shipped = load_platform_packages()
        p1 = parse_package(_pkg(f"{TAG}-immutable-release", f"{TAG} Immutable", "1.0.0"))
        self._sync(shipped + [p1])
        changed = parse_package(_pkg(f"{TAG}-immutable-release", f"{TAG} Immutable", "1.0.0", category="Marketing"))
        stats = self._sync(shipped + [changed])
        assert stats["skipped"] == 1
        row = self._row(f"{TAG}-immutable-release")
        assert row.version == "1.0.0" and row.category == "Sales"

    def test_release_rollback_is_logged_and_skipped_not_startup_fatal(self):
        shipped = load_platform_packages()
        newer = parse_package(_pkg(f"{TAG}-rollback", f"{TAG} Rollback", "2.0.0"))
        self._sync(shipped + [newer])
        older = parse_package(_pkg(f"{TAG}-rollback", f"{TAG} Rollback", "1.0.0"))
        stats = self._sync(shipped + [older])
        assert stats["skipped"] == 1
        assert self._row(f"{TAG}-rollback").version == "2.0.0"

    def test_release_name_clash_renames_local_template_and_publishes_consistent_snapshot(self):
        clash_name = f"{TAG} Clash"
        private = _publish(_save(name=clash_name)["template"])
        shipped = load_platform_packages()
        incoming = parse_package(_pkg(f"{TAG}-clash-release", clash_name, "1.0.0"))
        self._sync(shipped + [incoming])
        gallery = client.get("/api/templates").json()
        names = [x["name"].lower() for x in gallery]
        assert len(names) == len(set(names))
        renamed = next(x for x in gallery if x["id"] == private["template"]["id"] )
        assert renamed["name"].startswith(clash_name) and renamed["name"] != clash_name
        versions = client.get(f"/api/templates/{renamed['id']}/versions").json()
        assert versions[0]["name"] == renamed["name"]

    def test_dropped_release_template_is_archived_not_deleted(self):
        shipped = load_platform_packages()
        p = parse_package(_pkg(f"{TAG}-drop", f"{TAG} Drop"))
        self._sync(shipped + [p]); self._sync(shipped)
        row = self._row(f"{TAG}-drop")
        assert row.hidden is True and row.status == "archived"
        with Session(engine) as s:
            assert s.exec(select(TemplateVersion).where(TemplateVersion.template_id == row.id)).first()

    def test_studio_platform_template_survives_release_sync(self):
        studio = _save(name=f"{TAG} Studio Platform", source="platform")["template"]
        self._sync(load_platform_packages())
        with Session(engine) as s:
            row = s.get(Template, studio["id"])
            assert row and row.release_managed is False and row.status == "draft"

    def test_release_managed_template_is_read_only_in_studio(self):
        shipped = next(x for x in client.get("/api/templates/manage").json() if x["release_managed"])
        r = client.put(
            f"/api/templates/{shipped['id']}", headers={"If-Match": str(shipped["draft_revision"])},
            json={"desc": "hacked"},
        )
        assert r.status_code == 403
        assert client.delete(f"/api/templates/{shipped['id']}").status_code == 403


class TestInstallDuplicateExportImport:
    def test_instantiate_requires_published_snapshot_and_preserves_app_type_and_provenance(self):
        draft = _save(name=f"{TAG} chat", app_type="chatflow", flow=_chatflow())["template"]
        assert client.post(f"/api/templates/{draft['id']}/instantiate", json={}).status_code == 404
        pub = _publish(draft)
        r = client.post(f"/api/templates/{draft['id']}/instantiate", json={"name": f"{TAG} child"})
        assert r.status_code == 200, r.text
        a = r.json()["agent"]
        assert a["app_type"] == "chatflow"
        assert a["template_id"] == draft["id"]
        assert a["template_version_id"] == pub["version"]["id"]
        assert a["template_name"] == f"{TAG} chat"

    def test_obsolete_template_name_field_is_rejected_not_silently_ignored(self):
        r = client.post("/api/agents", json={
            "name": f"{TAG} obsolete", "subtype": "Workflow", "owner": "tests",
            "template_name": "Blank canvas",
        })
        assert r.status_code == 422

    def test_create_agent_uses_stable_template_id(self):
        pub = _publish(_save(name=f"{TAG} create-agent")["template"])
        r = client.post("/api/agents", json={
            "name": f"{TAG} via id", "subtype": "Workflow", "owner": "tests",
            "template_id": pub["template"]["id"],
        })
        assert r.status_code == 200, r.text
        assert r.json()["template_version_id"] == pub["version"]["id"]

    def test_duplicate_is_private_draft_and_can_copy_published_or_draft_state(self):
        pub = _publish(_save(name=f"{TAG} source")["template"])
        r = client.post(f"/api/templates/{pub['template']['id']}/duplicate", json={"state": "published"})
        assert r.status_code == 200
        assert (r.json()["source"], r.json()["status"], r.json()["current_version_id"]) == ("private", "draft", None)

    def test_export_import_roundtrip_creates_private_draft(self):
        pub = _publish(_save(name=f"{TAG} export")["template"])
        pkg = client.get(f"/api/templates/{pub['template']['id']}/export?state=published").json()
        assert pkg["manifest"]["app_type"] == "workflow"
        r = client.post("/api/templates/import", json={"package": pkg, "name": f"{TAG} imported"})
        assert r.status_code == 200, r.text
        assert (r.json()["template"]["source"], r.json()["template"]["status"]) == ("private", "draft")

    def test_export_draft_is_allowed_before_first_publish(self):
        draft = _save(name=f"{TAG} draft export")["template"]
        assert client.get(f"/api/templates/{draft['id']}/export?state=published").status_code == 409
        assert client.get(f"/api/templates/{draft['id']}/export?state=draft").status_code == 200
