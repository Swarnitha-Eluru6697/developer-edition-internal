"""
Create (or check) the Kafka Connect internal topics for the worker pools.

    python -m app.tools.provision_connect_topics                # create what is missing, for every pool with its own cluster
    python -m app.tools.provision_connect_topics --pool connect-cdc-pool --pool connect-sink-pool
    python -m app.tools.provision_connect_topics --check        # only report; exit 1 if anything is missing or off-design

Run it BEFORE the pool's Connect workers first start, so the topics exist with the designed settings (compacted, replicated,
min-ISR) instead of whatever the worker would create. See app/services/connect_topology.py.
"""

import argparse
import asyncio
import sys

from app.core.connect_pools import POOL_BY_ID
from app.services.connect_topology import (
    dedicated_pool_ids,
    provision_internal_topics,
    verify_topology,
)


async def _run(pools: list[str] | None, check_only: bool) -> int:
    if not check_only:
        for result in await provision_internal_topics(pools):
            for topic in result.created:
                print(f"created   {topic}")
            for topic in result.already_present:
                print(f"present   {topic}")
    exit_code = 0
    for pool in await verify_topology(pools or dedicated_pool_ids() or None):
        status = "ok" if pool.ok else "PROBLEMS"
        print(f"{pool.pool_id}: {pool.cluster_mode} — {status}")
        if pool.note:
            print(f"    {pool.note}")
        if pool.error:
            print(f"    {pool.error}")
        for topic in pool.topics:
            for problem in topic.problems:
                print(f"    {topic.topic}: {problem}")
        if not pool.ok:
            exit_code = 1
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool", action="append", choices=sorted(POOL_BY_ID), help="a pool to handle (repeatable)")
    parser.add_argument("--check", action="store_true", help="only verify; create nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.pool, args.check)))


if __name__ == "__main__":
    main()
