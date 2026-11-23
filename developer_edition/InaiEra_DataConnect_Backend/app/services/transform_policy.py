from typing import Any

SMT_TYPES: dict[str, str] = {
    "ValueToKey": "org.apache.kafka.connect.transforms.ValueToKey",
    "ExtractField": "org.apache.kafka.connect.transforms.ExtractField",
    "HoistField": "org.apache.kafka.connect.transforms.HoistField",
    "Flatten": "org.apache.kafka.connect.transforms.Flatten",
    "RegexRouter": "org.apache.kafka.connect.transforms.RegexRouter",
    "HeaderFrom": "org.apache.kafka.connect.transforms.HeaderFrom",
    "MaskField": "org.apache.kafka.connect.transforms.MaskField",
    "ReplaceField": "org.apache.kafka.connect.transforms.ReplaceField",
    "InsertField": "org.apache.kafka.connect.transforms.InsertField",
    "TimestampConverter": "org.apache.kafka.connect.transforms.TimestampConverter",
    # Debezium CDC: unwrap the {op,before,after,source} envelope to a flat row so that
    # field transforms (MaskField/ReplaceField/…) act on the actual columns.
    "ExtractNewRecordState": "io.debezium.transforms.ExtractNewRecordState",
}

# Kafka Connect ships these as an abstract class plus a `$Key` and a `$Value` variant; only the variants can be deployed.
SCOPED_TRANSFORMS = {
    "ExtractField",
    "HoistField",
    "Flatten",
    "HeaderFrom",
    "MaskField",
    "ReplaceField",
    "InsertField",
    "TimestampConverter",
}

NON_SMT_TRANSFORMATIONS = {
    "join",
    "aggregation",
    "stateful_deduplication",
    "complex_enrichment",
    "ml_cleansing",
    "fuzzy_matching",
    "cross_record_validation",
    "heavy_nested_restructuring",
}


# Settings Kafka Connect needs before the transform can even start. Missing one is not caught by Connect when the connector is
# created: the task starts, fails in configure(), and the pipeline goes straight to FAILED ("TimestampConverter requires format
# option to be specified when using string timestamps"). Checked here instead, so it is a clear 422 up front.
REQUIRED_CONFIG: dict[str, tuple[str, ...]] = {
    "ValueToKey": ("fields",),
    "ExtractField": ("field",),
    "HoistField": ("field",),
    "MaskField": ("fields",),
    "RegexRouter": ("regex", "replacement"),
    "HeaderFrom": ("fields", "headers", "operation"),
    # No field = convert the whole value, which for a source row (a struct) fails every record instead of the task.
    "TimestampConverter": ("field", "target.type"),
}
TIMESTAMP_TARGET_TYPES = ("string", "unix", "Date", "Time", "Timestamp")


def transform_config_problems(transform_type: str, config: dict[str, Any]) -> list[str]:
    """What is missing or invalid in one transform's settings, as messages naming the transform; [] when it can start."""
    def present(key: str) -> bool:
        return str(config.get(key) or "").strip() != ""

    problems = [f"{transform_type}: '{key}' is required." for key in REQUIRED_CONFIG.get(transform_type, ()) if not present(key)]
    if transform_type == "TimestampConverter" and present("target.type"):
        target = str(config["target.type"]).strip()
        if target not in TIMESTAMP_TARGET_TYPES:
            problems.append(f"TimestampConverter: target type '{target}' must be one of {', '.join(TIMESTAMP_TARGET_TYPES)}.")
        elif target == "string" and not present("format"):
            problems.append("TimestampConverter: 'format' is required when the target type is string (for example yyyy-MM-dd HH:mm:ss).")
    return problems


# A source writes to the topic its pipeline is named for (the topic naming contract: monitoring, lineage, quality and every sink find the data
# there), so the platform routes it there itself. A RegexRouter of the user's would either be overridden by that routing (no effect) or, if its
# result no longer matched, send the records somewhere nothing looks. So a source refuses it; a sink, which reads a topic, can use one.
SOURCE_REFUSED_TRANSFORMS: dict[str, str] = {
    "RegexRouter": (
        "RegexRouter can't be used on a source connector: a source always writes to the topic its pipeline is named for, and the platform routes it there "
        "itself. Use it on a sink connector, or remove it."
    ),
}


def source_transform_problems(transforms: list[dict[str, Any]]) -> list[str]:
    """What a SOURCE pipeline cannot use in this policy, as messages; [] when it is fine."""
    seen: list[str] = []
    for step in transforms:
        kind = step.get("type", "")
        if kind in SOURCE_REFUSED_TRANSFORMS and kind not in seen:
            seen.append(kind)
    return [f"{step_name}: {SOURCE_REFUSED_TRANSFORMS[step_name]}" for step_name in seen]


def transform_type_of_class(class_name: str) -> str | None:
    """'org.apache.kafka.connect.transforms.TimestampConverter$Value' -> 'TimestampConverter'; None for a class we don't offer."""
    base = class_name.split("$", 1)[0]
    return next((t for t, c in SMT_TYPES.items() if c == base), None)


def validate_transform(transform: dict[str, Any]) -> None:
    transform_type = transform.get("type")
    if transform_type in NON_SMT_TRANSFORMATIONS:
        raise ValueError(f"{transform_type} must be implemented outside Kafka Connect SMTs")
    if transform_type not in SMT_TYPES:
        raise ValueError(f"Unsupported SMT type: {transform_type}")
    name = transform.get("name")
    if not name or not str(name).replace("_", "").isalnum():
        raise ValueError("Transform name must be alphanumeric or underscore")
    scope = transform.get("scope", "value")
    if scope not in {"key", "value", "header"}:
        raise ValueError("Transform scope must be key, value, or header")


def validate_transform_settings(transforms: list[dict[str, Any]]) -> None:
    """Raises ValueError naming every missing/invalid setting. Run where a policy is saved and at deploy — deliberately not inside
    generate_smt_config, which also runs on every reconciler tick: a policy saved before this check existed must still let its
    pipeline be paused or deleted."""
    problems = [p for t in transforms for p in transform_config_problems(t.get("type", ""), t.get("config") or {})]
    if problems:
        raise ValueError(" ".join(problems))


def generate_smt_config(transforms: list[dict[str, Any]]) -> dict[str, str]:
    ordered = sorted(transforms, key=lambda item: item.get("order", 0))
    names: list[str] = []
    config: dict[str, str] = {}
    for transform in ordered:
        validate_transform(transform)
        name = transform["name"]
        transform_type = transform["type"]
        scope = transform.get("scope", "value")
        class_name = SMT_TYPES[transform_type]
        if transform_type in SCOPED_TRANSFORMS:
            class_name = f"{class_name}${scope.title()}"

        names.append(name)
        config[f"transforms.{name}.type"] = class_name
        for key, value in transform.get("config", {}).items():
            config[f"transforms.{name}.{key}"] = str(value)

    if names:
        config["transforms"] = ",".join(names)
    return config

