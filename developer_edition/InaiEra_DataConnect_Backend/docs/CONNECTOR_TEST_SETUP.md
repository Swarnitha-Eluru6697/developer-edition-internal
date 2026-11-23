# Connector test setup: files you need to copy

Two pieces of setup are needed to repeat some of the connector tests (see `CONNECTOR_TEST_LOG.md` for each test's
result). They are kept here, in full, instead of as loose files under `infra/`.

---

## 1. Plugin packaging fixes (`plugin-fixes.sh`)

Some vendor plugins cannot work as published:

| Plugin | Problem | What the script does |
|---|---|---|
| JMS Sink | Reaches its broker through JNDI but ships no ActiveMQ client (`Exception thrown while creating InitialContext`, then "Cannot send, channel has already failed" when `hawtbuf` is missing) | Copies `activemq-client`, `jakarta.jms-api` and `hawtbuf` from the JMS **source** plugin into the sink plugin's `lib/` |
| Qdrant Sink | Its shaded gRPC jar registers only the Unix-socket name resolver and no default load balancer (`NameResolver 'unix' ... not supported by transport`, then `Could not find policy 'pick_first'`) | Adds `DnsNameResolverProvider` and `PickFirstLoadBalancerProvider` to the jar's `META-INF/services` files |
| HDFS sources, Prometheus, Firebase, GemFire, Splunk sources, OmniSci and other Confluent plugins | They ship `connect-licensing-extensions` but not `confluent-licensing` (`NoClassDefFoundError io/confluent/license/InvalidLicenseException` at start) | Copies the licensing jars from the Syslog plugin into each affected plugin's `lib/` |

**How to use it:** save the script below as `plugin-fixes.sh`, then, with the plugins installed in the Connect worker
(default path `/usr/share/confluent-hub-components`), copy it in and run it as root, then restart the worker:

```bash
docker cp plugin-fixes.sh infra-kafka-connect-1:/tmp/plugin-fixes.sh
docker exec -u root infra-kafka-connect-1 bash /tmp/plugin-fixes.sh
docker restart infra-kafka-connect-1
```

Notes: the Syslog plugin must be installed for the licensing-jar step (it is the donor). The script is idempotent. A
plugin that you stash outside `/usr/share/confluent-hub-components` is not touched.

```bash
#!/usr/bin/env bash
# Packaging fixes for vendor Kafka Connect plugins that cannot work as published.
# Idempotent. Run as root inside the Connect image, after the plugins are installed:
#   COMP=/usr/share/confluent-hub-components ./plugin-fixes.sh
set -uo pipefail
COMP="${COMP:-/usr/share/confluent-hub-components}"

# JMS sink: reaches its broker through JNDI but ships no ActiveMQ client. The JMS
# source bundles one, so copy it (and the dependencies it needs at connect time) across.
JMS_SRC=$COMP/confluentinc-kafka-connect-jms
JMS_SINK=$COMP/confluentinc-kafka-connect-jms-sink
if [ -d "$JMS_SINK/lib" ] && ls "$JMS_SRC"/lib/activemq-client-*.jar >/dev/null 2>&1; then
  cp -n "$JMS_SRC"/lib/activemq-client-*.jar "$JMS_SRC"/lib/jakarta.jms-api-*.jar "$JMS_SRC"/lib/hawtbuf-*.jar "$JMS_SINK/lib/"
  echo "jms-sink: activemq-client + hawtbuf present"
fi

# Qdrant sink: its shaded gRPC jar lists only the Unix-socket name resolver and omits
# the pick_first load balancer, so every host name fails with "NameResolver 'unix' ...
# not supported by transport" and then "Could not find policy 'pick_first'".
QDRANT_JAR=$(ls "$COMP"/qdrant-qdrant-kafka/lib/kafka-*.jar 2>/dev/null | head -1)
if [ -n "$QDRANT_JAR" ]; then
  work=$(mktemp -d); cd "$work"
  jar xf "$QDRANT_JAR" META-INF/services/shadow.grpc.NameResolverProvider META-INF/services/shadow.grpc.LoadBalancerProvider
  grep -q DnsNameResolverProvider META-INF/services/shadow.grpc.NameResolverProvider \
    || printf '\nshadow.grpc.internal.DnsNameResolverProvider\n' >> META-INF/services/shadow.grpc.NameResolverProvider
  grep -q PickFirstLoadBalancerProvider META-INF/services/shadow.grpc.LoadBalancerProvider \
    || printf '\nshadow.grpc.internal.PickFirstLoadBalancerProvider\n' >> META-INF/services/shadow.grpc.LoadBalancerProvider
  jar uf "$QDRANT_JAR" META-INF/services/shadow.grpc.NameResolverProvider META-INF/services/shadow.grpc.LoadBalancerProvider
  cd - >/dev/null; rm -rf "$work"
  echo "qdrant: gRPC DNS resolver + pick_first registered"
fi

# Confluent connectors that ship `connect-licensing-extensions` but not `confluent-licensing`
# (HDFS 2/3 source, and others) die at start with NoClassDefFoundError
# io/confluent/license/InvalidLicenseException. Copy the jars from a plugin that bundles them
# (syslog) into each affected plugin's own lib/, since Connect isolates plugin classloaders.
LIC_DONOR=$(ls -d "$COMP"/confluentinc-kafka-connect-syslog 2>/dev/null | head -1)
if [ -n "$LIC_DONOR" ]; then
  for d in "$COMP"/*/; do
    [ -d "$d/lib" ] || continue
    ls "$d/lib" | grep -q "connect-licensing-extensions" || continue
    ls "$d/lib" | grep -q "confluent-licensing" && continue
    cp "$LIC_DONOR"/lib/confluent-licensing-*.jar "$LIC_DONOR"/lib/confluent-serializers-*.jar "$d/lib/" 2>/dev/null \
      && echo "licensing jars -> $(basename "$d")"
  done
fi
```

---

## 2. Lightstreamer adapter set (`adapters.xml`)

The stock `lightstreamer` image has no adapter listening on port 6661, so the Kafka Connect Lightstreamer sink has
nothing to connect to. This adapter set adds a **Proxy Data Adapter** on 6661 (the connector connects to it as a remote
adapter) and a built-in metadata adapter.

**How to use it:** save the XML below as `adapters.xml`, copy it into the Lightstreamer container and restart it:

```bash
docker run -d --name t1-ls --network dataconnect -p 8080:8080 -p 6661:6661 lightstreamer:latest
docker exec t1-ls mkdir -p /lightstreamer/adapters/kafka_connect
docker cp adapters.xml t1-ls:/lightstreamer/adapters/kafka_connect/adapters.xml
docker restart t1-ls
```

Things that matter (found while testing):

- Use the **robust** proxy adapter (`ROBUST_PROXY_FOR_REMOTE_ADAPTER`) as in the file. The plain
  `PROXY_FOR_REMOTE_ADAPTER` treats any non-protocol connection as a fatal load error and shuts the whole server down.
  The platform's connection check opens and closes a TCP connection to the connection's port, which is exactly what
  triggered the old `java.net.SocketException: Socket closed`.
- Point the DataConnect **connection** at the web port (8080) and put the adapter port in the connection's
  `proxy_port` (6661), so the connection check never touches the adapter port.
- The adapter accepts **one** connector connection at a time. A second or a paused connector task keeps the slot and the
  next one fails with `Socket closed`; restart the server (or the connector) to free it.
- Records must carry their schema (JSON with schema), and the pipeline options `topic_mappings` and `record_mappings`
  define the item and its fields.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<adapters_conf id="KAFKA_CONNECT">
    <metadata_provider>
        <adapter_class>com.lightstreamer.adapters.metadata.LiteralBasedProvider</adapter_class>
    </metadata_provider>
    <data_provider name="KafkaConnector">
        <!-- The Kafka Connect Lightstreamer sink connects to this proxy as a Remote Data Adapter.
             The ROBUST variant lets the server start before the connector is there and survive stray connections. -->
        <adapter_class>ROBUST_PROXY_FOR_REMOTE_ADAPTER</adapter_class>
        <classloader>log-enabled</classloader>
        <param name="request_reply_port">6661</param>
        <param name="connection_recovery_timeout_millis">10000</param>
        <param name="first_connection_timeout_millis">0</param>
        <param name="events_recovery">use_snapshot</param>
    </data_provider>
</adapters_conf>
```
