-- Developer Edition is single-user and has no RBAC page, so the one user's group
-- ("developers", sent by the gateway in x-user-groups) gets every privilege on every
-- catalog object. Without this row UCS data reads, ML Studio training/promotion and
-- Agents publishing are denied (they ask UCS). Re-running is harmless.
INSERT INTO rbac_bindings (tenant_id, group_name, group_source, role_name, catalog, schema_name,
                           object_name, object_type, privileges, active)
VALUES ('default', 'developers', 'oidc', 'developer-edition-all', '*', '*', '*', 'catalog',
        '["ALL_PRIVILEGES","agent:*"]', true)
ON CONFLICT ON CONSTRAINT uq_rbac_binding_scope DO UPDATE SET privileges = EXCLUDED.privileges, active = true;
