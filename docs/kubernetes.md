# Running on Kubernetes

An example Deployment with the Multica daemon and the web shell in one pod. Adjust names, storage
classes and secrets; the GitOps version for the `nixos-ai` cluster lives in that repo.

## Requirements per Docker mode

| | rootless (preferred) | rootful (fallback) |
|---|---|---|
| `hostUsers` | `false` (user namespace, needs Kubernetes ≥ 1.33 + containerd ≥ 2.0) | `true` (privileged pods cannot use user namespaces) |
| `runAsUser` | `0` (root inside the user namespace: the entrypoint fits `/etc/subuid`, then drops to 1000) | `0` (starts dockerd, drops to 1000) |
| `privileged` | no | **yes**: Docker access then equals root on the node |
| `allowPrivilegeEscalation` | `true` (setuid `newuidmap`) | `true` |
| `capabilities.add` | `[SYS_ADMIN]`: in the bounding set for `newuidmap` (the agent has no effective caps; with `hostUsers: false` it only applies inside the pod's user namespace) | (implied) |
| `seccompProfile` / `appArmorProfile` | `Unconfined` | (implied) |
| `procMount` | `Unmasked` (requires `hostUsers: false`) | (implied) |
| `/dev/net/tun` | required (rootlesskit network), from a device plugin (e.g. [generic-device-plugin](https://github.com/squat/generic-device-plugin): `resources.limits: {squat.ai/tun: 1}`). A `hostPath` volume does **not** work with `hostUsers: false`: containerd idmap-mounts volumes and devtmpfs refuses (`failed to set MOUNT_ATTR_IDMAP on /dev/net/tun`) | (implied) |
| env | `DIND_MODE=rootless` (or `auto`) | `DIND_MODE=rootful` |

Inside a user-namespaced pod only 65536 ids exist, so the agent gets subordinate ids `1001:64535`:
nested images with files owned by uid ≥ 64535 (e.g. `nobody`, 65534) can fail to unpack. idmapped
volume mounts are needed for PVCs (OpenZFS ≥ 2.2, ext4/xfs: yes).

generic-device-plugin args for `/dev/net/tun` (shared, every `open()` gets its own interface):

```yaml
args:
  - --device
  - |
    name: tun
    groups:
      - count: 100
        paths:
          - path: /dev/net/tun
```

GPU: start the container as root (`runAsUser: 0`, as for rootless Docker): the NVIDIA hook mounts a file
over `/proc/driver/nvidia/params`, which blocks `/proc` for every nested container until the entrypoint
removes it. GPU: `runtimeClassName: nvidia` (and `NVIDIA_VISIBLE_DEVICES` / a GPU resource, depending on how
the cluster exposes GPUs) with the `cuda` image. Several pods can share one GPU this way (no VRAM limit). `doctor --gpu` verifies nested GPU containers.

## Volumes

| Mount | Volume | Notes |
|---|---|---|
| `/home/agent` | PVC, ~5 Gi | logins (Claude Code, Codex, Pi, Oh My Pi), CLI config: back it up |
| `/data` | PVC, large | workspaces, Docker data, caches; must be a real volume (no nested overlay) |
| `/run/agent` | `emptyDir` | Docker socket shared by `app` and `shell` |
| `/dev/shm` | `emptyDir` `medium: Memory`, `sizeLimit: 2Gi` | Chrome |
| `/tmp` | `emptyDir` | scratch |

## Example (rootless)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: multica-runner
spec:
  replicas: 1
  strategy:
    type: Recreate            # RWO volumes, one daemon id
  selector:
    matchLabels: {app: multica-runner}
  template:
    metadata:
      labels: {app: multica-runner}
    spec:
      hostUsers: false
      automountServiceAccountToken: false
      enableServiceLinks: false
      terminationGracePeriodSeconds: 60
      securityContext:
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: app
          image: ghcr.io/niki-on-github/multica-agent-runtime:v0.8.0-cpu-<sha7>@sha256:<digest>
          args: ["daemon"]
          env:
            - {name: DIND_MODE, value: rootless}
            - {name: MULTICA_SERVER_URL, value: "http://multica-backend.multica.svc.cluster.local:8080"}
            - {name: MULTICA_APP_URL, value: "https://multica.example.com"}
            - {name: MULTICA_DAEMON_ID, value: multica-runner}
            - {name: MULTICA_DAEMON_DEVICE_NAME, value: multica-runner}
            - {name: MULTICA_AGENT_RUNTIME_NAME, value: "Cluster Runner"}
            - {name: MULTICA_DAEMON_MAX_CONCURRENT_TASKS, value: "4"}
            # caches (optional)
            - name: NIX_CONFIG
              value: |
                extra-substituters = https://ncps.example.lan?priority=1&trusted=1
                extra-trusted-public-keys = ncps.example.lan:...
            - {name: PIP_INDEX_URL, value: "https://proxpi.example.lan/index/"}
            - {name: UV_DEFAULT_INDEX, value: "https://proxpi.example.lan/index/"}
            - {name: HF_ENDPOINT, value: "https://olah.example.lan"}
            - {name: HF_HUB_DISABLE_XET, value: "1"}
            - {name: DOCKER_REGISTRY_MIRRORS, value: ""}
            - {name: SSL_CERT_FILE, value: /etc/ssl/trust-bundle/ca-bundle.pem}
          envFrom:
            # MULTICA_TOKEN, GH_TOKEN, GITEA_URL, GITEA_TOKEN, CLAUDE_CODE_OAUTH_TOKEN (optional),
            # OPENAI_API_KEY, LLM_API_KEY, GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL
            - secretRef: {name: multica-runner}
          securityContext:
            runAsUser: 0
            runAsGroup: 0
            allowPrivilegeEscalation: true
            capabilities:
              add: [SYS_ADMIN]
            procMount: Unmasked
            seccompProfile: {type: Unconfined}
            appArmorProfile: {type: Unconfined}
          resources:
            requests: {cpu: 500m, memory: 2Gi}
            limits: {cpu: "8", memory: 24Gi, squat.ai/tun: 1}   # /dev/net/tun from the device plugin
          volumeMounts: &mounts
            - {name: home, mountPath: /home/agent}
            - {name: data, mountPath: /data}
            - {name: run, mountPath: /run/agent}
            - {name: shm, mountPath: /dev/shm}
            - {name: tmp, mountPath: /tmp}
        # web terminal for the TUI logins; uses the app container's Docker, starts none itself
        - name: shell
          image: ghcr.io/niki-on-github/multica-agent-runtime:v0.8.0-cpu-<sha7>@sha256:<digest>
          args: ["shell"]
          ports:
            - {name: http, containerPort: 7681}
          envFrom:
            - secretRef: {name: multica-runner}
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            allowPrivilegeEscalation: false
          resources:
            requests: {cpu: 10m, memory: 64Mi}
            limits: {cpu: "2", memory: 2Gi}
          volumeMounts: *mounts
      volumes:
        - {name: home, persistentVolumeClaim: {claimName: multica-runner-home}}
        - {name: data, persistentVolumeClaim: {claimName: multica-runner-data}}
        - {name: run, emptyDir: {}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 2Gi}}
        - {name: tmp, emptyDir: {}}
```

Expose the shell (port 7681, WebSocket) only behind an authenticating reverse proxy (OIDC at the
gateway, oauth2-proxy, ...; see [Web shell](../README.md#web-shell)): ttyd has no login and the shell
gives access to every token in the pod and to Docker. Restrict it with a NetworkPolicy to the gateway.

Several runners (e.g. one `cpu`, one `cuda` with the GPU): one Deployment each, with its own
`MULTICA_DAEMON_ID` / `MULTICA_DAEMON_DEVICE_NAME` / `MULTICA_AGENT_RUNTIME_NAME`, its own PVCs and its
own shell hostname. They can share the secret.

Restart the `app` container after a new CLI login in the shell: the Multica daemon detects CLIs and
their models at start.

## Rootful variant

Replace the `app` security context with `privileged: true`, `runAsUser: 0`, drop `hostUsers: false`,
`procMount` and the `squat.ai/tun` resource, and set `DIND_MODE=rootful`.
