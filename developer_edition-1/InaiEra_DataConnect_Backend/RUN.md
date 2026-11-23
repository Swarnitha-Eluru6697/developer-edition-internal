# Run InaiEra DataConnect (backend + frontend) with Docker

Two repos, cloned **side by side**:

```
workspace/
  InaiEra_DataConnect/            # this repo (backend + infra)
  InaiEra_DataConnect_ractjs/     # frontend repo
```

```bash
git clone https://github.com/AadhyaAnalyticsInc/InaiEra_DataConnect.git
git clone https://github.com/AadhyaAnalyticsInc/InaiEra_DataConnect_ractjs.git

cd InaiEra_DataConnect/infra
docker compose up -d --build        # first build downloads connector jars (~few min)

# UI:      http://localhost:3000
# API:     http://localhost:8000/api/v1/dataconnect/health
# Kafka UI: http://localhost:8090
```

No seed/bootstrap scripts needed:
- **Templates** are auto-loaded into Postgres on backend startup → all connector templates show immediately.
- **Connector jars** (JDBC, S3, Elasticsearch, MongoDB, Debezium PG/MySQL/Mongo/SQLServer/Oracle, Local File)
  are downloaded at image build by `infra/kafka-connect.Dockerfile`, so deploying pipelines works out of the box.
- Create your own connections/pipelines in the UI.
- **Test sources** (MySQL, MongoDB, SQL Server, Splunk, … — the systems connectors are tested against) are *not*
  started by `docker compose up`. Start the ones you need with `./test-backends.py up mysql mongo`
  (`./test-backends.py list` shows all; heavy ones run one at a time). See `infra/TEST_BACKENDS.md`.

If your frontend clone folder has a different name:
```bash
FRONTEND_DIR=../../your-frontend-folder docker compose up -d --build
```

The `bootstrap.py`, `ingest_one.py`, `dump_topics.py`, `validate_report.py` at the repo root are **optional dev/test
tools** (they recreate a demo dataset) — not required to run the app.
