"""Git connections: provider calls, token handling, and the routes.

Provider HTTP is faked at egress.safe_http_request (the one place every
outbound call goes through), and the routes run against an in-memory
SQLite session holding only the two tables they touch — nothing here
reaches GitHub/GitLab/Bitbucket or the shared Postgres database.
"""
import base64
import json

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import egress, git_integrations as gi
from app.db import get_session
from app.main import app
from app.models import Agent, GitConnection

TOKEN = "ghp_SECRET_TOKEN_123"


# The Agent table's Postgres JSONB columns, rendered as plain JSON for the
# in-memory SQLite the route tests use.
@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


class FakeProvider:
    """Records calls; answers by (method, url-substring) rules."""

    def __init__(self, rules):
        self.rules, self.calls = rules, []

    def __call__(self, method, url, *, headers=None, params=None, json=None, data=None, **kw):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "params": params,
                           "json": json, "data": data})
        for (m, frag), resp in self.rules.items():
            if m == method and frag in url:
                status, body, *hdrs = resp
                text = body if isinstance(body, str) else __import__("json").dumps(body)
                return {"status_code": status, "headers": hdrs[0] if hdrs else {}, "text": text,
                        "content": text.encode(), "final_url": url}
        raise AssertionError(f"unexpected call {method} {url}")


@pytest.fixture
def fake(monkeypatch):
    def install(rules):
        f = FakeProvider(rules)
        monkeypatch.setattr(egress, "safe_http_request", f)
        return f
    return install


# --------------------------------------------------------------------------- provider logic

def test_github_auth_headers_and_whoami(fake):
    f = fake({("GET", "/user"): (200, {"login": "octo"})})
    c = gi._Client("github", TOKEN)
    assert gi.whoami(c) == "octo"
    h = f.calls[0]["headers"]
    assert h["Authorization"] == f"Bearer {TOKEN}" and h["X-GitHub-Api-Version"] == "2022-11-28"
    assert f.calls[0]["url"] == "https://api.github.com/user"


def test_github_enterprise_and_gitlab_self_managed_api_bases():
    assert gi._Client("github", TOKEN, base_url="https://ghe.acme.io/").api == "https://ghe.acme.io/api/v3"
    assert gi._Client("gitlab", TOKEN, base_url="https://git.acme.io").api == "https://git.acme.io/api/v4"
    assert gi._Client("gitlab", TOKEN).api == "https://gitlab.com/api/v4"


@pytest.mark.parametrize("url", ["http://ghe.acme.io", "https://u:p@ghe.acme.io", "ftp://x", "https://ghe.acme.io?x=1"])
def test_unsafe_base_urls_refused(url):
    with pytest.raises(gi.GitIntegrationError):
        gi.normalize_base_url("github", url)


def test_bitbucket_server_url_refused():
    with pytest.raises(gi.GitIntegrationError, match="Bitbucket Cloud"):
        gi.normalize_base_url("bitbucket", "https://bitbucket.acme.io")


def test_gitlab_uses_private_token_header(fake):
    f = fake({("GET", "/user"): (200, {"username": "dev"})})
    assert gi.whoami(gi._Client("gitlab", TOKEN)) == "dev"
    assert f.calls[0]["headers"] == {"PRIVATE-TOKEN": TOKEN}


def test_bitbucket_basic_with_username_bearer_without(fake):
    f = fake({("GET", "/user"): (200, {"username": "bb"})})
    gi.whoami(gi._Client("bitbucket", TOKEN, username="me@acme.io"))
    assert f.calls[0]["headers"]["Authorization"] == "Basic " + base64.b64encode(f"me@acme.io:{TOKEN}".encode()).decode()
    f2 = fake({("GET", "/user"): (401, {}), ("GET", "/repositories"): (200, {"values": []})})
    assert gi.whoami(gi._Client("bitbucket", TOKEN)) == "access token"
    assert f2.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_github_create_then_update_sends_sha(fake):
    f = fake({("GET", "/contents/agents/a.yaml"): (404, {"message": "Not Found"}),
              ("PUT", "/contents/agents/a.yaml"): (201, {"commit": {"sha": "c1", "html_url": "u"},
                                                        "content": {"html_url": "f"}})})
    r = gi.commit_file(gi._Client("github", TOKEN), "acme/agents", "main", "agents/a.yaml", "x: 1\n", "msg")
    put = f.calls[1]
    assert r["created"] is True and r["commit_sha"] == "c1"
    assert "sha" not in put["json"] and base64.b64decode(put["json"]["content"]).decode() == "x: 1\n"
    assert put["json"]["branch"] == "main" and put["url"].startswith("https://api.github.com/repos/acme/agents/")
    f = fake({("GET", "/contents/agents/a.yaml"): (200, {"sha": "old"}),
              ("PUT", "/contents/agents/a.yaml"): (200, {"commit": {"sha": "c2"}, "content": {}})})
    r = gi.commit_file(gi._Client("github", TOKEN), "acme/agents", "main", "agents/a.yaml", "x: 2\n", "msg")
    assert r["created"] is False and f.calls[1]["json"]["sha"] == "old"


def test_gitlab_commit_action_follows_existence(fake):
    f = fake({("GET", "/repository/files/"): (200, {}),
              ("POST", "/repository/commits"): (201, {"id": "abc", "web_url": "w"})})
    gi.commit_file(gi._Client("gitlab", TOKEN), "grp/sub/proj", "dev", "agents/a.yaml", "x", "m")
    assert "/projects/grp%2Fsub%2Fproj/repository/files/agents%2Fa.yaml" in f.calls[0]["url"]
    assert f.calls[1]["json"]["actions"][0]["action"] == "update"
    f = fake({("GET", "/repository/files/"): (404, {}), ("POST", "/repository/commits"): (201, {"id": "abc"})})
    gi.commit_file(gi._Client("gitlab", TOKEN), "grp/proj", "dev", "agents/a.yaml", "x", "m")
    assert f.calls[1]["json"]["actions"][0]["action"] == "create"


def test_bitbucket_commit_is_form_post(fake):
    f = fake({("GET", "/src/main/agents/a.yaml"): (404, {}),
              ("POST", "/repositories/ws/repo/src"): (201, "", {"location": "https://api.bitbucket.org/2.0/repositories/ws/repo/commit/deadbeef"})})
    r = gi.commit_file(gi._Client("bitbucket", TOKEN), "ws/repo", "main", "agents/a.yaml", "x", "m")
    assert f.calls[1]["data"] == {"agents/a.yaml": "x", "message": "m", "branch": "main"}
    assert r["commit_sha"] == "deadbeef" and r["created"] is True


ADO = "https://dev.azure.com/acme"


def test_azure_devops_requires_an_organization_url():
    with pytest.raises(gi.GitIntegrationError, match="organization URL"):
        gi._Client("azure_devops", TOKEN)
    with pytest.raises(gi.GitIntegrationError, match="your-org"):
        gi._Client("azure_devops", TOKEN, base_url="https://dev.azure.com")
    assert gi._Client("azure_devops", TOKEN, base_url="https://dev.azure.com/acme/Proj/").base_url == ADO
    assert gi._Client("azure_devops", TOKEN, base_url="https://tfs.corp.example/DefaultCollection").base_url \
        == "https://tfs.corp.example/DefaultCollection"


def test_azure_devops_pat_is_basic_with_empty_username_and_whoami(fake):
    f = fake({("GET", "/_apis/connectionData"): (200, {"authenticatedUser": {"providerDisplayName": "Ana Diaz"}})})
    assert gi.whoami(gi._Client("azure_devops", TOKEN, base_url=ADO)) == "Ana Diaz"
    assert f.calls[0]["url"] == f"{ADO}/_apis/connectionData"
    assert f.calls[0]["headers"]["Authorization"] == "Basic " + base64.b64encode(f":{TOKEN}".encode()).decode()


def test_azure_devops_rejected_pat_203_reads_as_bad_token(fake):
    fake({("GET", "/_apis/connectionData"): (203, "<html>Sign in</html>")})
    with pytest.raises(gi.GitIntegrationError, match="rejected the token") as e:
        gi.whoami(gi._Client("azure_devops", TOKEN, base_url=ADO))
    assert TOKEN not in str(e.value)


def test_azure_devops_lists_repos_as_project_slash_repo_and_branches(fake):
    fake({("GET", "/_apis/git/repositories/agents%20repo/refs"): (200, {"value": [
              {"name": "refs/heads/main"}, {"name": "refs/heads/feature/x"}]}),
          ("GET", "/_apis/git/repositories"): (200, {"value": [
              {"name": "agents repo", "defaultBranch": "refs/heads/main", "webUrl": "w",
               "project": {"name": "Data Platform", "visibility": "private"}}]})})
    c = gi._Client("azure_devops", TOKEN, base_url=ADO)
    assert gi.list_repos(c) == [{"full_name": "Data Platform/agents repo", "default_branch": "main",
                                 "private": True, "url": "w"}]
    assert gi.list_branches(c, "Data Platform/agents repo") == ["main", "feature/x"]


def test_azure_devops_commit_is_a_push_on_the_branch_tip(fake):
    rules = {("GET", "/refs"): (200, {"value": [{"name": "refs/heads/main", "objectId": "tip123"}]}),
             ("GET", "/items"): (404, {}),
             ("POST", "/pushes"): (201, {"commits": [{"commitId": "c0ffee"}]})}
    f = fake(rules)
    r = gi.commit_file(gi._Client("azure_devops", TOKEN, base_url=ADO), "Proj/repo", "main",
                       "agents/a.yaml", "x", "m")
    push = f.calls[2]
    assert push["url"] == f"{ADO}/Proj/_apis/git/repositories/repo/pushes"
    assert push["json"]["refUpdates"] == [{"name": "refs/heads/main", "oldObjectId": "tip123"}]
    change = push["json"]["commits"][0]["changes"][0]
    assert change["changeType"] == "add" and change["item"] == {"path": "/agents/a.yaml"}
    assert r["created"] is True and r["commit_url"] == f"{ADO}/Proj/_git/repo/commit/c0ffee"
    f = fake({**rules, ("GET", "/items"): (200, {})})
    gi.commit_file(gi._Client("azure_devops", TOKEN, base_url=ADO), "Proj/repo", "main", "agents/a.yaml", "x", "m")
    assert f.calls[2]["json"]["commits"][0]["changes"][0]["changeType"] == "edit"


def test_azure_devops_unknown_branch_is_a_clear_error(fake):
    fake({("GET", "/refs"): (200, {"value": []})})
    with pytest.raises(gi.GitIntegrationError, match="no branch 'nope'"):
        gi.commit_file(gi._Client("azure_devops", TOKEN, base_url=ADO), "Proj/repo", "nope", "agents/a.yaml", "x", "m")


@pytest.mark.parametrize("status,needle", [(401, "rejected the token"), (403, "scopes"), (404, "couldn't find"),
                                           (409, "refused the change"), (500, "HTTP 500")])
def test_provider_errors_are_sentences_without_the_token(fake, status, needle):
    fake({("GET", "/user"): (status, {"message": f"bad {TOKEN}"})})
    with pytest.raises(gi.GitIntegrationError) as e:
        gi.whoami(gi._Client("github", TOKEN))
    assert needle in str(e.value) and TOKEN not in str(e.value)


def test_ssrf_block_becomes_a_clear_error(monkeypatch):
    def blocked(*a, **k):
        raise egress.SSRFBlocked("resolves to a private address")
    monkeypatch.setattr(egress, "safe_http_request", blocked)
    with pytest.raises(gi.GitIntegrationError, match="egress policy"):
        gi.whoami(gi._Client("gitlab", TOKEN, base_url="https://internal.acme.io"))


@pytest.mark.parametrize("path", ["", "../x.yaml", "agents/../x.yaml", "agents/a.json", "/"])
def test_bad_paths_refused(path):
    with pytest.raises(gi.GitIntegrationError):
        gi.clean_path(path)


def test_yaml_round_trips_to_the_dsl():
    dsl = {"dsl_version": "1", "name": "Ünïcode agent", "subtype": "workflow", "type": "app", "app_type": "workflow",
           "description": "multi\nline", "flow": {"nodes": [{"id": "n1", "type": "llm", "data": {"t": 0.2}}], "edges": []}}
    text = gi.agent_yaml(dsl)
    assert text.startswith("# InaiEra agent: Ünïcode agent")
    assert yaml.safe_load(text) == dsl
    assert gi.agent_filename("My Agent / v2!") == "my-agent-v2.yaml"


# --------------------------------------------------------------------------- routes

@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[GitConnection.__table__, Agent.__table__])

    def session_override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = session_override
    with Session(engine) as s:
        s.add(Agent(id="ag1", name="Invoice Bot", type="app", subtype="workflow", app_type="workflow",
                    plane="app", owner="qa", description="d", flow={"nodes": [], "edges": []}))
        s.commit()
    yield TestClient(app), engine
    app.dependency_overrides.pop(get_session, None)


def test_create_tests_token_first_and_never_returns_it(client, fake):
    c, engine = client
    fake({("GET", "/user"): (200, {"login": "octo"})})
    r = c.post("/api/git-connections", json={"provider": "github", "token": TOKEN, "default_repo": "acme/agents",
                                             "default_branch": "main"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account"] == "octo" and body["token"] == gi.MASK and TOKEN not in r.text
    with Session(engine) as s:
        stored = s.get(GitConnection, body["id"]).token
    assert stored.startswith("enc:") and TOKEN not in stored
    listed = c.get("/api/git-connections").text
    assert TOKEN not in listed and "octo" in listed


def test_bad_token_saves_nothing(client, fake):
    c, _ = client
    fake({("GET", "/user"): (401, {"message": "Bad credentials"})})
    r = c.post("/api/git-connections", json={"provider": "github", "token": TOKEN})
    assert r.status_code == 400 and "Nothing was saved" in r.json()["detail"]
    assert c.get("/api/git-connections").json()["connections"] == []


def test_commit_on_download_requires_defaults(client, fake):
    c, _ = client
    fake({("GET", "/user"): (200, {"login": "octo"})})
    r = c.post("/api/git-connections", json={"provider": "github", "token": TOKEN, "commit_on_download": True})
    assert r.status_code == 400 and "default repository" in r.json()["detail"]


def test_patch_with_mask_keeps_token_and_skips_retest(client, fake):
    c, _ = client
    f = fake({("GET", "/user"): (200, {"login": "octo"})})
    cid = c.post("/api/git-connections", json={"provider": "github", "token": TOKEN}).json()["id"]
    n = len(f.calls)
    r = c.patch(f"/api/git-connections/{cid}", json={"token": gi.MASK, "name": "Team repo", "default_folder": "/bots/"})
    assert r.status_code == 200 and r.json()["name"] == "Team repo" and r.json()["default_folder"] == "bots"
    assert len(f.calls) == n  # no provider call: credentials unchanged


def test_export_yaml_and_commit_with_defaults(client, fake):
    c, _ = client
    fake({("GET", "/user"): (200, {"login": "octo"})})
    cid = c.post("/api/git-connections", json={"provider": "github", "token": TOKEN, "default_repo": "acme/agents",
                                               "default_branch": "main", "default_folder": "agents"}).json()["id"]
    y = c.get("/api/agents/ag1/export.yaml")
    assert y.status_code == 200 and 'filename="invoice-bot.yaml"' in y.headers["content-disposition"]
    assert yaml.safe_load(y.text)["name"] == "Invoice Bot"
    f = fake({("GET", "/contents/agents/invoice-bot.yaml"): (404, {}),
              ("PUT", "/contents/agents/invoice-bot.yaml"): (201, {"commit": {"sha": "c9", "html_url": "cu"},
                                                                  "content": {"html_url": "fu"}})})
    r = c.post("/api/agents/ag1/git-commit", json={"connection_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "agents/invoice-bot.yaml" and r.json()["commit_sha"] == "c9"
    committed = base64.b64decode(f.calls[1]["json"]["content"]).decode()
    assert yaml.safe_load(committed)["name"] == "Invoice Bot"


def test_commit_failure_is_recorded_and_reported(client, fake):
    c, _ = client
    fake({("GET", "/user"): (200, {"login": "octo"})})
    cid = c.post("/api/git-connections", json={"provider": "github", "token": TOKEN, "default_repo": "acme/agents",
                                               "default_branch": "main"}).json()["id"]
    fake({("GET", "/contents/"): (403, {"message": "Resource not accessible by personal access token"})})
    r = c.post("/api/agents/ag1/git-commit", json={"connection_id": cid})
    assert r.status_code == 400 and "scopes" in r.json()["detail"]
    conn = [x for x in c.get("/api/git-connections").json()["connections"] if x["id"] == cid][0]
    assert "scopes" in conn["last_error"]


# --------------------------------------------------------------------------- egress: NAT64

@pytest.mark.parametrize("addr,unsafe", [
    ("64:ff9b::14cf:4955", False),   # 20.207.73.85 (api.github.com) via NAT64
    ("64:ff9b::a9fe:a9fe", True),    # 169.254.169.254 metadata, wrapped
    ("64:ff9b::a00:1", True),        # 10.0.0.1, wrapped
    ("64:ff9b::7f00:1", True),       # 127.0.0.1, wrapped
])
def test_nat64_addresses_are_judged_by_their_ipv4(addr, unsafe):
    import ipaddress
    assert egress._is_unsafe_ip(ipaddress.ip_address(addr)) is unsafe
