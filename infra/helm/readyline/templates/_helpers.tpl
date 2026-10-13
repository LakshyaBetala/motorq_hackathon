{{- define "readyline.labels" -}}
app.kubernetes.io/name: readyline
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Shared env. Profile changes values only -- never behaviour (12-factor). */}}
{{- define "readyline.env" -}}
- name: PROFILE
  value: {{ .Values.profile | quote }}
- name: KAFKA_BROKERS
  value: {{ .Values.externalServices.redpanda.brokers | quote }}
- name: TELEMETRY_PARTITIONS
  value: {{ .Values.externalServices.redpanda.partitions | quote }}
- name: CLICKHOUSE_HOST
  value: {{ .Values.externalServices.clickhouse.host | quote }}
- name: CLICKHOUSE_PORT
  value: {{ .Values.externalServices.clickhouse.port | quote }}
- name: CLICKHOUSE_DB
  value: {{ .Values.externalServices.clickhouse.database | quote }}
- name: REDIS_HOST
  value: {{ .Values.externalServices.redis.host | quote }}
# A header is not an identity. Outside the local profile the API requires an OIDC bearer token.
- name: ALLOW_HEADER_AUTH
  value: {{ eq .Values.profile "local" | quote }}
{{- range $k := list "READYLINE_PG_APP_DSN" "CLICKHOUSE_PASSWORD" "GROQ_API_KEY" "OIDC_ISSUER" }}
- name: {{ $k }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secretName }}
      key: {{ $k }}
{{- end }}
{{- end -}}
