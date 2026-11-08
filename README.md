# multica-agent-runtime

Container images that run the [Multica](https://github.com/multica-ai/multica) agent daemon with
**Claude Code**, **OpenCode**, **Codex** and **Pi**, made for Kubernetes: Docker-in-Docker (rootless or
rootful), a CUDA variant with GPU access for nested containers, Chrome for Testing + Playwright for
browser tests, Nix with configurable caches, `gh` and `tea`, and a web terminal for the CLI logins.

```
ghcr.io/niki-on-github/multica-agent-runtime:<multica-version>-<variant>[-<sha7>]
                                             │                 │          └ commit of this repo (immutable)
                                             │                 └ cpu | cuda
                                             └ Multica version of the bundled `multica` CLI/daemon, e.g. v0.8.0
```

**The version in the tag is the Multica agent (daemon) version**, not a version of this repo: the
`multica` binary is copied from `ghcr.io/multica-ai/multica-backend:<multica-version>`. Pick the tag
that matches your Multica server. Everything else in the image (Claude Code, OpenCode, Codex, Chrome,
Docker, ...) is updated by Renovate independently and does not change the version part; the exact
versions of an image are in `/etc/multica-agent-runtime/versions.json` (`doctor` prints them).

| Tag | Example | Moves? |
|---|---|---|
| `<multica-version>-<variant>-<sha7>` | `v0.8.0-cuda-9b973bc` | no: one build. Pin this (plus `@sha256:` digest) in deployments |
| `<multica-version>-<variant>` | `v0.8.0-cuda` | yes: newest build for this Multica version |
| `<variant>` | `cuda` | yes: newest build for the newest Multica version |

| Variant | Base | Use |
|---|---|---|
| `cpu` | `ubuntu:26.04` | default |
| `cuda` | `nvidia/cuda:<cuda-version>-runtime-ubuntu26.04` + nvidia-container-toolkit | GPU runner: CUDA in the agent container and in nested containers |

Both variants run **Ubuntu 26.04 LTS** (switched from 24.04). Keep the two bases on the same release:
the Docker CE apt repository and package version are derived from the base's `/etc/os-release`
(`VERSION_CODENAME`/`VERSION_ID`), and Chrome's `deb.deps` names are mapped to the release's packages
at build time (e.g. `libasound2` → `libasound2t64`). Images are rebuilt on every dependency update
(Renovate) and weekly (Ubuntu security updates).

## What is inside

| | |
|---|---|
| Multica | `multica` CLI/daemon (copied from the official backend image) |
| Agents | Claude Code, OpenCode, Codex, Pi (npm, pinned, auto-update off) |
| Browser | Chrome for Testing at `/opt/google/chrome/chrome`, `@playwright/test`, Playwright MCP, chrome-devtools-mcp, xvfb |
| Containers | Docker engine (rootless extras, buildx, compose), slirp4netns, fuse-overlayfs; cuda: nvidia-container-toolkit, nvtop |
| Git | git, git-lfs, gh, tea, openssh-client |
| Dev | Node 26, npm, pnpm, Python 3.14 + venv + pip, uv/uvx, build-essential, Nix (single-user, flakes) |
| Shell | bash, vim, tmux, ttyd, jq, yq, ripgrep, fd, rsync, sqlite3, curl, wget, bubblewrap, socat, network/debug tools |

Everything else comes through Nix: `nix shell nixpkgs#go -c go version`.
`doctor` (or `doctor --json`) shows versions, logins, Docker mode, GPU and cache reachability.

## How Multica uses the agents

The daemon finds every supported CLI on `PATH` and registers **one runtime per CLI and workspace**
named `$MULTICA_AGENT_RUNTIME_NAME · <CLI>` (e.g. "multica-agent-gpu-01 · Claude Code"). In the Multica UI
an agent is bound to one runtime and model; issues assigned to it run with that CLI. Several runners
(e.g. a cpu and a cuda one) can serve the same workspace: give each its own `MULTICA_DAEMON_ID`,
runtime name and `/home/agent` volume (logins are per runner). Build with
`--build-arg AGENTS="claude opencode"` to leave one out. The daemon detects CLIs and their models at
start: restart it after logging in to a CLI.

## Commands

`tini` runs `entrypoint.sh <command>`:

| Command | |
|---|---|
| `daemon` (default) | `multica login --token $MULTICA_TOKEN`, then `multica daemon start --foreground`; starts Docker per `DIND_MODE` (default `auto`). Without a token it logs a hint and waits. |
| `shell` | web terminal (`ttyd`) on `$TTYD_PORT` (7681): a login shell per browser tab; `https://<host>/?arg=tmux` attaches the persistent tmux session `main` instead. Uses an existing Docker daemon, never starts one. **No authentication of its own**, see [Web shell](#web-shell). |
| `doctor [--gpu] [--json] [--strict]` | runtime check |
| anything else | runs it after the same setup (`DIND_MODE` default `off`) |

Startup (both `daemon` and `shell`): `$HOME` is seeded from `/etc/agent-skel` (missing files only),
the CA bundle is spread to all tools, git/gh/tea are configured from env, a CDI spec is generated
for visible GPUs, Docker is started.

## Web shell

`shell` runs [ttyd](https://github.com/tsl0922/ttyd): a full terminal as the agent user, with every token
of the container (Multica, GitHub, LLM keys, CLI logins) and the Docker daemon. ttyd itself has **no
login** (it only checks the WebSocket origin), so never expose port 7681 directly. Put it behind an
**authenticating reverse proxy** (also called an auth proxy or SSO/OIDC gateway) that lets only you
through, for example:

- OIDC at the ingress/gateway: Envoy Gateway `SecurityPolicy` (`oidc`), Traefik/NGINX forward auth
- a dedicated proxy in front: [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy),
  [Pomerium](https://www.pomerium.com/), Authelia, Authentik
- a tunnel with access control: Tailscale, Cloudflare Access

Use TLS (the terminal and OAuth codes go over the WebSocket), allow only the proxy to reach the port
(NetworkPolicy), and do not add exceptions for "internal" IPs. Without such a proxy use
`kubectl port-forward` / `docker run -p 127.0.0.1:7681:7681`, or as a minimal fallback ttyd's basic
auth: `TTYD_ARGS="--credential user:password"` (over TLS only).

## Logins and persistence

Mount a volume at `/home/agent`: logins and CLI state live there and survive restarts.

| Tool | Login | Alternative |
|---|---|---|
| Claude Code (subscription) | `claude` → `/login` in the web shell (shows a URL, paste the code back) | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (1 year), `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` |
| OpenCode | `opencode auth login` | provider config (`OPENCODE_CONFIG`) with an API key |
| Codex | `codex login --device-auth` | `OPENAI_API_KEY` |
| Pi | `pi` → `/login` in the web shell | provider API keys (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) |
| Multica | – | `MULTICA_TOKEN` (personal access token), re-applied on every start |
| GitHub | – | `GH_TOKEN` (gh as git credential helper) |
| Gitea | – | `GITEA_URL` + `GITEA_TOKEN` (`tea` login + git credential helper; `GITEA_USER` optional) |

Do the logins in the default web shell, not in `?arg=tmux`: the browser terminal wraps a long OAuth
URL itself, so clicking or copying it gets the whole URL. tmux redraws wrapped lines row by row,
which cuts the URL at the row end (e.g. `Invalid OAuth request: Missing state parameter`).

MCP servers with an OAuth `localhost` callback: open the URL, let the browser fail on the
redirect, copy that URL and run `curl '<url>'` in the web shell.

`CLAUDE_CONFIG_DIR=$HOME/.claude` keeps all Claude Code state in one directory. Optional SSH keys:
mount them at `/etc/agent-ssh` (copied to `~/.ssh` with mode 600).

## Docker-in-Docker

Real `dockerd`, listening on `DOCKER_HOST=unix:///run/agent/docker.sock`, data in
`$DIND_DATA_ROOT` (`/data/docker`, must be a volume). Mode by `DIND_MODE`:

| Mode | Container needs |
|---|---|
| `auto` | picks the first that works: existing daemon → `external`, root + privileged → `rootful`, else `rootless` if its prerequisites are met (missing ones are logged), else `off` |
| `rootless` | seccomp + AppArmor unconfined, unmasked `/proc`, `/dev/net/tun`, `CAP_SYS_ADMIN` in the bounding set and no `no_new_privs` (both for the setuid `newuidmap`; the agent has no effective capabilities). In a user-namespaced pod start as root (uid 0 of the namespace) so the entrypoint can fit the subordinate ids |
| `rootful` | `--privileged`, started as root (`--user 0`); the agent uses the daemon through the `docker` group |
| `external` | only the CLI, against `DOCKER_HOST` |
| `off` | no Docker |

`/run/agent` is usually an `emptyDir`, which survives container restarts together with the files of
the previous daemon. Files a rootless daemon created belong to its subordinate ids, so only root can
remove them: start the container as root (as for rootless on Kubernetes), the entrypoint cleans up
before it drops to the agent user.

Publish container ports on loopback (`-p 127.0.0.1:8080:8080`) and test against `127.0.0.1`; in
rootless mode container IPs are not reachable from the agent.

```sh
# rootless, as on Kubernetes (docs/kubernetes.md)
docker run --rm -it \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
  --cap-add SYS_ADMIN --device /dev/net/tun -v agent-home:/home/agent -v agent-data:/data \
  -e DIND_MODE=rootless ghcr.io/niki-on-github/multica-agent-runtime:cpu bash -l

# rootful
docker run --rm -it --privileged --user 0 -v agent-home:/home/agent -v agent-data:/data \
  -e DIND_MODE=rootful ghcr.io/niki-on-github/multica-agent-runtime:cpu bash -l
```

## GPU (cuda image)

Give the container the GPU (`runtimeClassName: nvidia` on Kubernetes, `--gpus all` with Docker).
At start `nvidia-ctk cdi generate` writes a CDI spec of exactly what the container sees, so nested
containers get the GPU with `docker run --device nvidia.com/gpu=all ...` (`--gpus all` works too).
Rootless mode runs the NVIDIA hook with `no-cgroups`.
The NVIDIA hook of the outer runtime mounts a file over `/proc/driver/nvidia/params`; any mount on top of
`/proc` makes the kernel refuse new proc mounts, so every nested container would fail (`runc ... error
mounting "proc" ... operation not permitted`, also with `--privileged`). The entrypoint removes that
mount when the container starts as root (also uid 0 of a user-namespaced pod).
In a user-namespaced pod the GPU device nodes belong to the overflow id 65534, which `nvidia-ctk`
copies into the spec (`gid`, `additionalGids`); rootless Docker cannot map it (`setgroups: invalid
argument`), so the entrypoint drops those ids from the spec. The NVIDIA runtime is set to `mode = "cdi"`:
`--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all` and `--device nvidia.com/gpu=all` use the same spec.
On NixOS nodes `nvidia-smi` and the other driver tools are Nix binaries whose ELF interpreter (glibc)
lives elsewhere in `/nix/store`; the entrypoint adds those store paths to the spec, otherwise nested
containers fail with `exec /usr/local/bin/nvidia-smi: no such file or directory`. `NVIDIA_CDI_ARGS` passes extra flags (e.g.
`--library-search-path` for unusual driver locations). `doctor --gpu` runs `nvidia-smi` in a nested
CUDA container.

All fixes above only act when their problem is present, so the same image works on Kubernetes and
plain Docker, on NixOS and on common distributions (driver in `/usr/lib`, nothing to add). The host
needs the NVIDIA driver and the NVIDIA Container Toolkit (`--gpus all` must work there). With plain
Docker start as root, since the image's default user is 1000:

```sh
docker run --rm -it --gpus all --user 0 \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
  --cap-add SYS_ADMIN --device /dev/net/tun -v agent-home:/home/agent -v agent-data:/data \
  -e DIND_MODE=rootless ghcr.io/niki-on-github/multica-agent-runtime:cuda doctor --gpu
```

Test images for nested containers: `nvidia/cuda:<version>-base-ubuntu26.04` (`nvidia-smi`), the CUDA
samples (`nvcr.io/nvidia/k8s/cuda-sample:vectoradd-*`, prints `Test PASSED`) or PyTorch
(`torch.cuda.get_device_name(0)`).

## Browser testing

Chrome for Testing sits at the default Chrome path, so Playwright (`channel: 'chrome'`),
Playwright MCP and chrome-devtools-mcp use it without configuration. The wrapper adds
`--no-sandbox` when the user-namespace sandbox is unavailable and `--disable-dev-shm-usage` with a
small `/dev/shm` (`CHROME_EXTRA_ARGS` for more). Give `/dev/shm` ≥ 1 GiB for heavy pages.
The agents' guide is in the image: `/usr/share/doc/multica-agent-runtime/GUIDE.md`.

## Caches

Nothing is baked in; the standard variables are read natively:

| Cache | Variables |
|---|---|
| Nix | `NIX_CONFIG` (`extra-substituters = ...`, `extra-trusted-public-keys = ...`) |
| pip / uv | `PIP_INDEX_URL`, `UV_DEFAULT_INDEX` |
| Hugging Face | `HF_ENDPOINT` (+ `HF_HUB_DISABLE_XET=1` for proxies like olah) |
| npm | `NPM_CONFIG_REGISTRY` |
| Docker registry | `DOCKER_REGISTRY_MIRRORS` (comma separated) |
| CA bundle | `SSL_CERT_FILE`: also set as `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `PIP_CERT`, `NIX_SSL_CERT_FILE` unless those are set |

## Environment

| Variable | Default | |
|---|---|---|
| `MULTICA_TOKEN` | – | required for `daemon` |
| `MULTICA_SERVER_URL`, `MULTICA_APP_URL` | – | self-hosted Multica |
| `MULTICA_DAEMON_ID`, `MULTICA_DAEMON_DEVICE_NAME`, `MULTICA_AGENT_RUNTIME_NAME`, `MULTICA_DAEMON_MAX_CONCURRENT_TASKS` | – | daemon (upstream names) |
| `MULTICA_WORKSPACES_ROOT`, `MULTICA_AGENT_TEMP_BASE` | `/data/workspaces`, `/data/tmp` | |
| `GH_TOKEN`, `GITEA_URL`, `GITEA_TOKEN`, `GITEA_USER` | – | git hosts |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | – | commit identity |
| `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENCODE_CONFIG` | – | agent auth |
| `DIND_MODE` | `auto` (`daemon`) | Docker mode |
| `DIND_DATA_ROOT`, `DIND_START_TIMEOUT`, `DIND_STORAGE_DRIVER`, `DIND_IPTABLES=legacy`, `DIND_DAEMON_JSON` | `/data/docker`, `60` | Docker tuning (`DIND_DAEMON_JSON` is merged into daemon.json) |
| `DOCKERD_ROOTLESS_ROOTLESSKIT_NET` | `slirp4netns` | rootless network driver (`pasta` also installed) |
| `NVIDIA_CDI_ARGS` | – | extra `nvidia-ctk cdi generate` flags |
| `TTYD_PORT`, `TTYD_ARGS` | `7681` | web shell |
| `CHROME_EXTRA_ARGS` | – | extra Chrome flags |

Values equal to `CHANGEME` count as unset.

## Kubernetes

See [docs/kubernetes.md](docs/kubernetes.md): pod security settings per Docker mode, volumes, an
example Deployment with the daemon and the web shell.

## Development

- `Dockerfile`: one file, `--build-arg VARIANT=cpu|cuda`; pinned versions in `ARG`s with
  `# renovate:` comments
- `rootfs/`: entrypoint, libraries, doctor, skeleton, Nix config, agent guide
- `tests/smoke.sh`: run by CI as the image command in three setups (plain, rootful, rootless); with
  Docker it builds a web image, runs it, and tests it with Playwright in Chrome (screenshots are
  uploaded as artifacts); the cuda job also builds a CUDA image
- `.github/workflows/build.yml`: matrix `cpu`/`cuda`, build → smoke tests → push (main only)
- Renovate (`renovate.json`) updates everything (base images, Multica, agent CLIs, Chrome, Docker,
  NVIDIA toolkit, release binaries, actions, majors included) in **one PR** ("all dependencies"),
  automerged immediately (no minimum release age, no PR checks to wait for). The merge to `main`
  triggers CI, which publishes only after the smoke tests pass, so a broken update never replaces
  the published tags
- Pushes that only touch `*.md`, `docs/` or `renovate.json` do not build (`paths-ignore`)

Local build: `docker build --build-arg VARIANT=cpu -t agent-runtime:test .`

## License

MIT
