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
in-cluster ECK service when elasticsearch.external is false. The ECK service
speaks https unless elasticsearch.eck.disableTls is true.
*/}}
{{- define "rig.esUrl" -}}
{{- if .Values.elasticsearch.external -}}
{{- .Values.elasticsearch.url -}}
{{- else if .Values.elasticsearch.eck.disableTls -}}
{{- printf "http://%s-es-http.%s.svc:9200" (include "rig.fullname" .) (include "rig.namespace" .) -}}
{{- else -}}
{{- printf "https://%s-es-http.%s.svc:9200" (include "rig.fullname" .) (include "rig.namespace" .) -}}
{{- end -}}
{{- end -}}

{{/*
"true" when the pods have a CA file for Elasticsearch at /es-ca-cert/ca.crt:
the PEM an operator set for an external cluster, or the CA the ECK operator
publishes for an in-cluster cluster with TLS on. Empty otherwise.
*/}}
{{- define "rig.hasEsCa" -}}
{{- if .Values.elasticsearch.external -}}
{{- if .Values.elasticsearch.caCert -}}true{{- end -}}
{{- else if not .Values.elasticsearch.eck.disableTls -}}
true
{{- end -}}
{{- end -}}

{{/* The mount that puts that CA file in place. Include only when rig.hasEsCa. */}}
{{- define "rig.esCaMount" -}}
- name: es-ca-cert
  mountPath: /es-ca-cert
  readOnly: true
{{- end -}}

{{/*
The volume behind rig.esCaMount. ECK keeps the CA of an in-cluster cluster in
the Secret <cluster>-es-http-certs-public; only its ca.crt key is mounted, not
the certificate.
*/}}
{{- define "rig.esCaVolume" -}}
- name: es-ca-cert
  {{- if .Values.elasticsearch.external }}
  configMap:
    name: {{ include "rig.fullname" . }}-es-ca-cert
  {{- else }}
  secret:
    secretName: {{ include "rig.fullname" . }}-es-http-certs-public
    items:
      - key: ca.crt
        path: ca.crt
  {{- end }}
{{- end -}}

{{/*
Refuse to render a combination that would give a read-only job the cluster
superuser or send its key in the clear. Included once from validate.yaml.

The audit's creds.json and the loop's creds.json carry an Elasticsearch
section for the repository veto. The chart never writes the ECK elastic user
into it. For an in-cluster cluster the operator supplies a read-only key (or a
read-only user) through values or existingSecret, and it must not be the
placeholder or the elastic user.
*/}}
{{- define "rig.validate" -}}
{{- $asks := or (and .Values.auditCronJob.enabled .Values.auditCronJob.askElasticsearch) (and .Values.qualify.enabled .Values.qualify.askElasticsearch) -}}
{{- if and (not .Values.elasticsearch.external) $asks (not .Values.credentials.existingSecret) -}}
{{- $es := .Values.credentials.elasticsearch -}}
{{- if eq $es.authMethod "apiKey" -}}
{{- if or (not $es.apiKey) (hasPrefix "CHANGEME" ($es.apiKey | toString)) -}}
{{- fail "elasticsearch.external is false and a job asks Elasticsearch for the repository, so credentials.elasticsearch.apiKey must be a read-only API key for that cluster. The chart does not substitute the ECK elastic superuser. Set credentials.elasticsearch.apiKey, or set credentials.existingSecret." -}}
{{- end -}}
{{- else -}}
{{- if eq ($es.username | toString) "elastic" -}}
{{- fail "credentials.elasticsearch.username is the elastic superuser; the audit must use a read-only user. Set credentials.elasticsearch.username to a read-only user, or use authMethod apiKey with credentials.elasticsearch.apiKey." -}}
{{- end -}}
{{- if or (not $es.password) (hasPrefix "CHANGEME" ($es.password | toString)) -}}
{{- fail "elasticsearch.external is false and a job asks Elasticsearch for the repository, so credentials.elasticsearch.password must be the read-only user's password. Set credentials.elasticsearch.password, or set credentials.existingSecret." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (not .Values.elasticsearch.external) .Values.auditCronJob.enabled .Values.auditCronJob.askElasticsearch .Values.elasticsearch.eck.disableTls -}}
{{- fail "the audit would send its Elasticsearch credential to the in-cluster cluster over plain http. Set elasticsearch.eck.disableTls to false so the audit uses https with the ECK CA, or set auditCronJob.askElasticsearch to false." -}}
{{- end -}}
{{- end -}}

{{/*
Container securityContext shared by every non-root container in this chart:
no privilege escalation, every Linux capability dropped, and a read-only
root filesystem, since each container gets its own writable path from an
emptyDir or PVC mount rather than from the filesystem itself. The one
container that cannot use this is rig.credentialStagingInit, which has to
run as root; it sets its own securityContext and says why.
*/}}
{{- define "rig.securityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop: ["ALL"]
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
  securityContext:
    {{- include "rig.securityContext" . | nindent 4 }}
  resources:
    {{- toYaml .Values.initContainerResources | nindent 4 }}
  volumeMounts:
    - name: workspace
      mountPath: /workspace
    {{- if .Values.source.existingSshSecret }}
    - name: ssh-key
      mountPath: /ssh
      readOnly: true
    # git needs somewhere writable to stage the known_hosts file and the
    # copied key; /root is on the read-only root filesystem, so give it an
    # emptyDir instead of relaxing readOnlyRootFilesystem for the whole step.
    - name: ssh-home
      mountPath: /root/.ssh
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

Called with a dict: root is the chart context, keys is the list of Secret keys
this pod's command reads. Only those keys are mounted (see
rig.credentialVolumes), so only those keys are copied.
*/}}
{{- define "rig.credentialStagingInit" -}}
{{- $root := .root -}}
{{- $eck := and (not $root.Values.elasticsearch.external) (has $root.Values.credentials.keys.esPassword .keys) -}}
- name: stage-credentials
  image: {{ $root.Values.image.python | quote }}
  # The deliberate exception: this step exists only because a Secret volume
  # is owned by root at mode 0600 and the UBI image's runtime user (uid
  # 1001) cannot read it, so something has to run as root to copy it out.
  # Everything else about it is locked down the same as every other
  # container: no privilege escalation, no capabilities, and its only write
  # target (/secrets) is an emptyDir, so the root filesystem stays read-only
  # even here.
  securityContext:
    runAsUser: 0
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
  resources:
    {{- toYaml $root.Values.initContainerResources | nindent 4 }}
  env:
    - name: PYTHONDONTWRITEBYTECODE
      value: "1"
  command:
    - python3
    - -c
    - |
      import os, pathlib, shutil
      raw, out = pathlib.Path("/secrets-raw"), pathlib.Path("/secrets")
      uid, gid = {{ $root.Values.securityContext.runAsUser | int64 }}, {{ $root.Values.securityContext.runAsGroup | int64 }}

      for src in sorted(raw.iterdir()):
          if src.is_file():
              dst = out / src.name
              shutil.copyfile(src, dst)
              os.chown(dst, uid, gid)
              os.chmod(dst, 0o600)
      {{- if $eck }}

      # An in-cluster cluster does not use the harness password from values:
      # ECK generates its own for the elastic user and rotates it whenever
      # the cluster is rebuilt. Take it from there, or the harness
      # authenticates with a password nothing ever set. Only the harness
      # login file is replaced. creds.json is left alone, so the read-only
      # veto never runs as the superuser.
      eck = pathlib.Path("/eck-elastic-user/elastic")
      if eck.exists():
          path = out / {{ $root.Values.credentials.keys.esPassword | quote }}
          path.write_text(eck.read_text().strip())
          os.chown(path, uid, gid)
          os.chmod(path, 0o600)
          print("harness login password taken from the ECK-generated secret")
      {{- end }}
  volumeMounts:
    - name: credentials-raw
      mountPath: /secrets-raw
      readOnly: true
    - name: credentials
      mountPath: /secrets
    {{- if $eck }}
    - name: eck-elastic-user
      mountPath: /eck-elastic-user
      readOnly: true
    {{- end }}
{{- end -}}

{{/*
The Secret as mounted, and the emptyDir the staging step writes into. Tools
read /secrets and never see /secrets-raw. Called with a dict (root, keys): the
Secret volume lists exactly those keys, so a pod cannot read a credential its
command does not use. A key missing from the Secret stops the pod at start
instead of mounting less than the command expects.
*/}}
{{- define "rig.credentialVolumes" -}}
{{- $root := .root -}}
- name: credentials-raw
  secret:
    secretName: {{ include "rig.credentialsSecretName" $root }}
    defaultMode: 0600
    items:
      {{- range .keys }}
      - key: {{ . | quote }}
        path: {{ . | quote }}
      {{- end }}
- name: credentials
  emptyDir: {}
{{- if and (not $root.Values.elasticsearch.external) (has $root.Values.credentials.keys.esPassword .keys) }}
- name: eck-elastic-user
  secret:
    secretName: {{ include "rig.fullname" $root }}-es-elastic-user
    defaultMode: 0600
{{- end }}
{{- end -}}

{{/*
Secret keys the load generator and teardown read: the harness login password
file, plus the store secret file when the bucket listing is on. The loop and
the audit pass their own lists, since they read creds.json.
*/}}
{{- define "rig.keysHarness" -}}
{{- $k := list .Values.credentials.keys.esPassword -}}
{{- if .Values.churnRig.listing.enabled -}}{{- $k = append $k .Values.credentials.keys.s3SecretAccessKey -}}{{- end -}}
{{- toJson $k -}}
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
- name: ssh-home
  emptyDir: {}
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
snapshot_churn_rig.py verifies certificates and has no switch that turns that
off, so insecureTls cannot be honoured. Fail the render instead of emitting a
flag the script's parser rejects with exit 2.
*/}}
{{- define "rig.requireVerifiedTls" -}}
{{- if .Values.elasticsearch.insecureTls -}}
{{- fail "elasticsearch.insecureTls is not supported: snapshot_churn_rig.py always verifies TLS and has no --insecure flag. Set elasticsearch.caCert to the PEM of the CA that signed the cluster certificate (under ECK: kubectl get secret <cluster>-es-http-certs-public -o jsonpath='{.data.ca\\.crt}' | base64 -d) and leave insecureTls false." -}}
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
{{- if include "rig.hasEsCa" . }}
- --ca-cert
- /es-ca-cert/ca.crt
{{- end }}
{{- include "rig.requireVerifiedTls" . }}
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
{{- if include "rig.hasEsCa" . }}
{{ include "rig.esCaMount" . }}
{{- end }}
{{- end -}}

{{- define "rig.teardownVolumes" -}}
{{- include "rig.sourceVolumes" . }}
- name: state
  persistentVolumeClaim:
    claimName: {{ include "rig.fullname" . }}-state
{{ include "rig.credentialVolumes" (dict "root" . "keys" (include "rig.keysHarness" . | fromJsonArray)) }}
{{- if include "rig.hasEsCa" . }}
{{ include "rig.esCaVolume" . }}
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

Over https the probe verifies the certificate with the CA the tools use
(/es-ca-cert/ca.crt) or, when none is configured, the image's own trust store.
When elasticsearch.insecureTls is true and no CA is configured, nothing could
verify the certificate, so the probe only opens a TCP connection and sends no
request.
*/}}
{{- define "rig.waitForElasticsearch" -}}
{{- $hasCa := include "rig.hasEsCa" . -}}
{{- $https := hasPrefix "https://" (include "rig.esUrl" .) -}}
{{- $tcpOnly := and $https .Values.elasticsearch.insecureTls (not $hasCa) -}}
- name: wait-for-elasticsearch
  image: {{ .Values.image.python | quote }}
  env:
    - name: ES_URL
      value: {{ include "rig.esUrl" . | quote }}
    - name: WAIT_SECONDS
      value: {{ .Values.elasticsearch.waitSeconds | int64 | quote }}
    - name: PYTHONDONTWRITEBYTECODE
      value: "1"
  securityContext:
    {{- include "rig.securityContext" . | nindent 4 }}
  resources:
    {{- toYaml .Values.initContainerResources | nindent 4 }}
  {{- if $hasCa }}
  volumeMounts:
    {{- include "rig.esCaMount" . | nindent 4 }}
  {{- end }}
  command:
    - python3
    - -c
    - |
      import os, socket, ssl, time, urllib.error, urllib.parse, urllib.request
      url, deadline = os.environ["ES_URL"], time.time() + int(os.environ["WAIT_SECONDS"])
      {{- if $tcpOnly }}
      parts = urllib.parse.urlsplit(url)
      address = (parts.hostname, parts.port or 443)
      {{- else }}
      ctx = ssl.create_default_context({{ if $hasCa }}cafile="/es-ca-cert/ca.crt"{{ end }})
      {{- end }}
      last = "no attempt made"
      while time.time() < deadline:
          try:
              {{- if $tcpOnly }}
              socket.create_connection(address, timeout=5).close()
              print(f"{url} accepts connections (certificate not checked)"); raise SystemExit(0)
              {{- else }}
              urllib.request.urlopen(url, timeout=5, context=ctx)
              print(f"{url} is answering"); raise SystemExit(0)
          except urllib.error.HTTPError as exc:
              print(f"{url} is answering (HTTP {exc.code})"); raise SystemExit(0)
              {{- end }}
          except Exception as exc:
              last = f"{type(exc).__name__}: {exc}"
          time.sleep(5)
      raise SystemExit(f"{url} did not answer within {os.environ['WAIT_SECONDS']}s. Last: {last}")
{{- end -}}

{{/*
Tear down a previous rig before starting a new one.

snapshot_churn_rig.py refuses to run when its state file already exists,
because a second rig writing over a first one's policies and data stream would
leave neither recoverable. The refusal is right. What was missing is that the
state file lives on a PersistentVolumeClaim, which outlives the Job, so a
second install of this chart always met the refusal and stopped:

    state file /state/rig-state.json already exists; a previous run was not
    torn down. Run teardown first, or point --state-file elsewhere and pick a
    fresh --prefix

This does what that message says. It runs only when the file is there, so a
first install skips it, and it uses the same arguments the teardown job uses,
so it removes exactly what the previous run created.
*/}}
{{- define "rig.teardownStaleStateInit" -}}
{{- if .Values.churnRig.teardownStaleState }}
- name: teardown-stale-state
  image: {{ .Values.image.python | quote }}
  workingDir: {{ include "rig.workdir" . }}
  env:
    - name: PYTHONDONTWRITEBYTECODE
      value: "1"
  securityContext:
    {{- include "rig.securityContext" . | nindent 4 }}
  # Runs the exact same teardown command as the standalone teardown Job
  # (rig.teardownArgs below), so it gets that job's own sizing rather than
  # the small initContainerResources profile used by the utility steps
  # above.
  resources:
    {{- toYaml .Values.teardown.resources | nindent 4 }}
  command:
    - sh
    - -c
    - |
      set -eu
      if [ ! -f {{ .Values.churnRig.stateFilePath | quote }} ]; then
        echo "no previous state file; nothing to tear down"
        exit 0
      fi
      echo "a previous rig left state behind; tearing it down first"
      python3 snapshot_churn_rig.py teardown "$@"
    - --
    {{- include "rig.teardownArgs" . | nindent 4 }}
  volumeMounts:
    {{- include "rig.teardownVolumeMounts" . | nindent 4 }}
{{- end }}
{{- end -}}
