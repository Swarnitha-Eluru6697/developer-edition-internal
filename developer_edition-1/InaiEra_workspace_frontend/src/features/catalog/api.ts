/**
 * Catalog feature package (WS-FE-014): database connections and the
 * catalog browsing endpoints (namespaces/tables/columns) scoped to one.
 */
import { workspaceApi } from "../../api/base";
import type {
  CatalogColumn,
  CatalogNamespace,
  CatalogTable,
  ConnectionTestResult,
  DatabaseConnection,
  DatabaseConnectionCreate,
  UcsCatalogOption,
  UUID,
} from "../../types";

export const catalogFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    listConnections: builder.query<DatabaseConnection[], void>({
      query: () => "/connections",
      providesTags: ["DatabaseConnection"],
    }),
    createConnection: builder.mutation<DatabaseConnection, DatabaseConnectionCreate>({
      query: (body) => ({ url: "/connections", method: "POST", body }),
      invalidatesTags: ["DatabaseConnection"],
    }),
    updateConnection: builder.mutation<DatabaseConnection, { id: UUID; body: Partial<DatabaseConnectionCreate> }>({
      query: ({ id, body }) => ({ url: `/connections/${id}`, method: "PATCH", body }),
      invalidatesTags: ["DatabaseConnection"],
    }),
    listUcsCatalogs: builder.query<UcsCatalogOption[], void>({
      query: () => "/connections/ucs-catalogs",
    }),
    deleteConnection: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/connections/${id}`, method: "DELETE" }),
      invalidatesTags: ["DatabaseConnection"],
    }),
    testConnection: builder.mutation<ConnectionTestResult, UUID>({
      query: (id) => ({ url: `/connections/${id}/test`, method: "POST" }),
    }),
    // Tagged with DatabaseConnection so editing a connection (TLS, link to a
    // Unified Catalog source, …) refetches its tree instead of showing a stale one.
    listNamespaces: builder.query<CatalogNamespace[], UUID>({
      query: (connId) => `/connections/${connId}/catalog/namespaces`,
      providesTags: ["DatabaseConnection"],
    }),
    listCatalogTables: builder.query<CatalogTable[], { connId: UUID; namespace: string }>({
      query: ({ connId, namespace }) => `/connections/${connId}/catalog/namespaces/${namespace}/tables`,
      providesTags: ["DatabaseConnection"],
    }),
    listColumns: builder.query<CatalogColumn[], { connId: UUID; namespace: string; table: string }>({
      query: ({ connId, namespace, table }) => `/connections/${connId}/catalog/namespaces/${namespace}/tables/${table}/columns`,
    }),
  }),
});

export const {
  useListConnectionsQuery,
  useCreateConnectionMutation,
  useUpdateConnectionMutation,
  useDeleteConnectionMutation,
  useTestConnectionMutation,
  useListUcsCatalogsQuery,
  useListNamespacesQuery,
  useListCatalogTablesQuery,
  useListColumnsQuery,
} = catalogFeatureApi;
