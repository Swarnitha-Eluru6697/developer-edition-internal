# Validation commands — sources, Kafka topics, destinations

Copy-paste commands to verify data end-to-end and check whether it was transformed.
Kafka broker container is `infra-kafka-1`; broker address inside it is `localhost:29092`.
Backend API: `http://localhost:8001`. MySQL and MongoDB commands use the `mysql` / `mongo` containers
(`./test-backends.py up mysql mongo`). The `psql` commands run against **your own test Postgres** (the one your
Postgres connection points at, port 5432 on your machine) — replace `<user>` and `<database>`; do not run them
against DataConnect's metadata database (`infra-postgres-1`, `localhost:5435`).

## One-shot tools (recommended)
```bash
python3 dump_topics.py           # every message from every pipeline topic
python3 dump_topics.py orders    # only topics matching "orders"
python3 validate_report.py       # full source↔topic↔dest report → VALIDATION_REPORT.md
```

---

## A. List & count every Kafka topic
```bash
# list pipeline topics
docker exec infra-kafka-1 kafka-topics --bootstrap-server localhost:29092 --list | grep '^org_1001'

# count messages in every org_1001 topic
for t in $(docker exec infra-kafka-1 kafka-topics --bootstrap-server localhost:29092 --list | grep '^org_1001'); do
  n=$(docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
        --topic "$t" --from-beginning --timeout-ms 6000 2>/dev/null | grep -c .)
  printf "%-55s %s\n" "$t" "$n"
done

# dump ALL messages of one topic
docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic org_1001.raw.catalog.pg.products.batch.v1 --from-beginning --timeout-ms 8000

# LISTEN LIVE (Ctrl-C to stop)
docker exec -it infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic org_1001.raw.cdc.pg.employees.cdc.v1
```

---

## B. Sources (origin data)
```bash
# PostgreSQL
psql -h localhost -U <user> -d <database> -c "\dt"
psql -h localhost -U <user> -d <database> -c "SELECT count(*) FROM employees;"
psql -h localhost -U <user> -d <database> -c "SELECT * FROM products;"

# MySQL
docker exec mysql mysql -uroot -pdataconnect <database> -e "SHOW TABLES; SELECT * FROM <table>;"

# MongoDB
docker exec mongo mongosh --quiet -u dataconnect -p dataconnect --authenticationDatabase admin <database> --eval 'db.<collection>.find().limit(3).toArray()'
docker exec mongo mongosh --quiet -u dataconnect -p dataconnect --authenticationDatabase admin <database> --eval 'db.<collection>.countDocuments()'

# File source (on the Kafka Connect worker)
docker exec infra-kafka-connect-1 cat /tmp/dc-file-source.txt
```

---

## C. Destinations (sinks wrote out)
```bash
# Postgres SINK table
psql -h localhost -U <user> -d <database> -c "SELECT count(*) FROM employees_sink;"
psql -h localhost -U <user> -d <database> -c "SELECT * FROM employees_sink LIMIT 5;"

# Elasticsearch SINK (indexes are named after the topic)
curl -s 'localhost:9200/_cat/indices?v&h=index,docs.count'
curl -s 'localhost:9200/org_1001.raw.feed.pg.employees.batch.v1/_search?size=3&pretty'

# S3 SINK — verify via consumer progress (current-offset should == topic size, lag 0)
docker exec infra-kafka-1 kafka-consumer-groups --bootstrap-server localhost:29092 \
  --describe --group connect-org_1001--sink--s3--empout--batch--v1
```

---

## D. Is the data transformed? (compare source row vs topic message)

**Employees — `salary`→`package`, `is_active` dropped**
```bash
psql -h localhost -U <user> -d <database> -c "SELECT id,salary,is_active FROM employees WHERE id=1;"
docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic org_1001.raw.sales.pg.employees.batch.v1 --from-beginning --max-messages 1 --timeout-ms 6000 2>/dev/null
#   → topic shows "package": 75000.0 and NO "is_active"  ✅ transformed
```

**Products — `price`→`list_price`, `catalog` inserted**
```bash
docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic org_1001.raw.catalog.pg.products.batch.v1 --from-beginning --max-messages 1 --timeout-ms 6000 2>/dev/null
#   → "list_price": ..., "catalog": "main"  ✅ transformed
```

**Orders — `email` masked, `amount`→`order_total`**
```bash
docker exec mysql mysql -uroot -pdataconnect <database> -e "SELECT id,email,amount FROM orders WHERE id=1;"
docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic org_1001.raw.sales.my.orders.batch.v1 --from-beginning --max-messages 1 --timeout-ms 6000 2>/dev/null
#   → "email": "" (masked), "order_total": ...  ✅ transformed
```

**Not transformed (feed topic keeps original columns)**
```bash
docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic org_1001.raw.feed.pg.employees.batch.v1 --from-beginning --max-messages 1 --timeout-ms 6000 2>/dev/null
#   → "salary": ..., "is_active": true  (original — this feeds the sinks)
```

---

## E. Quality results
```bash
curl -s -H 'X-Org-Id: org_1001' http://localhost:8001/api/v1/dataconnect/quality-results \
  | python3 -c "import sys,json;[print(r['status'],r['failed_count'],'/',r['total_count'],r['topic_name']) for r in json.load(sys.stdin)]"
```

---

## F. Reconcile a source vs its topic count (quick example)
```bash
SRC=$(psql -h localhost -U <user> -d <database> -t -A -c "SELECT count(*) FROM products")
TOP=$(docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
      --topic org_1001.raw.catalog.pg.products.batch.v1 --from-beginning --timeout-ms 6000 2>/dev/null | grep -c .)
echo "products source=$SRC  topic=$TOP  $([ "$SRC" = "$TOP" ] && echo MATCH || echo MISMATCH)"
```

> CDC caveat: a Debezium topic count equals the source only when nothing was deleted; `initial_only`
> topics stay at the snapshot size. Use `validate_report.py` for the interpreted verdicts.
