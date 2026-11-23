{{/*
Common labels & helpers for the Workspace chart.
*/}}

{{- define "workspace.name" -}}
workspace
{{- end }}

{{- define "workspace.fullname" -}}
workspace-{{ .Values.tenant.orgId }}
{{- end }}

{{- define "workspace.labels" -}}
app.kubernetes.io/name: {{ include "workspace.name" . }}
app.kubernetes.io/instance: {{ include "workspace.fullname" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: inaiera-data-plane
inaiera.com/org-id: {{ .Values.tenant.orgId | quote }}
inaiera.com/cloud: {{ .Values.tenant.cloud | quote }}
inaiera.com/product: workspace
{{- $profile := (.Values.governance).profile | default "production-governed" }}
inaiera.com/governance-profile: {{ $profile | quote }}
inaiera.com/supported-for-production: {{ eq $profile "production-governed" | quote }}
{{- end }}

{{- define "workspace.selectorLabels" -}}
app.kubernetes.io/name: {{ include "workspace.name" . }}
app.kubernetes.io/instance: {{ include "workspace.fullname" . }}
{{- end }}

{{- define "workspace.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "workspace.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end }}

{{/* Org id as a DNS label (sibling charts name Services this way). */}}
{{- define "workspace.orgSlug" -}}
{{- .Values.tenant.orgId | lower | replace "_" "-" -}}
{{- end }}

{{/*
Worker pods carry their own name/instance labels so the API Service (which
selects workspace.selectorLabels) never routes HTTP traffic to them, without
changing the API Deployment's immutable selector.
*/}}
{{- define "workspace.workerSelectorLabels" -}}
app.kubernetes.io/name: {{ include "workspace.name" . }}-worker
app.kubernetes.io/instance: {{ include "workspace.fullname" . }}-worker
{{- end }}

{{/* Pod-level security shared by every Workspace pod. */}}
{{- define "workspace.podSpecSecurity" -}}
serviceAccountName: {{ include "workspace.serviceAccountName" . }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountToken }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- end }}

{{/*
Secret-backed env shared by the API and the worker. Everything else comes
from the ConfigMap via envFrom.
*/}}
{{- define "workspace.secretEnv" -}}
{{- /* W-001: the gateway's shared secret. Required — without it every request is refused with 401. */}}
{{- $gw := required "gateway.internalSecretRef is required: Workspace only trusts identity headers from a gateway that presents this secret (see values.yaml `gateway`)" .Values.gateway.internalSecretRef }}
- name: ENVOY_INTERNAL_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "gateway.internalSecretRef.name is required" $gw.name }}
      key: {{ required "gateway.internalSecretRef.key is required" $gw.key }}
- name: DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.passwordSecretRef.name }}
      key: {{ .Values.database.passwordSecretRef.key }}
{{- if .Values.jupyterhub.apiTokenSecretRef }}
- name: JUPYTERHUB_ADMIN_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.jupyterhub.apiTokenSecretRef.name }}
      key: {{ .Values.jupyterhub.apiTokenSecretRef.key }}
{{- end }}
{{- if .Values.alerts.actionSecretRef }}
{{- /* Signs the Acknowledge / Resolve links in Slack / Teams messages (API pods serve them, worker pods build them). */}}
- name: ALERTS_ACTION_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "alerts.actionSecretRef.name is required" .Values.alerts.actionSecretRef.name }}
      key: {{ required "alerts.actionSecretRef.key is required" .Values.alerts.actionSecretRef.key }}
{{- end }}
{{- if .Values.codegen.gatewayApiKeySecretRef }}
- name: CODEGEN_GATEWAY_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.codegen.gatewayApiKeySecretRef.name }}
      key: {{ .Values.codegen.gatewayApiKeySecretRef.key }}
{{- end }}
{{- if .Values.codegen.schemaRegistryAuthSecretRef }}
- name: SCHEMA_REGISTRY_AUTH
  valueFrom:
    secretKeyRef:
      name: {{ .Values.codegen.schemaRegistryAuthSecretRef.name }}
      key: {{ .Values.codegen.schemaRegistryAuthSecretRef.key }}
{{- end }}
{{- if .Values.dbtRunner.enabled }}
- name: DBTRUNNER_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.dbtRunner.internalTokenSecretRef.name }}
      key: {{ .Values.dbtRunner.internalTokenSecretRef.key }}
{{- /* X-001: signs the delegated token Workspace presents to the dbt Runner instead of the user's own bearer. */}}
{{- $dbtDelegation := required "dbtRunner.delegationJwtSecretRef is required when dbtRunner.enabled: it signs the delegated token Workspace presents to the runner (see values.yaml `dbtRunner`)" .Values.dbtRunner.delegationJwtSecretRef }}
- name: DBTRUNNER_DELEGATION_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "dbtRunner.delegationJwtSecretRef.name is required" $dbtDelegation.name }}
      key: {{ required "dbtRunner.delegationJwtSecretRef.key is required" $dbtDelegation.key }}
{{- end }}
{{- if .Values.flinkSqlRunner.enabled }}
- name: FLINKSQL_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.flinkSqlRunner.internalTokenSecretRef.name }}
      key: {{ .Values.flinkSqlRunner.internalTokenSecretRef.key }}
{{- /* X-001: same for the FlinkSQL Runner. */}}
{{- $flinkDelegation := required "flinkSqlRunner.delegationJwtSecretRef is required when flinkSqlRunner.enabled: it signs the delegated token Workspace presents to the runner (see values.yaml `flinkSqlRunner`)" .Values.flinkSqlRunner.delegationJwtSecretRef }}
- name: FLINKSQL_DELEGATION_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "flinkSqlRunner.delegationJwtSecretRef.name is required" $flinkDelegation.name }}
      key: {{ required "flinkSqlRunner.delegationJwtSecretRef.key is required" $flinkDelegation.key }}
{{- end }}
{{- if .Values.dq.enabled }}
- name: DQ_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.dq.internalTokenSecretRef.name }}
      key: {{ .Values.dq.internalTokenSecretRef.key }}
{{- /* W-002: signs the scoped JWT dq_proxy sends to DQ. Outside dev the legacy token above is never sent, so without this /dq/validate/rule answers 503. */}}
{{- $dqJwt := required "dq.serviceJwtSecretRef is required when dq.enabled: it signs the scoped service JWT DQ's validate route requires (see values.yaml `dq`)" .Values.dq.serviceJwtSecretRef }}
- name: DQ_SERVICE_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "dq.serviceJwtSecretRef.name is required" $dqJwt.name }}
      key: {{ required "dq.serviceJwtSecretRef.key is required" $dqJwt.key }}
{{- /* X-001: signs the delegated token Workspace presents to DQ instead of the user's own bearer. */}}
{{- $dqDelegation := required "dq.delegationJwtSecretRef is required when dq.enabled: it signs the delegated token Workspace presents to DQ (see values.yaml `dq`)" .Values.dq.delegationJwtSecretRef }}
- name: DQ_DELEGATION_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "dq.delegationJwtSecretRef.name is required" $dqDelegation.name }}
      key: {{ required "dq.delegationJwtSecretRef.key is required" $dqDelegation.key }}
{{- end }}
{{- if .Values.sqlJobs.enabled }}
{{- /* SQL jobs on a connection: verifies the one-run token the dbt Runner signs. Its own Secret, never shared with another key. */}}
- name: RUNNER_JOB_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "sqlJobs.runnerJobJwtSecretRef.name is required when sqlJobs.enabled" .Values.sqlJobs.runnerJobJwtSecretRef.name }}
      key: {{ required "sqlJobs.runnerJobJwtSecretRef.key is required when sqlJobs.enabled" .Values.sqlJobs.runnerJobJwtSecretRef.key }}
{{- end }}
{{- if .Values.ucs.enabled }}
- name: UCS_GATEWAY_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.ucs.internalTokenSecretRef.name }}
      key: {{ .Values.ucs.internalTokenSecretRef.key }}
{{- end }}
{{- end }}

{{/*
Writable scratch mounts for a read-only root filesystem: the git working
copies, /tmp, and $HOME (Python/git write caches there).
*/}}
{{- define "workspace.scratchVolumeMounts" -}}
- name: git-workdir
  mountPath: /var/lib/workspace/git
- name: tmp
  mountPath: /tmp
- name: home
  mountPath: /home/workspace
{{- end }}

{{- define "workspace.scratchVolumes" -}}
- name: git-workdir
  emptyDir:
    sizeLimit: 5Gi
- name: tmp
  emptyDir:
    sizeLimit: 1Gi
- name: home
  emptyDir:
    sizeLimit: 256Mi
{{- end }}

{{/*
Metadata DB URL without the password (the app merges DATABASE_PASSWORD in).
asyncpg takes `ssl=` (disable|prefer|require|verify-ca|verify-full) — the
libpq-style `sslmode=` this used to render makes asyncpg's connect() raise
TypeError, i.e. the app could never reach its database.
*/}}
{{- define "workspace.databaseUrl" -}}
postgresql+asyncpg://{{ .Values.database.user }}@{{ .Values.database.host }}:{{ .Values.database.port }}/{{ .Values.database.name }}?ssl={{ .Values.database.sslmode }}
{{- end }}
