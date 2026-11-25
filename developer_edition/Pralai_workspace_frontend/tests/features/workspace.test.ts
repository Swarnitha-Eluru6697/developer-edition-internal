import { beforeEach, vi, describe, expect, it } from "vitest";
import {
  NEW_FILE_OPTIONS,
  allowedNewFileOptions,
  deleteWorkspaceConfirm,
  activeWorkspaceStorageKey,
  pickInitialWorkspace,
  readRememberedWorkspace,
  rememberWorkspace,
  resolveActiveWorkspace,
  baseName,
  collectFileIds,
  collectFilePaths,
  fileIcon,
  fileTreeView,
  workspaceLabel,
  filterWorkspaces,
  findNodeByPath,
  flattenMe,
  isUntitledPath,
  languageFromFileName,
  seedContent,
  suggestedSaveName,
  untitledFileName,
  workspaceFeatureApi,
} from "../../src/features/workspace";
import { FLINKSQL_TEMPLATE } from "../../src/features/flink";
import type { FileTreeNode, MeResponse, Workspace } from "../../src/types";
import { callEndpoint } from "../helpers/apiHarness";

const ME: MeResponse = {
  user: { id: "u-1", external_id: "ext-1", email: null, display_name: null },
  claims: { user_id: "sub-1", org_id: "org-1", email: "ada@example.com", username: "ada" },
  permissions: ["workspace:access:view", "workspace:sql:run"],
  workspace_model: "personal",
};

const node = (over: Partial<FileTreeNode>): FileTreeNode =>
  ({ name: "", path: "", type: "directory", children: [], ...over }) as FileTreeNode;

describe("workspace: getMe flattening", () => {
  it("flattens claims/user and derives display_name from the email (permissions come from /api/v1/me, not from here)", () => {
    expect(flattenMe(ME)).toEqual({
      user_id: "sub-1", org_id: "org-1", email: "ada@example.com", display_name: "ada",
    });
  });

  it("prefers the user mirror's display_name", () => {
    expect(flattenMe({ ...ME, user: { ...ME.user, display_name: "Ada L." } }).display_name).toBe("Ada L.");
  });

  it("getMe hits /auth/me and applies the transform", async () => {
    const { request, data } = await callEndpoint(workspaceFeatureApi.endpoints.getMe, undefined, ME);
    expect(request).toMatchObject({ method: "GET", url: "/api/v1/auth/me" });
    expect(data).toMatchObject({ email: "ada@example.com", display_name: "ada" });
  });
});

describe("workspace: file endpoints", () => {
  it("updateFile PUTs the optimistic-concurrency field in the JSON body", async () => {
    const { request } = await callEndpoint(workspaceFeatureApi.endpoints.updateFile,
      { workspaceId: "w1", fileId: "f1", body: { content: "select 1", if_match_sha256: "abc" } });
    expect(request).toEqual({ method: "PUT", url: "/api/v1/workspaces/w1/files/f1", body: { content: "select 1", if_match_sha256: "abc" } });
  });

  it("getFileTree passes the branch as a query param", async () => {
    const { request } = await callEndpoint(workspaceFeatureApi.endpoints.getFileTree, { workspaceId: "w1", branch: "dev" });
    expect(request.url).toBe("/api/v1/workspaces/w1/files/tree?branch=dev");
  });
});

describe("workspace: file helpers", () => {
  it("infers editor languages, *.flink.sql before .sql", () => {
    expect(languageFromFileName("job.flink.sql")).toBe("flinksql");
    expect(languageFromFileName("stg.SQL")).toBe("sql");
    expect(languageFromFileName("schema.yml")).toBe("yaml");
    expect(languageFromFileName("nb.ipynb")).toBe("ipynb");
    expect(languageFromFileName("Makefile")).toBe("other");
  });

  it("finds file nodes by path and collects every file id under a folder", () => {
    const tree = node({ children: [
      node({ name: "models", path: "models", children: [
        node({ name: "a.sql", path: "models/a.sql", type: "file", id: "1" }),
        node({ name: "b.sql", path: "models/b.sql", type: "file", id: "2" }),
      ] }),
      node({ name: "README.md", path: "README.md", type: "file", id: "3" }),
    ] });
    expect(findNodeByPath(tree, "models/b.sql")?.id).toBe("2");
    expect(findNodeByPath(tree, "models")).toBeUndefined(); // folders aren't files
    expect(collectFileIds(tree)).toEqual(["1", "2", "3"]);
  });

  it("labels an open tab with the file's name, from the tree (not 'file_<id>')", () => {
    const tree = node({ children: [
      node({ name: "scorecard", path: "scorecard", children: [
        node({ name: "README.md", path: "scorecard/README.md", type: "file", id: "a" }),
      ] }),
      node({ name: "README.md", path: "README.md", type: "file", id: "b" }),
      node({ name: "stg_orders.sql", path: "models/stg_orders.sql", type: "file", id: "c" }),
    ] });
    const paths = collectFilePaths(tree);
    expect(baseName(paths.get("a")!)).toBe("README.md");
    expect(baseName(paths.get("c")!)).toBe("stg_orders.sql");
    expect(paths.get("a")).toBe("scorecard/README.md");   // two README.md stay distinguishable by path (tooltip)
    expect(paths.get("missing")).toBeUndefined();          // caller falls back until the tree loads
    expect(collectFilePaths(undefined).size).toBe(0);
  });

  it("names and seeds new files", () => {
    expect(untitledFileName("flinksql", 42)).toBe("untitled42.flink.sql");
    expect(seedContent("flinksql")).toBe(FLINKSQL_TEMPLATE);
    expect(JSON.parse(seedContent("ipynb")).cells).toHaveLength(1);
    expect(seedContent("sql")).toBe("");
    expect(isUntitledPath("untitled123.sql")).toBe(true);
    expect(suggestedSaveName("untitled123.sql")).toBe("query.sql");
  });

  it("gates Add New options by permission (sql needs only workspace:files:edit)", () => {
    expect(NEW_FILE_OPTIONS.find((o) => o.kind === "sql")?.permission).toBeUndefined();
    expect(NEW_FILE_OPTIONS.find((o) => o.kind === "dbt")?.permission).toBe("workspace:dbt:edit");
    expect(allowedNewFileOptions([])).toEqual([]);
    expect(allowedNewFileOptions(["workspace:dbt:edit", "workspace:notebooks:run"])).toEqual([]);   // no workspace:files:edit
    expect(allowedNewFileOptions(["workspace:files:edit"]).map((o) => o.kind)).toEqual(["sql"]);
    expect(allowedNewFileOptions(["workspace:files:edit", "workspace:dbt:edit"]).map((o) => o.kind)).toEqual(["sql", "dbt"]);
  });

  it("filters workspaces case-insensitively", () => {
    const ws = [{ id: "1", name: "Analytics" }, { id: "2", name: "Ops" }] as Workspace[];
    expect(filterWorkspaces(ws, "ana").map((w) => w.id)).toEqual(["1"]);
    expect(filterWorkspaces(undefined, "x")).toEqual([]);
  });
});


describe("workspace: deleting a workspace", () => {
  const list = [{ id: "a" }, { id: "b" }];

  it("keeps the selection while it exists, otherwise moves to the first workspace", () => {
    const seen = new Set(["a", "b", "gone"]);
    expect(resolveActiveWorkspace(list, "b", seen)).toBe("b");
    expect(resolveActiveWorkspace(list, "gone", seen)).toBe("a");     // it WAS listed and is now gone: deleted
    expect(resolveActiveWorkspace(list, null, seen)).toBe("a");        // first load
    expect(resolveActiveWorkspace([], "gone", seen)).toBeNull();       // that was the last one
    expect(resolveActiveWorkspace(undefined, "b", seen)).toBe("b");    // list not loaded yet: don't jump
  });

  it("does not bounce off a workspace that was just created and isn't in the list yet", () => {
    // 'new' was selected right after creating it, before the list refetched: never seen -> keep it
    expect(resolveActiveWorkspace(list, "new", new Set(["a", "b"]))).toBe("new");
    // once the refetch lists it, it is seen; if it later disappears that is a deletion
    expect(resolveActiveWorkspace(list, "new", new Set(["a", "b", "new"]))).toBe("a");
  });

  it("makes the user type the workspace name and says the Git repo is not deleted", () => {
    const c = deleteWorkspaceConfirm("inaiera-dbt-test");
    expect(c).toMatchObject({ confirmText: "inaiera-dbt-test", danger: true, confirmLabel: "Delete workspace" });
    expect(c.message).toContain('"inaiera-dbt-test"');
    expect(c.message).toMatch(/Git repository is NOT deleted/);
    expect(c.message).toMatch(/not committed and pushed is lost/);
  });
});


describe("workspace: file icons", () => {
  it("picks an icon by file type, so tabs and the Files panel agree", () => {
    expect(fileIcon("stg_orders.sql").icon).toBe("ti-file-type-sql");
    expect(fileIcon("live.flink.sql").icon).toBe("ti-wave-sine");          // before .sql
    expect(fileIcon("README.md").icon).toBe("ti-markdown");
    expect(fileIcon("dbt_project.yml").icon).toBe("ti-file-settings");
    expect(fileIcon("schema.YAML").icon).toBe("ti-file-settings");
    expect(fileIcon("job.py").icon).toBe("ti-brand-python");
    expect(fileIcon("nb.ipynb").icon).toBe("ti-notebook");
    expect(fileIcon("data.json").icon).toBe("ti-braces");
    expect(fileIcon("Untitled-1.sql").icon).toBe("ti-file-type-sql");      // an unsaved new file
  });

  it("falls back to the stored language, then to a plain file", () => {
    expect(fileIcon("Makefile", "sql").icon).toBe("ti-file-type-sql");
    expect(fileIcon("notes", "md").icon).toBe("ti-markdown");
    expect(fileIcon("Makefile").icon).toBe("ti-file");
    expect(fileIcon(".gitkeep", "other").icon).toBe("ti-file");
  });

  it("only uses icon names that exist in the Tabler webfont the app loads (3.5.0)", () => {
    const names = ["sql", "md", "yml", "py", "ipynb", "json", "csv", "xml", "txt", "x.flink.sql", "unknown"].map((e) => fileIcon(`f.${e}`).icon);
    const known = new Set(["ti-file-type-sql", "ti-markdown", "ti-file-settings", "ti-brand-python", "ti-notebook", "ti-braces",
      "ti-file-type-csv", "ti-file-type-xml", "ti-file-type-txt", "ti-wave-sine", "ti-file"]);
    expect(names.every((n) => known.has(n))).toBe(true);
  });
});


describe("workspace: no placeholder text flashes while loading", () => {
  it("the switcher shows a placeholder until the list is loaded, never a made-up name", () => {
    const list = [{ id: "a", name: "inaiera-dbt-test" }, { id: "b", name: "other" }];
    expect(workspaceLabel(undefined, null)).toEqual({ kind: "loading" });           // list still loading
    expect(workspaceLabel(list, null)).toEqual({ kind: "loading" });                // loaded, active one picked a moment later
    expect(workspaceLabel(list, "b")).toEqual({ kind: "name", text: "other" });
    expect(workspaceLabel([], null)).toEqual({ kind: "none" });                     // really no workspaces
    expect(workspaceLabel(list, "deleted")).toEqual({ kind: "none" });
  });

  it("the Files panel says 'empty' only once the tree has loaded and has nothing", () => {
    const tree = { name: "", path: "", type: "directory", children: [] } as never;
    expect(fileTreeView(undefined, true)).toBe("loading");
    expect(fileTreeView(undefined, false)).toBe("empty");
    expect(fileTreeView(tree, false)).toBe("tree");
    expect(fileTreeView(tree, true)).toBe("tree");     // a refetch keeps showing what it has
  });
});

describe("workspace: the active workspace survives a refresh", () => {
  const list = [{ id: "a" }, { id: "b" }, { id: "c" }];

  it("reopens the workspace the user last worked in", () => {
    expect(pickInitialWorkspace(list, "b")).toBe("b");
  });

  it("falls back to the first when nothing is remembered, or the remembered one no longer exists", () => {
    expect(pickInitialWorkspace(list, null)).toBe("a");
    expect(pickInitialWorkspace(list, "deleted-long-ago")).toBe("a");
  });

  it("is none for an empty list", () => {
    expect(pickInitialWorkspace([], "b")).toBeNull();
  });

  it("does not depend on the order the list comes back in", () => {
    expect(pickInitialWorkspace([...list].reverse(), "b")).toBe("b");
  });

  describe("remembering", () => {
    beforeEach(() => window.localStorage.clear());

    it("round-trips per user", () => {
      rememberWorkspace("u1", "b");
      rememberWorkspace("u2", "c");
      expect(readRememberedWorkspace("u1")).toBe("b");
      expect(readRememberedWorkspace("u2")).toBe("c");
    });

    it("keeps one person's choice away from another on a shared browser", () => {
      rememberWorkspace("u1", "b");
      expect(readRememberedWorkspace("u3")).toBeNull();
    });

    it("forgets when cleared, and ignores a missing user", () => {
      rememberWorkspace("u1", "b");
      rememberWorkspace("u1", null);
      expect(readRememberedWorkspace("u1")).toBeNull();
      rememberWorkspace(undefined, "b");
      expect(readRememberedWorkspace(undefined)).toBeNull();
      expect(activeWorkspaceStorageKey(undefined)).toBeNull();
    });

    it("never throws when storage is unavailable", () => {
      const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
      const spy2 = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
      expect(readRememberedWorkspace("u1")).toBeNull();
      expect(() => rememberWorkspace("u1", "b")).not.toThrow();
      spy.mockRestore();
      spy2.mockRestore();
    });
  });
});
