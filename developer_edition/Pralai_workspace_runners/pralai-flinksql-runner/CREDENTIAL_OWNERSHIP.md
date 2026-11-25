# Who owns which credential (FL-008)

One model, stated once: **the Flink runtime owns the secrets that Flink jobs use to talk to Kafka, Schema Registry and Polaris. A job's SQL
only names governed objects; it never carries a credential, and the Runner never copies one into it.**

| Credential | Owner (where the Secret is mounted) | Used for | Not in |
|---|---|---|---|
| Kafka SASL user/password for **job connectors** | Flink cluster: SQL Gateway + TaskManagers (`docker/helm/flink-cluster`, `kafka.saslCredentialsSecretRef`) | the `kafka` connector inside a deployed job | job SQL, the Runner |
| Schema Registry credentials for **job formats** | Flink cluster | `avro-confluent` etc. inside a deployed job | job SQL, the Runner |
| Polaris (Iceberg REST catalog) client id/secret | Flink cluster (`polaris.credentialsSecretRef`) | the `inaiera` Iceberg catalog inside a job | job SQL, **the Runner (removed in FL-008: it was mounted but never read)** |
| Kafka SASL user/password for the **Runner's own clients** | Runner (`kafka.credentialsSecretRef`) | topic catalog/preview (REST Proxy), the quality-result consumer | job SQL |
| Schema Registry / REST Proxy credentials for the Runner's own calls | Runner | schema lookup in the topic catalog | job SQL |
| pg_lake (governed JDBC catalog) user/password | Runner (`pgLake.credentialsSecretRef`) | the Runner registers the `pg_lake` catalog in each Gateway session (FL-001) | job SQL, the manifest, logs |
| UCS internal token, delegation keys | Runner | calls to UCS; verifying Workspace's delegated tokens | everything else |

The Runner's Kafka identity and the Flink cluster's Kafka identity are **different identities for different jobs** — they may be the same
Secret in a small installation (`kafka-client-credentials`), but the Runner never hands its copy to a job.

## What is enforced

* `app/services/pg_lake.py` refuses (validate **and** deploy) SQL that writes a credential: options whose name contains
  `password`, `secret`, `credential`, `token`, `access-key`, `user-info` or `jaas`, and a `password=` inside an option value (a SASL JAAS
  string). A job that needs authenticated Kafka/Schema Registry/Polaris relies on the cluster's configuration instead.
* `_inject_catalog_config` writes only the Polaris **URI** and **warehouse** into a session — never a credential.
* The Runner's chart mounts no Polaris credential and `Settings` has no field for one.
* `tests/test_credential_ownership.py` checks all of the above on the rendered charts and the generated SQL, and that every Secret the
  Runner pod mounts is something the Runner's code actually reads.

## Known gap — needs a real secured cluster to close

`docker/helm/flink-cluster/values.yaml` sets `table.catalog.inaiera.credential: "${POLARIS_CREDENTIALS}"`, but nothing defines
`POLARIS_CREDENTIALS` (the pods get `POLARIS_CLIENT_ID` / `POLARIS_CLIENT_SECRET`) and Flink does not expand environment variables in its
configuration. Against a Polaris that requires authentication a job that uses the Iceberg catalog will therefore fail to authenticate until
the cluster chart supplies the credential in a form Flink reads (for example a mounted configuration file or a catalog created by the SQL
Gateway at start-up). This could not be exercised here: there is no secured Kafka / Schema Registry / Polaris in this environment, so the
**end-to-end test against a secured stack that FL-008 asks for is NOT done**. The checks above prove the ownership model on the charts and
the code; they do not prove that a secured cluster accepts the jobs.
