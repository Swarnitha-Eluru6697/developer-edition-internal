"""Links between jobs (Upstream Jobs and Downstream Jobs; stored as "Dependents": B is a Dependent of A == A is upstream of B).

The Runner only STORES the links, validates them and returns them; the rule they drive is enforced in Dagster
(InaiEra-dbt-runner-dagster, dependencies.py): a job does not run unless every job above it has a recent success. Dagster reads the links
from the internal project routes (`upstream_project_ids`).

A link is about jobs, not data: the projects need not share a table (a data dependency is something dbt itself checks inside a build).
Links chain: customers -> scorecard -> executive. Links may not form a loop (A -> B -> A); register/update refuse one.
"""
from __future__ import annotations

import uuid
from collections import deque

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import validation_failed
from app.models import Project, ProjectDependent

FIELD = "dependent_project_ids"

Edges = dict[uuid.UUID, set[uuid.UUID]]          # upstream project -> its direct Dependents


async def load_edges(db: AsyncSession, org_id: str) -> Edges:
    rows = await db.execute(
        select(ProjectDependent.project_id, ProjectDependent.dependent_project_id)
        .join(Project, Project.id == ProjectDependent.project_id)
        .where(Project.org_id == org_id)
    )
    edges: Edges = {}
    for up, down in rows.all():
        edges.setdefault(up, set()).add(down)
    return edges


def below(edges: Edges, start: uuid.UUID) -> list[uuid.UUID]:
    """Every project below `start` (its Dependents, theirs, ...), nearest first, `start` itself excluded."""
    seen: set[uuid.UUID] = {start}
    order: list[uuid.UUID] = []
    queue = deque([start])
    while queue:
        for nxt in sorted(edges.get(queue.popleft(), ()), key=str):
            if nxt not in seen:
                seen.add(nxt)
                order.append(nxt)
                queue.append(nxt)
    return order


def above(edges: Edges, start: uuid.UUID) -> list[uuid.UUID]:
    """Every project above `start` (whose failure reaches it), nearest first."""
    inverted: Edges = {}
    for up, downs in edges.items():
        for down in downs:
            inverted.setdefault(down, set()).add(up)
    return below(inverted, start)


def _path(edges: Edges, src: uuid.UUID, dst: uuid.UUID) -> list[uuid.UUID] | None:
    """A chain of links src -> ... -> dst, or None."""
    prev: dict[uuid.UUID, uuid.UUID | None] = {src: None}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        if node == dst:
            path = [node]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])            # type: ignore[arg-type]
            return path[::-1]
        for nxt in sorted(edges.get(node, ()), key=str):
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    return None


async def set_dependents(db: AsyncSession, project: Project, dependent_ids: list[uuid.UUID]) -> None:
    """Replace `project`'s Dependents. Refuses itself, a project that does not exist in this org, and a loop."""
    wanted = list(dict.fromkeys(dependent_ids))            # keep order, drop repeats
    if project.id in wanted:
        raise validation_failed(FIELD, "A project cannot be its own dependent.")
    if wanted:
        found = dict((await db.execute(
            select(Project.id, Project.name).where(Project.id.in_(wanted), Project.org_id == project.org_id)
        )).all())
        missing = [str(i) for i in wanted if i not in found]
        if missing:
            raise validation_failed(FIELD, f"No such project: {', '.join(missing)}.")
        edges = await load_edges(db, project.org_id)
        edges[project.id] = set(wanted)
        for dep in wanted:
            loop = _path(edges, dep, project.id)
            if loop:
                names = dict((await db.execute(select(Project.id, Project.name).where(Project.id.in_(loop)))).all())
                chain = " → ".join(names.get(i, str(i)) for i in [project.id, *loop])
                raise validation_failed(FIELD, f"These links would make a loop: {chain}.")
    await db.execute(delete(ProjectDependent).where(ProjectDependent.project_id == project.id))
    for dep in wanted:
        db.add(ProjectDependent(project_id=project.id, dependent_project_id=dep))
    await db.flush()


async def set_links(db: AsyncSession, project: Project, upstream_ids: list[uuid.UUID], dependent_ids: list[uuid.UUID]) -> None:
    """Replace BOTH ends at once: the projects above `project` and the projects below it. Used when a request carries both lists, so the
    result is judged as a whole: a project moved from "below" to "above" is not refused because of the link it is leaving, and a project
    named in both lists is refused as the loop it is."""
    ups, downs = list(dict.fromkeys(upstream_ids)), list(dict.fromkeys(dependent_ids))
    if project.id in ups or project.id in downs:
        raise validation_failed("upstream_project_ids" if project.id in ups else FIELD, "A project cannot be linked to itself.")
    wanted = list(dict.fromkeys([*ups, *downs]))
    if wanted:
        found = dict((await db.execute(
            select(Project.id, Project.name).where(Project.id.in_(wanted), Project.org_id == project.org_id)
        )).all())
        missing = [str(i) for i in wanted if i not in found]
        if missing:
            raise validation_failed("upstream_project_ids" if any(i in ups for i in [uuid.UUID(m) for m in missing]) else FIELD,
                                    f"No such project: {', '.join(missing)}.")
        edges = await load_edges(db, project.org_id)
        edges.pop(project.id, None)                      # the links out of `project` and into it are both being replaced
        for out in edges.values():
            out.discard(project.id)
        edges[project.id] = set(downs)
        for up in ups:
            edges.setdefault(up, set()).add(project.id)
        for dep in downs:                                # project -> dep closes a loop when dep already reaches project
            loop = _path(edges, dep, project.id)
            if loop:
                names = dict((await db.execute(select(Project.id, Project.name).where(Project.id.in_(loop)))).all())
                chain = " → ".join(names.get(i, str(i)) for i in [project.id, *loop])
                raise validation_failed("upstream_project_ids", f"These links would make a loop: {chain}.")
    await db.execute(delete(ProjectDependent).where(
        (ProjectDependent.project_id == project.id) | (ProjectDependent.dependent_project_id == project.id)))
    for up in ups:
        db.add(ProjectDependent(project_id=up, dependent_project_id=project.id))
    for dep in downs:
        db.add(ProjectDependent(project_id=project.id, dependent_project_id=dep))
    await db.flush()


async def set_upstreams(db: AsyncSession, project: Project, upstream_ids: list[uuid.UUID]) -> None:
    """Replace the projects directly ABOVE `project` (the ones whose failure fails it). Same links as set_dependents, written from the
    other end: refuses itself, a project that does not exist in this org, and a loop."""
    field = "upstream_project_ids"
    wanted = list(dict.fromkeys(upstream_ids))
    if project.id in wanted:
        raise validation_failed(field, "A project cannot be its own upstream.")
    if wanted:
        found = dict((await db.execute(
            select(Project.id, Project.name).where(Project.id.in_(wanted), Project.org_id == project.org_id)
        )).all())
        missing = [str(i) for i in wanted if i not in found]
        if missing:
            raise validation_failed(field, f"No such project: {', '.join(missing)}.")
        edges = await load_edges(db, project.org_id)
        for up in wanted:                                 # up -> project closes a loop when project already reaches up
            loop = _path(edges, project.id, up)
            if loop:
                names = dict((await db.execute(select(Project.id, Project.name).where(Project.id.in_(loop)))).all())
                chain = " → ".join(names.get(i, str(i)) for i in [up, *loop])
                raise validation_failed(field, f"These links would make a loop: {chain}.")
    await db.execute(delete(ProjectDependent).where(ProjectDependent.dependent_project_id == project.id))
    for up in wanted:
        db.add(ProjectDependent(project_id=up, dependent_project_id=project.id))
    await db.flush()


async def upstreams_of(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Each project's direct upstream projects (the ones whose failure fails it), in a stable order (by name)."""
    if not project_ids:
        return {}
    rows = await db.execute(
        select(ProjectDependent.dependent_project_id, ProjectDependent.project_id)
        .join(Project, Project.id == ProjectDependent.project_id)
        .where(ProjectDependent.dependent_project_id.in_(project_ids))
        .order_by(Project.name, Project.id)
    )
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for down, up in rows.all():
        out.setdefault(down, []).append(up)
    return out


async def dependents_of(db: AsyncSession, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Each project's direct Dependents, in a stable order (by name: links saved together share one timestamp)."""
    if not project_ids:
        return {}
    rows = await db.execute(
        select(ProjectDependent.project_id, ProjectDependent.dependent_project_id)
        .join(Project, Project.id == ProjectDependent.dependent_project_id)
        .where(ProjectDependent.project_id.in_(project_ids))
        .order_by(Project.name, Project.id)
    )
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for up, down in rows.all():
        out.setdefault(up, []).append(down)
    return out
