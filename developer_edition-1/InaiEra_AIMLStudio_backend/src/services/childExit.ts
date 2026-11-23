/**
 * How a training/profiling subprocess ended, in words that name the real cause.
 *
 * Node reports `code === null` when a child is terminated by a SIGNAL rather
 * than exiting on its own. Interpolated straight into a message that produced
 * "classical_trainer.py exited null:" — with an empty stderr, because a killed
 * process never gets to write one. That is the least useful thing a failed run
 * could say, and it is the common case: SIGKILL here is almost always the
 * kernel's OOM killer reclaiming a process that tried to allocate more than the
 * machine had.
 */
export function describeChildExit(
  script: string,
  code: number | null,
  signal: NodeJS.Signals | null,
  stderr: string,
  stderrLimit = 500
): string {
  const tail = summarizeStderr(stderr, stderrLimit);
  if (code === null) {
    const hint =
      signal === "SIGKILL"
        ? " — killed by the operating system. With no stderr this is almost always the OOM killer: the step asked for more memory than this machine has."
        : signal === "SIGTERM"
        ? " — terminated, usually a cancellation or a timeout."
        : "";
    return `${script} was killed by ${signal ?? "an unknown signal"}${hint}${tail ? ` Last output: ${tail}` : ""}`;
  }
  return `${script} exited ${code}${tail ? `: ${tail}` : " with no error output"}`;
}

/** The part of a child's stderr that names the cause. A Python traceback ends in
 *  the exception ("TypeError: unhashable type: 'list'"); taking the first N chars
 *  kept the "Traceback (most recent call last): File …" preamble and cut that
 *  line off, so a failed upload showed a stack frame and never the reason. */
export function summarizeStderr(stderr: string, limit: number): string {
  const text = stderr.trim();
  if (!text.includes("Traceback (most recent call last)")) return text.slice(0, limit);
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const cause = lines[lines.length - 1] ?? "";
  // The innermost frame says where; keep it when there's room.
  const frame = [...lines].reverse().find((l) => l.startsWith("File ")) ?? "";
  const out = frame && cause.length + frame.length + 4 <= limit ? `${cause} (at ${frame})` : cause;
  return out.slice(0, limit);
}
