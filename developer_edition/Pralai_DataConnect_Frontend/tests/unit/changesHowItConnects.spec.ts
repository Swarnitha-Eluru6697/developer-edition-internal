/*
 * Saving a connection edit tries the connection first, but only when the edit changes how it connects. Renaming one must not cost a
 * round trip to the source (or a spurious failure); a changed host, port, database, user, tables, secret or secret reference must.
 */
import { test, expect } from "@playwright/test";
import { changesHowItConnects } from "../../src/connectionConfig";
import type { Connection } from "../../src/types/api";

const saved = {
  id: "c1", name: "db", source_type: "postgresql", secret_ref: "secret://x",
  connection_config: { hostname: "postgres", port: 5432, database: "d", username: "u", password: "********", tables: ["customers"] },
} as unknown as Connection;
const same = { hostname: "postgres", port: 5432, database: "d", username: "u", tables: ["customers"] };     // what the form builds when nothing was touched

test.describe("changesHowItConnects", () => {
  test("nothing changed (a rename only) is not a connecting change", () => {
    expect(changesHowItConnects(saved, same, {}, "secret://x")).toBe(false);
  });
  test("a re-entered secret is", () => {
    expect(changesHowItConnects(saved, same, { password: true }, "secret://x")).toBe(true);
  });
  test("so is a different host, port, database, user or tables list", () => {
    for (const change of [{ hostname: "other" }, { port: 5433 }, { database: "e" }, { username: "v" }, { tables: ["a", "b"] }]) {
      expect(changesHowItConnects(saved, { ...same, ...change }, {}, "secret://x"), JSON.stringify(change)).toBe(true);
    }
  });
  test("so is a different secret reference", () => {
    expect(changesHowItConnects(saved, same, {}, "secret://y")).toBe(true);
  });
  test("so is clearing a saved setting", () => {
    const { tables: _tables, ...without } = same;
    expect(changesHowItConnects(saved, without, {}, "secret://x")).toBe(true);
  });
  test("a saved secret that the form leaves out (it was not touched) is not counted as cleared", () => {
    expect(changesHowItConnects(saved, same, {}, "secret://x")).toBe(false);          // `password` is in the saved config but not in the form's
  });
});
