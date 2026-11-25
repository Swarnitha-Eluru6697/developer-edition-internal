{{/* REQUIRED org id — fails rendering when unset or still the placeholder. */}}
{{- define "pralai-flinksql-runner.orgId" -}}
{{- $org := required "tenant.orgId is required (this data plane's org id)" .Values.tenant.orgId -}}
{{- if or (eq $org "REPLACE_ME") (eq $org "local-dev") -}}
{{- fail (printf "tenant.orgId must be a real org id, not the placeholder %q" $org) -}}
{{- end -}}
{{- $org -}}
{{- end }}

{{- define "pralai-flinksql-runner.fullname" -}}
{{- printf "flinksql-runner-%s" (include "pralai-flinksql-runner.orgId" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Selector labels — unchanged from the original chart (Deployment selectors are immutable). */}}
{{- define "pralai-flinksql-runner.selectorLabels" -}}
app: flinksql-runner
inaiera.com/org-id: {{ include "pralai-flinksql-runner.orgId" . | quote }}
{{- end }}

{{- define "pralai-flinksql-runner.labels" -}}
{{ include "pralai-flinksql-runner.selectorLabels" . }}
app.kubernetes.io/name: flinksql-runner
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end }}

{{- define "pralai-flinksql-runner.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pralai-flinksql-runner.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{/* Fail fast on missing mandatory values / inconsistent combinations. */}}
{{- define "pralai-flinksql-runner.validate" -}}
{{- $_ := include "pralai-flinksql-runner.orgId" . -}}
{{- if not (has .Values.environment (list "dev" "staging" "prod")) -}}
{{- fail (printf "environment must be dev, staging or prod (got %q)" .Values.environment) -}}
{{- end -}}
{{- $_ := required "image.repository is required" .Values.image.repository -}}
{{- $_ := required "image.tag is required" .Values.image.tag -}}
{{- $_ := required "flink.gatewayUrl is required" .Values.flink.gatewayUrl -}}
{{- $_ := required "flink.restUrl is required" .Values.flink.restUrl -}}
{{- $_ := required "kafka.bootstrapServers is required" .Values.kafka.bootstrapServers -}}
{{- $_ := required "database.host is required" .Values.database.host -}}
{{- $_ := required "database.passwordSecretRef.name is required" .Values.database.passwordSecretRef.name -}}
{{- $_ := required "controlPlane.jwksUrl is required" .Values.controlPlane.jwksUrl -}}
{{- $_ := required "controlPlane.issuer is required" .Values.controlPlane.issuer -}}
{{- $_ := required "internalServiceToken.secretRef.name is required" .Values.internalServiceToken.secretRef.name -}}
{{- /* X-001: this service accepts only its own audience, and Workspace calls it with a delegated token. */ -}}
{{- if or (not .Values.controlPlane.audience) (eq .Values.controlPlane.audience "workspace.inaiera") -}}
{{- fail "controlPlane.audience must be this service's own audience (default \"flinksql-runner\"), never empty or Workspace's \"workspace.inaiera\": a token minted for Workspace must not be accepted here" -}}
{{- end -}}
{{- $_ := required "delegation.secretRef.name is required: Workspace's delegated tokens are verified with this key" .Values.delegation.secretRef.name -}}
{{- $_ := required "delegation.secretRef.key is required" .Values.delegation.secretRef.key -}}
{{- if .Values.delegation.dq.enabled -}}
{{- /* X-001: the Data Quality service signs a user's identity with its own key; no other Secret of this chart may be it. */ -}}
{{- $dqn := required "delegation.dq.secretRef.name is required when delegation.dq.enabled (the key DQ signs its delegated tokens with)" .Values.delegation.dq.secretRef.name -}}
{{- $dqk := required "delegation.dq.secretRef.key is required when delegation.dq.enabled" .Values.delegation.dq.secretRef.key -}}
{{- range $label, $ref := dict "delegation.secretRef" .Values.delegation.secretRef "internalServiceToken.secretRef" .Values.internalServiceToken.secretRef -}}
{{- if and $ref.name (eq $ref.name $dqn) (eq (default "" $ref.key) $dqk) -}}
{{- fail (printf "delegation.dq.secretRef must not reuse the Secret of %s: a key that signs a user must not be shared with any other trust domain" $label) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if hasPrefix "SASL" .Values.kafka.securityProtocol -}}
{{- $_ := required "kafka.credentialsSecretRef.name is required when kafka.securityProtocol is SASL_*" .Values.kafka.credentialsSecretRef.name -}}
{{- end -}}
{{- if .Values.kafka.schemaRegistry.authEnabled -}}
{{- $_ := required "kafka.schemaRegistry.credentialsSecretRef.name is required when kafka.schemaRegistry.authEnabled=true" .Values.kafka.schemaRegistry.credentialsSecretRef.name -}}
{{- end -}}
{{- if .Values.pgLake.enabled -}}
{{- $_ := required "pgLake.jdbcBaseUrl is required when pgLake.enabled=true (e.g. jdbc:postgresql://host:5432)" .Values.pgLake.jdbcBaseUrl -}}
{{- $_ := required "pgLake.database is required when pgLake.enabled=true" .Values.pgLake.database -}}
{{- $_ := required "pgLake.credentialsSecretRef.name is required when pgLake.enabled=true: the password comes from a Secret, never from values or SQL" .Values.pgLake.credentialsSecretRef.name -}}
{{- end -}}
{{- if .Values.ucs.enabled -}}
{{- $_ := required "ucs.url is required when ucs.enabled=true" .Values.ucs.url -}}
{{- $_ := required "ucs.internalTokenSecretRef.name is required when ucs.enabled=true" .Values.ucs.internalTokenSecretRef.name -}}
{{- $_ := required "ucs.internalTokenSecretRef.key is required when ucs.enabled=true" .Values.ucs.internalTokenSecretRef.key -}}
{{- end -}}
{{- if and .Values.quality.consumerEnabled (not .Values.ucs.enabled) -}}
{{- fail "quality.consumerEnabled=true requires ucs.enabled=true (with ucs.url and ucs.internalTokenSecretRef)" -}}
{{- end -}}
{{- end }}

{{/* DATABASE_PASSWORD + DATABASE_URL env entries (app and migration Job). */}}
{{- define "pralai-flinksql-runner.databaseEnv" -}}
- name: DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.passwordSecretRef.name }}
      key: {{ .Values.database.passwordSecretRef.key }}
- name: DATABASE_URL
  value: "postgresql+asyncpg://{{ .Values.database.user }}:$(DATABASE_PASSWORD)@{{ .Values.database.host }}:{{ .Values.database.port }}/{{ .Values.database.name }}"
{{- end }}
