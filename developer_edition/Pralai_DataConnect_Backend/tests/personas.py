"""Test-only personas: the DataConnect roles as SEEDED in the Admin Portal (authpack seed/roles-permissions.json).

Role names live here, in test data only — the application never sees them. A persona is just the permission list
Envoy would forward for a user holding that role; the RBAC tests use them to prove today's role behaviour survives
the move to permissions.
"""

ADMIN: frozenset[str] = frozenset({
    "dataconnect:access:view",
    "dataconnect:audit:view",
    "dataconnect:connections:edit",
    "dataconnect:connections:test",
    "dataconnect:connections:view",
    "dataconnect:dashboard:view",
    "dataconnect:lineage:view",
    "dataconnect:monitoring:view",
    "dataconnect:pipelines:deploy",
    "dataconnect:pipelines:edit",
    "dataconnect:pipelines:operate",
    "dataconnect:pipelines:view",
    "dataconnect:quality:edit",
    "dataconnect:quality:view",
    "dataconnect:schemas:approve-drift",
    "dataconnect:schemas:view",
    "dataconnect:settings:edit",
    "dataconnect:settings:view",
    "dataconnect:templates:manage",
    "dataconnect:templates:view",
    "dataconnect:transforms:edit",
    "dataconnect:transforms:view",
})

INTEGRATOR: frozenset[str] = frozenset({
    "dataconnect:access:view",
    "dataconnect:audit:view",
    "dataconnect:connections:edit",
    "dataconnect:connections:test",
    "dataconnect:connections:view",
    "dataconnect:dashboard:view",
    "dataconnect:lineage:view",
    "dataconnect:monitoring:view",
    "dataconnect:pipelines:deploy",
    "dataconnect:pipelines:edit",
    "dataconnect:pipelines:operate",
    "dataconnect:pipelines:view",
    "dataconnect:quality:edit",
    "dataconnect:quality:view",
    "dataconnect:schemas:approve-drift",
    "dataconnect:schemas:view",
    "dataconnect:settings:view",
    "dataconnect:templates:view",
    "dataconnect:transforms:edit",
    "dataconnect:transforms:view",
})

VIEWER: frozenset[str] = frozenset({
    "dataconnect:access:view",
    "dataconnect:audit:view",
    "dataconnect:connections:view",
    "dataconnect:dashboard:view",
    "dataconnect:lineage:view",
    "dataconnect:monitoring:view",
    "dataconnect:pipelines:view",
    "dataconnect:quality:view",
    "dataconnect:schemas:view",
    "dataconnect:settings:view",
    "dataconnect:templates:view",
    "dataconnect:transforms:view",
})

# A role of ANOTHER application (seed: ml-platform-admin): holds no dataconnect:* permission at all.
OTHER_APP: frozenset[str] = frozenset({
    "mlstudio:access:view",
    "mlstudio:alerts:edit",
    "mlstudio:alerts:view",
    "mlstudio:base-models:sync",
    "mlstudio:clusters:view",
    "mlstudio:compute:approve",
})

# Not in the seed yet: added by this repo's permissions.json (see docs/auth/PERMISSIONS.md, "Seed delta").
SEED_DELTA_ADMIN_INTEGRATOR = frozenset({"dataconnect:schemas:edit", "dataconnect:lineage:edit"})
# Alerts (the old view_alerts / ack_alerts / manage_alerts): everyone views, Admin + Integrator respond, Admin manages.
SEED_DELTA_ALERTS_VIEW = frozenset({"dataconnect:alerts:view"})
SEED_DELTA_ALERTS_RESPOND = frozenset({"dataconnect:alerts:respond"})
SEED_DELTA_ALERTS_MANAGE = frozenset({"dataconnect:alerts:manage"})

PERSONAS: dict[str, frozenset[str]] = {
    "admin": ADMIN | SEED_DELTA_ADMIN_INTEGRATOR | SEED_DELTA_ALERTS_VIEW | SEED_DELTA_ALERTS_RESPOND | SEED_DELTA_ALERTS_MANAGE,
    "integrator": INTEGRATOR | SEED_DELTA_ADMIN_INTEGRATOR | SEED_DELTA_ALERTS_VIEW | SEED_DELTA_ALERTS_RESPOND,
    "viewer": VIEWER | SEED_DELTA_ALERTS_VIEW,
}
