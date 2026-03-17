{{/*
Expand the name of the chart.
*/}}
{{- define "opensandbox.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "opensandbox.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Server namespace.
*/}}
{{- define "opensandbox.serverNamespace" -}}
{{- default .Release.Name .Values.server.namespace }}
{{- end }}

{{/*
Controller namespace.
*/}}
{{- define "opensandbox.controllerNamespace" -}}
{{- if .Values.controller.namespace }}
{{- .Values.controller.namespace }}
{{- else }}
{{- printf "%s-system" .Release.Name }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "opensandbox.labels" -}}
app.kubernetes.io/name: {{ include "opensandbox.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Server labels.
*/}}
{{- define "opensandbox.serverLabels" -}}
{{ include "opensandbox.labels" . }}
app.kubernetes.io/component: server
{{- end }}

{{/*
Server selector labels.
*/}}
{{- define "opensandbox.serverSelectorLabels" -}}
app: {{ include "opensandbox.fullname" . }}-server
{{- end }}

{{/*
Controller labels.
*/}}
{{- define "opensandbox.controllerLabels" -}}
{{ include "opensandbox.labels" . }}
app.kubernetes.io/component: controller
control-plane: controller-manager
{{- end }}

{{/*
Controller selector labels.
*/}}
{{- define "opensandbox.controllerSelectorLabels" -}}
control-plane: controller-manager
app.kubernetes.io/name: {{ include "opensandbox.name" . }}
{{- end }}

{{/*
Server image.
*/}}
{{- define "opensandbox.serverImage" -}}
{{ .Values.images.server.repository }}:{{ .Values.images.server.tag }}
{{- end }}

{{/*
Controller image.
*/}}
{{- define "opensandbox.controllerImage" -}}
{{ .Values.images.controller.repository }}:{{ .Values.images.controller.tag }}
{{- end }}

{{/*
Execd image (for config.toml).
*/}}
{{- define "opensandbox.execdImage" -}}
{{ .Values.images.execd.repository }}:{{ .Values.images.execd.tag }}
{{- end }}

{{/*
Egress sidecar image.
*/}}
{{- define "opensandbox.egressImage" -}}
{{ .Values.images.egress.repository }}:{{ .Values.images.egress.tag }}
{{- end }}

{{/*
Resolve upstream DNS for egress sidecar.
Priority: explicit value > kube-dns lookup > omit (sidecar uses /etc/resolv.conf).
*/}}
{{- define "opensandbox.egressUpstreamDns" -}}
{{- if .Values.server.config.egress.upstream_dns -}}
{{- .Values.server.config.egress.upstream_dns }}
{{- else -}}
{{-   $svc := (lookup "v1" "Service" "kube-system" "kube-dns") -}}
{{-   if $svc -}}
{{- $svc.spec.clusterIP }}:53
{{-   end -}}
{{- end -}}
{{- end }}

{{/*
Render server config.toml from values.
Produces a valid TOML string from .Values.server.config.
*/}}
{{- define "opensandbox.configToml" -}}
[server]
host = "{{ .Values.server.config.server.host }}"
port = {{ .Values.server.config.server.port }}
log_level = "{{ .Values.server.config.server.log_level }}"
{{- if .Values.server.config.server.api_key }}
api_key = "{{ .Values.server.config.server.api_key }}"
{{- end }}

[runtime]
type = "{{ .Values.server.config.runtime.type }}"
execd_image = "{{ include "opensandbox.execdImage" . }}"
startup_sweep_timeout = {{ .Values.server.config.runtime.startupSweepTimeout | default 5 }}

[storage]
allowed_host_paths = [{{ range $i, $p := .Values.server.config.storage.allowed_host_paths }}{{ if $i }}, {{ end }}"{{ $p }}"{{ end }}]

[kubernetes]
namespace = "{{ default (include "opensandbox.serverNamespace" .) .Values.server.config.kubernetes.namespace }}"
informer_enabled = {{ .Values.server.config.kubernetes.informer_enabled }}
informer_resync_seconds = {{ .Values.server.config.kubernetes.informer_resync_seconds }}
informer_watch_timeout_seconds = {{ .Values.server.config.kubernetes.informer_watch_timeout_seconds }}
workload_provider = "{{ .Values.server.config.kubernetes.workload_provider }}"
batchsandbox_template_file = "/etc/opensandbox/batchsandbox-template.yaml"
filesystem_persistence = {{ .Values.server.config.kubernetes.filesystem_persistence }}
snapshot_enabled = {{ .Values.server.config.kubernetes.snapshot_enabled }}
{{- if .Values.server.config.kubernetes.snapshot_enabled }}
{{- if .Values.server.config.kubernetes.snapshot_class }}
snapshot_class = "{{ .Values.server.config.kubernetes.snapshot_class }}"
{{- end }}
snapshot_max_retention = {{ .Values.server.config.kubernetes.snapshot_max_retention }}
archive_after_seconds = {{ .Values.server.config.kubernetes.archive_after_seconds }}
{{- end }}
{{- if .Values.server.config.kubernetes.storage_class }}
storage_class = "{{ .Values.server.config.kubernetes.storage_class }}"
{{- end }}
{{- if .Values.server.config.kubernetes.storage_size }}
storage_size = "{{ .Values.server.config.kubernetes.storage_size }}"
{{- end }}

[ingress]
mode = "{{ .Values.server.config.ingress.mode }}"
{{- if .Values.server.config.egress.enabled }}

[egress]
image = "{{ include "opensandbox.egressImage" . }}"
{{- $upstreamDns := include "opensandbox.egressUpstreamDns" . -}}
{{- if $upstreamDns }}
upstream_dns = "{{ $upstreamDns }}"
{{- end }}
{{- if .Values.networkAccessConfig.enabled }}
network_access_config_file = "/etc/opensandbox/network-access-config.yaml"
{{- end }}
{{- end }}
{{- end }}
