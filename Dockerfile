# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# hadolint global ignore=DL3008,DL3016,DL4006,SC2016
#
# Multica agent runtime: the Multica daemon with Claude Code, OpenCode and Codex, Docker-in-Docker,
# Nix, Chrome for Testing + Playwright, gh and tea. VARIANT picks the base:
#   cpu   Ubuntu 26.04
#   cuda  nvidia/cuda runtime (same Ubuntu) + nvidia-container-toolkit for GPU containers inside
# Layers go from rarely to often changing: apt, Docker, Chrome, Nix/Node, release binaries, browser
# npm packages, agent CLIs (almost daily), Multica.

ARG VARIANT=cpu

# Multica CLI/daemon: static Go binary of the backend image; its tag is the image version.
FROM ghcr.io/multica-ai/multica-backend:v0.8.0@sha256:5e13e9ef8a2ee07e6768ae97a0a285bc5af735c520fe5b1a703fe6ced4b658e9 AS multica
# Nix: the official image ships a complete single-user store in /nix.
FROM docker.io/nixos/nix:2.35.4@sha256:f159a58f390677f41e8e900a74b6e2f9c7f27b364122ae6acf7a652ce6c551c0 AS nix
FROM docker.io/library/node:26.13.0-bookworm-slim@sha256:add5c17c14752c54d2f3d519ec46a3815684d41c33f198bcb09fb998f7dcbf68 AS node
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM docker.io/library/ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c AS base-cpu
# keep both on the same Ubuntu release (the Docker apt repo follows the base's codename)
FROM docker.io/nvidia/cuda:13.4.3-runtime-ubuntu26.04@sha256:83b873636b7ad03b418984f38f6b8a09e6f3c3a105d5cdab8088e998e8ed93f1 AS base-cuda

# hadolint ignore=DL3006
FROM base-${VARIANT}
ARG VARIANT
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Etc/UTC

# --- OS packages -----------------------------------------------------------------------------------
RUN apt-get update; \
  apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg less file procps psmisc tini util-linux bash-completion tzdata \
    git git-lfs openssh-client \
    vim tmux \
    jq yq ripgrep fd-find rsync sqlite3 zip unzip xz-utils zstd \
    build-essential pkg-config python3 python3-venv python3-pip \
    bubblewrap socat \
    dnsutils iputils-ping netcat-openbsd iproute2 strace \
    uidmap slirp4netns passt fuse-overlayfs iptables \
    xvfb xauth fonts-liberation fonts-noto-color-emoji fonts-dejavu-core; \
  ln -s /usr/bin/fdfind /usr/local/bin/fd; \
  install -m 0755 -d /etc/apt/keyrings; \
  rm -rf /var/lib/apt/lists/*

# --- Docker engine (rootful + rootless) -------------------------------------------------------------
# renovate: datasource=github-releases depName=moby/moby extractVersion=^docker-v(?<version>.+)$
ARG DOCKER_VERSION=29.9.3
RUN . /etc/os-release; \
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; \
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list; \
  apt-get update; \
  v="5:${DOCKER_VERSION}-1~ubuntu.${VERSION_ID}~${VERSION_CODENAME}"; \
  apt-get install -y --no-install-recommends \
    "docker-ce=$v" "docker-ce-cli=$v" "docker-ce-rootless-extras=$v" \
    containerd.io docker-buildx-plugin docker-compose-plugin; \
  rm -rf /var/lib/apt/lists/*; \
  docker --version; docker buildx version; docker compose version; rootlesskit --version

# --- NVIDIA container toolkit (cuda only): GPU access for nested containers ------------------------
# renovate: datasource=github-releases depName=NVIDIA/nvidia-container-toolkit extractVersion=^v(?<version>.+)$
ARG NVIDIA_CTK_VERSION=1.20.1
RUN if [ "$VARIANT" != cuda ]; then exit 0; fi; \
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit.gpg; \
  echo 'deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH) /' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list; \
  apt-get update; \
  v="${NVIDIA_CTK_VERSION}-1"; \
  apt-get install -y --no-install-recommends \
    "nvidia-container-toolkit=$v" "nvidia-container-toolkit-base=$v" \
    "libnvidia-container-tools=$v" "libnvidia-container1=$v" nvtop; \
  rm -rf /var/lib/apt/lists/*; \
  # rootless dockerd cannot manage device cgroups; CDI (--device nvidia.com/gpu=all) works in both modes.
  # mode=cdi: --runtime=nvidia / NVIDIA_VISIBLE_DEVICES use the CDI spec the entrypoint generates (and
  # cleans, see gpu.sh) instead of nvidia-container-cli, which needs host privileges.
  nvidia-ctk config --in-place --set nvidia-container-cli.no-cgroups \
    --set nvidia-container-runtime.mode=cdi \
    --set nvidia-container-runtime.modes.cdi.spec-dirs=/etc/cdi:/var/run/cdi:/run/agent/cdi; \
  grep -qE '^mode = "cdi"' /etc/nvidia-container-runtime/config.toml; \
  nvidia-ctk --version

# --- Chrome for Testing ----------------------------------------------------------------------------
# Archived per version (unlike the google-chrome .deb), no auto-update. Installed behind the default
# Chrome path /opt/google/chrome/chrome, so Playwright (channel "chrome") and chrome-devtools-mcp find
# it without configuration. deb.deps in the archive lists the runtime libraries.
# renovate: datasource=custom.chrome-for-testing depName=chrome-for-testing versioning=loose
ARG CHROME_VERSION=157.0.8103.38
COPY rootfs/usr/local/lib/multica-agent-runtime/chrome-wrapper.sh /tmp/chrome-wrapper.sh
RUN curl -fsSL -o /tmp/chrome.zip \
    "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip"; \
  unzip -q /tmp/chrome.zip -d /opt; \
  mv /opt/chrome-linux64 /opt/chrome-for-testing; \
  mapfile -t wanted < <(sed -E 's/ *\|.*//; s/ *\(.*\)//' /opt/chrome-for-testing/deb.deps | sort -u); \
  apt-get update; \
  # deb.deps uses pre-t64 names; since Ubuntu 24.04 several are virtual (libasound2 -> libasound2t64)
  deps=(); \
  for d in "${wanted[@]}"; do \
    if [ "$(apt-cache policy "$d" | sed -n 's/ *Candidate: //p')" != "(none)" ]; then deps+=("$d"); continue; fi; \
    p=$(apt-cache showpkg "$d" | sed -n '/^Reverse Provides:/,$p' | awk 'NR > 1 {print $1}' | grep -m1 t64 || true); \
    [ -n "$p" ] || { echo "no package for $d" >&2; exit 1; }; \
    deps+=("$p"); \
  done; \
  apt-get install -y --no-install-recommends "${deps[@]}"; \
  rm -rf /var/lib/apt/lists/* /tmp/chrome.zip; \
  install -D -m 0755 /tmp/chrome-wrapper.sh /opt/google/chrome/chrome; \
  rm /tmp/chrome-wrapper.sh; \
  for n in google-chrome google-chrome-stable chrome chromium; do ln -s /opt/google/chrome/chrome "/usr/local/bin/$n"; done; \
  /opt/chrome-for-testing/chrome --version

# --- Nix, Node, uv ---------------------------------------------------------------------------------
# Nix single-user: /nix owned by uid 1000 (the agent), no daemon.
COPY --from=nix --chown=1000:1000 /nix /nix
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
  ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
  node --version; npm --version; uv --version

# --- Release binaries: gh, tea, ttyd ---------------------------------------------------------------
# renovate-digest: depName=cli/cli
ARG GH_VERSION=v2.103.3
ARG GH_SHA256=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
# renovate-digest: depName=tsl0922/ttyd
ARG TTYD_VERSION=1.7.7
ARG TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
# gitea.com publishes a .sha256 per asset; it is checked at build time
# renovate: datasource=gitea-releases depName=gitea/tea registryUrl=https://gitea.com
ARG TEA_VERSION=v0.16.0
WORKDIR /tmp/build
RUN \
  gh_v="${GH_VERSION#v}"; \
  curl -fsSL -o gh.tgz "https://github.com/cli/cli/releases/download/${GH_VERSION}/gh_${gh_v}_linux_amd64.tar.gz"; \
  echo "${GH_SHA256}  gh.tgz" | sha256sum -c -; \
  tar -xzf gh.tgz; \
  install -m 0755 "gh_${gh_v}_linux_amd64/bin/gh" /usr/local/bin/gh; \
  curl -fsSL -o ttyd "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64"; \
  echo "${TTYD_SHA256}  ttyd" | sha256sum -c -; \
  install -m 0755 ttyd /usr/local/bin/ttyd; \
  tea_f="tea-${TEA_VERSION#v}-linux-amd64.xz"; \
  curl -fsSL -o "$tea_f" "https://gitea.com/gitea/tea/releases/download/${TEA_VERSION}/${tea_f}"; \
  curl -fsSL -o "$tea_f.sha256" "https://gitea.com/gitea/tea/releases/download/${TEA_VERSION}/${tea_f}.sha256"; \
  sha256sum -c "$tea_f.sha256"; \
  xz -d "$tea_f"; \
  install -m 0755 "${tea_f%.xz}" /usr/local/bin/tea; \
  rm -rf /tmp/build/*; \
  gh --version; tea --version; ttyd --version
WORKDIR /

# --- Browser automation: Playwright test runner, MCP servers, pnpm ---------------------------------
# @playwright/test brings the `playwright` CLI; @playwright/mcp pins its own playwright-core.
# Browsers dir owned by the agent, so `playwright install <browser>` works at runtime.
# renovate: datasource=npm depName=@playwright/test
ARG PLAYWRIGHT_VERSION=1.64.1
# renovate: datasource=npm depName=@playwright/mcp
ARG PLAYWRIGHT_MCP_VERSION=0.0.84
# renovate: datasource=npm depName=chrome-devtools-mcp
ARG CHROME_DEVTOOLS_MCP_VERSION=1.12.0
# renovate: datasource=npm depName=pnpm
ARG PNPM_VERSION=12.16.1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npm install --global --prefix /usr/local --no-fund --no-audit \
    "@playwright/test@${PLAYWRIGHT_VERSION}" \
    "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
    "chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}" \
    "pnpm@${PNPM_VERSION}"; \
  playwright install ffmpeg; \
  chown -R 1000:1000 /opt/ms-playwright; \
  npm cache clean --force; \
  playwright --version; pnpm --version

# --- Agent CLIs (change almost daily, so near the end) ---------------------------------------------
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.318
# renovate: datasource=npm depName=opencode-ai
ARG OPENCODE_VERSION=1.19.5
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.166.0
# AGENTS: which CLIs to install (the Multica daemon registers one runtime per CLI it finds)
ARG AGENTS="claude opencode codex"
RUN pkgs=(); \
  for a in $AGENTS; do \
    case "$a" in \
      claude) pkgs+=("@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}") ;; \
      opencode) pkgs+=("opencode-ai@${OPENCODE_VERSION}") ;; \
      codex) pkgs+=("@openai/codex@${CODEX_VERSION}") ;; \
      *) echo "unknown agent: $a" >&2; exit 1 ;; \
    esac; \
  done; \
  npm install --global --prefix /usr/local --no-fund --no-audit "${pkgs[@]}"; \
  npm cache clean --force

COPY --from=multica /app/multica /usr/local/bin/multica

# --- User, configuration, scripts ------------------------------------------------------------------
# uid 1000 is `ubuntu` in the base images; replace it with `agent`. Subordinate ids for rootless
# Docker; the entrypoint (when started as root) shrinks them inside a small user namespace.
RUN userdel -r ubuntu 2>/dev/null || true; \
  groupadd -g 1000 agent; \
  useradd -u 1000 -g 1000 -G docker -d /home/agent -s /bin/bash -M agent; \
  echo 'agent:100000:65536' > /etc/subuid; \
  echo 'agent:100000:65536' > /etc/subgid; \
  install -d -o 1000 -g 1000 -m 0755 /home/agent /data; \
  install -d -o 1000 -g 1000 -m 0700 /run/agent; \
  install -d -m 0755 /etc/cdi /etc/nix /etc/multica-agent-runtime

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/runtime-doctor /usr/local/lib/multica-agent-runtime/*.sh; \
  chmod -R a+rX /etc/agent-skel /etc/nix /etc/profile.d /usr/share/doc/multica-agent-runtime; \
  ln -s /usr/local/bin/runtime-doctor /usr/local/bin/doctor; \
  VARIANT="$VARIANT" /usr/local/lib/multica-agent-runtime/versions.sh > /etc/multica-agent-runtime/versions.json; \
  cat /etc/multica-agent-runtime/versions.json

# Set late: npm_config_prefix would redirect the global installs above into the agent's home.
# PATH: the agent's own installs first, Nix appended (the distro tools stay first).
ENV HOME=/home/agent \
    USER=agent \
    PATH=/home/agent/.local/bin:/home/agent/.nix-profile/bin:${PATH}:/nix/var/nix/profiles/default/bin \
    NODE_PATH=/usr/local/lib/node_modules \
    npm_config_prefix=/home/agent/.local \
    npm_config_update_notifier=false \
    XDG_RUNTIME_DIR=/run/agent \
    DOCKER_HOST=unix:///run/agent/docker.sock \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    DISABLE_AUTOUPDATER=1 \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    MULTICA_DAEMON_AUTO_UPDATE=false \
    MULTICA_WORKSPACES_ROOT=/data/workspaces \
    MULTICA_AGENT_TEMP_BASE=/data/tmp \
    EDITOR=vim \
    VISUAL=vim \
    TTYD_PORT=7681 \
    MULTICA_AGENT_RUNTIME_VARIANT=${VARIANT}

USER 1000:1000
WORKDIR /home/agent
# Build-time checks as the agent; tests/smoke.sh covers the runtime environment.
RUN set -eux; \
  HOME="$(mktemp -d)"; export HOME; \
  test -r /etc/nix/nix.conf; \
  nix config show experimental-features | grep -q flakes; \
  nix eval --expr '1 + 1'; \
  multica version; claude --version || true; command -v opencode codex claude || true; \
  rm -rf "$HOME"

EXPOSE 7681
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]

LABEL org.opencontainers.image.title="multica-agent-runtime" \
      org.opencontainers.image.description="Multica agent daemon with Claude Code, OpenCode, Codex, Docker-in-Docker, Nix and Chrome" \
      org.opencontainers.image.licenses="MIT"
