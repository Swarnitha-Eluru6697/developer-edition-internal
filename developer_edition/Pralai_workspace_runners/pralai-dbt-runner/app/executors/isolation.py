"""Run isolation.

validate_model() is already safe to run in-process — it's a throwaway
scratch project in a fresh tempdir with no project secrets attached.

run_project() is different: it runs arbitrary dbt code from a customer's
Git repo against real engine credentials. Outside a dev box this must not
run in the API process. RunnerIsolation picks the execution mode:

  - subprocess (local dev only): runs DbtExecutor.run_project() in this
    process (on a dedicated single worker thread so the event loop — and
    the run's lease heartbeat — keep running). Refused outright when
    ENVIRONMENT is staging or prod.

  - k8s_job: submits the run as a Kubernetes Job (same image as the API by
    default, entrypoint app/executors/job_entrypoint.py). Life cycle of one
    attempt, every step guarded by the run's lease (app/services/run_lease):

      1. record the per-attempt Job name on the run row (CAS; fails if the
         run was cancelled or is no longer ours -> nothing is created);
      2. create the Job SUSPENDED (no pod can start yet);
      3. create the per-attempt Secret (git URL / ref / DQ blocks) with an
         ownerReference to that Job, so Kubernetes garbage-collects it with
         the Job — this pod never needs get/delete on Secrets;
      4. release the Job (unsuspend) while holding the run row FOR UPDATE
         and re-checking status — a cancelled run is never released;
      5. poll asynchronously (event-loop sleeps; each k8s API call borrows
         a thread from a small bounded pool only for that call);
      6. read the result from the durable artifact store (reference marker
         in the pod log, or the deterministic (run_id, attempt) key), with a
         size-capped inline fallback;
      7. after the caller has persisted the result, delete the Job — only
         after verifying by label and UID that it is this attempt's Job.

    Engine credentials (pg_lake DSN, Trino JWT) and artifact-store
    credentials are injected via secretKeyRef only; outside dev the Job is
    refused if a required Secret reference is not configured. The Job pod
    gets no database credentials, no ServiceAccount token, a dedicated
    least-privilege ServiceAccount, RuntimeDefault seccomp, a fixed non-root
    UID/GID, and is fenced by the chart's Job NetworkPolicy.
"""
from __future__ import annotations

import asyncio
import functools
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import structlog
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from app.core.config import get_settings
from app.executors.dbt_executor import get_dbt_executor
from app.executors.job_entrypoint import RESULT_END, RESULT_REF, RESULT_START

log = structlog.get_logger()

_POLL_INITIAL_DELAY_SECONDS = 2.0
_POLL_MAX_DELAY_SECONDS = 15.0
# Grace period on top of the configured job timeout before the client gives
# up waiting — activeDeadlineSeconds on the Job is the authoritative limit.
_POLL_GRACE_SECONDS = 30.0
_LOG_TAIL_LINES = 200

MANAGED_BY_LABEL = "inaiera.com/managed-by"
RUN_ID_LABEL = "inaiera.com/run-id"
ATTEMPT_LABEL = "inaiera.com/attempt"

_k8s_config_loaded = False
_k8s_pool: ThreadPoolExecutor | None = None
# dbt keeps process-global state; never run two in-process builds at once.
_subprocess_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dbt-subprocess")


class JobSpecError(ValueError):
    """The Job cannot be built safely with the current configuration."""


def _ensure_k8s_config() -> None:
    global _k8s_config_loaded
    if _k8s_config_loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _k8s_config_loaded = True


def _pool() -> ThreadPoolExecutor:
    global _k8s_pool
    if _k8s_pool is None:
        _k8s_pool = ThreadPoolExecutor(max_workers=get_settings().runner_k8s_client_threads,
                                       thread_name_prefix="k8s-client")
    return _k8s_pool


async def _k8s(fn, *args, **kwargs):
    """Run one blocking kubernetes-client call on the bounded pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool(), functools.partial(fn, *args, **kwargs))


def _clients():
    _ensure_k8s_config()
    return client.BatchV1Api(), client.CoreV1Api()


def _aborted(reason: str) -> dict:
    return {"_aborted": True, "success": False, "errors": [reason], "nodes": []}


def _failure(message: str) -> dict:
    return {"success": False, "errors": [message], "nodes": []}


# ---------------------------------------------------------------------
# Names / labels / ownership
# ---------------------------------------------------------------------
def _job_name(run_id: uuid.UUID, attempt: int) -> str:
    return f"dbtrun-{run_id}-a{attempt}"  # <= 63 chars (pod label limit)


def _secret_name(run_id: uuid.UUID, attempt: int) -> str:
    return f"{_job_name(run_id, attempt)}-secret"


def _labels(run_id: uuid.UUID, attempt: int, settings) -> dict[str, str]:
    return {
        **(settings.runner_job_pod_labels or {}),
        "app.kubernetes.io/name": "dbtrunner-job",
        MANAGED_BY_LABEL: "dbtrunner",
        RUN_ID_LABEL: str(run_id),
        ATTEMPT_LABEL: str(attempt),
    }


def _is_ours(obj, run_id: uuid.UUID, attempt: int) -> bool:
    labels = (getattr(getattr(obj, "metadata", None), "labels", None) or {})
    return (labels.get(MANAGED_BY_LABEL) == "dbtrunner"
            and labels.get(RUN_ID_LABEL) == str(run_id)
            and labels.get(ATTEMPT_LABEL) == str(attempt))


def _delete_job_owned(batch, job_name: str, namespace: str, run_id: uuid.UUID, attempt: int) -> bool:
    """Delete a Job ONLY if it is this (run_id, attempt)'s Job — verified by
    labels, and pinned to the UID we just read via a delete precondition so
    a same-named replacement created in between is never deleted. Its
    Secret goes with it (ownerReference)."""
    try:
        job = batch.read_namespaced_job(job_name, namespace)
    except ApiException as e:
        if e.status != 404:
            log.warning("k8s_job_read_failed", job_name=job_name, error=str(e))
        return False
    if not _is_ours(job, run_id, attempt):
        log.error("k8s_job_not_ours_refusing_delete", job_name=job_name, run_id=str(run_id),
                  attempt=attempt)
        return False
    try:
        batch.delete_namespaced_job(
            job_name, namespace,
            body=client.V1DeleteOptions(
                propagation_policy="Foreground",
                preconditions=client.V1Preconditions(uid=job.metadata.uid),
            ),
        )
        return True
    except ApiException as e:
        if e.status not in (404, 409):
            log.warning("k8s_job_delete_failed", job_name=job_name, error=str(e))
        return False


# What the cluster says about an attempt's Job (DBT-005). A cancelled run may only be reported `cancelled`
# once it is in a CONFIRMED state.
JOB_GONE = "gone"                # 404: no such Job
JOB_TERMINAL = "terminal"        # it already finished (succeeded / failed): nothing is running
JOB_FOREIGN = "foreign"          # the name belongs to a resource that is not this attempt's Job: ours does not exist
JOB_TERMINATING = "terminating"  # deletion requested, the Job (and its pods) still exist: NOT yet confirmed
JOB_ACTIVE = "active"            # exists and is not being deleted (running, or suspended): NOT yet confirmed
CONFIRMED_JOB_STATES = frozenset({JOB_GONE, JOB_TERMINAL, JOB_FOREIGN})


def _read_job_state(batch, job_name: str, namespace: str, run_id: uuid.UUID, attempt: int):
    """(state, job). Any API error other than 404 is RAISED: an unreadable Job is not a gone Job."""
    try:
        job = batch.read_namespaced_job(job_name, namespace)
    except ApiException as e:
        if e.status == 404:
            return JOB_GONE, None
        raise
    if not _is_ours(job, run_id, attempt):
        return JOB_FOREIGN, job
    if getattr(job.metadata, "deletion_timestamp", None):
        return JOB_TERMINATING, job
    status = getattr(job, "status", None)
    if status is not None and (getattr(status, "succeeded", None) or getattr(status, "failed", None)):
        return JOB_TERMINAL, job
    return JOB_ACTIVE, job


def _terminate_job_sync(batch, job_name: str, namespace: str, run_id: uuid.UUID, attempt: int) -> str:
    """Ask for this attempt's Job to be deleted (idempotent) and report what the cluster says NOW.

    Foreground deletion returns as soon as the Job is marked for deletion; the Job and its pods linger until the
    pods are gone, so "the delete call succeeded" is not "the Job is gone": the state is read again afterwards
    and the caller keeps asking until it is a confirmed one. Only a Job labelled as this (run, attempt) is ever
    deleted, pinned to the UID we read so a same-named replacement is never touched."""
    state, job = _read_job_state(batch, job_name, namespace, run_id, attempt)
    if state == JOB_TERMINAL:
        try:                                    # tidy up the finished Job; the TTL is the backstop
            batch.delete_namespaced_job(job_name, namespace, body=client.V1DeleteOptions(
                propagation_policy="Foreground", preconditions=client.V1Preconditions(uid=job.metadata.uid)))
        except ApiException:
            pass
        return state
    if state != JOB_ACTIVE:
        return state
    try:
        batch.delete_namespaced_job(
            job_name, namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground",
                                        preconditions=client.V1Preconditions(uid=job.metadata.uid)),
        )
    except ApiException as e:
        if e.status not in (404, 409):          # 404: already gone; 409: the UID changed under us, re-read below
            raise
    return _read_job_state(batch, job_name, namespace, run_id, attempt)[0]


# ---------------------------------------------------------------------
# Spec construction
# ---------------------------------------------------------------------
def _secret_env(name: str, secret: str, key: str) -> client.V1EnvVar:
    return client.V1EnvVar(name=name, value_from=client.V1EnvVarSource(
        secret_key_ref=client.V1SecretKeySelector(name=secret, key=key)))


def _engine_env(engine: str, settings) -> list[client.V1EnvVar]:
    strict = settings.environment != "dev"
    env: list[client.V1EnvVar] = []
    if engine == "pg_lake":
        if settings.pg_lake_dsn_secret_name:
            env.append(_secret_env("PG_LAKE_DSN", settings.pg_lake_dsn_secret_name,
                                   settings.pg_lake_dsn_secret_key or "dsn"))
        elif strict:
            raise JobSpecError(
                "refusing to create the run Job: engine pg_lake needs PG_LAKE_DSN_SECRET_NAME "
                f"(a Secret reference) when ENVIRONMENT={settings.environment}; the plaintext "
                "DSN is never copied into a Job spec outside dev")
        elif settings.pg_lake_dsn:
            log.warning("pg_lake_dsn_plaintext_dev_only",
                        note="dev only: injecting PG_LAKE_DSN as a plain value")
            env.append(client.V1EnvVar(name="PG_LAKE_DSN", value=settings.pg_lake_dsn))
    elif engine == "trino":
        if settings.trino_host:
            env += [
                client.V1EnvVar(name="TRINO_HOST", value=settings.trino_host),
                client.V1EnvVar(name="TRINO_PORT", value=str(settings.trino_port)),
                client.V1EnvVar(name="TRINO_CATALOG", value=settings.trino_catalog),
                client.V1EnvVar(name="TRINO_HTTP_SCHEME", value=settings.trino_http_scheme),
                client.V1EnvVar(name="TRINO_AUTH_METHOD", value=settings.trino_auth_method),
                client.V1EnvVar(name="TRINO_USER", value=settings.trino_user),
            ]
        if settings.trino_auth_method == "none":
            pass                       # a dev Trino without authentication: Settings refuses this outside ENVIRONMENT=dev
        elif settings.trino_jwt_secret_name:
            env.append(_secret_env("TRINO_JWT", settings.trino_jwt_secret_name,
                                   settings.trino_jwt_secret_key))
        elif strict:
            raise JobSpecError(
                "refusing to create the run Job: engine trino needs TRINO_JWT_SECRET_NAME "
                f"(the warehouse credential Secret) when ENVIRONMENT={settings.environment}")
    elif engine == "clickhouse":
        if settings.clickhouse_host:
            env += [
                client.V1EnvVar(name="CLICKHOUSE_HOST", value=settings.clickhouse_host),
                client.V1EnvVar(name="CLICKHOUSE_PORT", value=str(settings.clickhouse_port)),
                client.V1EnvVar(name="CLICKHOUSE_SECURE", value="true" if settings.clickhouse_secure else "false"),
                client.V1EnvVar(name="CLICKHOUSE_USER", value=settings.clickhouse_user),
                client.V1EnvVar(name="CLICKHOUSE_DATABASE", value=settings.clickhouse_database),
            ]
        if settings.clickhouse_password_secret_name:
            env.append(_secret_env("CLICKHOUSE_PASSWORD", settings.clickhouse_password_secret_name,
                                   settings.clickhouse_password_secret_key))
        elif strict:
            raise JobSpecError(
                "refusing to create the run Job: engine clickhouse needs CLICKHOUSE_PASSWORD_SECRET_NAME "
                f"(the ClickHouse credential Secret) when ENVIRONMENT={settings.environment}; the password "
                "is never copied into a Job spec outside dev")
        elif settings.clickhouse_password:
            log.warning("clickhouse_password_plaintext_dev_only", note="dev only: injecting CLICKHOUSE_PASSWORD as a plain value")
            env.append(client.V1EnvVar(name="CLICKHOUSE_PASSWORD", value=settings.clickhouse_password))
    else:
        raise JobSpecError(f"unsupported engine: {engine}")
    # The Job builds the OpenLineage event (namespace only — it never sends it).
    env.append(client.V1EnvVar(name="OPENLINEAGE_NAMESPACE", value=settings.openlineage_namespace))
    return env


def _artifact_env(settings) -> list[client.V1EnvVar]:
    if settings.artifact_store_backend != "s3":
        if settings.environment != "dev":
            raise JobSpecError(
                "refusing to create the run Job: k8s_job mode needs ARTIFACT_STORE_BACKEND=s3 "
                f"when ENVIRONMENT={settings.environment} (the Job's filesystem disappears with "
                "its pod, so results must go to durable storage)")
        # dev: nowhere durable to write from inside the pod — inline fallback.
        return [client.V1EnvVar(name="ARTIFACT_STORE_BACKEND", value="none")]
    if not settings.artifact_s3_bucket:
        raise JobSpecError("ARTIFACT_STORE_BACKEND=s3 requires ARTIFACT_S3_BUCKET")
    env = [
        client.V1EnvVar(name="ARTIFACT_STORE_BACKEND", value="s3"),
        client.V1EnvVar(name="ARTIFACT_S3_BUCKET", value=settings.artifact_s3_bucket),
        client.V1EnvVar(name="ARTIFACT_S3_PREFIX", value=settings.artifact_s3_prefix),
    ]
    if settings.artifact_s3_endpoint_url:
        env.append(client.V1EnvVar(name="ARTIFACT_S3_ENDPOINT_URL", value=settings.artifact_s3_endpoint_url))
    if settings.artifact_s3_region:
        env.append(client.V1EnvVar(name="ARTIFACT_S3_REGION", value=settings.artifact_s3_region))
        env.append(client.V1EnvVar(name="AWS_DEFAULT_REGION", value=settings.artifact_s3_region))
    if settings.artifact_s3_credentials_secret_name:
        env += [
            _secret_env("AWS_ACCESS_KEY_ID", settings.artifact_s3_credentials_secret_name,
                        settings.artifact_s3_access_key_id_key),
            _secret_env("AWS_SECRET_ACCESS_KEY", settings.artifact_s3_credentials_secret_name,
                        settings.artifact_s3_secret_access_key_key),
        ]
    return env


def _build_secret(*, run_id: uuid.UUID, attempt: int, namespace: str, git_url: str, git_ref: str,
                  dq_schema_blocks: dict, settings, owner_job_uid: str | None = None,
                  project_path: str = "", git_sha: str = "", git_token: str | None = None,
                  workspace_job_token: str = "") -> client.V1Secret:
    """Per-attempt Secret carrying the genuinely per-run sensitive values
    (the clone token — never inside the URL, DBT-009 — and the repository URL) plus the rendered DQ
    payload and the commit the run is frozen to (DBT-006). Owned by the
    attempt's Job so it is GC'd with it."""
    owner_refs = None
    if owner_job_uid:
        owner_refs = [client.V1OwnerReference(
            api_version="batch/v1", kind="Job", name=_job_name(run_id, attempt),
            uid=owner_job_uid, controller=False, block_owner_deletion=False)]
    return client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=_secret_name(run_id, attempt), namespace=namespace,
            labels=_labels(run_id, attempt, settings), owner_references=owner_refs,
        ),
        type="Opaque",
        string_data={
            "GIT_URL": git_url,
            "GIT_REF": git_ref,
            "GIT_SHA": git_sha,
            "GIT_TOKEN": git_token or "",
            "PROJECT_PATH": project_path or "",
            "DQ_SCHEMA_TESTS_JSON": json.dumps(dq_schema_blocks or {}),
            # A SQL job on a Workspace connection: the one token that lets this attempt ask the Workspace to run its statements.
            "WORKSPACE_JOB_TOKEN": workspace_job_token,
        },
    )


def _workspace_job_token(kind: str, connection_id: str | None, project_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The one-run token for a SQL job on a Workspace connection (empty for every other job)."""
    if kind != "sql" or not connection_id:
        return ""
    from app.executors.connection_sql_job import mint_run_token
    return mint_run_token(project_id=project_id, run_id=run_id, connection_id=connection_id)


def _job_kind_env(kind: str, settings, on_connection: bool = False) -> list[client.V1EnvVar]:
    """What a SQL / Python job's pod needs beyond a dbt run's: where the Workspace is (SQL on a connection), and the package
    index (Python jobs install their requirements.txt). Neither is a secret."""
    env: list[client.V1EnvVar] = []
    if kind == "sql" and on_connection and settings.workspace_url:
        env.append(client.V1EnvVar(name="WORKSPACE_URL", value=settings.workspace_url))
    if kind == "python":
        for name in ("python_job_install_timeout_seconds", "python_job_timeout_seconds", "python_job_output_max_chars"):
            env.append(client.V1EnvVar(name=name.upper(), value=str(getattr(settings, name))))
        if settings.pip_index_url:
            env.append(client.V1EnvVar(name="PIP_INDEX_URL", value=settings.pip_index_url))
    return env


def _proxy_env(settings) -> list[client.V1EnvVar]:
    """DBT-010: git and `dbt deps` leave the Job through the FQDN-allow-listing egress proxy when one is configured. The
    NetworkPolicy then lets the pod reach only that proxy; in-cluster warehouse/artifact hosts are exempted via NO_PROXY."""
    url = getattr(settings, "job_git_proxy_url", "") or ""
    if not url:
        return []
    no_proxy = ".svc,.svc.cluster.local,localhost,127.0.0.1"
    return [client.V1EnvVar(name=n, value=url) for n in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")] + [
        client.V1EnvVar(name="NO_PROXY", value=no_proxy), client.V1EnvVar(name="no_proxy", value=no_proxy)]


def _build_job(*, run_id: uuid.UUID, attempt: int, engine: str, select: str | None, settings,
               suspend: bool = True, kind: str = "dbt", on_connection: bool = False) -> client.V1Job:
    job_name = _job_name(run_id, attempt)
    secret_name = _secret_name(run_id, attempt)
    labels = _labels(run_id, attempt, settings)

    args = ["--run-id", str(run_id), "--attempt", str(attempt), "--engine", engine]
    if select:
        args += ["--select", select]
    if kind != "dbt":
        args += ["--kind", kind]

    env = [
        _secret_env("RUNNER_GIT_URL", secret_name, "GIT_URL"),
        _secret_env("RUNNER_GIT_REF", secret_name, "GIT_REF"),
        _secret_env("RUNNER_GIT_SHA", secret_name, "GIT_SHA"),
        _secret_env("RUNNER_GIT_TOKEN", secret_name, "GIT_TOKEN"),
        _secret_env("RUNNER_PROJECT_PATH", secret_name, "PROJECT_PATH"),
        _secret_env("RUNNER_DQ_SCHEMA_TESTS_JSON", secret_name, "DQ_SCHEMA_TESTS_JSON"),
        _secret_env("RUNNER_WORKSPACE_JOB_TOKEN", secret_name, "WORKSPACE_JOB_TOKEN"),
        client.V1EnvVar(name="GIT_TERMINAL_PROMPT", value="0"),
        client.V1EnvVar(name="GIT_WORKDIR", value=settings.git_workdir),
        client.V1EnvVar(name="ENVIRONMENT", value=settings.environment),
        client.V1EnvVar(name="ORG_ID", value=settings.org_id),
        client.V1EnvVar(name="DO_NOT_TRACK", value="1"),
        client.V1EnvVar(name="HOME", value="/tmp"),
        client.V1EnvVar(name="RUNNER_INLINE_RESULT_MAX_BYTES",
                        value=str(settings.runner_inline_result_max_bytes)),
    ] + _engine_env(engine, settings) + _artifact_env(settings) + _proxy_env(settings) + _job_kind_env(kind, settings, on_connection)

    seccomp = client.V1SeccompProfile(type="RuntimeDefault")
    container = client.V1Container(
        name="dbtrunner-job",
        image=settings.runner_image,
        command=["python", "-m", "app.executors.job_entrypoint"],
        args=args,
        env=env,
        resources=client.V1ResourceRequirements(
            requests={"cpu": settings.runner_job_cpu_request, "memory": settings.runner_job_memory_request},
            limits={"cpu": settings.runner_job_cpu_limit, "memory": settings.runner_job_memory_limit},
        ),
        security_context=client.V1SecurityContext(
            run_as_non_root=True,
            run_as_user=settings.runner_job_run_as_user,
            run_as_group=settings.runner_job_run_as_group,
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
            seccomp_profile=seccomp,
        ),
        volume_mounts=[
            client.V1VolumeMount(name="workdir", mount_path=settings.git_workdir),
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
        ],
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        service_account_name=settings.runner_job_service_account,
        # The job pod never talks to the k8s API.
        automount_service_account_token=False,
        enable_service_links=False,
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=settings.runner_job_run_as_user,
            run_as_group=settings.runner_job_run_as_group,
            fs_group=settings.runner_job_run_as_group,
            seccomp_profile=seccomp,
        ),
        containers=[container],
        volumes=[
            client.V1Volume(name="workdir", empty_dir=client.V1EmptyDirVolumeSource(size_limit="5Gi")),
            client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource(size_limit="1Gi")),
        ],
    )

    return client.V1Job(
        metadata=client.V1ObjectMeta(name=job_name, namespace=settings.runner_namespace, labels=labels),
        spec=client.V1JobSpec(
            suspend=suspend,  # released only after the cancel re-check (see module doc)
            backoff_limit=0,  # we own retry policy at the run level, not the Job's
            active_deadline_seconds=settings.runner_job_timeout_seconds,
            ttl_seconds_after_finished=settings.runner_job_ttl_seconds,
            template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec),
        ),
    )


# ---------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------
def _read_job_logs(core, job_name: str, namespace: str) -> str:
    try:
        pods = core.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
    except ApiException:
        log.exception("k8s_job_list_pods_failed", job_name=job_name)
        return ""
    if not pods.items:
        return ""
    pod_name = pods.items[-1].metadata.name
    try:
        return core.read_namespaced_pod_log(pod_name, namespace, tail_lines=_LOG_TAIL_LINES)
    except ApiException:
        log.exception("k8s_job_read_log_failed", job_name=job_name, pod=pod_name)
        return ""


def _extract_ref(log_text: str) -> dict | None:
    for line in reversed(log_text.splitlines()):
        if line.startswith(RESULT_REF):
            try:
                return json.loads(line[len(RESULT_REF):].strip())
            except json.JSONDecodeError:
                log.warning("k8s_job_result_ref_unparsable")
                return None
    return None


def _extract_result(log_text: str) -> dict | None:
    if RESULT_START not in log_text or RESULT_END not in log_text:
        return None
    payload = log_text.split(RESULT_START, 1)[1].split(RESULT_END, 1)[0].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        log.warning("k8s_job_result_unparsable")
        return None


def _store():
    from app.services.artifact_store import get_artifact_store
    try:
        return get_artifact_store(get_settings())
    except Exception:  # noqa: BLE001
        log.exception("artifact_store_unavailable")
        return None


async def _stored_result(run_id: uuid.UUID, attempt: int, ref: dict | None = None) -> dict | None:
    from app.services.artifact_store import get_run_result
    store = _store()
    if store is None:
        return None
    attempts = []
    if ref and ref.get("uri"):
        attempts.append((ref["uri"], ref.get("sha256")))
    attempts.append((None, None))  # deterministic (run_id, attempt) key
    for uri, sha in attempts:
        try:
            result = await _k8s(get_run_result, store, run_id=run_id, attempt=attempt, ref=uri, sha256=sha)
        except Exception:  # noqa: BLE001
            log.exception("artifact_store_read_failed", run_id=str(run_id), attempt=attempt)
            continue
        if result is not None:
            return result
    return None


class RunnerIsolation:
    async def execute_run(
        self, *, run_id: uuid.UUID, project_dir, engine: str, select: str | None,
        git_url: str | None = None, git_ref: str | None = None,
        project_id: uuid.UUID | None = None, lease=None, project_path: str = "",
        git_sha: str | None = None, dq_schema_blocks: dict | None = None, git_token: str | None = None,
        kind: str = "dbt", connection_id: str | None = None,
    ) -> dict:
        settings = get_settings()
        if settings.runner_mode == "subprocess":
            if settings.environment in ("staging", "prod"):
                raise RuntimeError(
                    f"RUNNER_MODE=subprocess is not permitted when ENVIRONMENT={settings.environment} — "
                    "customer dbt code must run in an isolated k8s Job. Set RUNNER_MODE=k8s_job."
                )
            log.warning("runner_subprocess_mode", run_id=str(run_id), note="Not isolated — local dev only")
            if lease is not None and not await lease.still_owner():
                return _aborted("run was cancelled (or is no longer owned) before it started")
            loop = asyncio.get_running_loop()
            if kind == "sql" and connection_id:     # `project_dir` is the file here (run_service._resolve_job_file)
                from app.executors.connection_sql_job import mint_run_token, run_sql_file_on_connection
                if project_id is None:
                    raise ValueError("project_id is required to run a SQL job on a connection")
                token = mint_run_token(project_id=project_id, run_id=run_id, connection_id=connection_id)
                return await loop.run_in_executor(_subprocess_pool, functools.partial(
                    run_sql_file_on_connection, sql_path=project_dir, token=token))
            if kind == "sql":
                from app.executors.sql_job_executor import run_sql_file
                return await loop.run_in_executor(_subprocess_pool, functools.partial(
                    run_sql_file, engine=engine, sql_path=project_dir))
            if kind == "python":
                from app.executors.python_job_executor import run_python_file
                return await loop.run_in_executor(_subprocess_pool, functools.partial(
                    run_python_file, py_path=project_dir, engine=engine, run_id=str(run_id), git_sha=git_sha))
            executor = get_dbt_executor()
            return await loop.run_in_executor(_subprocess_pool, functools.partial(
                executor.run_project, project_dir=project_dir, engine=engine, select=select, run_id=run_id))

        if not git_url or not git_ref or project_id is None:
            raise ValueError("git_url, git_ref and project_id are required for k8s_job execution")
        if lease is None:
            raise ValueError("k8s_job execution requires the run's lease")
        if not git_sha:
            # DBT-006: the Job must be told which commit to check out; a ref alone can have moved since submission.
            raise ValueError("k8s_job execution requires the run's frozen git_sha")

        return await self._execute_k8s_job(
            lease=lease, run_id=run_id, engine=engine, select=select,
            git_url=git_url, git_ref=git_ref, project_id=project_id, project_path=project_path,
            git_sha=git_sha, dq_schema_blocks=dq_schema_blocks or {}, git_token=git_token, kind=kind,
            connection_id=connection_id,
        )

    async def _execute_k8s_job(
        self, *, lease, run_id: uuid.UUID, engine: str, select: str | None,
        git_url: str, git_ref: str, project_id: uuid.UUID, project_path: str = "",
        git_sha: str, dq_schema_blocks: dict, git_token: str | None = None, kind: str = "dbt",
        connection_id: str | None = None,
    ) -> dict:
        # The Job pod never gets database credentials: it is handed the commit and the DQ blocks frozen in the
        # run's manifest (DBT-006/007) — not the project's current ones — through its Secret.
        settings = get_settings()
        attempt = lease.attempt
        namespace = settings.runner_namespace
        job = _build_job(run_id=run_id, attempt=attempt, engine=engine, select=select,
                         settings=settings, suspend=True, kind=kind,
                         on_connection=bool(connection_id))  # JobSpecError -> run fails clearly
        job_name = job.metadata.name

        # 1. record the name first: a concurrent cancel can always find it.
        if not await lease.record_job_name(job_name):
            return _aborted("run was cancelled (or is no longer owned) before its Job was created")

        batch, core = _clients()
        log.info("k8s_job_submit", run_id=str(run_id), attempt=attempt, job_name=job_name,
                 namespace=namespace)
        # 2. create suspended
        try:
            created = await _k8s(batch.create_namespaced_job, namespace=namespace, body=job)
        except ApiException as e:
            if e.status != 409:
                log.exception("k8s_job_submit_failed", run_id=str(run_id))
                return _failure(f"failed to submit k8s Job: {e}")
            existing = await _k8s(batch.read_namespaced_job, job_name, namespace)
            if not _is_ours(existing, run_id, attempt):
                return _failure(f"k8s Job name {job_name} is taken by a resource this attempt did not "
                                "create; refusing to touch it")
            created = existing
        uid = getattr(created.metadata, "uid", None)

        # 3. per-attempt Secret, owned by the Job
        secret = _build_secret(run_id=run_id, attempt=attempt, namespace=namespace, git_url=git_url,
                               git_ref=git_ref, dq_schema_blocks=dq_schema_blocks, settings=settings,
                               owner_job_uid=uid, project_path=project_path, git_sha=git_sha, git_token=git_token,
                               workspace_job_token=_workspace_job_token(kind, connection_id, project_id, run_id))
        try:
            await _k8s(core.create_namespaced_secret, namespace=namespace, body=secret)
        except ApiException as e:
            # 409 means a same-named Secret this attempt did NOT create exists:
            # never delete or reuse it. Either way, withdraw our own Job.
            log.error("k8s_secret_create_failed", run_id=str(run_id), status=e.status)
            await _k8s(_delete_job_owned, batch, job_name, namespace, run_id, attempt)
            return _failure(f"failed to create the run's k8s Secret: {e}")

        # 4. release, atomically with the cancel re-check
        async def _release():
            await _k8s(batch.patch_namespaced_job, job_name, namespace, {"spec": {"suspend": False}})

        if not await lease.release_job(_release):
            await _k8s(_delete_job_owned, batch, job_name, namespace, run_id, attempt)
            return _aborted("run was cancelled before its Job was released; Job withdrawn")

        # 5./6.
        return await self.monitor_job(lease=lease, run_id=run_id, job_name=job_name)

    async def monitor_job(self, *, lease, run_id: uuid.UUID, job_name: str) -> dict:
        """Poll a released Job until terminal, without pinning a thread.
        Returns the run result (with "_k8s_job" set so the caller can clean
        up after persisting), or an "_aborted" marker if the lease was lost
        (cancel, or takeover after a stall — in which case the new owner
        drives the Job; we must not touch it)."""
        settings = get_settings()
        attempt = lease.attempt
        namespace = settings.runner_namespace
        batch, core = _clients()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.runner_job_timeout_seconds + _POLL_GRACE_SECONDS
        delay = _POLL_INITIAL_DELAY_SECONDS
        while True:
            if lease.lost.is_set():
                return _aborted("run lease lost (cancelled or taken over); stopped monitoring")
            try:
                job = await _k8s(batch.read_namespaced_job_status, job_name, namespace)
            except ApiException as e:
                if e.status == 404:
                    return await self.result_for_missing_job(run_id=run_id, attempt=attempt,
                                                             job_name=job_name, released=True)
                raise
            status = job.status
            if status.succeeded or status.failed:
                outcome = "succeeded" if status.succeeded else "failed"
                log.info("k8s_job_terminal", run_id=str(run_id), job_name=job_name, outcome=outcome)
                result = await self._collect_result(core, run_id, attempt, job_name, outcome)
                result["_k8s_job"] = {"name": job_name, "attempt": attempt}
                return result
            if loop.time() >= deadline:
                result = await _stored_result(run_id, attempt) or _failure(
                    f"dbt build did not complete within the configured timeout (job={job_name})")
                result["_k8s_job"] = {"name": job_name, "attempt": attempt}
                return result
            try:
                await asyncio.wait_for(lease.lost.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 1.5, _POLL_MAX_DELAY_SECONDS)

    async def _collect_result(self, core, run_id: uuid.UUID, attempt: int, job_name: str,
                              outcome: str) -> dict:
        namespace = get_settings().runner_namespace
        log_text = await _k8s(_read_job_logs, core, job_name, namespace)
        stored = await _stored_result(run_id, attempt, _extract_ref(log_text))
        if stored is not None:
            return stored
        inline = _extract_result(log_text)
        if inline is not None:
            return inline
        tail = "\n".join(log_text.splitlines()[-40:]) if log_text else "(no logs captured)"
        return _failure(f"k8s Job {job_name} ended without reporting a result (outcome={outcome}). "
                        f"Last log lines:\n{tail}")

    async def result_for_missing_job(self, *, run_id: uuid.UUID, attempt: int, job_name: str,
                                     released: bool) -> dict:
        """The attempt's Job no longer exists. Never re-runs a build that may
        have executed: a recorded result is used if one exists; a Job that
        was provably never released is reported as never started (safe to
        requeue); otherwise the run fails with an explicit reason."""
        stored = await _stored_result(run_id, attempt)
        if stored is not None:
            return stored
        if not released:
            return {"_never_started": True, "success": False, "errors": [], "nodes": []}
        return _failure(
            f"k8s Job {job_name} no longer exists and no result was recorded in the artifact store "
            "(garbage-collected after its TTL or deleted externally). Not re-running automatically "
            "to avoid executing the build twice — trigger a new run if needed.")

    async def adopt(self, *, lease, run_id: uuid.UUID, job_name: str, released: bool) -> dict:
        """Reattach to an attempt whose previous owner died (reconciler)."""
        namespace = get_settings().runner_namespace
        batch, _ = _clients()
        try:
            job = await _k8s(batch.read_namespaced_job, job_name, namespace)
        except ApiException as e:
            if e.status != 404:
                raise
            return await self.result_for_missing_job(run_id=run_id, attempt=lease.attempt,
                                                     job_name=job_name, released=released)
        if not _is_ours(job, run_id, lease.attempt):
            return _failure(f"k8s Job {job_name} is not labelled as this run's attempt; refusing to adopt")
        if job.spec is not None and job.spec.suspend:
            # Never released -> never ran. Withdraw it so the run can requeue.
            await _k8s(_delete_job_owned, batch, job_name, namespace, run_id, lease.attempt)
            return {"_never_started": True, "success": False, "errors": [], "nodes": []}
        return await self.monitor_job(lease=lease, run_id=run_id, job_name=job_name)

    async def cleanup_job(self, *, run_id: uuid.UUID, attempt: int, job_name: str) -> bool:
        batch, _ = _clients()
        return await _k8s(_delete_job_owned, batch, job_name, get_settings().runner_namespace,
                          run_id, attempt)

    async def terminate_job(self, *, run_id: uuid.UUID, attempt: int, job_name: str) -> str:
        """Request deletion of this attempt's Job and return its state now (see CONFIRMED_JOB_STATES).
        Raises on a Kubernetes API error, so the caller retries instead of assuming it worked."""
        batch, _ = _clients()
        return await _k8s(_terminate_job_sync, batch, job_name, get_settings().runner_namespace,
                          run_id, attempt)

    async def cancel_job(self, *, run_id: uuid.UUID, attempt: int, job_name: str | None) -> bool:
        """Delete this attempt's Job (if any). Returns True if one was deleted."""
        if not job_name:
            return False
        return await self.cleanup_job(run_id=run_id, attempt=attempt, job_name=job_name)

    async def k8s_reachable(self, timeout: float = 3.0) -> bool:
        try:
            _ensure_k8s_config()
            await asyncio.wait_for(_k8s(client.VersionApi().get_code), timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False


_isolation: RunnerIsolation | None = None


def get_runner_isolation() -> RunnerIsolation:
    global _isolation
    if _isolation is None:
        _isolation = RunnerIsolation()
    return _isolation
