import { describe, expect, it } from "vitest";
import { dirtyFileCount, gitFeatureApi, repoHostLabel, repoShortName } from "../../src/features/git";
import type { GitStatus } from "../../src/types";
import { callEndpoint } from "../helpers/apiHarness";

describe("git: status helpers", () => {
  it("counts modified AND untracked files", () => {
    const status = { modified_files: ["a"], untracked_files: ["b", "c"] } as GitStatus;
    expect(dirtyFileCount(status)).toBe(3);
    expect(dirtyFileCount(undefined)).toBe(0);
  });

  it("shortens repo URLs", () => {
    expect(repoShortName("https://github.com/acme/dbt.git")).toBe("acme/dbt.git");
    expect(repoHostLabel("https://github.com/acme/dbt")).toBe("github.com/acme");
    expect(repoHostLabel(undefined)).toBeUndefined();
  });
});

describe("git: endpoints", () => {
  it("commitAndPush POSTs the commit message", async () => {
    const { request } = await callEndpoint(gitFeatureApi.endpoints.commitAndPush, { workspaceId: "w1", body: { message: "m" } }, {});
    expect(request).toEqual({ method: "POST", url: "/api/v1/git/workspaces/w1/commit", body: { message: "m" } });
  });

  it("switchBranch PATCHes the branch", async () => {
    const { request } = await callEndpoint(gitFeatureApi.endpoints.switchBranch, { workspaceId: "w1", branch: "dev" }, { branch: "dev" });
    expect(request).toEqual({ method: "PATCH", url: "/api/v1/git/workspaces/w1/branch", body: { branch: "dev" } });
  });
});
