{{- define "hub.name" -}}commontrace-hub{{- end -}}
{{- define "hub.labels" -}}
app: {{ include "hub.name" . }}
app.kubernetes.io/name: {{ include "hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "hub.image" -}}
{{- $repo := required "image.repository is required (build the repo-root Dockerfile and push it)" .Values.image.repository -}}
{{- $tag := toString (required "image.tag is required: pin a tag or digest, never latest" .Values.image.tag) -}}
{{- if eq $tag "latest" }}{{ fail "image.tag must not be latest" }}{{ end -}}
{{ $repo }}:{{ $tag }}
{{- end -}}
{{- define "hub.secret" -}}
{{- required "existingSecret is required: create the Secret yourself; the chart never renders secret values" .Values.existingSecret -}}
{{- end -}}
{{- define "hub.guards" -}}
{{- $replicas := int .Values.replicaCount -}}
{{- if .Values.autoscaling.enabled }}{{ $replicas = int .Values.autoscaling.maxReplicas }}{{ end -}}
{{- if and (gt $replicas 1) (ne (index .Values.config "HUB_RATE_LIMIT_BACKEND") "postgres") -}}
{{- fail "more than one replica needs config.HUB_RATE_LIMIT_BACKEND=postgres: the in-process limiter would give each replica its own allowance" -}}
{{- end -}}
{{- $pool := add (int (index .Values.config "HUB_DB_POOL_SIZE")) (int (index .Values.config "HUB_DB_MAX_OVERFLOW")) -}}
{{- if gt (int .Values.postgres.maxConnections) 0 -}}
{{- $worst := mul $pool (add $replicas 1) -}}
{{- if gt $worst (int .Values.postgres.maxConnections) -}}
{{- fail (printf "connections: (pool %d + overflow %d) x (%d replicas + 1 surge) = %d exceeds postgres.maxConnections %d" (int (index .Values.config "HUB_DB_POOL_SIZE")) (int (index .Values.config "HUB_DB_MAX_OVERFLOW")) $replicas $worst (int .Values.postgres.maxConnections)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
