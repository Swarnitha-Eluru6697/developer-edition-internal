"""
Splitting a quality job's input batch into chunks to stage it (GAP-13).

A batch is validated against three byte limits (one record, the whole batch — the request body is limited earlier, by
app/core/body_limit.py, before it is even parsed) and cut into ordered chunks that close at whichever of
settings.quality_job_chunk_records / settings.quality_job_chunk_bytes is reached first. A worker later streams through the
chunks one at a time, so no more than one chunk of the batch is ever in memory at once, and records which chunks it
finished so a reclaimed job resumes rather than starting over.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import settings


@dataclass(frozen=True)
class Chunk:
    index: int
    records: list[dict[str, Any]]
    byte_size: int


def record_size(record: dict[str, Any]) -> int:
    """Serialized size in bytes, as it is stored (compact JSON, UTF-8)."""
    return len(json.dumps(record, separators=(",", ":"), default=str, ensure_ascii=False).encode())


def plan_chunks(records: list[dict[str, Any]]) -> list[Chunk]:
    """The batch as ordered chunks, or ValueError (surfaced to the caller as 413) if it breaks a size limit. An empty
    batch is one empty chunk, so a job always has at least one chunk to run."""
    max_record = settings.quality_job_max_record_bytes
    max_total = settings.quality_job_max_total_bytes
    chunks: list[Chunk] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    total = 0

    for position, record in enumerate(records):
        size = record_size(record)
        if size > max_record:
            raise ValueError(
                f"Record {position} is {size:,} bytes; a single record may be at most {max_record:,} bytes."
            )
        total += size
        if total > max_total:
            raise ValueError(
                f"The batch is larger than the {max_total:,} bytes a job may stage (reached at record {position}). "
                "Submit it in smaller batches."
            )
        if current and (
            len(current) >= settings.quality_job_chunk_records
            or current_bytes + size > settings.quality_job_chunk_bytes
        ):
            chunks.append(Chunk(len(chunks), current, current_bytes))
            current, current_bytes = [], 0
        current.append(record)
        current_bytes += size

    chunks.append(Chunk(len(chunks), current, current_bytes))
    return chunks
