"""
Graceful shutdown: every step runs, none can hang it, and nothing is silently skipped.

The lifespan's teardown used to be one long straight line of `await`s. An exception in any step skipped every later one — so a failure
stopping the operation worker meant the Kafka producer and the database engine were never closed — and a step stuck on an unreachable
broker blocked shutdown forever, until the orchestrator SIGKILLed the process with work half-finished and connections open.

`run_shutdown` fixes both: steps run IN ORDER (the order encodes dependencies: stop the things that use a client before closing the client),
each is isolated (its failure is logged and the next still runs) and each is bounded (it is abandoned after `timeout` seconds and logged).
A CancelledError is NOT swallowed: if the process is being cancelled again (a second signal), shutdown stops being polite.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import structlog

logger = structlog.get_logger(__name__)

ShutdownStep = tuple[str, Callable[[], Awaitable[object]]]


async def run_step(name: str, step: Callable[[], Awaitable[object]], *, timeout: float) -> bool:
    """Run one step; True if it completed. Never raises (except CancelledError)."""
    started = time.monotonic()
    try:
        await asyncio.wait_for(step(), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.error("shutdown.step_timed_out", step=name, timeout_seconds=timeout)
        return False
    except Exception:
        logger.exception("shutdown.step_failed", step=name)
        return False
    logger.debug("shutdown.step_done", step=name, seconds=round(time.monotonic() - started, 3))
    return True


async def run_shutdown(steps: Sequence[ShutdownStep], *, timeout: float) -> list[str]:
    """Run every step in order; returns the names of the ones that failed or timed out (empty = clean shutdown)."""
    failed: list[str] = []
    for name, step in steps:
        if not await run_step(name, step, timeout=timeout):
            failed.append(name)
    if failed:
        logger.error("shutdown.completed_with_failures", failed=failed)
    return failed


async def stop_all_isolated(stoppers: Sequence[tuple[str, Callable[[], Awaitable[object]]]], *, timeout: float) -> list[str]:
    """Stop many independent things AT ONCE (a worker per pipeline, say): one that fails or hangs must not leave the others running."""
    names = [name for name, _ in stoppers]
    results = await asyncio.gather(*(run_step(name, stop, timeout=timeout) for name, stop in stoppers), return_exceptions=True)
    return [name for name, result in zip(names, results, strict=True) if result is not True]
