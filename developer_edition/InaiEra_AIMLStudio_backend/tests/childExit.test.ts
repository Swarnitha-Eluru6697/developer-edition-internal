import { describe, expect, it } from "vitest";
import { describeChildExit, summarizeStderr } from "../src/services/childExit";

const TRACEBACK = [
  "Traceback (most recent call last):",
  '  File "/app/src/training/dataset_profiler.py", line 266, in <module>',
  "    main()",
  '  File "/app/src/training/dataset_profiler.py", line 232, in main',
  "    entry.update(profile_column(df[col], row_count))",
  "TypeError: unhashable type: 'list'",
].join("\n");

describe("child exit messages", () => {
  it("a Python traceback is summarised by the exception it ended in, not its preamble", () => {
    const msg = describeChildExit("dataset_profiler.py", 1, null, TRACEBACK, 300);
    expect(msg).toContain("TypeError: unhashable type: 'list'");
    expect(msg).not.toContain("Traceback (most recent call last)");
    expect(msg).toContain('line 232, in main');
  });

  it("the cause survives a tight limit even when the frame doesn't fit", () => {
    expect(summarizeStderr(TRACEBACK, 40)).toBe("TypeError: unhashable type: 'list'");
  });

  it("plain stderr is passed through, capped", () => {
    expect(describeChildExit("x.py", 1, null, "boom", 300)).toBe("x.py exited 1: boom");
    expect(summarizeStderr("a".repeat(500), 10)).toBe("a".repeat(10));
  });
});
