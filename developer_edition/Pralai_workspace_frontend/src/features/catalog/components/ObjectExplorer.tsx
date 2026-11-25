/**
 * ObjectExplorer — Database / Catalog tree in the left sidebar.
 * Lists objects (Iceberg tables) via the selected connection.
 *
 * Users can add/manage connections from here. Clicking a table name
 * calls onInsertTable so the editor can append it to the current SQL.
 */
import { useState, useRef, useEffect } from "react";
import { useDispatch } from "react-redux";
import {
  useListConnectionsQuery,
  useListNamespacesQuery,
  useListCatalogTablesQuery,
  useDeleteConnectionMutation,
} from "../../../api/client";
import { openConnectionModal, setActiveConnection, notify } from "../../../store/uiSlice";
import type { AppDispatch } from "../../../store";
import type { DatabaseConnection, UUID } from "../../../types";
import { apiErrorMessage, asApiError } from "../../../api/errors";
import { askConfirm } from "../../../shared/ui/dialogStore";
import { useHasPermission } from "../../../rbac/hooks";
import SearchSelect from "../../../shared/ui/SearchSelect";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

interface Props {
  onInsertTable?: (tableName: string) => void;
}

export function ObjectExplorer({ onInsertTable }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const [activeConnId, setActiveConnId] = useState<UUID | null>(null);
  const [openNS, setOpenNS] = useState<Record<string, boolean>>({});
  const [height, setHeight] = useState(220);
  const [collapsed, setCollapsed] = useState(false);
  const drag = useRef<{ y: number; h: number } | null>(null);

  const onResizeDown = (e: React.MouseEvent) => {
    e.preventDefault();
    drag.current = { y: e.clientY, h: height };
    const move = (ev: MouseEvent) => {
      if (!drag.current) return;
      // Drag up → taller (the panel is anchored at the bottom of the sidebar).
      const next = drag.current.h + (drag.current.y - ev.clientY);
      setHeight(Math.max(90, Math.min(next, 640)));
    };
    const up = () => {
      drag.current = null;
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const { data: connections } = useListConnectionsQuery();

  const [deleteConn] = useDeleteConnectionMutation();
  // Browsing/selecting a connection needs no extra permission here; creating,
  // editing, deleting or testing one is workspace:connections:manage.
  const canManageConnections = useHasPermission("workspace:connections:manage");

  const firstConn = connections?.[0];
  const connId = activeConnId ?? firstConn?.id ?? null;
  const { data: namespaces, error: nsError, isFetching: nsLoading } =
    useListNamespacesQuery(connId ?? ("" as UUID), { skip: !connId });

  const handleDelete = async (c: DatabaseConnection) => {
    const ok = await askConfirm({
      title: "Delete database connection",
      message: `Delete the connection "${c.name}"? It is shared across all workspaces in your org, and queries using it will stop working. This cannot be undone.`,
      confirmLabel: "Delete connection",
      confirmText: c.name,
      danger: true,
    });
    if (!ok) return;
    try {
      await deleteConn(c.id).unwrap();
      if (activeConnId === c.id) setActiveConnId(null);
      dispatch(notify({ kind: "success", message: `Deleted connection "${c.name}"` }));
    } catch (e: unknown) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, `Failed to delete "${c.name}"`, { prefer: "detail" }) }));
    }
  };

  // Publish the selected connection so the SQL editor runs against its DB.
  useEffect(() => {
    dispatch(setActiveConnection(connId));
  }, [connId, dispatch]);

  return (
    <div style={{ borderTop: "0.5px solid var(--ws-line, #E3E8E2)", display: "flex", flexDirection: "column", height: collapsed ? "auto" : height, flexShrink: 0 }}>
      {/* Drag-to-resize handle */}
      <div
        onMouseDown={onResizeDown}
        title="Drag to resize"
        style={{ height: 6, cursor: "row-resize", background: "transparent", flexShrink: 0 }}
        onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(37,99,235,0.25)")}
        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
      />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 10px 6px", flexShrink: 0 }}>
        <span
          onClick={() => setCollapsed((c) => !c)}
          style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ws-muted, #5B6D62)", cursor: "pointer", display: "flex", alignItems: "center", gap: 4, userSelect: "none" }}
        >
          <i className={`ti ${collapsed ? "ti-chevron-right" : "ti-chevron-down"}`} style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)" }} />
          Database Explorer
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          {canManageConnections && (
            <button
              onClick={() => dispatch(openConnectionModal(null))}
              title="Add connection"
              style={{ background: "none", border: "none", cursor: "pointer", color: "var(--ws-subtle, #888)", fontSize: 14, padding: 2 }}
            ><i className="ti ti-plus" /></button>
          )}
          <button
            onClick={() => setCollapsed((c) => !c)}
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--ws-subtle, #888)", fontSize: 14, padding: 2 }}
            title={collapsed ? "Expand" : "Collapse"}
          ><i className={`ti ${collapsed ? "ti-layout-bottombar-expand" : "ti-layout-bottombar-collapse"}`} /></button>
        </div>
      </div>
      {!collapsed && (<>
      <div style={{ flex: 1, overflow: "auto", padding: "4px" }}>
          <>
            {/* Connection selector */}
            {connections && connections.length > 0 && (
              <div style={{ padding: "4px 6px", marginBottom: 4 }}>
                <SearchSelect
                  size="sm"
                  ariaLabel="Database connection"
                  noun="connection"
                  menuMinWidth={240}
                  value={connId ?? ""}
                  onChange={(v) => setActiveConnId(v as UUID)}
                  options={connections.map((c) => ({ value: c.id, label: c.name, hint: c.connection_type }))}
                />
              </div>
            )}

            {/* Only once the list has loaded: `connections` is undefined while loading, which is not "none". */}
            {connections !== undefined && connections.length === 0 && (
              <div style={{ padding: "16px 8px", textAlign: "center", fontSize: 12, color: "var(--ws-subtle, #888)" }}>
                No connections yet.
                {canManageConnections && (<>
                  <br />
                  <button
                    onClick={() => dispatch(openConnectionModal(null))}
                    style={{ marginTop: 8, fontSize: 11.5, color: "var(--ws-accent, #086347)", background: "none", border: "none", cursor: "pointer", textDecoration: "underline" }}
                  >Add a database connection</button>
                </>)}
              </div>
            )}

            {nsLoading && !namespaces && (
              <div style={{ padding: "6px 10px", fontSize: 11, color: "var(--ws-subtle, #73827a)" }}>Loading…</div>
            )}
            {nsError && <BrowseError error={nsError} />}
            {!nsError && namespaces && namespaces.length === 0 && (
              <div style={{ padding: "6px 10px", fontSize: 11, color: "var(--ws-subtle, #73827a)" }}>No schemas found.</div>
            )}

            {/* Namespace tree */}
            {!nsError && namespaces?.map((ns) => (
              <NamespaceNode
                key={ns.name}
                namespace={ns.name}
                connId={connId!}
                open={!!openNS[ns.name]}
                onToggle={() => setOpenNS((prev) => ({ ...prev, [ns.name]: !prev[ns.name] }))}
                onInsertTable={onInsertTable}
              />
            ))}

            {/* Manage connections link — workspace:connections:manage; everyone
                else can still see/select them above, just not edit/delete/test. */}
            {canManageConnections && connections && connections.length > 0 && (
              <div style={{ padding: "6px 6px 2px", borderTop: "0.5px solid var(--ws-line, #EEF2ED)", marginTop: 4 }}>
                {connections.map((c) => (
                  <div key={c.id} style={{ display: "flex", alignItems: "center", gap: 5, padding: "2px 4px", fontSize: 11, color: "var(--ws-subtle, #888)" }}>
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
                    <span style={{ color: c.is_verified ? "var(--ws-ok, #2f6b14)" : "#f59e0b", fontSize: 10 }}>●</span>
                    <IconAction size="sm" icon={ACTION_ICON.edit} label={`Edit ${c.name}`} onClick={() => dispatch(openConnectionModal(c.id))} />
                    <IconAction size="sm" icon={ACTION_ICON.delete} label={`Delete ${c.name}`} danger onClick={() => handleDelete(c)} />
                  </div>
                ))}
              </div>
            )}
          </>

      </div>
      </>)}
    </div>
  );
}

function NamespaceNode({
  namespace, connId, open, onToggle, onInsertTable,
}: { namespace: string; connId: UUID; open: boolean; onToggle: () => void; onInsertTable?: (t: string) => void }) {
  const { data: tables, error } = useListCatalogTablesQuery({ connId, namespace }, { skip: !open });

  return (
    <div>
      <div
        onClick={onToggle}
        style={{ display: "flex", alignItems: "center", gap: 5, padding: "3px 5px", cursor: "pointer", borderRadius: 4, fontSize: 11, fontFamily: "monospace", color: "var(--ws-muted, #5B6D62)" }}
        onMouseEnter={(e) => (e.currentTarget.style.background = "var(--ws-hover, #EEF2ED)")}
        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
      >
        <i className={`ti ${open ? "ti-chevron-down" : "ti-chevron-right"}`} style={{ fontSize: 10, color: "var(--ws-faint, #999)" }} />
        <i className="ti ti-database" style={{ fontSize: 12, color: "var(--ws-accent, #0A7D5A)" }} />
        {namespace.toUpperCase()}
      </div>
      {open && error && <BrowseError error={error} indent />}
      {open && tables?.length === 0 && (
        <div style={{ padding: "3px 5px 3px 22px", fontSize: 11, color: "var(--ws-subtle, #73827a)" }}>No tables</div>
      )}
      {open && tables?.map((t) => (
        <div
          key={t.name}
          onClick={() => onInsertTable?.(`${namespace}.${t.name}`)}
          style={{ display: "flex", alignItems: "center", gap: 5, padding: "3px 5px 3px 22px", cursor: "pointer", fontSize: 11, fontFamily: "monospace", color: "var(--ws-muted, #5B6D62)", borderRadius: 4 }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--ws-hover, #EEF2ED)")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
          title={`Click to insert ${namespace}.${t.name}`}
        >
          <i className={`ti ${t.table_type === "view" ? "ti-eye" : "ti-table"}`} style={{ fontSize: 11, color: "var(--ws-accent, #0A7D5A)" }} />
          {t.name}
        </div>
      ))}
    </div>
  );
}

/** The backend's reason a browse call failed (CATALOG_UNAVAILABLE, …). */
function BrowseError({ error, indent }: { error: unknown; indent?: boolean }) {
  const message = apiErrorMessage(error, asApiError(error).error ?? "Couldn't load the catalog.", { prefer: "detail" });
  return (
    <div style={{ margin: indent ? "2px 6px 4px 22px" : "4px 6px", padding: "6px 8px", fontSize: 11, lineHeight: 1.45, color: "var(--ws-bad, #b23b3b)", background: "var(--ws-bad-bg, #fbe9e9)", borderRadius: 5 }}>
      <i className="ti ti-alert-circle" style={{ fontSize: 11 }} /> {message}
    </div>
  );
}
