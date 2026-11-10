# multica-agent-runtime: plan

Container images that run the [Multica](https://github.com/multica-ai/multica) agent daemon with Claude Code and Codex,
built here with GitHub Actions and deployed to the `nixos-ai` cluster later (step 2).

Status: step 1 implemented (images, CI, Renovate); not yet built on GitHub. Step 2 (cluster) open.
Per-task sandbox (bubblewrap): see [PLAN-sandbox.md](PLAN-sandbox.md).
Deviations found while implementing are marked *(impl)*.

## 1. Variants and tags

One Dockerfile, `ARG BASE_IMAGE` / `ARG VARIANT`, two images in a CI matrix:

| Variant | Base | Use |
|---|---|---|
| `cpu` | `ubuntu:26.04@sha256:…` | default runner |
| `cuda` | `nvidia/cuda:<ver>-runtime-ubuntu26.04@sha256:…` | GPU passthrough (`runtimeClassName: nvidia`) |

Same distro for both, so one package list. `runtime`, not `devel`: agents that need nvcc get it via Nix.

Image `ghcr.io/niki-on-github/multica-agent-runtime`, tags per variant:

- `:<multica-ver>-<variant>`: moving, rebuilt on every change
- `:<multica-ver>-<variant>-<sha7>`: immutable, one per commit (pin this, by digest)
- `:<variant>`: latest

## 2. Repo layout

```
multica-agent-runtime/
├── Dockerfile
├── rootfs/
│   ├── etc/agent-skel/                 # seeded into $HOME on start, missing files only
│   │   ├── .bashrc  .tmux.conf  .vimrc
│   ├── etc/nix/nix.conf                # single-user, flakes, sandbox = false
│   ├── etc/profile.d/{nix,agent}.sh
│   ├── etc/docker/daemon.json          # rootful defaults (data-root /data/docker)
│   ├── etc/nvidia-container-runtime/   # cuda only: no-cgroups = true for rootless
│   ├── usr/share/doc/multica-agent-runtime/GUIDE.md   # agent guide (build → GPU run → Chrome test)
│   └── usr/local/bin/
│       ├── entrypoint.sh               # subcommands: daemon (default) | shell | doctor | <cmd>
│       ├── lib/{caches,dind,git-auth,home}.sh
│       └── runtime-doctor
├── tests/smoke.sh
├── .github/workflows/build.yml
├── renovate.json
├── docs/kubernetes.md                  # example pod spec for step 2
├── PLAN.md
└── README.md
```

## 3. Image contents

**Agents and Multica**
- `multica`: `COPY --from=ghcr.io/multica-ai/multica-backend:<tag>@sha256:… /app/multica`; the tag sets the image version
- Claude Code (`@anthropic-ai/claude-code`), Codex (`@openai/codex`), Pi (`@earendil-works/pi-coding-agent`): pinned npm
  ARGs, global prefix `/usr/local` *(impl: runtime installs of the agent go to `~/.local`)*, auto-update off (`DISABLE_AUTOUPDATER=1`, the daemon has
  `MULTICA_DAEMON_AUTO_UPDATE=false`). Oh My Pi (`omp`): pinned release binary with sha256 (its npm package needs Bun). `ARG AGENTS="claude codex pi omp"` can drop one.
- Browser MCPs preinstalled (no `npx` download at task time): `@playwright/mcp`, `chrome-devtools-mcp`
- Chrome for Testing (Stable), pinned, installed to `/opt/google/chrome/chrome` (the default path of both MCPs),
  plus its system libs and `fonts-liberation`, `fonts-noto-color-emoji`
- Scripted browser tests: `@playwright/test` (brings the `playwright` CLI) *(impl: no separate `playwright`
  package, bin conflict; no global channel default, tests set `channel: 'chrome'`, see GUIDE.md)*; Playwright's `ffmpeg` installed to
  `/opt/ms-playwright` (video recording); `xvfb` for headed runs (`xvfb-run`)

**Git**: `git`, `git-lfs`, `gh` (release tarball, sha256), `tea` (gitea.com release, checksum file),
`openssh-client`

**Dev base**: `build-essential`, `pkg-config`, `python3` + `python3-venv`, `uv`/`uvx`
(`COPY --from=ghcr.io/astral-sh/uv`), `node` 24 LTS (`COPY --from=node:24-…-slim`) + `pnpm` via corepack

**Shell / CLI**: `vim` (default `EDITOR`/`VISUAL`), `tmux`, `ttyd`, `bash-completion`, `less`, `curl`, `wget`,
`jq`, `yq`, `ripgrep`, `fd-find`, `file`, `rsync`, `sqlite3`, `zip`/`unzip`/`xz-utils`/`zstd`, `procps`,
`psmisc`, `tini`, `bubblewrap` + `socat` (Claude Code sandbox mode on Linux)

**Debugging**: `dnsutils`, `iputils-ping`, `netcat-openbsd`, `iproute2`, `strace`

**Containers**: `docker-ce`, `docker-ce-rootless-extras`, `docker-buildx-plugin`, `docker-compose-plugin`,
`containerd.io`, `uidmap`, `slirp4netns`, `fuse-overlayfs`, `iptables` (legacy alternative selected)

**cuda only**: `nvidia-container-toolkit` (`nvidia-ctk`, `nvidia-container-runtime`, from NVIDIA's apt repo,
pinned), `nvtop`; `nvidia-smi` comes from the driver mount. No nvcc in the image itself: CUDA code is built inside
containers (`FROM nvidia/cuda:*-devel`), see section 5a.

**Nix**: `COPY --from=nixos/nix:<ver>` with `/nix` owned by uid 1000 (single-user, no daemon, as in
rakazo-computer). Everything rare stays out of the image: Go, Rust, Terraform, kubectl/helm/flux,
shellcheck, … via `nix shell nixpkgs#…`.

**User**: `agent`, uid/gid 1000, `HOME=/home/agent` (empty in the image, the PVC goes there).

`/etc/multica-agent-runtime/versions.json` lists every component version (also as OCI labels).

## 4. Entrypoint

`tini` → `entrypoint.sh <subcommand>`:

| Subcommand | Does |
|---|---|
| `daemon` (default) | home seed → caches → git auth → DinD → `multica login --token` → `multica daemon start` (foreground, signals forwarded) |
| `shell` | home seed → caches → git auth → `ttyd --writable --port 7681 --check-origin tmux new -A -s main` |
| `doctor` | `runtime-doctor`: versions, login state per CLI (no tokens printed), Docker mode, GPU, cache reachability |
| anything else | exec'd as is after the same setup |

Without `MULTICA_TOKEN` the `daemon` subcommand logs a hint and waits instead of crash-looping.

Startup steps:

1. **Home seed**: copy `/etc/agent-skel` into `$HOME`, missing files only (never overwrites logins or edits).
2. **Caches**: write the configs from env (section 6).
3. **Git auth**: `gh auth setup-git` if `GH_TOKEN`; `tea login add` + git credential helper for the Gitea host if
   `GITEA_URL` + `GITEA_TOKEN`; `GIT_AUTHOR_*` / `GIT_COMMITTER_*`; optional SSH key + `known_hosts` from a mount.
4. **DinD** (section 5).
   *(impl)* Before it, under the same setup lock: **browser MCPs** (`mcp.sh`) register `playwright` and
   `chrome-devtools` in the user config of every installed agent, missing names only. `~/.claude.json`
   becomes a link to `$CLAUDE_CONFIG_DIR/.claude.json`: the Multica daemon merges the servers of
   `~/.claude.json` into a task with managed MCP servers and runs Claude with `--strict-mcp-config`.

## 5. Docker-in-Docker

Real `dockerd` (full compose / buildx / Testcontainers compatibility), modelled on
[vegardit/docker-gitea-act-runner](https://github.com/vegardit/docker-gitea-act-runner) but with one image and
the mode chosen at runtime:

| `DIND_MODE` | Behaviour | Pod needs |
|---|---|---|
| `auto` (default) | preflight (seccomp from `/proc/$$/status`, AppArmor from `/proc/$$/attr/current`, `/proc` mask, userns, uid) picks the first working mode, logs the missing options otherwise | – |
| `rootless` | `dockerd-rootless.sh` as uid 1000 (rootlesskit, slirp4netns), `DOCKER_HOST=unix:///run/agent/docker.sock` | seccomp + AppArmor `Unconfined`, `procMount: Unmasked`, preferably `hostUsers: false`; *(impl)* **`/dev/net/tun`** (every rootlesskit network driver needs it, `--net=host` is refused) `, **`allowPrivilegeEscalation: true`** and **`capabilities.add: [SYS_ADMIN]`** (bounding set; setuid `newuidmap` cannot write the id map without it, found in local tests on kernel 6.18) |
| `rootful` | container starts as root, starts `dockerd` (moby `hack/dind`, iptables-legacy), then `setpriv` to uid 1000 for the agent; no sudo | `privileged: true` |
| `external` | a daemon already answers at `DOCKER_HOST` (sidecar, or the daemon container of the same pod), CLI only; *(impl)* the `shell` command always uses this | – |
| `off` | no Docker | – |

- Docker data in `/data/docker` (`data-root`), never in `$HOME`
- waits until `docker info` answers, max `DIND_START_TIMEOUT` (default 60 s), then continues without Docker
  and says so (agents still work)
- subuid/subgid for `agent`: full `100000:65536` normally; inside a `hostUsers: false` pod only 65536 ids exist,
  so the entrypoint detects the userns size and uses `1001:<rest>`
- registry mirror: `DOCKER_REGISTRY_MIRRORS` → `registry-mirrors` in `daemon.json`; internal CA trusted
- cuda: see 5a

## 5a. CUDA containers and browser tests (cuda image)

Goal: an agent builds a CUDA image, runs it on the GPU, and tests its web UI / API with Chrome, all inside the
runner pod.

**Build**: plain `docker build` / `docker buildx` (BuildKit in the inner `dockerd`). No GPU needed for building;
base images like `nvidia/cuda:*-devel` are large, so Docker data lives on the big `/data` volume and the registry
mirror (`DOCKER_REGISTRY_MIRRORS`) is worth setting.

**Run on the GPU** (the outer pod already sees the GPU through `runtimeClassName: nvidia`):

1. on start, `entrypoint.sh` runs `nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (writable path owned by
   `agent`, or `$XDG_RUNTIME_DIR/cdi` in rootless mode); the spec lists the device nodes and driver libs exactly
   where they appear in the outer container (also below `/nix/store` on NixOS nodes; `--library-search-path`
   fallback if the ldcache does not know them)
2. `daemon.json`: CDI enabled (`"features": {"cdi": true}`, `cdi-spec-dirs`) and the `nvidia` runtime registered
   (`nvidia-ctk runtime configure --runtime=docker`) for tools that still use `--gpus` / `--runtime=nvidia`
3. rootless: `no-cgroups = true` in `/etc/nvidia-container-runtime/config.toml` (rootless runc cannot manage
   device cgroups); the device nodes are bind-mounted, mode 0666
4. usage: `docker run --rm --device nvidia.com/gpu=all …` (CDI, preferred) or `docker run --gpus all …`;
   `docker compose` with `deploy.resources.reservations.devices` or `devices: [nvidia.com/gpu=all]`
5. `runtime-doctor` checks the chain: `nvidia-smi` outside → CDI spec present → `docker run --device
   nvidia.com/gpu=all <cuda-base> nvidia-smi` inside

Without a GPU (cpu image, CI) the CDI step is skipped with a log line, everything else works.

**Test with Chrome**: Chrome runs in the agent container, the app in the nested container:

- publish ports on loopback: `docker run -p 127.0.0.1:8188:8188 …`. In rootless mode container IPs live in
  rootlesskit's namespace and are not reachable from the agent, so published ports are the rule (documented in
  the agent guide); in rootful mode both work
- Playwright MCP / chrome-devtools-mcp (interactive, through the agent) or `npx playwright test` (scripted, headless,
  screenshots, traces, video) against `http://127.0.0.1:<port>`
- WebGL/canvas UIs render with SwiftShader (software) in headless Chrome; no GPU needed for the browser
- `xvfb-run` for tests that need a headed browser

**Agent guide**: `/usr/share/doc/multica-agent-runtime/GUIDE.md` (like rakazo-computer's) describes the
workflow (build → run with GPU → publish on 127.0.0.1 → test with Chrome → clean up containers/images), the
port rule, disk hygiene (`docker system prune`) and that the GPU is shared with other cluster workloads.
Referenced from the agents' instructions in Multica.

## 5b. How Multica picks the agent CLI

- on start the daemon detects every supported CLI on `PATH` (`claude`, `codex`, …) and refuses to start
  without one
- it registers **one runtime per CLI and workspace**, e.g. "Cluster Runner · Claude Code" and
  "Cluster Runner · Codex"
- in the Multica UI an agent (teammate) is bound to one runtime + model; issues assigned to that agent run with that
  CLI. Several agents on different runtimes side by side are fine
- the model list comes from the runtime (CLI login/config); a CLI that is installed but not logged in still
  appears, its runs fail: `runtime-doctor` shows the login state
- new or newly logged-in CLIs are only detected after a daemon restart (restart the `app` container after a
  `/login` in the web shell)
- `ARG AGENTS` decides which CLIs exist, hence which runtimes appear
- one daemon serves all agents; the GPU runner (step 2) is a separate daemon (`MULTICA_DAEMON_ID`)

## 6. Caches (runtime env, nothing baked in)

| Cache | Env | Written to |
|---|---|---|
| Nix (ncps) | `NIX_CONFIG` (`extra-substituters`, `extra-trusted-public-keys`), `NIX_SSL_CERT_FILE` | read natively |
| pip / uv (proxpi) | `PIP_INDEX_URL`, `UV_DEFAULT_INDEX`, `PIP_CERT` | read natively |
| Hugging Face (olah) | `HF_ENDPOINT`, `HF_HUB_DISABLE_XET=1` | read natively |
| npm | `NPM_CONFIG_REGISTRY` | read natively |
| Container registry | `DOCKER_REGISTRY_MIRRORS` | `daemon.json` |
| Internal CA | `SSL_CERT_FILE` (set by the cluster's Kyverno CA inject) | also `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, Docker certs, if unset |

## 7. Persistence and login

| Tool | State | Kept by |
|---|---|---|
| Claude Code | `CLAUDE_CONFIG_DIR=$HOME/.claude` (credentials + `.claude.json`) | home PVC, or `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`, 1 year) / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` from the Secret |
| Codex | `~/.codex` | home PVC or `OPENAI_API_KEY` |
| Multica | `~/.multica/config.json` | `MULTICA_TOKEN`, re-applied every start |
| gh / tea | `~/.config/gh`, `~/.config/tea` | `GH_TOKEN` / `GITEA_TOKEN`, re-applied every start |

OAuth tokens rotate, so they need a writable PVC, not a read-only Secret mount.

## 8. Interactive login: web terminal

`shell` subcommand = ttyd + tmux (session survives closed tabs and browsers). All TUI logins work without a
localhost callback:

| CLI | Command | Flow |
|---|---|---|
| Claude Code | `claude` → `/login` | URL, paste the code back |
| Codex | `codex login --device-auth` | device code |
| gh | `gh auth login --web` | device code (usually not needed, `GH_TOKEN`) |
| MCP OAuth (localhost callback) | – | copy the failed redirect URL from the browser, `curl '<url>'` in the shell |

No ttyd auth of its own: Kanidm in front (step 2). `kubectl exec -it … -- tmux new -A -s main` is the fallback.

## 9. Environment reference

| Variable | Default | Purpose |
|---|---|---|
| `MULTICA_TOKEN` | – | required for `daemon` |
| `MULTICA_SERVER_URL`, `MULTICA_APP_URL` | – | self-hosted backend |
| `MULTICA_DAEMON_*`, `MULTICA_WORKSPACES_ROOT` | `/data/workspaces` | daemon settings (upstream names) |
| `GH_TOKEN`, `GITEA_URL`, `GITEA_TOKEN` | – | git hosts |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | – | commit identity |
| `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` | – | Claude Code |
| `OPENAI_API_KEY` | – | Codex |
| `DIND_MODE`, `DIND_START_TIMEOUT`, `DOCKER_REGISTRY_MIRRORS` | `auto`, `60` | Docker |
| `NIX_CONFIG`, `PIP_INDEX_URL`, `UV_DEFAULT_INDEX`, `HF_ENDPOINT`, `NPM_CONFIG_REGISTRY` | – | caches |
| `TTYD_PORT` | `7681` | `shell` |

## 10. GitHub Actions (`.github/workflows/build.yml`)

- triggers: `push` to `main`, `workflow_dispatch`, `schedule` weekly (apt security updates)
- `permissions: contents: read, packages: write`; concurrency per ref, cancel in progress
- matrix `variant: [cpu, cuda]`, `fail-fast: false`; free-disk-space step for `cuda`
- steps: checkout → tags from the Multica tag in the Dockerfile → Buildx → build with `load` (GHA cache,
  scope per variant) → smoke tests → GHCR login → push → step summary with digest and `versions.json`
- all actions pinned by digest; runner `ubuntu-24.04`

## 11. Smoke tests (`tests/smoke.sh`)

Run as uid 1000 with an empty tmpfs `$HOME`:

- `multica`, `claude`, `codex`, `gh`, `tea`, `vim`, `tmux`, `ttyd`, Chrome `--version`
- home seed creates the skel files and does not overwrite an existing one
- `nix run nixpkgs#hello`, `NIX_CONFIG` honoured
- Chrome headless screenshot of `data:text/html,…`; both MCPs start (`--help`)
- `doctor` exits 0
- DinD: `docker run --rm alpine true` + `docker compose version`, twice:
  - `rootful`: `--privileged`
  - `rootless`: `seccomp=unconfined`, `apparmor=unconfined`, `systempaths=unconfined`
    (Ubuntu runner: `kernel.apparmor_restrict_unprivileged_userns=0`)
- end to end, both DinD modes: `docker build` a tiny image (`FROM nvidia/cuda:<ver>-base-ubuntu26.04` on cuda,
  `alpine` on cpu) serving a test page → `docker run -d -p 127.0.0.1:18080:8080` → `npx playwright test`
  (channel chrome) asserts the page and takes a screenshot (uploaded as a CI artifact)
- `cuda`: `nvidia-ctk --version`, CUDA libs present, CDI generation skipped cleanly without a GPU (no GPU on the
  runner; the GPU path is tested in the cluster, step 2)

## 12. Renovate (`renovate.json`)

- `config:recommended`, `docker:pinDigests`, `helpers:pinGitHubActionDigestsToSemver`
- `automerge: true`, `automergeType: branch`, `ignoreTests: true`: commits go straight to `main`; CI publishes
  only after the smoke tests pass, so a broken update never replaces the published tags
- `minimumReleaseAge: 1 day`, dependency dashboard on
- one regex manager for `# renovate: datasource=… depName=…` comments above every `ARG`

| Dependency | Datasource |
|---|---|
| Claude Code, Codex, Playwright MCP, Playwright, chrome-devtools-mcp | npm |
| nvidia-container-toolkit | github-releases (`NVIDIA/nvidia-container-toolkit`) |
| Multica backend, Ubuntu, CUDA, Nix, Node, uv | docker (tag + digest) |
| gh, ttyd, Docker (`moby/moby`) | github-releases / github-release-attachments |
| tea | gitea-releases (gitea.com) |
| Chrome for Testing | custom JSON datasource (`googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json`, Stable) |
| GitHub Actions | github-actions |
| apt packages | not pinned, weekly rebuild |

Groups (one commit, one build): `agent CLIs` (Claude Code, Codex), `browser` (Chrome, both MCPs,
Playwright), `github actions`.

One-time setup: enable the Renovate GitHub app for this repo.

## 13. Step 2: cluster (`nixos-ai`, later)

- `kubernetes/apps/multica/runner/`: switch to this image; drop the initContainers and `install-tools.sh`;
  the image entrypoint replaces `start.sh`
- containers in the runner pod, same image:
  - `app` (`daemon`)
  - `shell` (`shell`, optional switch): Service 7681, HTTPRoute `multica-shell.${SECRET_DOMAIN}` via the
    `kanidm-oidc` template, **stricter group than `apps`**, NetworkPolicy only from Envoy
- volumes:

  | Mount | Volume | Backup |
  |---|---|---|
  | `/home/agent` | PVC `multica-runner-home`, ~5 Gi | VolSync restic (holds credentials) |
  | `/data` | PVC `multica-runner-data` (workspaces, docker, caches) | none |
  | `/dev/shm` | `emptyDir` `medium: Memory` | – |
  | `$XDG_RUNTIME_DIR` | `emptyDir` (Docker socket shared with `shell`) | – |

- DinD: preferred `hostUsers: false` + `procMount: Unmasked` + seccomp/AppArmor `Unconfined` + `rootless`;
  fallback `privileged` + `rootful` (no userns then; Docker access = node root, so only if rootless fails)
- caches: env as in section 6 (`CONFIG_HF_CACHE_URL`, `CONFIG_PIP_INDEX_URL`, ncps) + egress to ncps, proxpi,
  olah in the NetworkPolicy
- secret: add `CLAUDE_CODE_OAUTH_TOKEN`, `GITEA_URL`, `GITEA_TOKEN`
- second controller `runner-gpu` (`cuda` image, `runtimeClassName: nvidia`, `NVIDIA_VISIBLE_DEVICES` as in the
  rakazo policy, `NVIDIA_DRIVER_CAPABILITIES=compute,utility`, own `MULTICA_DAEMON_ID`, own home/data PVCs)
- GPU end-to-end test: `runtime-doctor` + build/run a CUDA container + Chrome test against it
- the cluster's Renovate bumps the image digest; enable automerge for this image only if wanted
- keep the backend and the runner image on the same Multica version

Cluster facts (checked 2026-11-05): single node `ai-cluster`, k3s v1.35.6, containerd 2.2.5, kernel 6.18,
NixOS 26.05, cgroup v2, OpenEBS ZFS PVCs.

## 14. Risks, to test in step 2

- rootless Docker inside a `hostUsers: false` pod: only 65536 ids, nested images with files owned by uid
  65534 may fail to unpack
- OpenEBS ZFS PVC with user namespaces needs idmapped mounts (OpenZFS ≥ 2.2)
- GPU in nested containers on NixOS: driver libs live under `/nix/store`; `nvidia-ctk cdi generate` inside the
  pod must find them (ldcache or `--library-search-path`)
- the GPU is shared: nested CUDA containers compete for VRAM with the other GPU apps on the same card
- CUDA images are large (`-devel` ~ several GB): size `/data` accordingly, prune regularly
- the image's Multica CLI can run ahead of the server (Renovate in both repos)

## 15. Implementation order

1. Dockerfile `cpu`: base, tools, agents, Nix, user, skel, `versions.json`
2. `entrypoint.sh` + libs (home, caches, git auth), `runtime-doctor`
3. DinD modes + preflight
4. ttyd/tmux `shell`
5. Chrome for Testing + MCPs + Playwright
6. `cuda` variant: nvidia-container-toolkit, CDI generation, agent guide
7. `tests/smoke.sh`, workflow, Renovate, README, `docs/kubernetes.md`
8. first build on GitHub, check both images, then step 2 in `nixos-ai`
