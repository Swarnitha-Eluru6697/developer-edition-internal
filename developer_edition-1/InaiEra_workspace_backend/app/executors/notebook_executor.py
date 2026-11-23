"""Notebook cell executor (Phase 2).

Architecture:
  - On session start, Workspace asks JupyterHub to spawn a kernel pod for the user.
  - JupyterHub returns a kernel ID and a URL.
  - For each cell execution, Workspace sends an 'execute_request' over the Jupyter
    messaging protocol (WebSocket) and streams back stream / display_data /
    execute_result / error messages as MIME bundles.

This module is fully fleshed but the JupyterHub deployment piece is operational
not code — provisioned by Infra & Product Release Mgmt as part of the data plane.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
import structlog
import websockets

from app.core.config import get_settings

log = structlog.get_logger()


@dataclass
class NotebookChunk:
    output_type: str  # 'stream' | 'display_data' | 'execute_result' | 'error' | 'done'
    payload: dict
    row_count: int = 0
    is_final: bool = False


class JupyterHubClient:
    """Thin client over JupyterHub admin + Jupyter Server APIs."""

    def __init__(self, hub_url: str, admin_token: str):
        self._hub_url = hub_url.rstrip("/")
        self._admin_token = admin_token

    async def ensure_user(self, username: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._hub_url}/hub/api/users/{username}",
                headers={"Authorization": f"token {self._admin_token}"},
            )
            if resp.status_code not in (201, 409):
                resp.raise_for_status()

    async def start_server(self, username: str) -> str:
        """Start the user's notebook server and return its base URL."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self._hub_url}/hub/api/users/{username}/server",
                headers={"Authorization": f"token {self._admin_token}"},
            )
            if resp.status_code not in (201, 202, 400):
                resp.raise_for_status()
            # Poll for readiness
            for _ in range(60):
                info_resp = await client.get(
                    f"{self._hub_url}/hub/api/users/{username}",
                    headers={"Authorization": f"token {self._admin_token}"},
                )
                info_resp.raise_for_status()
                info = info_resp.json()
                server = info.get("servers", {}).get("", {})
                if server.get("ready"):
                    return f"{self._hub_url}/user/{username}"
                await asyncio.sleep(2)
        raise RuntimeError(f"JupyterHub server for {username} did not become ready")

    async def create_kernel(self, server_url: str, image: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload: dict = {}
            if image:
                payload["name"] = image
            resp = await client.post(
                f"{server_url}/api/kernels",
                json=payload,
                headers={"Authorization": f"token {self._admin_token}"},
            )
            resp.raise_for_status()
            return resp.json()


class NotebookExecutor:
    """Runs notebook cells against a per-user kernel."""

    def __init__(self):
        settings = get_settings()
        if not settings.jupyterhub_admin_token:
            self._hub: JupyterHubClient | None = None
        else:
            self._hub = JupyterHubClient(
                settings.jupyterhub_url, settings.jupyterhub_admin_token,
            )

    @staticmethod
    def username_for(user_id: str) -> str:
        """Map a Workspace user id to its JupyterHub username. Must match the
        mapping used in execute() so cells target the kernel we allocated."""
        return f"u{user_id.replace('-', '')[:24]}"

    @property
    def configured(self) -> bool:
        return self._hub is not None

    async def ensure_kernel(self, username: str, image: str | None = None) -> dict:
        if not self._hub:
            raise RuntimeError("JupyterHub not configured")
        await self._hub.ensure_user(username)
        server_url = await self._hub.start_server(username)
        return await self._hub.create_kernel(server_url, image)

    async def execute(
        self,
        *,
        execution_id: str,
        user_id: str,
        code_text: str,
        target: str | None,  # target = kernel_id here
        max_rows: int,
        timeout_seconds: int,
    ) -> AsyncIterator[NotebookChunk]:
        if not self._hub:
            yield NotebookChunk(
                output_type="error",
                payload={"error_code": "NO_JUPYTERHUB", "error_message": "Notebook execution not configured"},
                is_final=True,
            )
            return

        settings = get_settings()
        # target is the kernel_id we created earlier
        kernel_id = target
        if not kernel_id:
            yield NotebookChunk(
                output_type="error",
                payload={"error_code": "NO_KERNEL", "error_message": "No kernel allocated for session"},
                is_final=True,
            )
            return

        # username is the user_id mapped to JupyterHub
        username = self.username_for(user_id)
        kernel_ws = (
            settings.jupyterhub_url.replace("http", "ws").rstrip("/")
            + f"/user/{username}/api/kernels/{kernel_id}/channels"
        )

        msg_id = str(uuid.uuid4())
        request = {
            "header": {
                "msg_id": msg_id,
                "username": username,
                "session": execution_id,
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code_text,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }

        async with websockets.connect(
            kernel_ws,
            extra_headers={"Authorization": f"token {self._hub._admin_token}"},
            max_size=10 * 1024 * 1024,
        ) as ws:
            await ws.send(json.dumps(request))
            output_index = 0
            try:
                async with asyncio.timeout(timeout_seconds):
                    async for raw in ws:
                        msg = json.loads(raw)
                        msg_type = msg["header"]["msg_type"]
                        parent_msg_id = msg.get("parent_header", {}).get("msg_id")
                        if parent_msg_id != msg_id:
                            continue
                        if msg_type == "stream":
                            yield NotebookChunk(
                                output_type="stream",
                                payload={
                                    "output_index": output_index,
                                    "name": msg["content"]["name"],
                                    "text": msg["content"]["text"],
                                },
                            )
                            output_index += 1
                        elif msg_type == "display_data":
                            yield NotebookChunk(
                                output_type="display_data",
                                payload={
                                    "output_index": output_index,
                                    "mime_bundle": msg["content"]["data"],
                                    "metadata": msg["content"].get("metadata", {}),
                                },
                            )
                            output_index += 1
                        elif msg_type == "execute_result":
                            yield NotebookChunk(
                                output_type="execute_result",
                                payload={
                                    "output_index": output_index,
                                    "execution_count": msg["content"]["execution_count"],
                                    "mime_bundle": msg["content"]["data"],
                                    "metadata": msg["content"].get("metadata", {}),
                                },
                            )
                            output_index += 1
                        elif msg_type == "error":
                            yield NotebookChunk(
                                output_type="error",
                                payload={
                                    "output_index": output_index,
                                    "ename": msg["content"]["ename"],
                                    "evalue": msg["content"]["evalue"],
                                    "traceback": msg["content"]["traceback"],
                                },
                                is_final=False,
                            )
                            output_index += 1
                        elif msg_type == "status":
                            if msg["content"]["execution_state"] == "idle":
                                yield NotebookChunk(
                                    output_type="done",
                                    payload={"output_count": output_index},
                                    is_final=True,
                                )
                                return
            except TimeoutError:
                yield NotebookChunk(
                    output_type="error",
                    payload={"error_code": "TIMEOUT", "error_message": f"Cell exceeded {timeout_seconds}s"},
                    is_final=True,
                )

    async def shutdown(self, *, user_id: str, kernel_id: str) -> None:
        """Delete a kernel on the user's Jupyter server (WS-LIFE-013)."""
        if not self._hub:
            return
        url = (
            get_settings().jupyterhub_url.rstrip("/")
            + f"/user/{self.username_for(user_id)}/api/kernels/{kernel_id}"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.delete(url, headers={"Authorization": f"token {self._hub._admin_token}"})
                if resp.status_code not in (204, 404):
                    resp.raise_for_status()
                log.info("notebook_kernel_shutdown", kernel_id=kernel_id)
            except httpx.HTTPError as e:
                log.warning("notebook_kernel_shutdown_failed", kernel_id=kernel_id, error=str(e))

    async def cancel(self, *, execution_id: str, user_id: str, target: str | None) -> None:
        """Interrupt the cell's kernel (target = kernel id) — the Jupyter
        equivalent of Ctrl-C: the running cell raises KeyboardInterrupt, the
        kernel and its state survive for the next cell."""
        if not self._hub or not target:
            return
        url = (
            get_settings().jupyterhub_url.rstrip("/")
            + f"/user/{self.username_for(user_id)}/api/kernels/{target}/interrupt"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, headers={"Authorization": f"token {self._hub._admin_token}"})
                resp.raise_for_status()
                log.info("notebook_kernel_interrupted", execution_id=execution_id, kernel_id=target)
            except httpx.HTTPError as e:
                log.warning("notebook_interrupt_failed", execution_id=execution_id, error=str(e))
