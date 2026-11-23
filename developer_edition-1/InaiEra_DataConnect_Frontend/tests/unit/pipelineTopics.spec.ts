/*
 * GAP-12: how the UI names the topic of a source (generated) versus a sink (it only reads existing topics).
 */
import { test, expect } from "@playwright/test";
import { inputTopics, isSinkPipeline, searchableTopics, topicLabel, topicSummary } from "../../src/pipelineTopics";

const source = { direction: "SOURCE", topic_name: "org_1-raw-sales-pg-orders-batch-v1", connector_config: { tables: ["orders"] } };
const sink = { direction: "SINK", topic_name: null, connector_config: { topics: "org_1-raw-sales-pg-orders-batch-v1" } };
const multiSink = { direction: "SINK", topic_name: null, connector_config: { topics: "org_1-raw-a-pg-t-batch-v1, org_1-raw-b-pg-t-batch-v1 ,org_1-raw-c-pg-t-batch-v1" } };

test("a source shows its generated topic", () => {
  expect(topicSummary(source)).toBe("org_1-raw-sales-pg-orders-batch-v1");
  expect(topicLabel(source)).toBe("Topic");
  expect(isSinkPipeline(source)).toBe(false);
});

test("a sink shows what it reads, marked as an input rather than an output", () => {
  expect(topicSummary(sink)).toBe("← org_1-raw-sales-pg-orders-batch-v1");
  expect(topicLabel(sink)).toBe("Input topic");
});

test("a sink with several inputs summarizes them", () => {
  expect(inputTopics(multiSink)).toEqual(["org_1-raw-a-pg-t-batch-v1", "org_1-raw-b-pg-t-batch-v1", "org_1-raw-c-pg-t-batch-v1"]);
  expect(topicSummary(multiSink)).toBe("← org_1-raw-a-pg-t-batch-v1 (+2 more)");
  expect(topicLabel(multiSink)).toBe("Input topics");
});

test("a list-valued topics setting is read too", () => {
  expect(inputTopics({ direction: "SINK", connector_config: { topics: ["org_1-raw-a-pg-t-batch-v1", "org_1-raw-b-pg-t-batch-v1"] } })).toEqual(["org_1-raw-a-pg-t-batch-v1", "org_1-raw-b-pg-t-batch-v1"]);
});

test("a sink with no input configured, or a source with no topic, shows nothing rather than 'null'", () => {
  expect(topicSummary({ direction: "SINK", topic_name: null, connector_config: {} })).toBe("");
  expect(topicSummary({ direction: "SOURCE", topic_name: null })).toBe("");
});

test("search matches a sink by the topic it reads", () => {
  expect(searchableTopics(multiSink)).toContain("org_1-raw-b-pg-t-batch-v1");
  expect(searchableTopics(source)).toContain("orders-batch");
});
