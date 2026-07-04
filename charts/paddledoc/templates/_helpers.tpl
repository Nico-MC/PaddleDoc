{{- define "paddledoc.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "paddledoc.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "paddledoc.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "paddledoc.labels" -}}
helm.sh/chart: {{ include "paddledoc.chart" . }}
app.kubernetes.io/name: {{ include "paddledoc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "paddledoc.selectorLabels" -}}
app.kubernetes.io/name: {{ include "paddledoc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "paddledoc.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "paddledoc.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "paddledoc.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis" (include "paddledoc.fullname" .) -}}
{{- else -}}
{{- required "redis.host is required when redis.enabled=false" .Values.redis.host -}}
{{- end -}}
{{- end -}}
