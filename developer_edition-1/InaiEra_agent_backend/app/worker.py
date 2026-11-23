"""
Temporal worker process. Runs alongside the FastAPI app, in a separate
process — it's what actually executes durable-path workflows by polling the
task queue. Without this running, a durable-path run will hang waiting for
a worker.

Run it with:
    python -m app.worker

Requires a Temporal server reachable at 127.0.0.1:7233 (the local dev
server: `temporal server start-dev`, or point TEMPORAL_ADDRESS at a real
cluster).
"""
import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .temporal_workflow import AgentRunWorkflow, execute_node_activity, render_hitl_form_activity

TASK_QUEUE = "inaiera-agents-task-queue"
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")


async def main():
    # Real, fail-CLOSED secrets-encryption-key check (§0.89,
    # GAP_REPORT) — the real, direct fix for the report's own core
    # concern: this Temporal worker is a genuinely separate process
    # from the API server, and without a real, shared
    # SECRETS_ENCRYPTION_KEY, this process would generate its OWN,
    # independent, temporary key the moment it first needs to encrypt/
    # decrypt a secret during workflow execution — a real secret the
    # API process encrypted would then be genuinely undecryptable
    # here. Checked before ever connecting to Temporal, matching the
    # same real "fail loud before doing anything else" philosophy
    # main.py's own identical check already established for the API
    # process. See secrets_crypto.py's own check_production_key_
    # requirement for the full real reasoning.
    from .secrets_crypto import check_production_key_requirement
    check_production_key_requirement()

    client = await Client.connect(TEMPORAL_ADDRESS, namespace="default")
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AgentRunWorkflow],
        activities=[execute_node_activity, render_hitl_form_activity],
    )
    print(f"Temporal worker started — connected to {TEMPORAL_ADDRESS}, polling '{TASK_QUEUE}'")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
