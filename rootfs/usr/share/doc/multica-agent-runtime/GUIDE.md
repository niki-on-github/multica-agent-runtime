# multica-agent-runtime: guide for agents

You run inside a container of the Multica agent runtime. This page is what you can rely on.
`doctor` prints the live state (logins, Docker mode, GPU, caches).

## Layout

| Path | What | Persistent |
|---|---|---|
| `$HOME` (`/home/agent`) | your config, CLI logins, shell history | yes (volume) |
| `/data/workspaces` | Multica task workspaces (repos) | yes |
| `/data/docker` | Docker images, containers, build cache | yes, but treat it as cache |
| `/data/tmp`, `/tmp` | scratch | no |
| `/nix/store` | Nix packages you add | no (comes back fast through the cache) |

You are uid 1000 (`agent`) without sudo. Install things for yourself:

- any tool: `nix shell nixpkgs#go nixpkgs#terraform -c ...` (or `nix profile install nixpkgs#foo`)
- Python: `uv venv && uv pip install ...`, one-off tools `uvx ruff check .`
- Node: project-local `pnpm install` / `npm install`; global installs land in `~/.local`

Package caches (Nix, PyPI, Hugging Face, npm) may be configured through the environment; use the
standard tools and they are used automatically.

## Docker

`docker`, `docker compose` and `docker buildx` talk to a daemon in this container
(`DOCKER_HOST=unix:///run/agent/docker.sock`). `doctor` shows the mode (`rootless` or `rootful`).

- **Publish ports on loopback and test against `127.0.0.1`:** `docker run -d -p 127.0.0.1:8080:8080 ...`.
  In rootless mode container IPs are not reachable from your shell; published ports are.
- Containers do not inherit your environment. Pass what they need (`-e`, `--env-file`), e.g. a CA
  bundle: `-v "$SSL_CERT_FILE:/etc/ssl/certs/ca-certificates.crt:ro"`.
- Clean up what you start: `docker rm -f <name>`, `docker compose down -v`. Disk is shared:
  `docker system prune -f` (add `-a` for unused images) when you are done with large images.
- Rootless limits: nested images whose files belong to very high uids (e.g. 65534) can fail to
  unpack in some clusters; `--privileged` inside rootless Docker gives no host privileges.

## GPU (cuda image)

`nvidia-smi` shows the GPU(s) this container got. Nested containers get them through CDI:

```sh
docker run --rm --device nvidia.com/gpu=all nvidia/cuda:13.4.3-base-ubuntu26.04 nvidia-smi
```

- `--gpus all` works too (nvidia runtime); prefer `--device nvidia.com/gpu=all`.
- compose: `devices: ["nvidia.com/gpu=all"]` on the service.
- Build CUDA code inside images (`FROM nvidia/cuda:<ver>-devel-ubuntu26.04`); this image has the CUDA
  runtime, not the compiler. No GPU is needed to build.
- The GPU is shared with other workloads: keep VRAM use modest, stop GPU containers when done.
- `doctor --gpu` checks the whole chain.

## Browser testing

Chrome for Testing is at the default Chrome path (`/opt/google/chrome/chrome`, also `google-chrome`).

- **MCP** (interactive): Playwright MCP (`playwright-mcp`) and `chrome-devtools-mcp` are installed;
  they find Chrome without extra flags.
- **Scripted**: `@playwright/test` is installed globally (`playwright test`). Use the installed Chrome:

  ```js
  // playwright.config.js
  module.exports = { use: { channel: 'chrome', headless: true } };
  ```

  Projects that pin their own `@playwright/test` also work with `channel: 'chrome'`; without it they
  want their bundled Chromium (`playwright install chromium`, downloads ~150 MB).
- Headed runs: `xvfb-run playwright test --headed`.
- WebGL/canvas render in software (SwiftShader); no GPU needed for the browser.

Typical loop for a web app in a container:

1. `docker build -t app .`
2. `docker run -d --name app -p 127.0.0.1:8080:8080 [--device nvidia.com/gpu=all] app`
3. wait until `curl -fsS http://127.0.0.1:8080/` answers (`docker logs app` if not)
4. test with Playwright (screenshots, traces) or the browser MCP
5. `docker rm -f app`

## Git

- GitHub: `gh` is set up as git credential helper when `GH_TOKEN` is set (`gh pr create` etc.).
- Gitea: `tea` is logged in and git has a credential helper when `GITEA_URL`/`GITEA_TOKEN` are set
  (`tea pr create`, `tea issues`).
