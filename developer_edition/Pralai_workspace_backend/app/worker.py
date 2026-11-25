"""Execution worker process — `python -m app.worker` (WS-EXEC-005).

Runs the execution router's worker side (claim pending executions, run
them, reconcile orphans) and the lifecycle reconciler, with no HTTP server.
Deployed as its own Kubernetes Deployment so long queries and notebook cells
never share CPU, memory or event-loop time with API request handling, and
so the two scale independently. Any number of replicas is safe: claims and
recoveries are atomic per row (see execution_router.transition).

Health: while the worker loops are alive and ticking, the process touches a
heartbeat file; `python -m app.worker --check` exits non-zero if it's stale,
which the Deployment uses as its liveness/readiness exec probe.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import structlog

from app.core.config import get_settings
from app.core.logging_setup import configure_logging
from app.executors.local_kernel_executor import assert_local_kernel_safe_for_environment
from app.services.alerts.sweeper import get_alert_sweeper
from app.services.execution_router import get_execution_router
from app.services.lifecycle_service import get_lifecycle_reconciler
from app.storage.secrets import assert_secrets_backend_safe_for_environment

log = structlog.get_logger()

HEARTBEAT_PATH = os.environ.get("WORKER_HEARTBEAT_PATH", "/tmp/workspace-worker.heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_MAX_AGE_SECONDS = 60


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    assert_secrets_backend_safe_for_environment()
    assert_local_kernel_safe_for_environment()

    router = get_execution_router()
    lifecycle = get_lifecycle_reconciler()
    await router.start()
    await lifecycle.start()
    alert_sweeper = get_alert_sweeper()
    await alert_sweeper.start()
    log.info("workspace.worker_started", concurrency=settings.execution_worker_concurrency)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            if router.healthy():
                with open(HEARTBEAT_PATH, "w") as f:
                    f.write(str(time.time()))
            else:
                log.error("workspace.worker_unhealthy")
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                pass
    finally:
        log.info("workspace.worker_stopping")
        await alert_sweeper.stop()
        await lifecycle.stop()
        await router.stop()


def check() -> int:
    """Exit status for the probe: 0 if the heartbeat is fresh."""
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
    except OSError:
        return 1
    return 0 if age < HEARTBEAT_MAX_AGE_SECONDS else 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    asyncio.run(run())
