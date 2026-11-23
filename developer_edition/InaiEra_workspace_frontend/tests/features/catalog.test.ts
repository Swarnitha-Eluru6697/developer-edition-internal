import { describe, expect, it } from "vitest";
import { catalogFeatureApi } from "../../src/features/catalog";
import { callEndpoint } from "../helpers/apiHarness";

describe("catalog: endpoints", () => {
  it("lists Unified Catalog sources a connection can link to", async () => {
    const opts = [{ name: "pg_sales", engine: "postgres", host: "db", port: 5432, database: "sales", username: "ro" }];
    const { request, data } = await callEndpoint(catalogFeatureApi.endpoints.listUcsCatalogs, undefined, opts);
    expect(request).toMatchObject({ method: "GET", url: "/api/v1/connections/ucs-catalogs" });
    expect(data).toEqual(opts);
  });

  it("browses tables within a namespace of a connection", async () => {
    const { request } = await callEndpoint(catalogFeatureApi.endpoints.listCatalogTables, { connId: "c1", namespace: "public" }, []);
    expect(request.url).toBe("/api/v1/connections/c1/catalog/namespaces/public/tables");
  });

  it("PATCHes connection updates", async () => {
    const { request } = await callEndpoint(catalogFeatureApi.endpoints.updateConnection, { id: "c1", body: { name: "renamed" } }, {});
    expect(request).toEqual({ method: "PATCH", url: "/api/v1/connections/c1", body: { name: "renamed" } });
  });
});
