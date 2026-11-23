{{- define "dq.fullname" -}}
{{- printf "data-quality-%s" .Values.tenant.orgId | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "inaiera-data-quality.fullname" -}}
{{- include "dq.fullname" . }}
{{- end }}

{{- define "dq.selectorLabels" -}}
app: data-quality
inaiera.com/org-id: {{ .Values.tenant.orgId | quote }}
{{- end }}

{{- define "dq.labels" -}}
{{ include "dq.selectorLabels" . }}
app.kubernetes.io/name: inaiera-data-quality
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- define "dq.serviceAccountName" -}}
{{- default (include "dq.fullname" .) .Values.serviceAccount.name }}
{{- end }}

{{/*
DQ-CFG-001: fail the render when an enabled integration is missing its URL
or its own signing-secret reference, when two integrations share a Secret,
or when tenant/database config is missing.
*/}}
{{- define "dq.validate" -}}
{{- $org := required "tenant.orgId is required" .Values.tenant.orgId -}}
{{- if or (eq $org "REPLACE_ME") (eq $org "local-dev") -}}
{{- fail "tenant.orgId must be set to the data plane's real org id" -}}
{{- end -}}
{{- $_ := required "database.passwordSecretRef.name is required" .Values.database.passwordSecretRef.name -}}
{{- $seen := dict -}}
{{- range $name, $i := .Values.integrations -}}
{{- if $i.enabled -}}
{{- $url := required (printf "integrations.%s.url is required when integrations.%s.enabled=true" $name $name) $i.url -}}
{{- if not (regexMatch "^https?://[^/\\s]+" $url) -}}
{{- fail (printf "integrations.%s.url must be an absolute http(s) URL (got %q)" $name $url) -}}
{{- end -}}
{{- $sname := required (printf "integrations.%s.serviceJwtSecret.name is required when integrations.%s.enabled=true" $name $name) ($i.serviceJwtSecret).name -}}
{{- $_ := required (printf "integrations.%s.serviceJwtSecret.key is required when integrations.%s.enabled=true" $name $name) ($i.serviceJwtSecret).key -}}
{{- $ref := printf "%s/%s" $sname $i.serviceJwtSecret.key -}}
{{- if hasKey $seen $ref -}}
{{- fail (printf "integrations.%s and integrations.%s must use separate signing secrets (both use %s)" (get $seen $ref) $name $ref) -}}
{{- end -}}
{{- $_ := set $seen $ref $name -}}
{{- end -}}
{{- end -}}
{{- end }}
