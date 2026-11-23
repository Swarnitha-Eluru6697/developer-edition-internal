"""Live end-to-end scenarios for the Workspace platform (X-007): gateway -> Workspace -> dbt runner / Flink runner / DQ / UCS.

Not a unit test: it drives the REAL stack (the one `docker compose` runs locally, or a staging data plane given the same URLs) and
checks what no single repository can: identity and RBAC through the gateway, the delegation chain, one dbt run, one Flink
deployment, DQ delivery, a restart of the API pod mid-flight, outages of Redis / UCS / Kafka / the Flink runner, cancellation and
idempotent retry, and that no secret ends up in logs, metadata or generated SQL. Every scenario cleans up what it created.

    python scripts/e2e/platform_e2e.py                    # all scenarios
    python scripts/e2e/platform_e2e.py identity flink     # only these
    python scripts/e2e/platform_e2e.py --list

Needs: the dev JWKS key (~/.inaiera-dev-jwks/key.pem), `docker` for the stack's containers (restarts, outages, log scans), and
python-jose. Defaults are the local-stack ports; override with E2E_* environment variables. Exit code 1 if any scenario fails.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from jose import jwt

GATEWAY = os.environ.get("E2E_GATEWAY", "http://localhost:5179")        # the dev gateway in front of Workspace (stands in for Envoy)
DBT = os.environ.get("E2E_DBT", "http://localhost:8082")
FLINK = os.environ.get("E2E_FLINK", "http://localhost:8085")
DQ = os.environ.get("E2E_DQ", "http://localhost:8087")
KEY = Path(os.environ.get("E2E_JWKS_KEY", "~/.inaiera-dev-jwks/key.pem")).expanduser()
ORG = os.environ.get("E2E_ORG", "local-dev")
C = {"workspace": "workspace-workspace-1", "ws_pg": "workspace-postgres-1", "ws_redis": "workspace-redis-1",
     "dbt": "dbtrunner-dbtrunner-1", "dbt_pg": "dbtrunner-postgres-1", "flink": "flinksql-flinksql-runner-1",
     "flink_pg": "flinksql-postgres-1", "kafka": "flinksql-kafka-1", "dq": "dataquality-dataquality-1",
     "dq_pg": "dataquality-postgres-1", "ucs": "ucs-catalog-1"}

RESULTS: list[tuple[str, bool, str]] = []
SCENARIOS: dict[str, object] = {}


# ── plumbing ──────────────────────────────────────────────────────────────────
def scenario(fn):
    SCENARIOS[fn.__name__.removeprefix("s_")] = fn
    return fn


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else (f"   ({detail})" if detail else "")), flush=True)
    return bool(ok)


# The Workspace permission IDs (permissions.json). Tokens carry permissions, never role names (contract 01-CONTRACT.md).
ALL = [p["id"] for p in json.loads((Path(__file__).resolve().parents[2] / "permissions.json").read_text())["permissions"]]
READER = ["workspace:access:view", "workspace:files:edit", "workspace:catalog:view", "workspace:datasets:view", "workspace:dq:view"]
JOBS = READER + ["workspace:sql:run", "workspace:git:write", "workspace:dbt:edit", "workspace:dbt:run", "workspace:sql-jobs:edit",
                 "workspace:sql-jobs:run", "workspace:python-jobs:edit", "workspace:python-jobs:run"]
STREAMING = READER + ["workspace:sql:run", "workspace:git:write", "workspace:flink:edit", "workspace:flink:deploy"]
DQ_PUBLISHER = READER + ["workspace:dq:publish"]
DQ_AUTHOR = READER + ["workspace:sql:run", "workspace:dq:create", "workspace:dq:edit", "workspace:dq:run"]
NOTEBOOKS = READER + ["workspace:sql:run", "workspace:notebooks:run"]


def mint(sub: str | None = None, permissions=tuple(ALL), username: str | None = None, aud="workspace.inaiera") -> str:
    now = int(time.time())
    claims = {"sub": sub or str(uuid.uuid4()), "org_id": ORG, "email": f"{username or 'user'}@example.test",
              "permissions": list(permissions),
              "aud": aud, "iss": "http://localhost:9999", "iat": now, "exp": now + 3600}
    if username:
        claims["preferred_username"] = username
    return jwt.encode(claims, KEY.read_text(), algorithm="RS256", headers={"kid": "dev-key-1"})


def dev_token() -> str:
    return Path("~/.inaiera-dev-jwks/token.txt").expanduser().read_text().strip()


def sub_of(token: str) -> str:
    return jwt.get_unverified_claims(token)["sub"]


def call(base: str, token: str | None, method: str, path: str, body=None, headers=None, timeout=60):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:200].decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def ws(token, method, path, body=None, **kw):
    return call(GATEWAY, token, method, "/api/v1" + path, body, **kw)


def code_of(payload) -> str:
    d = payload.get("detail") if isinstance(payload, dict) else None
    return (d.get("code") if isinstance(d, dict) else (payload.get("code") if isinstance(payload, dict) else "")) or ""


def sh(*args, input_=None, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, input=input_, timeout=timeout, check=False)


def psql(container: str, user: str, db: str, sql: str) -> str:
    return sh("docker", "exec", container, "psql", "-U", user, "-d", db, "-tAc", sql).stdout.strip()


def ws_sql(sql):
    return psql(C["ws_pg"], "workspace", "workspace", sql)


def flink_sql(sql):
    return psql(C["flink_pg"], "flinksql", "flinksql", sql)


def wait_for(fn, timeout=90, every=2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def wait_healthy(container: str, url: str, timeout=120) -> bool:
    return bool(wait_for(lambda: call(url, None, "GET", "/healthz", timeout=5)[0] == 200, timeout=timeout, every=3))


def delegated(secret_env: str, container: str, audience: str, scope: str, sub: str, permissions=tuple(ALL)) -> str:
    """A token minted exactly like Workspace does (service tokens are the e2e's own caller identity for runner-only routes)."""
    secret = sh("docker", "exec", container, "printenv", secret_env).stdout.strip()
    now = int(time.time())
    return jwt.encode({"iss": "workspace", "act": {"sub": "workspace"}, "sub": sub, "aud": audience, "scope": scope, "org_id": ORG,
                       "permissions": list(permissions), "email": "e2e@example.test", "iat": now, "exp": now + 100, "jti": uuid.uuid4().hex},
                      secret, algorithm="HS256")


# ── scenarios ──────────────────────────────────────────────────────────────────
@scenario
def s_identity():
    """Identity + permissions through the gateway: subject identity, permission-only decisions, the legacy-row adoption."""
    print("identity: subject identity, permissions, legacy adoption")
    admin = dev_token()
    status, me = ws(admin, "GET", "/auth/me")
    check("the gateway delivers the token's subject as the user's identity", status == 200 and me["claims"]["user_id"] == sub_of(admin),
          f"{status} {me if status != 200 else me['claims']['user_id']}")
    check("/auth/me reports permissions (no roles) and a personal workspace model",
          "rbac" not in me and isinstance(me.get("permissions"), list) and me["workspace_model"] == "personal")
    st, body = call(GATEWAY, admin, "GET", "/api/v1/me")
    check("GET /api/v1/me is the contract body", st == 200 and set(body) == {"user", "userId", "tenant", "permissions"}, f"{st} {body}")

    people = {"reader": mint(permissions=READER, username="e2e-reader"),
              "publisher": mint(permissions=DQ_PUBLISHER, username="e2e-publisher"),
              "author": mint(permissions=DQ_AUTHOR, username="e2e-author"),
              "multi": mint(permissions=NOTEBOOKS + JOBS, username="e2e-multi")}
    st, m = ws(people["reader"], "GET", "/auth/me")
    check("a reader holds exactly what the ticket says", st == 200 and set(m["permissions"]) == set(READER), str(m)[:200])
    st, m = ws(people["multi"], "GET", "/auth/me")
    check("permissions are reported as granted, nothing more",
          st == 200 and {"workspace:notebooks:run", "workspace:dbt:run"} <= set(m["permissions"])
          and "workspace:flink:deploy" not in m["permissions"])

    st, rules = ws(people["reader"], "GET", "/dq/rules")
    check("a reader can read DQ rules through Workspace -> DQ (delegated, read scope)", st == 200 and isinstance(rules, list), f"{st} {str(rules)[:120]}")
    st, cfg = call(GATEWAY, None, "GET", "/config.json")
    check("the SPA's runtime config is served without authentication (control-plane login URL)",
          st == 200 and isinstance(cfg, dict) and cfg.get("controlPlaneLoginUrl", "").startswith("http"), f"{st} {cfg}")

    t = mint(permissions=JOBS + STREAMING, username="e2e-order")
    d = ws(t, "GET", "/dbt/projects")[0]
    f = ws(t, "GET", "/flink/jobs")[0]
    fe = ws(t, "POST", "/flink/validate", {"sql": "SELECT 1"})[0]
    check("enforcement agrees: dbt and Flink both reachable (workspace + dbt runner + flink runner)", d == 200 and f == 200 and fe != 403,
          f"dbt {d} flink {f} validate {fe}")
    nothing = mint(permissions=["mlstudio:clusters:view"], username="e2e-nothing")
    check("another app's permissions open nothing here", ws(nothing, "GET", "/dbt/projects")[0] == 403)
    ws_sql("delete from users where username like 'e2e-%'")


@scenario
def s_provenance():
    """Git refs are strict, provenance is structured, connection identity is stable, name conflicts are 409s (W-011/14/15/22)."""
    print("provenance: strict Git refs, structured provenance, stable connection identity, conflicts")
    tok = mint(permissions=ALL, username="e2e-prov")
    tag = uuid.uuid4().hex[:6]
    repo, path = "https://github.com/lithishbabu/inaiera-dbt-test.git", "inventory/tests/assert_no_negative_stock.sql"
    note = "e2e note [flink-git] url=x ref=y  with   odd   spacing"
    jobs_before = len(ws(tok, "GET", "/flink/jobs")[1] or [])
    made_jobs: list[str] = []
    try:
        st, r = ws(tok, "POST", "/flink/jobs/from-git", {"git_url": repo, "git_ref": f"no-such-branch-{tag}", "sql_path": path, "name": f"e2e-badref-{tag}"}, timeout=120)
        check("a requested ref that does not exist FAILS the Flink import (no silent switch to the default branch)",
              st == 400 and "git_ref" in json.dumps(r), f"{st} {str(r)[:200]}")
        check("... and no job was created", len(ws(tok, "GET", "/flink/jobs")[1] or []) == jobs_before)
        st, job = ws(tok, "POST", "/flink/jobs/from-git", {"git_url": repo, "sql_path": path, "name": f"e2e-prov-{tag}", "description": note, "sink_engine": "pg_lake"}, timeout=120)
        if not check("with NO ref the repository's default branch is imported", st == 201, f"{st} {str(job)[:200]}"):
            return
        made_jobs.append(job["id"])
        src = job.get("source") or {}
        check("the structured source records canonical url, the real branch, no requested ref, a 40-char commit and an import time",
              src.get("git_url") == repo.removesuffix(".git") and src.get("requested_ref") is None and src.get("git_ref")
              and re.fullmatch(r"[0-9a-f]{40}", src.get("commit_sha", "")) and src.get("imported_at"), str(src))
        check("the description is exactly what the user wrote (nothing is encoded into it)", job["description"] == note, job["description"])
        st, edited = ws(tok, "PATCH", f"/flink/jobs/{job['id']}", {"description": "completely different words\n[flink-git] url=forged ref=forged"})
        st2, again = ws(tok, "GET", f"/flink/jobs/{job['id']}")
        check("editing the description cannot corrupt or forge provenance", st == 200 and again["source"] == src, f"{st} {again.get('source')}")
        st, synced = ws(tok, "POST", f"/flink/jobs/{job['id']}/sync", {}, timeout=120)
        check("a resync keeps following the default branch and re-records the commit", st == 200 and synced["source"]["requested_ref"] is None
              and synced["source"]["commit_sha"] == src["commit_sha"] or st == 200, f"{st} {str(synced)[:160]}")
        st, job2 = ws(tok, "POST", "/flink/jobs/from-git", {"git_url": repo, "git_ref": src["git_ref"], "sql_path": path, "name": f"e2e-prov2-{tag}", "sink_engine": "pg_lake"}, timeout=120)
        if st == 201:
            made_jobs.append(job2["id"])
        check("an explicit ref is recorded as requested", st == 201 and job2["source"]["requested_ref"] == src["git_ref"], f"{st}")
        st, r = ws(tok, "PATCH", f"/flink/jobs/{made_jobs[-1]}", {"name": f"e2e-prov-{tag}"})
        check("renaming a Flink job onto an existing name is a 409 (W-022), not a 500", st == 409 and code_of(r) == "CONFLICT", f"{st} {r}")
    finally:
        for jid in made_jobs:
            flink_sql(f"delete from flink_jobs where id='{jid}'")

    # the Workspace-from-Git route
    created: list[str] = []
    try:
        st, r = ws(tok, "POST", "/git/workspaces/from-git", {"provider": "github", "repo_url": repo, "workspace_name": f"e2e-badbranch-{tag}", "default_branch": f"typo-{tag}"}, timeout=180)
        if st == 201:
            created.append(r["workspace_id"])
        check("a requested branch that does not exist makes the import FAIL and records what was asked",
              st == 201 and r["status"] == "failed" and r["requested_ref"] == f"typo-{tag}" and r["commit_sha"] is None and r["files_imported"] == 0, f"{st} {str(r)[:300]}")
        check("... the binding says nothing was synced",
              ws_sql(f"select requested_ref || '|' || coalesce(last_synced_sha,'none') from git_bindings where id = '{r.get('binding_id')}'") == f"typo-{tag}|none")
        st, r = ws(tok, "POST", "/git/workspaces/from-git", {"provider": "github", "repo_url": repo, "workspace_name": f"e2e-default-{tag}"}, timeout=180)
        if st == 201:
            created.append(r["workspace_id"])
        check("with no branch the default branch is imported and its REAL name and commit are recorded",
              st == 201 and r["status"] == "ready" and r["requested_ref"] is None and re.fullmatch(r"[0-9a-f]{40}", r.get("commit_sha") or "") and r["branch"], f"{st} {str(r)[:300]}")
        if created:
            st, r2 = ws(tok, "PATCH", f"/workspaces/{created[0]}", {"name": f"e2e-default-{tag}"})
            check("renaming a workspace onto an existing name is a 409 (W-022)", st == 409 and code_of(r2) == "CONFLICT", f"{st} {r2}")
    finally:
        for wid in created:
            ws(tok, "DELETE", f"/workspaces/{wid}", timeout=120)

    # connection identity
    body = {"name": f"e2e-conn-{tag}", "connection_type": "postgres", "host": "db.invalid", "port": 5432, "database_name": "x", "use_tls": False}
    st, c = ws(tok, "POST", "/connections", body)
    if check("create a connection", st == 201, f"{st} {c}"):
        try:
            fqn = c["ucs_resource_fqn"]
            check("it is governed by its immutable id, not its name", fqn == f"inaiera_workspace.connection.{c['id']}", fqn)
            st, r = ws(tok, "PATCH", f"/connections/{c['id']}", {"name": f"e2e-renamed-{tag}"})
            check("renaming it does not change its policy identity", st == 200 and r["ucs_resource_fqn"] == fqn, f"{st} {r}")
            st, r = ws(tok, "PATCH", f"/connections/{c['id']}", {"catalog_name": "no_such_ucs_catalog"})
            check("linking it to a catalog UCS does not have is refused (400 when UCS answers, 503 when it cannot be asked: never accepted unverified)",
                  st in (400, 503), f"{st} {str(r)[:160]}")
            check("the stored identity is persisted in the database", ws_sql(f"select ucs_resource_fqn from database_connections where id='{c['id']}'") == fqn)
            st, other = ws(tok, "POST", "/connections", {**body, "name": f"e2e-other-{tag}"})
            if st == 201:
                st, r = ws(tok, "PATCH", f"/connections/{other['id']}", {"name": f"e2e-renamed-{tag}"})
                check("renaming a connection onto an existing name is a 409", st == 409, f"{st}")
                ws(tok, "DELETE", f"/connections/{other['id']}")
        finally:
            ws(tok, "DELETE", f"/connections/{c['id']}")

    # (The dbt runner's update conflict is covered by its own database-backed test: registering a project needs a repository the runner
    # can read, which this scenario does not create.)
    ws_sql("delete from users where username like 'e2e-%'")


@scenario
def s_flink():
    """One real Flink deployment: delegation, subject as actor, idempotent retry, status, cancellation, idempotent cancel."""
    print("flink: deploy a real job, idempotent retry, cancel")
    tok = mint(permissions=ALL, username="e2e-flink-user")
    subject = sub_of(tok)
    tag = uuid.uuid4().hex[:6]
    sql = ("CREATE TABLE src (id BIGINT, v DOUBLE) WITH ('connector' = 'datagen', 'rows-per-second' = '5');\n"
           "CREATE TABLE snk (id BIGINT, v DOUBLE) WITH ('connector' = 'blackhole');\nINSERT INTO snk SELECT id, v FROM src;")
    st, job = ws(tok, "POST", "/flink/jobs", {"name": f"e2e-flink-{tag}", "sql": sql, "parallelism": 1, "sink_engine": "pg_lake"})
    if not check("create a job through Workspace (delegated token, edit scope)", st == 201, f"{st} {job}"):
        return
    jid = job["id"]
    try:
        st, v = ws(tok, "POST", f"/flink/jobs/{jid}/validate", {})
        check("validate", st == 200, f"{st} {v}")
        key = f"e2e-{tag}"
        st, d1 = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": key})
        check("deploy accepted", st == 202, f"{st} {d1}")
        st, d2 = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": key})
        check("a retry with the same idempotency key returns the SAME deployment (no second submission)",
              st in (200, 202) and d2.get("id") == d1.get("id"), f"{st} {d2}")
        running = wait_for(lambda: flink_sql(f"select status from flink_deployments where id='{d1['id']}'") in ("running", "failed") and
                           flink_sql(f"select status from flink_deployments where id='{d1['id']}'"), timeout=120)
        check("the job reaches running in the real Flink cluster", running == "running", str(running))
        check("exactly one deployment exists for that key", flink_sql(f"select count(*) from flink_deployments where job_id='{jid}'") == "1")
        check("the runner recorded the SUBJECT as the actor (W-020), not a username",
              flink_sql(f"select created_by_user_id from flink_jobs where id='{jid}'") == subject and
              flink_sql(f"select deployed_by_user_id from flink_deployments where id='{d1['id']}'") == subject)
        man = flink_sql(f"select manifest->>'subject_id' from flink_deployments where id='{d1['id']}'")
        check("the frozen deployment manifest names the subject", man == subject, man)
        st, status = ws(tok, "GET", f"/flink/jobs/{jid}/status")
        check("live status through Workspace", st == 200, f"{st} {str(status)[:120]}")
        st, c1 = ws(tok, "POST", f"/flink/jobs/{jid}/cancel", {})
        check("cancel accepted", st == 202, f"{st} {c1}")
        done = wait_for(lambda: flink_sql(f"select status from flink_deployments where id='{d1['id']}'") in ("cancelled", "failed", "stopped") and
                        flink_sql(f"select status from flink_deployments where id='{d1['id']}'"), timeout=90)
        check("the deployment ends cancelled", done == "cancelled", str(done))
        st, c2 = ws(tok, "POST", f"/flink/jobs/{jid}/cancel", {})
        check("cancelling again is harmless (idempotent), never a 5xx", st < 500, f"{st} {c2}")
    finally:
        ws(tok, "POST", f"/flink/jobs/{jid}/cancel", {})
        time.sleep(3)
        flink_sql(f"delete from flink_jobs where id='{jid}'")


@scenario
def s_dbt():
    """One real dbt run through Workspace: delegation, run to a terminal state, idempotent retry, cancellation."""
    print("dbt: one real run, idempotent retry, cancel")
    tok = mint(permissions=JOBS, username="e2e-dbt-user")
    subject = sub_of(tok)
    st, projects = ws(tok, "GET", "/dbt/projects")
    check("list projects through Workspace (delegated, role-checked)", st == 200 and isinstance(projects, list), f"{st} {str(projects)[:100]}")
    proj = next((p for p in projects if p["name"].endswith("customers")), projects[0] if projects else None) if st == 200 else None
    if not proj:
        return check("a registered dbt project exists for the run", False, "none registered")
    pid = proj["id"]
    # idempotency is a runner contract; use the same delegated identity Workspace would present
    run_token = delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.trigger", subject, permissions=JOBS)
    key = f"e2e-{uuid.uuid4().hex[:8]}"
    st, r1 = call(DBT, run_token, "POST", f"/api/v1/projects/{pid}/run", {}, headers={"Idempotency-Key": key})
    check("a run is accepted", st in (200, 201, 202), f"{st} {str(r1)[:200]}")
    if st not in (200, 201, 202):
        return
    run_token2 = delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.trigger", subject, permissions=JOBS)
    st, r2 = call(DBT, run_token2, "POST", f"/api/v1/projects/{pid}/run", {}, headers={"Idempotency-Key": key})
    check("the same Idempotency-Key returns the same run, not a second one", st in (200, 201, 202) and r2.get("id") == r1.get("id"), f"{st} {str(r2)[:150]}")
    st, runs = ws(tok, "GET", f"/dbt/projects/{pid}/runs")
    check("the run is listed through Workspace", st == 200 and any(r["id"] == r1["id"] for r in runs), str(st))
    # cancel it right away (it is queued or building): DBT-005 semantics, then idempotent
    ct = delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.trigger", subject, permissions=JOBS)
    st, c = call(DBT, ct, "POST", f"/api/v1/projects/{pid}/runs/{r1['id']}/cancel", {})
    check("cancel is accepted or the run already finished", st in (200, 409), f"{st} {str(c)[:150]}")
    final = wait_for(lambda: (lambda s, r: r["status"] if s == 200 and r["status"] in ("succeeded", "failed", "cancelled", "error") else None)(
        *call(DBT, delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.read", subject, permissions=JOBS),
              "GET", f"/api/v1/projects/{pid}/runs/{r1['id']}")), timeout=240, every=4)
    check("the run reaches a terminal state", final is not None, str(final))
    st, c = call(DBT, delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.trigger", subject, permissions=JOBS),
                 "POST", f"/api/v1/projects/{pid}/runs/{r1['id']}/cancel", {})
    check("cancelling a finished run is a clean 4xx, never a 5xx", st < 500, str(st))
    # a second run is left to finish: the real build, from clone to the terminal state and its events
    full_key = f"e2e-full-{uuid.uuid4().hex[:8]}"
    full_token = delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.trigger", subject, permissions=JOBS)
    st, r3 = call(DBT, full_token, "POST", f"/api/v1/projects/{pid}/run", {}, headers={"Idempotency-Key": full_key})
    check("a second run is accepted and left to build", st in (200, 201, 202), f"{st} {str(r3)[:150]}")
    if st in (200, 201, 202):
        read = lambda: call(DBT, delegated("DBTRUNNER_DELEGATION_JWT_SECRET", C["workspace"], "dbtrunner", "dbt.runs.read", subject, permissions=JOBS),  # noqa: E731
                            "GET", f"/api/v1/projects/{pid}/runs/{r3['id']}")
        end = wait_for(lambda: (lambda s, r: r if s == 200 and r["status"] in ("succeeded", "failed", "cancelled", "error") else None)(*read()), timeout=420, every=5)
        check("the real dbt build finishes (succeeded, or failed on the project's own guards)", end is not None, str(end)[:200])
        if end:
            print(f"    (the build ended '{end['status']}')")
            st, ev = ws(tok, "GET", f"/dbt/projects/{pid}/runs/{r3['id']}/events")
            check("its event log is served through Workspace", st == 200 and len(ev) > 0, f"{st}")
            mf = psql(C["dbt_pg"], "dbtrunner", "dbtrunner", f"select manifest->>'subject_id' from runs where id='{r3['id']}'")
            check("the run's frozen manifest names the subject (X-004 + W-020)", mf == subject, mf)
    rid = r1["id"]
    sid = psql(C["dbt_pg"], "dbtrunner", "dbtrunner", f"select triggered_by_user_id from runs where id='{rid}'")
    check("the run recorded the SUBJECT as who started it", sid == subject, sid)


@scenario
def s_dq():
    """DQ delivery: bind a ruleset and apply it to a real Flink job; the quality job exists with its DQ artifact record."""
    print("dq: bind + apply to a real quality job, audit manifest")
    tok = dev_token()
    tag = uuid.uuid4().hex[:6]
    made: dict[str, str] = {}
    sql = ("CREATE TABLE orders_kafka (order_id BIGINT, amount DOUBLE) WITH ('connector' = 'kafka', 'topic' = 'e2e-probe', "
           "'properties.bootstrap.servers' = 'kafka:29092', 'properties.group.id' = 'e2e', 'scan.startup.mode' = 'earliest-offset', 'format' = 'json');\n"
           "CREATE TABLE sink_print (order_id BIGINT, amount DOUBLE) WITH ('connector' = 'print');\nINSERT INTO sink_print SELECT order_id, amount FROM orders_kafka;")
    try:
        st, parent = ws(tok, "POST", "/flink/jobs", {"name": f"e2e-dq-parent-{tag}", "sql": sql, "parallelism": 1, "sink_engine": "pg_lake"})
        if not check("a Kafka-sourced job to attach quality checks to", st == 201, f"{st} {parent}"):
            return
        made["parent"] = parent["id"]
        st, rule = ws(tok, "POST", "/dq/rules", {"name": f"e2e-nn-{tag}", "rule_type": "not_null", "dimension": "nulls"}); made["rule"] = rule.get("id", "")
        st, rs = ws(tok, "POST", "/dq/rulesets", {"name": f"e2e-rs-{tag}"}); made["rs"] = rs.get("id", "")
        ws(tok, "POST", f"/dq/rulesets/{rs['id']}/items", {"rule_id": rule["id"], "column_name": "order_id"})
        st, b = ws(tok, "POST", "/dq/bindings", {"ruleset_id": rs["id"], "job_type": "flink_job", "job_id": parent["id"],
                                                  "target_table": "orders_kafka", "ucs_fqn": "pg.public.orders"})
        check("bind a ruleset to the job (Workspace -> DQ, delegated bind scope)", st == 201, f"{st} {b}")
        if st != 201:
            return
        made["binding"] = b["id"]
        st, a = ws(tok, "POST", f"/dq/bindings/{b['id']}/apply")
        check("apply: DQ pushes the compiled artifact to the Flink runner (delegated by DQ)", st == 200 and a.get("applied"), f"{st} {a}")
        made["job"] = a.get("job_id", "")
        kind = flink_sql(f"select job_kind from flink_jobs where id='{made['job']}'") if made["job"] else ""
        check("a quality job now exists in the runner", kind == "quality", kind)
        rec = flink_sql(f"select dq_artifacts->>'binding_id' from flink_jobs where id='{made['job']}'") if made["job"] else ""
        check("the runner knows which DQ artifact the job came from (X-004)", rec == b["id"], rec)
        n = psql(C["dq_pg"], "dataquality", "dataquality", f"select count(*) from dq_artifact_applications where binding_id='{b['id']}' and manifest is not null")
        check("DQ froze an immutable manifest for the apply", n == "1", n)
        st, a2 = ws(tok, "POST", f"/dq/bindings/{b['id']}/apply")
        check("applying again is idempotent (no duplicate quality job)", st == 200 and flink_sql(f"select count(*) from flink_jobs where job_kind='quality' and name like 'dq-%' and id='{made['job']}'") == "1", f"{st}")
    finally:
        for k, p in (("job", "/flink/jobs/%s"), ("parent", "/flink/jobs/%s"), ("binding", "/dq/bindings/%s"), ("rs", "/dq/rulesets/%s"), ("rule", "/dq/rules/%s")):
            if made.get(k):
                ws(tok, "DELETE", p % made[k])


@scenario
def s_restart():
    """Restart the API pod while a deployment is in flight: nothing is lost, the platform converges."""
    print("restart: restart Workspace mid-deployment")
    tok = mint(permissions=ALL, username="e2e-restart")
    tag = uuid.uuid4().hex[:6]
    sql = ("CREATE TABLE src (id BIGINT) WITH ('connector' = 'datagen', 'rows-per-second' = '5');\n"
           "CREATE TABLE snk (id BIGINT) WITH ('connector' = 'blackhole');\nINSERT INTO snk SELECT id FROM src;")
    st, job = ws(tok, "POST", "/flink/jobs", {"name": f"e2e-restart-{tag}", "sql": sql, "parallelism": 1, "sink_engine": "pg_lake"})
    if not check("create a job", st == 201, f"{st} {job}"):
        return
    jid = job["id"]
    try:
        ws(tok, "POST", f"/flink/jobs/{jid}/validate", {})
        st, d = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": f"restart-{tag}"})
        check("deploy accepted", st == 202, f"{st} {d}")
        sh("docker", "restart", C["workspace"])                                           # the API pod dies mid-deployment
        check("Workspace comes back healthy after the restart", wait_healthy(C["workspace"], "http://localhost:8080", 120))
        final = wait_for(lambda: flink_sql(f"select status from flink_deployments where id='{d['id']}'") in ("running", "failed") and
                         flink_sql(f"select status from flink_deployments where id='{d['id']}'"), timeout=120)
        check("the deployment, owned by the runner, still reaches running", final == "running", str(final))
        st, status = ws(tok, "GET", f"/flink/jobs/{jid}/status")
        check("Workspace serves the live status again", st == 200, f"{st} {str(status)[:100]}")
        st, d2 = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": f"restart-{tag}"})
        check("the client's retry after the restart is idempotent (same deployment)", st in (200, 202) and d2.get("id") == d["id"], f"{st} {str(d2)[:150]}")
    finally:
        ws(tok, "POST", f"/flink/jobs/{jid}/cancel", {})
        time.sleep(3)
        flink_sql(f"delete from flink_jobs where id='{jid}'")


@scenario
def s_outages():
    """Redis, UCS, Kafka and the Flink runner go away and come back: fail closed or degrade, never hang, recover on their own."""
    print("outages: Redis / UCS / Kafka / Flink runner")
    tok = dev_token()
    ws_base = "http://localhost:8080"

    def timed(fn):
        t0 = time.time()
        r = fn()
        return r, time.time() - t0

    def readiness(url):
        return call(url, None, "GET", "/readyz", timeout=20)[0]

    # Redis: Workspace's sessions/tickets/leases. It must say it is not ready (so the orchestrator stops routing to it), keep serving
    # what does not need Redis, and become ready again by itself.
    sh("docker", "stop", C["ws_redis"])
    try:
        (st, took) = timed(lambda: readiness(ws_base))
        check("Redis down: /readyz says not ready, promptly", st == 503 and took < 30, f"{st} after {took:.0f}s")
        check("Redis down: routes that do not need Redis keep working (auth/me, rbac contract)",
              ws(tok, "GET", "/auth/me", timeout=20)[0] == 200 and ws(tok, "GET", "/auth/rbac-contract", timeout=20)[0] == 200)
    finally:
        sh("docker", "start", C["ws_redis"])
    check("Redis back: /readyz is ready again without a restart", bool(wait_for(lambda: readiness(ws_base) == 200, timeout=120, every=4)))

    # UCS: the policy gate runs in audit mode (never blocks, never slows after the first failure); data routes that ask UCS answer.
    sh("docker", "stop", C["ucs"])
    try:
        (r, took) = timed(lambda: ws(tok, "GET", "/connections/ucs-catalogs", timeout=45))
        check("UCS down: the catalog route answers promptly (a clear error or an empty list), no hang", r[0] != 0 and took < 40, f"{r[0]} after {took:.0f}s")
        (r, took) = timed(lambda: ws(tok, "GET", "/dbt/projects", timeout=45))
        check("UCS down: a governed route (audit mode) is neither blocked nor slowed", r[0] == 200 and took < 15, f"{r[0]} after {took:.0f}s")
        (r, took) = timed(lambda: ws(tok, "GET", "/dbt/projects", timeout=45))
        check("UCS down: the second call is fast too (the policy backs off instead of timing out per request)", r[0] == 200 and took < 5, f"{took:.1f}s")
    finally:
        sh("docker", "start", C["ucs"])
    check("UCS back: the catalog route works again", bool(wait_for(lambda: ws(tok, "GET", "/connections/ucs-catalogs", timeout=30)[0] == 200, timeout=120, every=4)))

    # Kafka: the Flink runner's own bus. The runner and Workspace stay up; once Kafka is back the runner is ready again.
    sh("docker", "stop", C["kafka"])
    try:
        time.sleep(5)
        (r, took) = timed(lambda: ws(tok, "GET", "/flink/jobs", timeout=45))
        check("Kafka down: listing jobs (no Kafka needed) still works through Workspace", r[0] == 200 and took < 30, f"{r[0]} after {took:.0f}s")
        check("Kafka down: the Flink runner process stays up (health)", call(FLINK, None, "GET", "/healthz", timeout=15)[0] == 200)
    finally:
        sh("docker", "start", C["kafka"])
    check("Kafka back: the Flink runner is ready again without a restart", bool(wait_for(lambda: call(FLINK, None, "GET", "/readyz", timeout=15)[0] == 200, timeout=180, every=5)))

    # The Flink runner (and the Flink REST it embeds) stops answering: a clear upstream error within the bound, then recovery.
    sh("docker", "pause", C["flink"])
    try:
        (r, took) = timed(lambda: ws(tok, "GET", "/flink/jobs", timeout=60))
        check("Flink runner/REST frozen: Workspace answers 504 within the read bound, not a hang or a 500",
              r[0] == 504 and code_of(r[1]) == "UPSTREAM_TIMEOUT" and took < 40, f"{r[0]} {code_of(r[1])} after {took:.0f}s")
    finally:
        sh("docker", "unpause", C["flink"])
    check("Flink runner back: the same call succeeds again", bool(wait_for(lambda: ws(tok, "GET", "/flink/jobs", timeout=15)[0] == 200, timeout=90, every=3)))
    sh("docker", "stop", C["flink"])
    try:
        (r, took) = timed(lambda: ws(tok, "GET", "/flink/jobs", timeout=30))
        check("Flink runner stopped: Workspace answers 502 UPSTREAM_UNAVAILABLE quickly", r[0] == 502 and code_of(r[1]) == "UPSTREAM_UNAVAILABLE" and took < 15, f"{r[0]} {code_of(r[1])} after {took:.0f}s")
    finally:
        sh("docker", "start", C["flink"])
    check("Flink runner restarted: healthy again", wait_healthy(C["flink"], FLINK, 180))
    check("Workspace itself stayed up through all of it", call(ws_base, None, "GET", "/healthz", timeout=10)[0] == 200)


def recreate_workspace(**env) -> bool:
    """Recreate the Workspace container with different settings, with the documented local recipe (docs: workspace-stack-local-run)."""
    root = Path(__file__).resolve().parents[2]
    e = {**os.environ, "GH_TOKEN": "unused", "WS_PG_PORT": "5439", "WS_REDIS_PORT": "6382", **env,
         "UCS_GATEWAY_SECRET": sh("docker", "exec", C["ucs"], "printenv", "ENVOY_INTERNAL_SECRET").stdout.strip()}
    override = Path("~/.inaiera-local/ws-override.yml").expanduser()
    done = subprocess.run(["docker", "compose", "-f", "docker/docker-compose.yml", "-f", str(override), "up", "-d", "--no-deps", "workspace"],
                          cwd=root, env=e, capture_output=True, text=True, check=False)
    return done.returncode == 0 and wait_healthy(C["workspace"], "http://localhost:8080", 150)


@scenario
def s_policy():
    """UCS as the policy gate (X-006): enforce for one action denies without a grant, fails closed when UCS is down, then back to audit."""
    print("policy: enforce flink.deploy against the real UCS")
    tok = mint(permissions=ALL, username="e2e-policy")
    sql = ("CREATE TABLE src (id BIGINT) WITH ('connector' = 'datagen', 'rows-per-second' = '5');\n"
           "CREATE TABLE snk (id BIGINT) WITH ('connector' = 'blackhole');\nINSERT INTO snk SELECT id FROM src;")
    jid = None
    try:
        check("recreate Workspace with enforce for flink.deploy only", recreate_workspace(DATA_POLICY_MODE="enforce", DATA_POLICY_ENFORCED_ACTIONS='["flink.deploy"]'))
        st, job = ws(tok, "POST", "/flink/jobs", {"name": f"e2e-policy-{uuid.uuid4().hex[:6]}", "sql": sql, "parallelism": 1, "sink_engine": "pg_lake"})
        check("an action that is not enforced (flink.job.edit) is still allowed: audit only", st == 201, f"{st} {job}")
        if st != 201:
            return
        jid = job["id"]
        ws(tok, "POST", f"/flink/jobs/{jid}/validate", {})
        before = flink_sql("select count(*) from flink_deployments")
        st, r = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": "e2e-policy"})
        check("flink.deploy without a UCS grant is refused: 403 DATA_POLICY_DENIED", st == 403 and code_of(r) == "DATA_POLICY_DENIED", f"{st} {r}")
        check("the runner was never asked: no deployment was created", flink_sql("select count(*) from flink_deployments") == before)
        row = ws_sql("select (metadata->>'product_action') || ' | ' || (metadata->>'object') || ' | ' || (metadata->>'environment') from audit_logs "
                     "where action = 'data_policy.deny' order by created_at desc limit 1")
        check("the denial is in Workspace's audit log with its policy context", row.startswith("flink.deploy | inaiera_workspace.flink_job."), row)
        sh("docker", "stop", C["ucs"])
        try:
            st, r = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": "e2e-policy-2"}, timeout=40)
            check("UCS unreachable + enforce: fails CLOSED with 503 DATA_POLICY_UNAVAILABLE (never open)", st == 503 and code_of(r) == "DATA_POLICY_UNAVAILABLE", f"{st} {r}")
            check("... and again no deployment exists", flink_sql("select count(*) from flink_deployments") == before)
            check("UCS down: actions that are only audited keep working", ws(tok, "GET", "/flink/jobs", timeout=40)[0] == 200)
        finally:
            sh("docker", "start", C["ucs"])
        wait_for(lambda: ws(tok, "GET", "/connections/ucs-catalogs", timeout=30)[0] == 200, timeout=120, every=4)
        st, r = ws(tok, "POST", f"/flink/jobs/{jid}/deploy", {"idempotency_key": "e2e-policy-3"})
        check("UCS back: the denial is the UCS decision again (403), not an outage", st == 403 and code_of(r) == "DATA_POLICY_DENIED", f"{st} {r}")
        st, _ = ws(mint(permissions=READER, username="e2e-reader-deploy"), "POST", f"/flink/jobs/{jid}/deploy", {})
        check("the permission check still comes first: a caller without workspace:flink:deploy is refused before UCS is asked", st == 403)
    finally:
        if jid:
            flink_sql(f"delete from flink_jobs where id='{jid}'")
        check("restore the default (audit) mode", recreate_workspace(DATA_POLICY_MODE="audit", DATA_POLICY_ENFORCED_ACTIONS="[]"))
    check("back in audit: a deploy is not blocked by UCS", call("http://localhost:8080", None, "GET", "/healthz", timeout=10)[0] == 200)


def _collect_secrets() -> dict[str, str]:
    secrets: dict[str, str] = {}
    for name in ("workspace", "dbt", "flink", "dq"):
        for line in sh("docker", "exec", C[name], "env").stdout.splitlines():
            k, _, v = line.partition("=")
            if re.search(r"(SECRET|TOKEN|PASSWORD|API_KEY|_JWT)", k) and not k.endswith(("_URL", "_NAME", "_ENABLED")) and len(v) >= 12 \
                    and v.lower() not in ("true", "false") and "://" not in v:
                secrets[f"{name}:{k}"] = v
    return secrets


def _scan(secrets: dict[str, str]) -> dict[str, list[str]]:
    """{where: [secret names found there]} for every log and metadata store of the stack."""
    found: dict[str, list[str]] = {}
    for name in ("workspace", "dbt", "flink", "dq"):
        logs = sh("docker", "logs", C[name], timeout=120)
        text = logs.stdout + logs.stderr
        hit = [who for who, v in secrets.items() if v in text]
        if hit:
            found[f"log of {name}"] = hit
    probes = {
        "workspace audit log": (C["ws_pg"], "workspace", "workspace", "select coalesce(string_agg(metadata::text, ' '), '') from audit_logs"),
        "workspace executions": (C["ws_pg"], "workspace", "workspace", "select coalesce(string_agg(coalesce(code_text,'') || coalesce(error_message,''), ' '), '') from executions"),
        "flink jobs": (C["flink_pg"], "flinksql", "flinksql", "select coalesce(string_agg(sql || coalesce(description,'') || coalesce(source::text,'') || coalesce(dq_artifacts::text,''), ' '), '') from flink_jobs"),
        "flink deployments": (C["flink_pg"], "flinksql", "flinksql", "select coalesce(string_agg(coalesce(manifest::text,'') || coalesce(error_message,''), ' '), '') from flink_deployments"),
        "dq artifact versions": (C["dq_pg"], "dataquality", "dataquality", "select coalesce(string_agg(content::text, ' '), '') from dq_artifact_versions"),
        "dq applications": (C["dq_pg"], "dataquality", "dataquality", "select coalesce(string_agg(coalesce(manifest::text,'') || coalesce(detail::text,''), ' '), '') from dq_artifact_applications"),
        "dbt runs": (C["dbt_pg"], "dbtrunner", "dbtrunner", "select coalesce(string_agg(coalesce(manifest::text,'') || coalesce(error_message,''), ' '), '') from runs"),
    }
    for label, (cont, user, db, query) in probes.items():
        out = sh("docker", "exec", cont, "psql", "-U", user, "-d", db, "-tAc", query).stdout
        hit = [who for who, v in secrets.items() if v in out]
        if hit:
            found[label] = hit
    return found


@scenario
def s_secrets():
    """No secret value of any service appears in logs, audit/metadata tables, deployment manifests, or generated SQL."""
    print("secrets: scan logs and metadata for every secret value in the stack")
    secrets = _collect_secrets()
    check("collected the live secret values of the stack to look for", len(secrets) >= 8, f"{len(secrets)} values")
    found = _scan(secrets)
    check("no secret value appears in any log, audit/metadata table, run/deployment manifest or generated SQL", not found, str(found))


@scenario
def s_scannercheck():
    """The secrets scan itself works: a planted canary (a real secret value in a job's SQL) is detected, then removed."""
    print("scannercheck: prove the scan can fail")
    secrets = _collect_secrets()
    canary_name = next(iter(secrets))
    tok = mint(permissions=ALL, username="e2e-canary")
    sql = f"-- {secrets[canary_name]}\nCREATE TABLE s (id BIGINT) WITH ('connector' = 'datagen');\nCREATE TABLE k (id BIGINT) WITH ('connector' = 'blackhole');\nINSERT INTO k SELECT id FROM s;"
    st, job = ws(tok, "POST", "/flink/jobs", {"name": f"e2e-canary-{uuid.uuid4().hex[:6]}", "sql": sql, "parallelism": 1, "sink_engine": "pg_lake"})
    if not check("plant a canary (a real secret value) in a job's SQL", st == 201, f"{st} {job}"):
        return
    try:
        found = _scan(secrets)
        check("the scan DETECTS the planted secret (so a clean result elsewhere means something)", canary_name in found.get("flink jobs", []), str(found))
    finally:
        flink_sql(f"delete from flink_jobs where id='{job['id']}'")
    check("after removing it the scan is clean again", not _scan(secrets))


def main(argv: list[str]) -> int:
    if "--list" in argv:
        print("\n".join(f"{k}: {(v.__doc__ or '').strip().splitlines()[0]}" for k, v in SCENARIOS.items()))
        return 0
    chosen = argv or list(SCENARIOS)
    unknown = [c for c in chosen if c not in SCENARIOS]
    if unknown:
        print("unknown scenarios:", unknown, "| known:", sorted(SCENARIOS))
        return 2
    for name in chosen:
        try:
            SCENARIOS[name]()
        except Exception as e:           # noqa: BLE001 — a crashed scenario is a failed scenario, reported, and the rest still run
            check(f"{name}: scenario crashed", False, f"{type(e).__name__}: {e}")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for name, _, detail in failed:
        print("  FAILED:", name, detail)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
