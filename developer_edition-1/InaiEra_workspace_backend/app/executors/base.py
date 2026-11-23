"""Executor protocol.

Two concrete implementations:
  - SqlExecutor:      runs SQL against the Data Lake House query service
  - NotebookExecutor: runs notebook cells via JupyterHub kernels in the data plane

The execution_router dispatches by request type.
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol


class ExecutorResult(Protocol):
    output_type: str
    payload: dict
    row_count: int
    is_final: bool


class Executor(Protocol):
    """An executor runs code and yields output chunks."""

    async def execute(
        self,
        *,
        execution_id: str,
        user_id: str,
        code_text: str,
        target: str | None,
        max_rows: int,
        timeout_seconds: int,
    ) -> AsyncIterator[ExecutorResult]:
        """Yield chunks as they arrive. The router publishes each to WebSocket."""
        ...

    async def cancel(self, *, execution_id: str, user_id: str, target: str | None) -> None:
        """Stop the in-flight run (lakehouse cancel / kernel interrupt)."""
        ...
