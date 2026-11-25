{{/*
Expand the name of the chart.
*/}}
{{- define "dagster-dbt-runner.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "dagster-dbt-runner.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "dagster-dbt-runner.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Selector labels for a given component
*/}}
{{- define "dagster-dbt-runner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dagster-dbt-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Service account name
*/}}
{{- define "dagster-dbt-runner.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "dagster-dbt-runner.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Dagster Postgres DSN environment variables (works for both embedded and external PG)
*/}}
{{- define "dagster-dbt-runner.pgEnv" -}}
- name: DAGSTER_PG_HOST
  value: {{ if .Values.postgresql.enabled }}{{ include "dagster-dbt-runner.fullname" . }}-postgresql{{ else }}{{ .Values.postgresql.external.host | quote }}{{ end }}
- name: DAGSTER_PG_PORT
  value: {{ if .Values.postgresql.enabled }}"5432"{{ else }}{{ .Values.postgresql.external.port | default 5432 | quote }}{{ end }}
- name: DAGSTER_PG_DB
  value: {{ .Values.postgresql.external.database | default "dagster" | quote }}
- name: DAGSTER_PG_USER
  value: {{ .Values.postgresql.external.user | default "dagster" | quote }}
- name: DAGSTER_PG_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgresql.external.passwordSecretRef.name }}
      key: {{ .Values.postgresql.external.passwordSecretRef.key }}
{{- end }}

{{/*
Env vars every Dagster container needs (the webserver and the daemon get ONLY these).
*/}}
{{- define "dagster-dbt-runner.commonEnv" -}}
- name: DAGSTER_HOME
  value: /dagster-config
- name: DAGSTER_DEPLOYMENT_TARGET
  value: {{ .Values.deploymentTarget | quote }}
- name: DAGSTER_CODE_SERVER_HOST
  value: {{ include "dagster-dbt-runner.fullname" . }}-code
- name: DAGSTER_CODE_SERVER_PORT
  value: "4000"
- name: DAGSTER_COMPUTE_LOGS_DIR
  value: {{ .Values.storage.computeLogsDir | quote }}
- name: DAGSTER_ARTIFACTS_DIR
  value: {{ .Values.storage.artifactsDir | quote }}
{{ include "dagster-dbt-runner.pgEnv" . }}
{{- end }}

{{/*
DG-003: Dagster's identity at the dbt Runner — ONLY the code server gets it. Sensors are evaluated and runs are executed
inside the code server's process (the daemon and webserver just talk to it over gRPC), so a compromise of the webserver
or the daemon no longer yields a key that can mint runner tokens (project enumeration, run triggering).
*/}}
{{- define "dagster-dbt-runner.runnerEnv" -}}
- name: DBT_RUNNER_BASE_URL
  value: {{ include "dagster-dbt-runner.dbtRunnerUrl" . | quote }}
{{- /* DBT-003: Dagster's own scoped identity at the runner; never the runner's static internal token. */}}
{{- $sched := required "dbtRunner.schedulerJwtSecretRef is required: it is the key Dagster signs its scoped tokens for the dbt Runner with" .Values.dbtRunner.schedulerJwtSecretRef }}
{{- $schedName := required "dbtRunner.schedulerJwtSecretRef.name is required" $sched.name }}
{{- if contains "internal" ($schedName | lower) }}
{{- fail (printf "dbtRunner.schedulerJwtSecretRef.name %q looks like the runner's static internal-token Secret: Dagster must use its own scheduler key, not the internal token" $schedName) }}
{{- end }}
- name: SCHEDULER_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ $schedName }}
      key: {{ required "dbtRunner.schedulerJwtSecretRef.key is required" $sched.key }}
- name: ORG_ID
  value: {{ required "tenant.orgId is required: it is the org claim of Dagster's scoped tokens for the dbt Runner" .Values.tenant.orgId | quote }}
{{- end }}

{{/* NetworkPolicy egress building blocks (templates/networkpolicy.yaml). */}}
{{- define "dagster-dbt-runner.egressDns" -}}
- to:
    {{- toYaml .Values.networkPolicy.dns.to | nindent 4 }}
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end }}

{{- define "dagster-dbt-runner.egressPostgres" -}}
{{- if .Values.postgresql.enabled }}
{{- /* DG-004: the in-cluster database is a pod of this release; networkPolicy.postgres.to is for an EXTERNAL one. */ -}}
- to:
    - podSelector:
        matchLabels:
          {{- include "dagster-dbt-runner.selectorLabels" (dict "Chart" .Chart "Values" .Values "Release" .Release "component" "postgresql") | nindent 10 }}
  ports:
    - protocol: TCP
      port: 5432
{{- else }}
- to:
    {{- toYaml .Values.networkPolicy.postgres.to | nindent 4 }}
  ports:
    {{- range .Values.networkPolicy.postgres.ports }}
    - protocol: TCP
      port: {{ . }}
    {{- end }}
{{- end }}
{{- end }}

{{/* The dbt Runner (Service port -> the runner POD's port). One definition, used by the code server AND the helm-test pod, so
`helm test` exercises the very rule the code server relies on. */}}
{{- define "dagster-dbt-runner.egressRunner" -}}
{{- $np := .Values.networkPolicy }}
{{- $ns := default .Release.Namespace .Values.dbtRunner.namespace }}
- to:
    {{- if $np.dbtRunner.to }}
    {{- toYaml $np.dbtRunner.to | nindent 4 }}
    {{- else }}
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: {{ $ns | quote }}
    {{- end }}
  ports:
    {{- range $np.dbtRunner.ports }}
    - protocol: TCP
      port: {{ . }}
    {{- end }}
{{- end }}

{{- define "dagster-dbt-runner.egressCode" -}}
- to:
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: {{ include "dagster-dbt-runner.name" . }}
          app.kubernetes.io/instance: {{ .Release.Name }}
          app.kubernetes.io/component: code-server
  ports:
    - protocol: TCP
      port: 4000
{{- end }}

{{/*
Common volume mounts for all Dagster containers
*/}}
{{- define "dagster-dbt-runner.commonVolumeMounts" -}}
- name: dagster-config
  mountPath: /dagster-config
  readOnly: true
- name: compute-logs
  mountPath: {{ .Values.storage.computeLogsDir }}
- name: artifacts
  mountPath: {{ .Values.storage.artifactsDir }}
{{- end }}

{{/*
Volumes for ONE component (DG-019). Every pod gets both paths (the Dagster instance creates its directories), but a
persistent volume goes only to the pods that use it, so no ReadWriteOnce volume is shared between Deployments:
  sharesLogs    -> the compute-logs ReadWriteMany PVC (mode rwx, persistence enabled): the code server and the webserver
  ownsArtifacts -> the artifacts ReadWriteOnce PVC: the code server alone
Usage: include "dagster-dbt-runner.volumes" (dict "ctx" . "sharesLogs" true "ownsArtifacts" false)
*/}}
{{- define "dagster-dbt-runner.volumes" -}}
{{- $v := .ctx.Values -}}
- name: dagster-config
  configMap:
    name: {{ include "dagster-dbt-runner.fullname" .ctx }}-config
- name: compute-logs
  {{- if and .sharesLogs $v.persistence.enabled (eq $v.persistence.computeLogs.mode "rwx") }}
  persistentVolumeClaim:
    claimName: {{ include "dagster-dbt-runner.fullname" .ctx }}-compute-logs
  {{- else }}
  emptyDir: {}
  {{- end }}
- name: artifacts
  {{- if and .ownsArtifacts $v.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ include "dagster-dbt-runner.fullname" .ctx }}-artifacts
  {{- else }}
  emptyDir: {}
  {{- end }}
{{- end }}

{{/*
Storage settings that cannot work are refused at render time (DG-019), not discovered as a pod stuck in Pending.
*/}}
{{- define "dagster-dbt-runner.validateDatabase" -}}
{{- $pg := .Values.postgresql -}}
{{- $_ := required "postgresql.external.passwordSecretRef.name is required (the Secret must already exist: it holds the database password, for the in-cluster database too)" $pg.external.passwordSecretRef.name -}}
{{- $_ := required "postgresql.external.passwordSecretRef.key is required" $pg.external.passwordSecretRef.key -}}
{{- if $pg.enabled -}}
{{- $_ := required "postgresql.internal.persistence.size is required" $pg.internal.persistence.size -}}
{{- else if not $pg.external.host -}}
{{- fail "postgresql.external.host is required when postgresql.enabled=false: Dagster needs a metadata database, and none is deployed unless postgresql.enabled=true" -}}
{{- end -}}
{{- end }}

{{- define "dagster-dbt-runner.validateStorage" -}}
{{- $p := .Values.persistence -}}
{{- if not (has $p.computeLogs.mode (list "rwx" "s3")) -}}
{{- fail (printf "persistence.computeLogs.mode must be rwx or s3, got %q" $p.computeLogs.mode) -}}
{{- end -}}
{{- if and (eq $p.computeLogs.mode "rwx") $p.enabled (not $p.computeLogs.storageClass) -}}
{{- fail "persistence.computeLogs.storageClass is required: the compute logs are written by the code server and read by the webserver, so they need a ReadWriteMany class (NFS, EFS, Azure Files, CephFS...). Set it, or use persistence.computeLogs.mode=s3, or persistence.enabled=false (the UI then cannot show step logs). A ReadWriteOnce volume cannot be shared between independently scheduled pods." -}}
{{- end -}}
{{- if and (eq $p.computeLogs.mode "s3") (not $p.computeLogs.s3.bucket) -}}
{{- fail "persistence.computeLogs.s3.bucket is required when persistence.computeLogs.mode=s3" -}}
{{- end -}}
{{- if and $p.enabled (gt (int .Values.codeServer.replicaCount) 1) -}}
{{- fail "codeServer.replicaCount > 1 cannot share the ReadWriteOnce artifacts volume: set persistence.enabled=false (and persistence.computeLogs.mode=s3 so the logs stay shared)" -}}
{{- end -}}
{{- end }}

{{/*
S3 credentials for the compute log manager: only the pods that write (code server) or read (webserver) logs get them.
*/}}
{{- define "dagster-dbt-runner.s3LogsEnv" -}}
{{- $s3 := .Values.persistence.computeLogs.s3 -}}
{{- if and (eq .Values.persistence.computeLogs.mode "s3") $s3.credentialsSecretRef.name }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ $s3.credentialsSecretRef.name }}
      key: {{ $s3.credentialsSecretRef.accessKeyIdKey }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $s3.credentialsSecretRef.name }}
      key: {{ $s3.credentialsSecretRef.secretAccessKeyKey }}
{{- end }}
{{- end }}

{{/*
In-cluster URL of the dbt Runner API (DG-005). Unless dbtRunner.baseUrl overrides it, it is the Service the
runner chart really creates: dbt-runner-<orgId> (lower-cased, "_" -> "-", at most 47 chars; see
dbtrunner.fullname in the runner chart), in the runner's namespace, on its Service port (80 -> 8080).
tests/test_helm_chart.py renders both charts and checks they agree.
*/}}
{{- define "dagster-dbt-runner.dbtRunnerUrl" -}}
{{- if .Values.dbtRunner.baseUrl -}}
{{- .Values.dbtRunner.baseUrl | trimSuffix "/" -}}
{{- else -}}
{{- $org := required "tenant.orgId is required (it names the dbt Runner Service, dbt-runner-<orgId>); or set dbtRunner.baseUrl explicitly" .Values.tenant.orgId -}}
{{- $svc := printf "dbt-runner-%s" ($org | lower | replace "_" "-") | trunc 47 | trimSuffix "-" -}}
{{- $ns := default .Release.Namespace .Values.dbtRunner.namespace -}}
{{- $port := int (default 80 .Values.dbtRunner.servicePort) -}}
{{- if eq $port 80 -}}
http://{{ $svc }}.{{ $ns }}.svc.cluster.local
{{- else -}}
http://{{ $svc }}.{{ $ns }}.svc.cluster.local:{{ $port }}
{{- end -}}
{{- end -}}
{{- end }}
