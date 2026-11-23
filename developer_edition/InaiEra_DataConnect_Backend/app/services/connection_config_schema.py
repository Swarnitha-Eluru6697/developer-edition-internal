"""
Typed field schemas for ConnectionProfile.connection_config, per source_type.

connection_config is free-form JSON because different source types need
genuinely different fields, but that made three things hard to do
correctly: knowing which fields a given source type actually needs (for
validation and for rendering the right form in the UI), knowing which of
those fields are secrets (for redaction — previously a single hardcoded,
source-type-agnostic key list in app/services/secrets.py), and knowing
which are safe to persist verbatim.

This is the one place that answers all of those questions, from one
definition per field — exactly the metadata shape requested:
`{"key": "password", "type": "secret", "persist": False, "required": True}`
(see ConfigFieldSchema in app/schemas/connection.py). `secrets.py`'s
redaction now asks THIS module which keys are sensitive for a given
source_type, instead of relying solely on its own hardcoded set (kept as a
fallback for a source_type with no schema entry here, or a field this
schema doesn't yet know about).

Every field listed here is audited against what actually reads
connection_config: app/services/connect_config.py (the real Kafka Connect
config builder), app/services/connection_adapters.py, and
app/services/connection_discovery.py. Unknown keys a caller sends that
aren't declared here are still allowed through unvalidated — this documents
and enforces the KNOWN fields a source type reads, it doesn't lock out
connector-specific passthrough options.
"""

from typing import Any

STRING = "string"
NUMBER = "number"
BOOLEAN = "boolean"
SECRET = "secret"
ENUM = "enum"
LIST = "list"


def _field(
    key: str,
    label: str,
    type_: str,
    *,
    required: bool = False,
    persist: bool = True,
    default: Any = None,
    placeholder: str | None = None,
    options: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key, "label": label, "type": type_, "required": required, "persist": persist,
        "default": default, "placeholder": placeholder, "options": options, "description": description,
    }


def _database_fields(default_port: int) -> list[dict[str, Any]]:
    return [
        _field("hostname", "Host", STRING, required=True, placeholder="db.internal"),
        _field("port", "Port", NUMBER, default=default_port),
        _field("database", "Database", STRING, required=True),
        _field("username", "Username", STRING, required=True),
        _field("password", "Password", SECRET, required=True, persist=False),
        _field("tables", "Tables", LIST, description="Table names, or '*' for all"),
    ]


_DB_DEFAULT_PORTS = {
    "postgres": 5432, "postgresql": 5432, "mysql": 3306,
    "sqlserver": 1433, "oracle": 1521, "jdbc": 5432, "mongodb": 27017,
}

CONNECTION_CONFIG_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    source_type: _database_fields(port) for source_type, port in _DB_DEFAULT_PORTS.items()
}

CONNECTION_CONFIG_SCHEMAS["elasticsearch"] = [
    _field("hostname", "Host", STRING, required=True),
    _field("port", "Port", NUMBER, default=9200),
    _field("scheme", "Scheme", ENUM, default="http", options=["http", "https"]),
    _field("base_url", "Base URL (overrides host/port/scheme)", STRING),
    _field("username", "Username", STRING),
    _field("password", "Password", SECRET, persist=False),
]

for _api_type in ("api", "rest", "http"):
    CONNECTION_CONFIG_SCHEMAS[_api_type] = [
        _field("base_url", "Base URL", STRING, required=True, placeholder="https://api.example.com"),
        _field("health_path", "Health check path", STRING, placeholder="/health"),
        _field("endpoints", "Endpoints", LIST, description="Known endpoint names"),
    ]

CONNECTION_CONFIG_SCHEMAS["webhook"] = [
    _field("base_url", "Base URL", STRING, required=True, placeholder="https://hooks.example.com"),
    _field("health_path", "Health check path", STRING, placeholder="/health"),
    _field("endpoints", "Endpoints", LIST, description="Known endpoint names"),
    _field("auth_type", "Authentication", ENUM, default="none", options=["none", "bearer", "basic", "hmac"]),
    _field("username", "Username (basic auth)", STRING),
    _field("auth_token", "Token, password or HMAC secret", SECRET, persist=False),
]

CONNECTION_CONFIG_SCHEMAS["s3"] = [
    _field("bucket", "Bucket", STRING, required=True),
    _field("region", "Region", STRING, default="us-east-1"),
    _field("endpoint", "Endpoint URL (MinIO / S3-compatible)", STRING, placeholder="http://minio:9000",
           description="Leave empty for Amazon S3."),
    _field("access_key_id", "Access key ID", SECRET, persist=False),
    _field("secret_access_key", "Secret access key", SECRET, persist=False),
    _field("prefix", "Object key prefix", STRING),
]

CONNECTION_CONFIG_SCHEMAS["gcs"] = [
    _field("bucket", "Bucket", STRING, required=True),
    _field("project_id", "Project ID", STRING),
    _field("prefix", "Object key prefix", STRING),
    _field("credentials_json", "Service account credentials (JSON)", SECRET, persist=False,
           description="Omit to use the connector worker's default application credentials."),
]

CONNECTION_CONFIG_SCHEMAS["azure_blob"] = [
    _field("account", "Storage account", STRING, required=True),
    _field("container", "Container", STRING),
    _field("account_key", "Account key", SECRET, persist=False, description="Or give a SAS token instead."),
    _field("sas_token", "SAS token", SECRET, persist=False),
    _field("endpoint", "Endpoint URL (Azurite / private endpoint)", STRING, placeholder="http://azurite:10000"),
]

CONNECTION_CONFIG_SCHEMAS["adls_gen2"] = [
    _field("account_name", "Storage account name", STRING, required=True),
    _field("filesystem", "Filesystem (container) name", STRING, required=True),
    _field("client_id", "AAD application (client) ID", STRING, required=True),
    _field("client_secret", "AAD application client secret", SECRET, required=True, persist=False),
    _field("tenant_id", "AAD tenant ID", STRING,
           description="Used to derive the OAuth token endpoint; omit only for a worker-managed identity."),
]

CONNECTION_CONFIG_SCHEMAS["iceberg"] = [
    _field("warehouse", "Warehouse location", STRING, required=True, placeholder="s3://bucket/warehouse"),
    _field("catalog_name", "Catalog name", STRING, default="default"),
    _field("catalog_type", "Catalog type", ENUM, default="hadoop", options=["hadoop", "hive", "rest", "glue"]),
    _field("catalog_uri", "Catalog URI (Hive/REST/Nessie)", STRING),
    _field("namespace", "Table namespace", STRING, default="default"),
]

for _ftp_type, _ftp_port in (("ftp", 21), ("sftp", 22)):
    CONNECTION_CONFIG_SCHEMAS[_ftp_type] = [
        _field("hostname", "Host", STRING, required=True),
        _field("port", "Port", NUMBER, default=_ftp_port),
        _field("username", "Username", STRING),
        _field("password", "Password", SECRET, persist=False),
        _field("path", "Remote path", STRING, default="."),
    ]

CONNECTION_CONFIG_SCHEMAS["sftp"].insert(4, _field(
    "private_key", "Private key (instead of password)", SECRET, persist=False,
    description="PEM private key; leave empty to log in with the password."))
CONNECTION_CONFIG_SCHEMAS["sftp"].append(_field(
    "host_key", "Host key fingerprint", STRING, description="Leave empty to skip host key checking."))

CONNECTION_CONFIG_SCHEMAS["file"] = [
    _field("path", "Path", STRING, required=True),
    _field("objects", "Objects", LIST, description="Known file names"),
]


# ── Connection types for the rest of the template catalog (2026-11-05) ──────────────────────────────────────────────────────────────
# One type per backing system, so a template is no longer pointed at a generic `jdbc` connection. The field keys are the ones the config
# builders read (app/services/connect_config_t1.py: host(cfg) -> "hostname", port_of(cfg) -> "port", cfg["username"] / cfg["password"],
# cfg["url"], cfg["api_key"], ...). Types whose templates have no builder yet carry the fields the connector itself requires.

def _url(label: str, placeholder: str, *, required: bool = True, key: str = "url") -> dict[str, Any]:
    return _field(key, label, STRING, required=required, placeholder=placeholder)


def _host(placeholder: str = "host.internal") -> dict[str, Any]:
    return _field("hostname", "Host", STRING, required=True, placeholder=placeholder)


def _port(default: int) -> dict[str, Any]:
    return _field("port", "Port", NUMBER, default=default)


def _user(required: bool = False) -> dict[str, Any]:
    return _field("username", "Username", STRING, required=required)


def _password(required: bool = False, label: str = "Password") -> dict[str, Any]:
    return _field("password", label, SECRET, required=required, persist=False)


def _secret(key: str, label: str, *, required: bool = False, description: str | None = None) -> dict[str, Any]:
    return _field(key, label, SECRET, required=required, persist=False, description=description)


def _sql_like(default_port: int, *, database_required: bool = True) -> list[dict[str, Any]]:
    return [_host(), _port(default_port), _field("database", "Database", STRING, required=database_required),
            _user(required=True), _password(required=True)]


NEW_SOURCE_TYPES: dict[str, list[dict[str, Any]]] = {
    # ── templates that have a config builder ──
    "activemq": [
        _host("activemq"), _port(61616),
        _url("Broker URL (overrides host/port)", "tcp://activemq:61616", required=False),
        _field("destination_type", "Destination type", ENUM, default="queue", options=["queue", "topic"]),
        _field("queue", "Destination name", STRING, description="Queue or topic name; a pipeline can override it."),
        _user(), _password(),
    ],
    "cassandra": [
        _host("cassandra"), _port(9042),
        _field("datacenter", "Local datacenter", STRING, default="datacenter1"),
        _field("keyspace", "Keyspace", STRING, required=True),
        _field("replication_factor", "Replication factor (new keyspace)", NUMBER, default=1),
        _user(), _password(),
    ],
    "scylladb": [
        _host("scylla"), _port(9042), _field("keyspace", "Keyspace", STRING, required=True), _user(), _password(),
    ],
    "hdfs": [_url("Namenode URL", "hdfs://namenode:9000")],
    "hbase": [
        _host("zookeeper"), _port(2181),
        _field("znode_parent", "ZooKeeper znode parent", STRING, default="/hbase"),
    ],
    "splunk": [
        _url("Splunk URL (HEC or management)", "https://splunk:8088"),
        _field("scheme", "Scheme", ENUM, default="https", options=["http", "https"]),
        _secret("token", "HEC token", description="HTTP Event Collector token, for the Splunk Sink."),
        _user(), _password(),
        _field("validate_certs", "Validate TLS certificates", BOOLEAN, default=True),
    ],
    "redis": [
        _host("redis"), _port(6379), _field("database", "Database number", NUMBER, default=0),
        _password(label="Password (leave empty for no auth)"),
    ],
    "aerospike": [_host("aerospike"), _port(3000), _field("namespace", "Namespace", STRING, required=True, default="test")],
    "clickhouse": _sql_like(8123),
    "singlestore": _sql_like(3306),
    "vertica": _sql_like(5433),
    "heavyai": _sql_like(6274),
    "yugabytedb": _sql_like(5433),
    "questdb": [_host("questdb"), _port(9009)],
    "influxdb": [
        _url("InfluxDB URL", "http://influxdb:8086"), _field("database", "Database", STRING, required=True),
        _user(), _password(),
    ],
    "neo4j": [
        _url("Bolt URL", "bolt://neo4j:7687"), _field("database", "Database", STRING, default="neo4j"),
        _user(required=True), _password(required=True),
    ],
    "couchbase": [_host("couchbase"), _field("bucket", "Bucket", STRING, required=True), _user(required=True), _password(required=True)],
    "milvus": [
        _url("Milvus URL", "http://milvus:19530"), _field("database", "Database", STRING, default="default"),
        _secret("token", "Token", description="`user:password` or an API key; leave empty for root:Milvus."),
    ],
    "qdrant": [_url("Qdrant URL", "http://qdrant:6334"), _secret("api_key", "API key")],
    "weaviate": [
        _url("Weaviate URL", "http://weaviate:8080"),
        _field("grpc_url", "gRPC address", STRING, placeholder="weaviate:50051"),
        _field("grpc_port", "gRPC port", NUMBER, default=50051),
        _secret("api_key", "API key"),
    ],
    "timeplus": [
        _url("Timeplus URL", "http://timeplus:3218"), _field("workspace", "Workspace", STRING, default="default"),
        _secret("api_key", "API key", required=True),
    ],
    "littlehorse": [_host("littlehorse"), _port(2023)],
    "cosmosdb": [
        _url("Account endpoint", "https://myaccount.documents.azure.com:443/"),
        _field("database", "Database", STRING, required=True),
        _secret("master_key", "Primary key", required=True),
    ],
    "firebase": [
        _url("Realtime Database URL", "https://my-project.firebaseio.com"),
        _field("credentials_path", "Service account file on the Connect worker", STRING, placeholder="/secrets/firebase.json"),
    ],
    "gridgain": [_host("gridgain"), _port(10800), _user(), _password()],
    "gemfire": [_host("locator"), _port(10334), _user(), _password()],
    "diffusion": [
        _host("diffusion"), _port(8080), _url("Diffusion URL (overrides host/port)", "ws://diffusion:8080", required=False),
        _user(required=True), _password(),
    ],
    "lightstreamer": [_host("lightstreamer"), _port(6661), _user(), _password()],
    "aws_eventbridge": [
        _field("region", "Region", STRING, required=True, default="us-east-1"),
        _field("event_bus", "Event bus name", STRING, default="default"),
        _url("Endpoint URL (LocalStack)", "http://localstack:4566", required=False, key="endpoint_url"),
    ],
    "prometheus": [_url("Prometheus URL", "http://prometheus:9090")],
    "data_diode": [
        _host("diode-receiver"), _port(3456), _secret("password", "Encryption password", required=True),
        _secret("salt", "Encryption salt", required=True),
    ],
    "none": [],
    # ── templates without a config builder yet: the connector's own required settings ──
    "rabbitmq": [
        _host("rabbitmq"), _port(5672), _field("virtual_host", "Virtual host", STRING, default="/"),
        _field("exchange", "Exchange", STRING, required=True), _field("queue", "Queue", STRING, required=True),
        _user(required=True), _password(required=True),
    ],
    "snowflake": [
        _url("Account URL", "https://myaccount.snowflakecomputing.com"), _user(required=True),
        _secret("private_key", "Private key (key-pair auth)", required=True),
        _secret("private_key_passphrase", "Private key passphrase"),
        _field("role", "Role", STRING), _field("warehouse", "Warehouse", STRING),
        _field("database", "Database", STRING, required=True), _field("schema", "Schema", STRING, default="PUBLIC"),
    ],
    "ably": [_secret("api_key", "API key", required=True), _field("channel", "Channel", STRING, required=True)],
    "azure_data_explorer": [
        _url("Ingestion URL", "https://ingest-mycluster.region.kusto.windows.net", key="ingestion_url"),
        _url("Query URL", "https://mycluster.region.kusto.windows.net", key="query_url"),
        _field("database", "Database", STRING, required=True),
        _field("client_id", "AAD application (client) ID", STRING, required=True),
        _secret("client_secret", "AAD application client secret", required=True),
        _field("tenant_id", "AAD tenant ID", STRING, required=True),
    ],
    "datadog": [
        _field("site", "Site", ENUM, default="datadoghq.com",
               options=["datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu", "ap1.datadoghq.com"]),
        _secret("api_key", "API key", required=True),
    ],
    "bigquery": [
        _field("project_id", "Project ID", STRING, required=True), _field("dataset", "Dataset", STRING, required=True),
        _secret("credentials_json", "Service account credentials (JSON)", required=True),
    ],
    "gcp_functions": [
        _field("project_id", "Project ID", STRING, required=True), _field("region", "Region", STRING, required=True),
        _field("function_name", "Function name", STRING, required=True),
        _secret("credentials_json", "Service account credentials (JSON)", required=True),
    ],
    "newrelic": [
        _field("region", "Region", ENUM, default="US", options=["US", "EU"]),
        _field("account_id", "Account ID", STRING, required=True), _secret("api_key", "Insert API key", required=True),
    ],
    "pagerduty": [_secret("routing_key", "Integration (routing) key", required=True)],
    "salesforce": [
        _url("Instance URL", "https://mycompany.my.salesforce.com"), _user(required=True), _password(required=True),
        _secret("security_token", "Security token"), _field("consumer_key", "Connected app consumer key", STRING, required=True),
        _secret("consumer_secret", "Connected app consumer secret", required=True),
    ],
    "vectara": [
        _url("API URL", "https://api.vectara.io", required=False), _field("customer_id", "Customer ID", STRING, required=True),
        _field("corpus_id", "Corpus ID", STRING, required=True), _secret("api_key", "API key", required=True),
    ],
    "sap": [
        _url("Service URL (OData / ODP)", "https://sap.internal:44300/sap/opu/odata/sap/"), _field("client", "SAP client", STRING),
        _user(required=True), _password(required=True),
    ],
    "teradata": _sql_like(1025),
    "ibm_mq": [
        _host("mq.internal"), _port(1414), _field("queue_manager", "Queue manager", STRING, required=True),
        _field("channel", "Channel", STRING, required=True), _field("queue", "Queue", STRING, required=True),
        _user(), _password(),
    ],
    "tibco_ems": [
        _url("Server URL", "tcp://ems.internal:7222"), _field("destination", "Destination", STRING, required=True),
        _user(), _password(),
    ],
    "weblogic_jms": [
        _url("Provider URL", "t3://weblogic.internal:7001"), _field("connection_factory", "Connection factory JNDI name", STRING, required=True),
        _field("destination", "Destination JNDI name", STRING, required=True), _user(), _password(),
    ],
    "as400": [
        _host("ibmi.internal"), _user(required=True), _password(required=True),
        _field("library", "Library", STRING), _field("object_name", "Data queue or program", STRING),
    ],
    "netezza": _sql_like(5480),
    "mapr_db": [_host("cldb.internal"), _port(7222), _user(), _password()],
    "dataproc": [
        _field("project_id", "Project ID", STRING, required=True), _field("region", "Region", STRING, required=True),
        _field("cluster_name", "Cluster name", STRING, required=True),
        _secret("credentials_json", "Service account credentials (JSON)", required=True),
    ],
    "adobe_aep": [
        _url("Streaming endpoint URL", "https://dcs.adobedc.net/collection/..."), _field("ims_org_id", "IMS org ID", STRING, required=True),
        _field("client_id", "Client ID", STRING, required=True), _secret("client_secret", "Client secret", required=True),
        _field("sandbox", "Sandbox", STRING, default="prod"),
    ],
    "appdynamics": [_url("Machine agent / controller URL", "http://appd-agent:8293"), _field("account", "Account", STRING),
                    _secret("api_key", "API key")],
    "celonis_ems": [_url("EMS URL", "https://team.realm.celonis.cloud"), _field("data_pool_id", "Data pool ID", STRING, required=True),
                    _secret("api_key", "API key", required=True)],
    "firebolt": [
        _field("account", "Account", STRING, required=True), _field("engine", "Engine", STRING, required=True),
        _field("database", "Database", STRING, required=True), _field("client_id", "Client ID", STRING, required=True),
        _secret("client_secret", "Client secret", required=True),
    ],
    "rockset": [_url("API server URL", "https://api.usw2a1.rockset.com"), _secret("api_key", "API key", required=True)],
    "kinetica": [_url("Kinetica URL", "http://kinetica:9191"), _user(), _password()],
    "crux": [_url("Node URL", "http://crux:3000")],
    "imply": [_url("Polaris URL", "https://myorg.app.imply.io"), _field("project_id", "Project ID", STRING, required=True),
              _secret("api_key", "API key", required=True)],
    "onibex": [
        _url("Target JDBC URL (ClickHouse / Snowflake / Databricks)", "jdbc:clickhouse://clickhouse:8123/default"),
        _user(), _password(), _secret("licence_key", "Onibex licence key", required=True),
    ],
    "privitar": [_url("Publisher URL", "https://privitar.internal"), _field("policy_id", "Policy ID", STRING, required=True),
                 _secret("api_key", "API key", required=True)],
    "fix": [_host("fix-gateway"), _port(9878), _field("sender_comp_id", "SenderCompID", STRING, required=True),
            _field("target_comp_id", "TargetCompID", STRING, required=True)],
}
CONNECTION_CONFIG_SCHEMAS.update(NEW_SOURCE_TYPES)


def schema_for_source_type(source_type: str | None) -> list[dict[str, Any]]:
    return CONNECTION_CONFIG_SCHEMAS.get((source_type or "").lower(), [])


def sensitive_keys_for_source_type(source_type: str | None) -> set[str]:
    """Keys this source type's schema marks as secret and/or not for
    persistence in raw form — the schema-driven redaction signal."""
    return {
        field["key"]
        for field in schema_for_source_type(source_type)
        if field["type"] == SECRET or not field["persist"]
    }


def _check_type(key: str, value: Any, field: dict[str, Any]) -> str | None:
    field_type = field["type"]
    if field_type == NUMBER:
        if isinstance(value, bool) or not isinstance(value, int | float):
            try:
                float(value)
            except (TypeError, ValueError):
                return f"'{key}' must be a number"
        return None
    if field_type == BOOLEAN:
        if not isinstance(value, bool):
            return f"'{key}' must be a boolean"
        return None
    if field_type == ENUM:
        options = field.get("options") or []
        if options and value not in options:
            return f"'{key}' must be one of {options}"
        return None
    if field_type == LIST:
        if not isinstance(value, list | str):
            return f"'{key}' must be a list (or a comma-separated string)"
        return None
    # string / secret / json — any JSON-serializable value is accepted
    return None


def validate_connection_config_fields(source_type: str | None, config: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Same structural validation as validate_connection_config(), but returns
    each error paired with the field key it's about — (key, message) — so a
    caller can report field-level errors (see ConnectorConfigError's
    field_errors) instead of only a flattened message string."""
    errors: list[tuple[str, str]] = []
    config = config or {}
    for field in schema_for_source_type(source_type):
        key = field["key"]
        value = config.get(key)
        if field["required"] and (value is None or value == ""):
            errors.append((key, f"'{key}' is required for source type '{source_type}'"))
            continue
        if value is None:
            continue
        error = _check_type(key, value, field)
        if error:
            errors.append((key, error))
    return errors


def validate_connection_config(source_type: str | None, config: dict[str, Any] | None) -> list[str]:
    """Structural validation against source_type's schema: required fields
    present, present fields of the declared type. Returns a list of human
    -readable error messages (empty if valid). A source_type with no
    registered schema validates as clean — nothing to check against."""
    return [message for _key, message in validate_connection_config_fields(source_type, config)]
