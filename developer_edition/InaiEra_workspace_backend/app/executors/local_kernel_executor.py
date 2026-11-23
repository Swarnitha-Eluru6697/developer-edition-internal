"""Local IPython kernel executor.

Dev / self-hosted fallback for running Python notebook cells when JupyterHub
isn't configured. Starts an in-process IPython kernel (subprocess) per session
and executes cells against it over the Jupyter messaging protocol — so variables
and imports persist across cells, exactly like a Jupyter notebook.

The output chunk shape (NotebookChunk) mirrors NotebookExecutor's, so the
execution_router and the /result endpoint treat both paths identically.

Not for multi-worker production: kernels live in this process's memory, so a
'--workers N' deployment would not share them across workers. Use JupyterHub
there (set jupyterhub_admin_token). Dev runs a single reloader worker.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator

import structlog

from app.core.config import get_settings
from app.executors.notebook_executor import NotebookChunk

log = structlog.get_logger()


def assert_local_kernel_safe_for_environment() -> None:
    """Startup guard (WS-RUNTIME-012): the in-process kernel fallback must
    never be a production execution path — it runs arbitrary user code as a
    subprocess of the API server itself, inside the API pod's own security
    boundary, with no CPU/memory/network isolation beyond what that pod
    already gets for ordinary API traffic. That's an acceptable trade for a
    single-worker dev box; it is not for staging/prod, where notebooks must
    run in isolated JupyterHub-spawned kernel pods instead."""
    settings = get_settings()
    if settings.local_python_kernel_enabled and settings.environment in ("staging", "prod"):
        raise RuntimeError(
            "local_python_kernel_enabled=true is not permitted when environment is "
            f"'{settings.environment}'. Set LOCAL_PYTHON_KERNEL_ENABLED=false and "
            "configure JUPYTERHUB_URL/JUPYTERHUB_ADMIN_TOKEN so notebooks run in "
            "isolated JupyterHub-spawned kernel pods instead."
        )


class LocalKernelExecutor:
    def __init__(self):
        # kernel_id -> (AsyncKernelManager, AsyncKernelClient)
        self._kernels: dict[str, tuple] = {}
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        try:
            import jupyter_client  # noqa: F401
            return True
        except Exception:
            return False

    async def ensure_kernel(self, kernel_id: str | None = None) -> str:
        """Start a kernel (or reuse an existing one for kernel_id) and return
        its id. Reusing the id lets a kernel that was lost to a reloader restart
        transparently re-spawn under the same id the session already stored."""
        async with self._lock:
            if kernel_id and kernel_id in self._kernels:
                return kernel_id
            from jupyter_client.manager import AsyncKernelManager

            km = AsyncKernelManager(kernel_name="python3")
            await km.start_kernel()
            kc = km.client()
            kc.start_channels()
            try:
                await kc.wait_for_ready(timeout=60)
            except RuntimeError:
                kc.stop_channels()
                await km.shutdown_kernel(now=True)
                raise
            kid = kernel_id or uuid.uuid4().hex
            self._kernels[kid] = (km, kc)
            log.info("local_kernel_started", kernel_id=kid)
            return kid

    async def execute(
        self,
        *,
        execution_id: str,
        user_id: str,
        code_text: str,
        target: str | None,  # kernel_id
        max_rows: int,
        timeout_seconds: int,
    ) -> AsyncIterator[NotebookChunk]:
        if not self.available:
            yield NotebookChunk(
                output_type="error",
                payload={
                    "error_code": "NO_LOCAL_KERNEL",
                    "error_message": "Local Python kernel unavailable — rebuild the "
                                     "workspace image so jupyter_client and ipykernel are installed.",
                },
                is_final=True,
            )
            return

        # Ensure we have a live kernel. If the stored id is stale (process
        # restarted), ensure_kernel re-spawns one under that id.
        try:
            kid = await self.ensure_kernel(target)
        except Exception as e:  # noqa: BLE001
            yield NotebookChunk(
                output_type="error",
                payload={"error_code": "KERNEL_START_FAILED", "error_message": str(e)[:500]},
                is_final=True,
            )
            return

        km, kc = self._kernels[kid]
        msg_id = kc.execute(code_text, store_history=True, allow_stdin=False, stop_on_error=True)
        output_index = 0
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    msg = await kc.get_iopub_msg()
                    if msg.get("parent_header", {}).get("msg_id") != msg_id:
                        continue
                    msg_type = msg["header"]["msg_type"]
                    content = msg["content"]
                    if msg_type == "stream":
                        yield NotebookChunk(
                            output_type="stream",
                            payload={
                                "output_index": output_index,
                                "name": content.get("name", "stdout"),
                                "text": content.get("text", ""),
                            },
                        )
                        output_index += 1
                    elif msg_type == "display_data":
                        yield NotebookChunk(
                            output_type="display_data",
                            payload={
                                "output_index": output_index,
                                "mime_bundle": content.get("data", {}),
                                "metadata": content.get("metadata", {}),
                            },
                        )
                        output_index += 1
                    elif msg_type == "execute_result":
                        yield NotebookChunk(
                            output_type="execute_result",
                            payload={
                                "output_index": output_index,
                                "execution_count": content.get("execution_count"),
                                "mime_bundle": content.get("data", {}),
                                "metadata": content.get("metadata", {}),
                            },
                        )
                        output_index += 1
                    elif msg_type == "error":
                        yield NotebookChunk(
                            output_type="error",
                            payload={
                                "output_index": output_index,
                                "ename": content.get("ename", "Error"),
                                "evalue": content.get("evalue", ""),
                                "traceback": content.get("traceback", []),
                            },
                            is_final=False,
                        )
                        output_index += 1
                    elif msg_type == "status" and content.get("execution_state") == "idle":
                        yield NotebookChunk(
                            output_type="done",
                            payload={"output_count": output_index},
                            is_final=True,
                        )
                        return
        except TimeoutError:
            try:
                await km.interrupt_kernel()
            except Exception:  # noqa: BLE001
                pass
            yield NotebookChunk(
                output_type="error",
                payload={"error_code": "TIMEOUT", "error_message": f"Cell exceeded {timeout_seconds}s"},
                is_final=True,
            )

    async def cancel(self, *, execution_id: str, user_id: str, target: str | None) -> None:
        """Interrupt the cell's kernel (target = kernel id); the kernel and
        its state survive for the next cell."""
        entry = self._kernels.get(target or "")
        if not entry:
            return
        km, _kc = entry
        try:
            await km.interrupt_kernel()
            log.info("local_kernel_interrupted", execution_id=execution_id, kernel_id=target)
        except Exception as e:  # noqa: BLE001
            log.warning("local_kernel_interrupt_failed", execution_id=execution_id, error=str(e))

    async def shutdown(self, kernel_id: str) -> None:
        """Stop a kernel and free its process (WS-LIFE-013: idle/ended sessions)."""
        async with self._lock:
            entry = self._kernels.pop(kernel_id, None)
        if not entry:
            return
        km, kc = entry
        try:
            kc.stop_channels()
            await km.shutdown_kernel(now=True)
            log.info("local_kernel_shutdown", kernel_id=kernel_id)
        except Exception as e:  # noqa: BLE001
            log.warning("local_kernel_shutdown_failed", kernel_id=kernel_id, error=str(e))
