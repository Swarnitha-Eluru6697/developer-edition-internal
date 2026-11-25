from fastapi import APIRouter

from app.api.routes import (
    alert_actions,
    alerts,
    audit,
    catalog,
    connection_config_schemas,
    connections,
    diagnostics,
    discovery,
    health,
    lineage,
    me,
    monitoring,
    operations,
    pipelines,
    quality,
    topic_policies,
    transforms,
)
from app.api.routes import schemas as schema_routes

# Liveness/readiness are probed by the container runtime and load balancers, which carry no user identity: they are the
# gateway's public paths (app/core/gateway.py's public_paths), the only API routes reachable without a principal. They
# return no tenant data and no internal detail — see app/api/routes/health.py.
public_router = APIRouter()
public_router.include_router(health.router, tags=["health"])
# The Acknowledge / Resolve buttons in a Slack / Teams alert: the person clicking is not signed in to DataConnect, so the credential is the
# signed token in the link, not the gateway (app/api/routes/alert_actions.py; not in the OpenAPI document).
public_router.include_router(alert_actions.router)

api_router = APIRouter()
api_router.include_router(me.router, tags=["me"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(audit.router, prefix="/audit", tags=["audit"])
api_router.include_router(diagnostics.router, prefix="/diagnostics", tags=["diagnostics"])
api_router.include_router(catalog.router, prefix="/connector-templates", tags=["connector catalog"])
api_router.include_router(connections.router, prefix="/connections", tags=["connections"])
api_router.include_router(
    connection_config_schemas.router,
    prefix="/connection-config-schemas",
    tags=["connection catalog"],
)
api_router.include_router(pipelines.router, prefix="/pipelines", tags=["pipelines"])
api_router.include_router(transforms.router, prefix="/transform-policies", tags=["transforms"])
api_router.include_router(topic_policies.router, prefix="/topic-policies", tags=["topic policies"])
api_router.include_router(schema_routes.router, prefix="/schemas", tags=["schemas"])
api_router.include_router(quality.router, tags=["quality"])
api_router.include_router(lineage.router, tags=["lineage"])
api_router.include_router(monitoring.router, prefix="/runtime", tags=["monitoring"])
api_router.include_router(operations.router, prefix="/operations", tags=["operations"])
api_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])

