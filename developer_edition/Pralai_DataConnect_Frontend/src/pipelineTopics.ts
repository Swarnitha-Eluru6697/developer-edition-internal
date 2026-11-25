/*
 * GAP-12: which topic a pipeline's data is on, as the UI shows it.
 *
 * A SOURCE pipeline produces to a topic DataConnect generates for it (`topic_name`). A SINK produces nothing to Kafka —
 * it reads topics that already exist, named in `connector_config.topics` — so it has no `topic_name` (null) and the UI
 * shows what it reads instead, labelled as such, never as if it were an output.
 */

interface TopicBearing {
  direction?: string | null;
  topic_name?: string | null;
  connector_config?: Record<string, unknown> | null;
}

export function isSinkPipeline(p: TopicBearing): boolean {
  return (p.direction || "").toUpperCase() === "SINK";
}

/** The topics a sink reads, in order (empty for a source, or a sink with none configured). */
export function inputTopics(p: TopicBearing): string[] {
  const raw = p.connector_config?.topics;
  const parts = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(",") : [];
  return parts.map((t) => String(t).trim()).filter(Boolean);
}

/** One line for a table cell: a source's generated topic, or "← first-input (+N more)" for a sink. */
export function topicSummary(p: TopicBearing): string {
  if (!isSinkPipeline(p)) return p.topic_name || "";
  const topics = inputTopics(p);
  if (!topics.length) return "";
  return `← ${topics[0]}${topics.length > 1 ? ` (+${topics.length - 1} more)` : ""}`;
}

/** The label for the topic row in a detail view. */
export function topicLabel(p: TopicBearing): string {
  return isSinkPipeline(p) ? (inputTopics(p).length > 1 ? "Input topics" : "Input topic") : "Topic";
}

/** The text to search against: everything a user might type to find this pipeline's topic. */
export function searchableTopics(p: TopicBearing): string {
  return [p.topic_name || "", ...inputTopics(p)].join(" ").toLowerCase();
}
