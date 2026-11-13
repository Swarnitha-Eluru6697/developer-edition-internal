"""Developer Edition: register every deployed (published) agent in UCS.

Publishing an agent registers it in UCS (routers/agents.py → ucs_sync.sync_agent /
sync_agent_version + ucs_publish.publish_all). Agents that were published before UCS was
reachable, or that came from the seed data, never went through that path, so they are
missing from the catalog. This does the same registration for them, with the product's own
functions, and records the outcome on the agent row exactly as a publish does
(ucs_synced, ucs_sync_error, ucs_fqn).

Idempotent: an agent already in the catalog is re-confirmed, not duplicated.
Runs inside the agents image (docker compose run --rm ucs-register).
"""
import os
import sys

from sqlmodel import Session, select

from app.db import engine
from app.models import Agent
from app.ucs_client import UCSUnavailable, agent_fqn
from app.ucs_sync import UCSSyncError, sync_agent, sync_agent_version
from app import ucs_publish

PLATFORM_BASE = os.getenv("PLATFORM_BASE_URL", "http://127.0.0.1:8000")


def main() -> int:
    failed = 0
    with Session(engine) as session:
        agents = session.exec(select(Agent).where(Agent.status == "published")).all()
        print(f"[ucs-register] {len(agents)} published agent(s)")
        for a in agents:
            run_url = f"{PLATFORM_BASE}/api/agents/{a.id}/run"
            try:
                sync_agent(a.id, a.name, a.description or "")
                sync_agent_version(a.id, a.name, a.version or "v1", run_url, f"Published {a.version or 'v1'}")
                ucs_publish.publish_all(a, run_url=run_url, session=session)
                a.ucs_synced, a.ucs_sync_error, a.ucs_fqn = True, None, agent_fqn(a.id, a.name)
                print(f"[ucs-register]   ok      {a.id} {a.name} {a.version} → {a.ucs_fqn}")
            except (UCSSyncError, UCSUnavailable) as e:
                a.ucs_synced, a.ucs_sync_error = False, str(e)[:500]
                failed += 1
                print(f"[ucs-register]   FAILED  {a.id} {a.name}: {str(e)[:200]}")
            session.add(a)
            session.commit()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
