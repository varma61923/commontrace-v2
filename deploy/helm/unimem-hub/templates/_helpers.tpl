{{- define "hub.name" -}}unimem-hub{{- end -}}
{{- define "hub.labels" -}}
app: {{ include "hub.name" . }}
app.kubernetes.io/name: {{ include "hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "hub.image" -}}
{{- $repo := required "image.repository is required (build the repo-root Dockerfile and push it)" .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .Values.image.digest) }}{{ fail "image.digest must be sha256 followed by 64 lowercase hex characters" }}{{ end -}}
{{ $repo }}@{{ .Values.image.digest }}
{{- else -}}
{{- $tag := toString (required "image.tag is required: pin a tag or digest, never latest" .Values.image.tag) -}}
{{- if eq $tag "latest" }}{{ fail "image.tag must not be latest" }}{{ end -}}
{{ $repo }}:{{ $tag }}
{{- end -}}
{{- end -}}
{{- define "hub.secret" -}}
{{- required "existingSecret is required: create the Secret yourself; the chart never renders secret values" .Values.existingSecret -}}
{{- end -}}
{{- define "hub.guards" -}}
{{- range $key := list "HUB_DATABASE_URL" "HUB_API_KEY_PEPPER" "HUB_ENCRYPTION_KEY" "HUB_ENCRYPTION_KEY_PREVIOUS" "HUB_CONSOLE_SECRET" "HUB_ADMIN_TOKEN" "HUB_METRICS_TOKEN" "HUB_LEDGER_SIGNING_KEY" "HUB_COMMONS_SIGNING_KEY" "STRIPE_SECRET_KEY" "STRIPE_WEBHOOK_SECRET" -}}
{{- if hasKey $.Values.config $key -}}
{{- fail (printf "%s must be supplied through existingSecret, not a ConfigMap" $key) -}}
{{- end -}}
{{- end -}}
{{- $grace := int (default "30" (index .Values.config "HUB_GRACEFUL_SHUTDOWN_SECONDS")) -}}
{{- if le (int .Values.terminationGracePeriodSeconds) $grace -}}
{{- fail "terminationGracePeriodSeconds must exceed config.HUB_GRACEFUL_SHUTDOWN_SECONDS" -}}
{{- end -}}
{{- if ge (int (default "2" (index .Values.config "HUB_READYZ_TIMEOUT_SECONDS"))) 3 -}}
{{- fail "config.HUB_READYZ_TIMEOUT_SECONDS must be below the 3s readiness probe timeout" -}}
{{- end -}}
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
