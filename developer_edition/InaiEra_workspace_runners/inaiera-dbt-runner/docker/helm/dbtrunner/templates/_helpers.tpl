{{/*
Common labels & helpers for the dbt Runner chart.
*/}}

{{- define "dbtrunner.name" -}}
dbtrunner
{{- end }}

{{/* Lower-cased, RFC 1123-safe. Real org UUIDs render unchanged. */}}
{{- define "dbtrunner.fullname" -}}
{{- printf "dbt-runner-%s" (.Values.tenant.orgId | lower | replace "_" "-") | trunc 47 | trimSuffix "-" -}}
{{- end }}

{{- define "dbtrunner.labels" -}}
app.kubernetes.io/name: {{ include "dbtrunner.name" . }}
app.kubernetes.io/instance: {{ include "dbtrunner.fullname" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: inaiera-data-plane
inaiera.com/org-id: {{ .Values.tenant.orgId | quote }}
inaiera.com/cloud: {{ .Values.tenant.cloud | quote }}
inaiera.com/product: dbt-runner
{{- end }}

{{- define "dbtrunner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dbtrunner.name" . }}
app.kubernetes.io/instance: {{ include "dbtrunner.fullname" . }}
{{- end }}

{{/* Labels stamped on run Job pods (by the app) and selected by the Job NetworkPolicy. */}}
{{- define "dbtrunner.jobPodLabels" -}}
app.kubernetes.io/name: dbtrunner-job
app.kubernetes.io/instance: {{ include "dbtrunner.fullname" . }}
{{- end }}

{{- define "dbtrunner.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "dbtrunner.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end }}

{{- define "dbtrunner.jobServiceAccountName" -}}
{{- if .Values.runner.jobServiceAccount.create -}}
{{ default (printf "%s-job" (include "dbtrunner.fullname" .)) .Values.runner.jobServiceAccount.name }}
{{- else -}}
{{ default "default" .Values.runner.jobServiceAccount.name }}
{{- end -}}
{{- end }}

{{- define "dbtrunner.runnerImage" -}}
{{- default (printf "%s:%s" .Values.image.repository .Values.image.tag) .Values.runner.image -}}
{{- end }}

{{/*
Fail rendering on invalid combinations instead of shipping a pod that
crash-loops or silently weakens isolation.
*/}}
{{- define "dbtrunner.validate" -}}
{{- if not (has .Values.environment (list "dev" "staging" "prod")) -}}
{{- fail (printf "environment must be one of dev|staging|prod (got %q)" .Values.environment) -}}
{{- end -}}
{{- if not (has .Values.runner.mode (list "k8s_job" "subprocess")) -}}
{{- fail (printf "runner.mode must be k8s_job or subprocess (got %q)" .Values.runner.mode) -}}
{{- end -}}
{{- if and (ne .Values.environment "dev") (eq .Values.runner.mode "subprocess") -}}
{{- fail "runner.mode=subprocess is only allowed with environment=dev (customer dbt code must run in isolated Jobs)" -}}
{{- end -}}
{{- if and (eq .Values.runner.mode "k8s_job") (ne .Values.environment "dev") -}}
{{- if ne .Values.artifactStore.backend "s3" -}}
{{- fail "artifactStore.backend must be s3 for runner.mode=k8s_job outside dev (Job results must be durable)" -}}
{{- end -}}
{{- $_ := required "artifactStore.s3.bucket is required for runner.mode=k8s_job outside dev" .Values.artifactStore.s3.bucket -}}
{{- end -}}
{{- /* DBT-010: Job pods run customer code — egress must name its destinations; "the internet" is a dev-only opt-in. */ -}}
{{- if and .Values.networkPolicy.enabled (eq .Values.runner.mode "k8s_job") -}}
{{- $j := .Values.networkPolicy.job -}}
{{- if and $j.allowPublicInternet (ne .Values.environment "dev") -}}
{{- fail "networkPolicy.job.allowPublicInternet is only allowed with environment=dev: customer dbt code must not be able to reach arbitrary public hosts" -}}
{{- end -}}
{{- if not $j.allowPublicInternet -}}
{{- if and (not $j.git.to) (not $j.git.proxy.url) -}}
{{- fail "networkPolicy.job.git.to (exact git hosts) or networkPolicy.job.git.proxy.url (an FQDN-aware egress proxy) is required: Job pods have no default route to the internet" -}}
{{- end -}}
{{- if and $j.git.proxy.url (not $j.git.proxy.to) -}}
{{- fail "networkPolicy.job.git.proxy.to is required with git.proxy.url: say which pods/addresses are the proxy" -}}
{{- end -}}
{{- if not $j.warehouse.to -}}
{{- fail "networkPolicy.job.warehouse.to is required: name the warehouse (Trino / pg_lake) endpoints Job pods may reach" -}}
{{- end -}}
{{- if not $j.artifactStore.to -}}
{{- fail "networkPolicy.job.artifactStore.to is required: name the artifact-store endpoint Job pods may reach" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- /* X-001: this service accepts only its own audience, and Workspace calls it with a delegated token. */ -}}
{{- if or (not .Values.controlPlane.audience) (eq .Values.controlPlane.audience "workspace.inaiera") -}}
{{- fail "controlPlane.audience must be this service's own audience (default \"dbtrunner\"), never empty or Workspace's \"workspace.inaiera\": a token minted for Workspace must not be accepted here" -}}
{{- end -}}
{{- $_ := required "delegation.secretRef.name is required: Workspace's delegated tokens are verified with this key" .Values.delegation.secretRef.name -}}
{{- $_ := required "delegation.secretRef.key is required" .Values.delegation.secretRef.key -}}
{{- if .Values.delegation.dq.enabled -}}
{{- /* X-001: the Data Quality service signs a user's identity with its own key; no other Secret of this chart may be it. */ -}}
{{- $dqn := required "delegation.dq.secretRef.name is required when delegation.dq.enabled (the key DQ signs its delegated tokens with)" .Values.delegation.dq.secretRef.name -}}
{{- $dqk := required "delegation.dq.secretRef.key is required when delegation.dq.enabled" .Values.delegation.dq.secretRef.key -}}
{{- range $label, $ref := dict "delegation.secretRef" .Values.delegation.secretRef "internalServiceToken.secretRef" .Values.internalServiceToken.secretRef "serviceJwt.secretRef" .Values.serviceJwt.secretRef "schedulerJwt.secretRef" .Values.schedulerJwt.secretRef -}}
{{- if and $ref.name (eq $ref.name $dqn) (eq (default "" $ref.key) $dqk) -}}
{{- fail (printf "delegation.dq.secretRef must not reuse the Secret of %s: a key that signs a user must not be shared with any other trust domain" $label) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $ref, $_ := .Values.gitCredentials -}}
{{- if not (regexMatch "^[a-z][a-z0-9_]{0,62}$" $ref) -}}
{{- fail (printf "gitCredentials key %q is not a valid credential_ref (lower-case letters, digits and _, starting with a letter)" $ref) -}}
{{- end -}}
{{- end -}}
{{- /* SQL jobs on a Workspace connection: their own signing key (it authorises one run on someone's connection), never another identity's. */ -}}
{{- if .Values.jobs.workspace.enabled -}}
{{- $_ := required "jobs.workspace.url is required when jobs.workspace.enabled" .Values.jobs.workspace.url -}}
{{- $_ := required "jobs.workspace.secretRef.name is required when jobs.workspace.enabled" .Values.jobs.workspace.secretRef.name -}}
{{- $_ := required "jobs.workspace.secretRef.key is required when jobs.workspace.enabled" .Values.jobs.workspace.secretRef.key -}}
{{- range $label, $ref := dict "delegation.secretRef" .Values.delegation.secretRef "internalServiceToken.secretRef" .Values.internalServiceToken.secretRef "serviceJwt.secretRef" .Values.serviceJwt.secretRef "schedulerJwt.secretRef" .Values.schedulerJwt.secretRef -}}
{{- if and $ref.name (eq $ref.name $.Values.jobs.workspace.secretRef.name) (eq (default "" $ref.key) $.Values.jobs.workspace.secretRef.key) -}}
{{- fail (printf "jobs.workspace.secretRef must not reuse the Secret of %s: the key that authorises a SQL job on a connection must not be shared with any other trust domain" $label) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- /* DBT-003: Dagster's own signing key, never the static internal token's Secret. */ -}}
{{- if .Values.schedulerJwt.enabled -}}
{{- $_ := required "schedulerJwt.secretRef.name is required when schedulerJwt.enabled: it holds the key Dagster signs its scoped scheduler tokens with" .Values.schedulerJwt.secretRef.name -}}
{{- $_ := required "schedulerJwt.secretRef.key is required when schedulerJwt.enabled" .Values.schedulerJwt.secretRef.key -}}
{{- if and (eq .Values.schedulerJwt.secretRef.name .Values.internalServiceToken.secretRef.name) (eq .Values.schedulerJwt.secretRef.key .Values.internalServiceToken.secretRef.key) -}}
{{- fail "schedulerJwt.secretRef must not point at the internalServiceToken Secret: the static internal token must not be able to sign scheduler tokens" -}}
{{- end -}}
{{- end -}}
{{- if .Values.integrations.dataQuality.enabled -}}
{{- $_ := required "serviceJwt.secretRef.name is required when integrations.dataQuality.enabled" .Values.serviceJwt.secretRef.name -}}
{{- end -}}
{{- end }}

{{/* Database env shared by the API pod and the migrate Job. */}}
{{- define "dbtrunner.dbEnv" -}}
- name: DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.passwordSecretRef.name }}
      key: {{ .Values.database.passwordSecretRef.key }}
{{- end }}
