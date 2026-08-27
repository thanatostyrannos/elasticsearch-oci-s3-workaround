{{/*
Chart name, truncated the way every Helm starter chart does it, so generated
object names stay under Kubernetes' 63 character label limit.
*/}}
{{- define "rig.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rig.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "rig.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rig.namespace" -}}
{{- .Values.namespace.name | default .Release.Namespace -}}
{{- end -}}

{{- define "rig.labels" -}}
app.kubernetes.io/name: {{ include "rig.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
The name of the Secret every tool's credentials come from: either the one
this chart creates from values.credentials.*, or an operator-supplied one.
*/}}
{{- define "rig.credentialsSecretName" -}}
{{- if .Values.credentials.existingSecret -}}
{{- .Values.credentials.existingSecret -}}
{{- else -}}
{{- printf "%s-credentials" (include "rig.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
The Elasticsearch endpoint every tool points at: the external URL, or the
in-cluster ECK service when elasticsearch.external is false.
*/}}
{{- define "rig.esUrl" -}}
{{- if .Values.elasticsearch.external -}}
{{- .Values.elasticsearch.url -}}
{{- else -}}
{{- printf "http://%s-es-http.%s.svc:9200" (include "rig.fullname" .) (include "rig.namespace" .) -}}
{{- end -}}
{{- end -}}

{{/*
The initContainer that clones this tool's source into /workspace, shared by
every Job/CronJob pod in this chart. A no-op list when source.enabled is
false, so a caller can `{{- include "rig.sourceInitContainers" . | nindent 6 }}`
unconditionally.
*/}}
{{- define "rig.sourceInitContainers" -}}
{{- if .Values.source.enabled }}
- name: clone-source
  image: {{ .Values.source.cloneImage | quote }}
  command:
    - sh
    - -c
    - |
      set -eu
      {{- if .Values.source.existingSshSecret }}
      mkdir -p /root/.ssh
      cp /ssh/* /root/.ssh/
      chmod 600 /root/.ssh/*
      ssh-keyscan -H "$(echo "$REPO_URL" | sed -E 's#.*@##; s#:.*##; s#/.*##')" >> /root/.ssh/known_hosts 2>/dev/null || true
      {{- end }}
      git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" /workspace
  env:
    - name: REPO_URL
      value: {{ .Values.source.repoUrl | quote }}
    - name: REPO_REF
      value: {{ .Values.source.ref | quote }}
  volumeMounts:
    - name: workspace
      mountPath: /workspace
    {{- if .Values.source.existingSshSecret }}
    - name: ssh-key
      mountPath: /ssh
      readOnly: true
    {{- end }}
{{- end }}
{{- end -}}

{{/*
Stage the credentials where the runtime user can actually read them.

A Secret volume is owned by root. Every tool here refuses a credentials file
carrying any group or world bit, so the mount has to be 0600, and 0600 owned by
root is unreadable to a container that does not run as root. The UBI base image
runs as uid 1001, so the tools cannot open their own credential.

This copies each file into an emptyDir, owned by the runtime user and still
0600. It is the only container here that runs as root, it runs before anything
else, and it does nothing but the copy.
*/}}
{{- define "rig.credentialStagingInit" -}}
- name: stage-credentials
  image: {{ .Values.image.python | quote }}
  securityContext:
    runAsUser: 0
  command:
    - sh
    - -c
    - |
      set -eu
      for f in /secrets-raw/*; do
        [ -e "$f" ] || continue
        install -m 0600 -o {{ .Values.securityContext.runAsUser | int64 }}           -g {{ .Values.securityContext.runAsGroup | int64 }}           "$f" "/secrets/$(basename "$f")"
      done
  volumeMounts:
    - name: credentials-raw
      mountPath: /secrets-raw
      readOnly: true
    - name: credentials
      mountPath: /secrets
{{- end -}}

{{/*
The Secret as mounted, and the emptyDir the staging step writes into. Tools
read /secrets and never see /secrets-raw.
*/}}
{{- define "rig.credentialVolumes" -}}
- name: credentials-raw
  secret:
    secretName: {{ include "rig.credentialsSecretName" . }}
    defaultMode: 0600
- name: credentials
  emptyDir: {}
{{- end -}}

{{/*
Volumes backing rig.sourceInitContainers plus the shared workspace, common to
every pod that runs one of these tools.
*/}}
{{- define "rig.sourceVolumes" -}}
- name: workspace
  emptyDir: {}
{{- if and .Values.source.enabled .Values.source.existingSshSecret }}
- name: ssh-key
  secret:
    secretName: {{ .Values.source.existingSshSecret }}
    defaultMode: 0600
{{- end }}
{{- end -}}

{{/*
Where the tool's code lives inside the container: /workspace when this chart
cloned it, or the image's own working directory when it is baked in.
*/}}
{{- define "rig.workdir" -}}
{{- if .Values.source.enabled -}}
/workspace
{{- else -}}
/app
{{- end -}}
{{- end -}}

{{/*
python3 snapshot_churn_rig.py teardown's full argument list, shared between
the automatic pre-delete hook and the standalone manual safety-net Job so
the two can never drift apart.
*/}}
{{- define "rig.teardownArgs" -}}
- --es
- {{ include "rig.esUrl" . | quote }}
- --user
- {{ .Values.credentials.harnessEsUser | quote }}
- --password-file
- /secrets/{{ .Values.credentials.keys.esPassword }}
{{- if and .Values.elasticsearch.external .Values.elasticsearch.caCert }}
- --ca-cert
- /es-ca-cert/ca.crt
{{- end }}
{{- if .Values.elasticsearch.insecureTls }}
- --insecure
{{- end }}
- --prefix
- {{ .Values.churnRig.prefix | quote }}
{{- if .Values.churnRig.dataStream }}
- --data-stream
- {{ .Values.churnRig.dataStream | quote }}
{{- end }}
- --state-file
- {{ .Values.churnRig.stateFilePath | quote }}
- --repo-type
- {{ .Values.churnRig.repository.type | quote }}
- --bucket
- {{ .Values.churnRig.repository.bucket | quote }}
- --s3-client
- {{ .Values.churnRig.repository.s3Client | quote }}
{{- if .Values.churnRig.repository.basePath }}
- --base-path
- {{ .Values.churnRig.repository.basePath | quote }}
{{- end }}
{{- if .Values.churnRig.repository.location }}
- --location
- {{ .Values.churnRig.repository.location | quote }}
{{- end }}
{{- if .Values.churnRig.listing.enabled }}
{{- if .Values.churnRig.listing.s3Endpoint }}
- --s3-endpoint
- {{ .Values.churnRig.listing.s3Endpoint | quote }}
{{- end }}
- --s3-region
- {{ .Values.churnRig.listing.s3Region | quote }}
{{- if .Values.churnRig.listing.s3AccessKey }}
- --s3-access-key
- {{ .Values.churnRig.listing.s3AccessKey | quote }}
{{- end }}
- --s3-secret-key-file
- /secrets/{{ .Values.credentials.keys.s3SecretAccessKey }}
{{- end }}
{{- if .Values.teardown.deriveFromPrefix }}
- --derive-from-prefix
{{- end }}
{{- if .Values.teardown.purgeBucket }}
- --purge-bucket
{{- end }}
{{- end -}}

{{/*
Volume mounts and volumes shared by every teardown container. Kept separate
from rig.sourceVolumes because teardown also needs the state PVC (to read
--state-file) and the credentials Secret, which not every pod using
rig.sourceVolumes needs.
*/}}
{{- define "rig.teardownVolumeMounts" -}}
- name: workspace
  mountPath: /workspace
- name: state
  mountPath: /state
- name: credentials
  mountPath: /secrets
  readOnly: true
{{- if and .Values.elasticsearch.external .Values.elasticsearch.caCert }}
- name: es-ca-cert
  mountPath: /es-ca-cert
  readOnly: true
{{- end }}
{{- end -}}

{{- define "rig.teardownVolumes" -}}
{{- include "rig.sourceVolumes" . }}
- name: state
  persistentVolumeClaim:
    claimName: {{ include "rig.fullname" . }}-state
{{ include "rig.credentialVolumes" . }}
{{- if and .Values.elasticsearch.external .Values.elasticsearch.caCert }}
- name: es-ca-cert
  configMap:
    name: {{ include "rig.fullname" . }}-es-ca-cert
{{- end }}
{{- end -}}

{{/*
Wait for Elasticsearch to answer before starting a tool that needs it.

Helm and Argo both create the Elasticsearch resource and these Jobs in the same
pass, so on a fresh install the load generator reaches the cluster before it is
listening and exits on "Connection refused". Retrying inside the tool would
hide a real outage; waiting here does not, because it waits only once, at the
start, and gives up loudly.

Any HTTP answer counts, including 401. The point is that something is
listening, not that this container can authenticate.
*/}}
{{- define "rig.waitForElasticsearch" -}}
- name: wait-for-elasticsearch
  image: {{ .Values.image.python | quote }}
  env:
    - name: ES_URL
      value: {{ include "rig.esUrl" . | quote }}
    - name: WAIT_SECONDS
      value: {{ .Values.elasticsearch.waitSeconds | int64 | quote }}
  command:
    - python3
    - -c
    - |
      import os, ssl, time, urllib.request, urllib.error
      url, deadline = os.environ["ES_URL"], time.time() + int(os.environ["WAIT_SECONDS"])
      ctx = ssl.create_default_context()
      ctx.check_hostname = False
      ctx.verify_mode = ssl.CERT_NONE
      last = "no attempt made"
      while time.time() < deadline:
          try:
              urllib.request.urlopen(url, timeout=5, context=ctx)
              print(f"{url} is answering"); raise SystemExit(0)
          except urllib.error.HTTPError as exc:
              print(f"{url} is answering (HTTP {exc.code})"); raise SystemExit(0)
          except Exception as exc:
              last = f"{type(exc).__name__}: {exc}"
          time.sleep(5)
      raise SystemExit(f"{url} did not answer within {os.environ['WAIT_SECONDS']}s. Last: {last}")
{{- end -}}
