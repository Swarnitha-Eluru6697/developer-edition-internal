"""Operator maintenance from the command line.

    python -m app.maintenance sweep-uploads
    python -m app.maintenance migrate-backend --target s3 --limit 500
    python -m app.maintenance reconcile-hitl
    python -m app.maintenance reconcile-kb acme_support_faq

This is the SUPPORTED way to run these operations, and the reason the
HTTP routes can fail closed. They were plain, unauthenticated routes on
the product API surface (`POST /api/uploads/_sweep-expired` and
friends), which meant an operation that deletes rows with their storage
objects, or relocates up to 10,000 of them, was reachable by anything
that could reach the product API. A CronJob or a Kubernetes Job runs
these commands with no network surface at all — nothing to expose and
nothing to authenticate.

Each command calls the SAME function the HTTP route calls. There is one
implementation of every operation; this module only decides how it is
invoked and prints what happened.
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlmodel import Session

from .db import engine


def _run(fn, **kwargs) -> int:
    with Session(engine) as session:
        result = fn(session=session, **kwargs)
    print(json.dumps(result, indent=2, default=str))
    # Non-zero when an operation reports per-item failures, so a
    # CronJob's own status reflects reality rather than "the command ran".
    failed = result.get("failed") if isinstance(result, dict) else None
    if isinstance(failed, list) and failed:
        return 1
    if isinstance(failed, int) and failed:
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.maintenance", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sweep-uploads", help="delete uploads past their retention TTL, and their stored objects")

    migrate = sub.add_parser("migrate-backend", help="move stored objects to a different storage backend")
    migrate.add_argument("--target", required=True, help="target backend name, e.g. 'local' or 's3'")
    migrate.add_argument("--limit", type=int, default=100,
                         help="how many rows to migrate in this batch (default 100); run repeatedly "
                              "until nothing is left rather than raising this to cover a large backlog")

    sub.add_parser("reconcile-hitl", help="resolve HITL requests whose owning run is no longer live")

    kb = sub.add_parser("reconcile-kb", help="finish outbox rows whose remote commit succeeded locally did not")
    kb.add_argument("kb_id", help="knowledge base id")

    args = parser.parse_args(argv)

    # Imported inside, not at module level: importing the routers pulls
    # in the whole application, and `--help` should not need a database.
    if args.command == "sweep-uploads":
        from .routers.uploads import trigger_expired_sweep
        return _run(trigger_expired_sweep)
    if args.command == "migrate-backend":
        from .routers.uploads import trigger_backend_migration
        return _run(trigger_backend_migration, target=args.target, limit=args.limit)
    if args.command == "reconcile-hitl":
        from .routers.execution import trigger_stale_hitl_reconcile
        return _run(trigger_stale_hitl_reconcile)
    if args.command == "reconcile-kb":
        from .routers.knowledge_bases import reconcile_kb
        with Session(engine) as session:
            print(json.dumps(reconcile_kb(args.kb_id, session), indent=2, default=str))
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
