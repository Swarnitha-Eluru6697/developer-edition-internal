"""Permission IDs the tests hand out (Workspace's permissions.json). Tokens carry these, never role names."""
VIEWER = ["workspace:catalog:view", "workspace:datasets:view"]
EDITOR = VIEWER + ["workspace:flink:edit"]
ENGINEER = EDITOR + ["workspace:flink:deploy", "workspace:dq:publish"]
DQ_PUBLISHER = VIEWER + ["workspace:dq:publish"]
