# multica-agent-runtime: per-task sandbox plan

Goal: each Multica task runs its agent CLI (Claude, Codex, Pi, OMP) inside its own bubblewrap sandbox in the
runner pod. A task should not be able to read the daemon's tokens or other tasks' files, and should not leave
anything behind that runs in a later task of another agent. Follow-up tasks on the same issue keep their work
directory and can resume their session.

Status: implemented (`AGENT_SANDBOX`, default `off`), steps 1–3 of section 9. Mechanism spike done in the
cluster (7r). Still open: a real runner with real tasks (7, items 2–5 and 1a) and the rollout steps 4–5.

Implementation: `rootfs/usr/local/bin/agent-sandbox` (the wrapper), shims in
`rootfs/usr/local/lib/multica-agent-runtime/sandbox-bin/`, `sandbox.sh` (entrypoint hook, preflight),
a `runtime-doctor` section, `tests/sandbox.sh` (CI, rootful and rootless jobs). How it differs from
the sections below *(impl)*:

- **Env filter by name, not an allowlist.** The daemon merges the agent's custom env into the task
  env, and the wrapper cannot tell them apart. So everything passes except names that look secret
  (`TOKEN`, `SECRET`, `PASSW`, `PRIVATE`, `CREDENTIAL`, `COOKIE`, `API_KEY`, `ACCESS_KEY`, `AUTH_KEY`, `*_KEY`,
  `*_PAT`), unless listed in `AGENT_SANDBOX_PASS_ENV`. `MULTICA_TOKEN` passes only as the task's `mat_`
  token.
- **`env -i` before `bwrap`**, not only `--clearenv`. With `--unshare-pid`, pid 1 inside is a fork of
  `bwrap` and keeps `bwrap`'s environment, so an unfiltered `bwrap` would leave every secret readable in
  `/proc/1/environ` inside. Nothing is passed with `--setenv` either: it would show in `/proc/1/cmdline`.
- **Daemon temp files.** The daemon writes Claude's `--mcp-config`, the Pi MCP extension and the omp
  skills with `os.MkdirTemp("")`, i.e. in *its* `/tmp`. The wrapper binds every `/tmp/<dir>` named in
  the arguments, and the parent dir of argument paths below `/data` or `~/.multica`. Pi's `--session`
  file lives in `~/.multica/pi-sessions/`, so that dir is shared by all Pi tasks (transcripts only).
- **Task root.** It binds `<workspaces>/<ws>/<task>/` (the parent of `workdir`): `codex-home`, the Claude
  `--settings` file, output files live there.
- **Repo cache.** It binds `.repos/<workspace id>` as a whole, not one bare repo: checkouts made during
  the task create new bare repos there.
- **Runtime-installed CLIs.** If the real binary is below `~/.local`, `~/.local` is bound ro.
- **Claude file login.** `.credentials.json` is a single-file bind (rw), so a refresh by rename would
  fail (7r). The recommendation stays `CLAUDE_CODE_OAUTH_TOKEN` via `AGENT_SANDBOX_PASS_ENV`. Then the
  file is not mounted.
- **Probes** (no `MULTICA_TASK_ID`): the home is a tmpfs at `/home/agent`, the daemon's `MULTICA_TOKEN`
  is dropped, `models.*` of Pi/omp are bound (model discovery). Claims about Multica were checked against the v0.8.1 source (the version
pinned in the `Dockerfile`) and master `079ec9f` (2026-11-11), not yet in a live pod.

Multica version: no need to move to master. Between v0.8.1 and master (144 commits, v0.8.2 released)
nothing changed that this plan depends on: launch path, `MULTICA_*_PATH` probing, env inheritance
(`buildEnv`) and the checkout layout are the same. Take v0.8.2 through Renovate as usual. Master has no
setting to choose the checkout layout either: `repoCheckoutModeFor` (`internal/daemon/daemon.go`) gives
the standalone clone (`.git` inside the work dir) to Codex on Linux/Windows only, hard-coded.

## 1. Problem (today)

Each task is a child process of the daemon. It runs as the same uid 1000, with the same `HOME` and network.
`pkg/agent/claude.go: buildEnv` = `mergeEnv(os.Environ(), extra)`: **every task inherits the daemon's full
environment**. Only `MULTICA_TOKEN` is replaced by the task-scoped `mat_` token.

| A task can reach | Why |
|---|---|
| `GH_TOKEN`, `GITEA_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`, … | inherited environment (no `/proc` trick needed) |
| the daemon's own `MULTICA_TOKEN` | `/proc/<daemon pid>/environ`, same uid, `procMount: Unmasked` |
| other running tasks' work dirs, env, transcripts | same uid, same `/data/workspaces`, same `/proc` |
| every CLI login in `/home/agent` | shared `HOME` on the PVC |
| persistence into later tasks | `~/.bashrc`, `~/.claude/settings.json`, `~/.config`, `/nix/store`, git hooks |
| all containers and volumes | one rootless `dockerd`, socket in `/run/agent` |

## 2. Hook point

`pkg/agent/launch.go` is the only place that starts a runtime CLI. The executable comes from
`MULTICA_CLAUDE_PATH`, `MULTICA_CODEX_PATH`, `MULTICA_PI_PATH`, `MULTICA_OMP_PATH` (`agents_probe.go`). Point
these at shims that `exec` the real CLI inside `bwrap`. No fork of Multica.

What the wrapper gets from the daemon:

- cwd = the task work dir
- env: `MULTICA_AGENT_ID`, `MULTICA_TASK_ID`, `MULTICA_WORKSPACE_ID`, `MULTICA_TASK_CONFIG_ROOT`,
  `MULTICA_TASK_WORKSPACES_ROOT`, `MULTICA_DAEMON_PORT`, `TMPDIR`, task `MULTICA_TOKEN`, and for Codex a
  per-task `CODEX_HOME` inside the task root
- stdin/stdout carry the agent protocol and pass through unchanged
- version/model probes run from an empty temporary dir with no task env (`withProbeDirectory`)

## 3. Follow-up tasks: identity and data reuse

No new identifier and no hash. Multica already keys everything we need:

| State | Key Multica uses | Sandbox rule |
|---|---|---|
| work dir | per issue: the server sends `PriorWorkDir`, the daemon reuses that dir (`execenv.Reuse`) | bind `$PWD`; a follow-up gets the same cwd, so it sees the same files |
| Claude session | `$CLAUDE_CONFIG_DIR/projects/<slug of cwd>`; the **daemon** checks and migrates it before launch (`migrateClaudeWorktreeTranscript`) | bind this one project dir **at the same absolute path** the daemon sees |
| Codex session | `~/.codex/multica-sessions/<ns>/<agent>/<issue>`, linked from the per-task `CODEX_HOME/sessions` | bind `$CODEX_HOME` and the resolved target of `$CODEX_HOME/sessions` at the same path |
| tool caches, `~/.config`, `~/.docker`, shell history | none | per-agent home `/data/agent-homes/<MULTICA_AGENT_ID>`, bound at the same path and used as `HOME` (4a explains why not `/home/agent`) |

Rules that follow from this:

- **Path identity.** The daemon checks whether a session can be resumed by looking in *its own* view of
  the filesystem (`sessionHomeReachable`, `gateResumeToReachableSession`). If the sandbox put session files
  somewhere else, the daemon would silently drop the resume. So session stores are bind-mounted at the same
  path inside the sandbox, never moved to another path.
- **Home per agent, not per issue.** `MULTICA_AGENT_ID` is a stable UUID (sanitize to `[A-Za-z0-9-]`, refuse
  otherwise). Per issue would lose caches and make many homes. Per task would break resume. Isolation between
  issues of the *same* agent comes from separate work dirs; the home is shared within one agent by design.
- **Probes** (no `MULTICA_AGENT_ID`): tmpfs home, nothing persisted.

## 4. Sandbox layout

`/usr/local/lib/multica-agent-runtime/sandbox/run` (one script) plus shims in
`/usr/local/lib/multica-agent-runtime/sandbox/bin/{claude,codex,pi,omp}` (`exec run <real path> "$@"`).
The real path is resolved once by the entrypoint (`/usr/local/bin/…`, or `~/.local/bin/…` for runtime
installs), written to `$XDG_RUNTIME_DIR/sandbox.env`.

| Inside | Source | Mode |
|---|---|---|
| `/usr`, `/etc`, `/opt` (Chrome, Playwright ffmpeg) | image | ro |
| `/nix` | image (single-user store, uid 1000) | rw, shared (see 6, decision B) |
| `/proc` | new proc (`--unshare-pid`) | – |
| `/dev` | `--dev /dev`; `/dev/shm` own tmpfs (Chrome); `/dev/nvidia*` + `/dev/dri` only in the `cuda` image | – |
| `/tmp`, `$XDG_RUNTIME_DIR` | tmpfs | rw |
| `/data/agent-homes/<agent>` (= `HOME`) | same path | rw |
| `/home/agent` | tmpfs, holding only `.claude` → `/data/agent-homes/<agent>/.claude` (keeps `CLAUDE_CONFIG_DIR=/home/agent/.claude` valid) | rw |
| `$PWD`, `$TMPDIR`, `$MULTICA_TASK_CONFIG_ROOT`, `$CODEX_HOME` | same path | rw, each only if below `/data/workspaces` or `/data/tmp`, else refuse |
| Claude project dir, Codex session store (3) | same path | rw |
| `/data/workspaces/.repos/…` the work dir's worktree points at | same path | rw (see 6, risk) |
| CLI login, only the one this CLI needs | env (`AGENT_SANDBOX_PASS_ENV`) or the one credential file, same path | rw (see 6, decision C) |
| Docker socket `/run/agent/docker.sock` | same path, `DOCKER_HOST` passed | rw, **always** (4a) |

Flags: `--die-with-parent --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try --new-session
--clearenv` then `--setenv` for an allowlist (`PATH`, `HOME`, `LANG`, `TERM`, `TMPDIR`/`TMP`/`TEMP`, all
`MULTICA_*` the daemon set for the task, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_TMPDIR`, CA and cache
variables from `env.sh`, `NIX_CONFIG`, `DISABLE_AUTOUPDATER`, the agent's custom env minus a denylist).
Network is shared (pod network): the daemon port on localhost, the backend, MCP servers and caches keep
working. `multica repo checkout` is an HTTP call to the daemon (`/repo/checkout`, task token checked), so the
checkout happens outside and shows up inside through the bound work dir.

## 4a. Docker inside the sandbox

Requirement: Docker works in every task as it does today (build, run, compose, buildx, Testcontainers, GPU
via CDI). Isolation between tasks on the Docker side is not a goal: all tasks share the one `dockerd`.

- **Socket**: both DinD modes (rootless and rootful) listen on `unix:///run/agent/docker.sock`. The wrapper
  binds that one file into the sandbox's private `/run/agent` tmpfs at the same path and passes
  `DOCKER_HOST`. With `DIND_MODE=off`, or when the socket is missing, nothing is bound (no error).
  Rootful: the socket is group `docker`. Check that the supplementary group still grants access inside the
  user namespace (it should: the kernel checks the real credentials).
- **`dockerd` runs outside the sandbox.** It resolves bind-mount sources (`-v`, compose `volumes:`,
  `--mount`) in the **pod's** filesystem view, not the sandbox's. So every path a task may pass to Docker
  must be the same inside and outside:
  - work dir, `$TMPDIR`, `$CODEX_HOME`: same path already
  - `HOME`: the reason `HOME` is `/data/agent-homes/<agent>` and not `/home/agent`. Otherwise
    `-v ~/x:/x` would mount the runner's real home (its logins) into the container.
  - `/tmp` inside the sandbox is private, so `docker run -v /tmp/x:…` mounts the pod's `/tmp`, which is a
    different directory. Point `TMPDIR` users at `$TMPDIR` (the agent guide says so); acceptable.
- **Escape through Docker**: a task with the socket can start a container that mounts `/home/agent` or
  `/data/workspaces` and read every login and work dir. Known and accepted (Docker isolation is not a goal).
  In rootless mode the reach stays uid 1000 in the pod: no node root. Rootful: Docker access = node root,
  as already noted in `PLAN.md` 13. So the sandbox protects against careless or confused agents and
  prompt-injected file reads; against a task that deliberately uses Docker to escape, it does not.
  Hardening later, if wanted: a socket proxy that rejects bind mounts outside `/data/workspaces/<task>`
  (e.g. an allowlist proxy in front of `docker.sock`).
- **Ports**: published ports (`-p 127.0.0.1:8080:8080`) end up on the pod's localhost (rootlesskit port
  driver). The sandbox shares the pod network, so `curl`, Playwright and Chrome reach them as today.
- **Client state**: `~/.docker` (buildx builders, contexts, `config.json` registry logins) is in the
  per-agent home. Registry mirrors and CDI live in `daemon.json` on the daemon side, so they are unchanged.
  Registry logins done by the runner (`docker login` in the shell container) are not visible in tasks; if
  needed, seed `~/.docker/config.json` ro per agent.
- **GPU**: `--device nvidia.com/gpu=all` is resolved by `dockerd` from the CDI spec outside, so it works
  without binding `/dev/nvidia*` into the sandbox. Bind those only for tasks that use the GPU directly
  (`cuda` image).
- **Nested userns** stays allowed (no `--disable-userns`): Chrome's own sandbox (`chrome-wrapper.sh` probes
  `unshare --user`) and rootless podman inside a task keep working.

Per-agent home, first use: seed from `/etc/agent-skel`, then run `register_browser_mcps` with that `HOME`
(Pi/OMP read `~/.pi/agent/mcp.json`, `~/.omp/agent/mcp.json`); `~/.pi/agent/models.json` ro-bound from the
runner home. Under `flock` per agent, so two concurrent tasks of one agent do not seed twice.

## 5. Entrypoint and image

- `AGENT_SANDBOX=off|on|required` (default `off` until 9.3). `on`: if `bwrap` cannot create a namespace at
  startup (preflight `bwrap --unshare-pid true`), log and run unsandboxed. `required`: refuse to start the
  daemon.
- `on`/`required`: export `MULTICA_{CLAUDE,CODEX,PI,OMP}_PATH` to the shims, unless already set by the user.
- `bubblewrap` is already in the image (`Dockerfile`, apt list): 0.11.1, all flags used here work (7r).
- `AGENT_SANDBOX_PASS_ENV`: comma-separated variable names passed from the daemon's environment into the
  sandbox. A runner-wide default in the pod env (e.g. `CLAUDE_CODE_OAUTH_TOKEN`), and per agent in the
  agent's custom env (e.g. `GH_TOKEN` for agents that push). Per agent extends the runner default; it does not replace it.
  Names starting with `MULTICA_` are refused (the daemon's own token stays out; the task token is passed anyway).
- `runtime-doctor`: sandbox mode, preflight result, per-agent homes count/size, which shims are active.
- `docs/kubernetes.md`: no new pod permissions (rootless DinD already needs userns, `Unconfined`, unmasked
  `/proc`); `/data/agent-homes` lives on the data PVC, no backup.

## 6. Decisions and risks

**A. Git credentials in tasks** (decided: per-agent opt-in). Today every task has `GH_TOKEN`. `--clearenv`
removes it, so agents can't push. `AGENT_SANDBOX_PASS_ENV=GH_TOKEN` (or `GITEA_TOKEN`) in an agent's custom
env passes it for that agent only. Later: fine-grained / per-repo tokens per agent.

**B. `/nix`** (decided: rw, shared). Single-user Nix writes `/nix/store` as uid 1000, and `nix shell` must
work in tasks. Bound rw like today. Accepted: a task can change store paths that other tasks and the daemon
use (persistence through the store). Same as today, not made worse. Later, if wanted: overlay per agent or
a nix daemon outside the sandbox.

**C. CLI logins and token refresh** (decided: env token for Claude, rw credential file otherwise). A task
must be able to read its own CLI's credential, so that one is never hidden. The goal is that it cannot read the
*other* CLIs' credentials, the daemon token or the git tokens.

Two kinds of Claude login, and how refresh works for each:

| Login | Where | Refresh |
|---|---|---|
| `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (recommended) | env, via `AGENT_SANDBOX_PASS_ENV` | **none.** A long-lived token (about a year; check the expiry date when it is created). Claude never writes it. When it expires, run `claude setup-token` again and update the runner secret. `runtime-doctor` reports when the token is rejected. |
| `claude /login` → `$CLAUDE_CONFIG_DIR/.credentials.json` | file | short-lived access token plus refresh token. Claude refreshes it itself and **writes the file**, so the file must be rw. |

So with the env token, nothing in the sandbox needs to write a credential, and `.credentials.json` isn't
mounted at all. If the runner uses the file login instead: bind `/home/agent/.claude/.credentials.json` rw at
the same path. All tasks then share one file, as today; a refresh in one task updates it for all.

Codex: the daemon puts a **symlink** `$CODEX_HOME/auth.json → /home/agent/.codex/auth.json` into each
per-task home (`codexSymlinkedFiles` in `execenv/codex_home.go`), so that refreshes reach every task. In the
sandbox, bind `/home/agent/.codex/auth.json` rw at the same path, so the link resolves and refreshes write
through. With an API key (`OPENAI_API_KEY` in `AGENT_SANDBOX_PASS_ENV`) there's no file and no refresh.
Pi/OMP: `~/.pi/agent/auth.json`, `~/.omp/agent/auth.json` rw at the same path, or the provider's API key
variable via `AGENT_SANDBOX_PASS_ENV`.

Single-file bind mounts break atomic writes: a CLI that writes a temp file and `rename()`s it over the
target gets `EBUSY`, because a mount point cannot be replaced. Test each CLI's refresh in 7. If one renames,
bind the containing directory instead, with only that file in it. For that the entrypoint moves the file to
`/home/agent/.sandbox-creds/<cli>/` and leaves a symlink at the old path.

**D. Claude transcripts.** Bind only `projects/<slug($PWD)>`. If the slug rule differs from "cwd with every
non-alphanumeric char → `-`", or the daemon writes elsewhere, fall back to setting `CLAUDE_CONFIG_DIR` per agent in
the agent's custom env (not on the daemon's blocklist, so the daemon sees the same path).

Risks:

- **Repo cache.** Worktrees point at `/data/workspaces/.repos/<…>`, which must be rw for commits. Tasks of
  one Multica workspace still share it, and a task could set `core.hooksPath` or drop hooks there. Mitigation: bind only the
  one bare repo the worktree's `.git` file names (resolved at launch; repos checked out mid-task fall back to the
  workspace's `.repos` dir), and a daemon-side check (doctor) for hooks/`core.hooksPath` in the cache.
  The clean fix is upstream: a daemon setting (e.g. `MULTICA_REPO_CHECKOUT_MODE=isolated`) that makes
  `repoCheckoutModeFor` return the standalone clone for every provider, as it already does for Codex. Small
  PR; then the cache never needs to be bound.
- Same kernel, uid and pod network for all tasks: no per-task egress filter (`--unshare-net` + pasta + proxy
  allowlist is a later step).
- Docker socket in every task: bypasses the file isolation on purpose (4a).
- Chrome inside a nested userns: `chrome-wrapper.sh` flags (`--no-sandbox` or not) must still work.
- Cancellation: `--die-with-parent` must kill the agent and its children when the daemon kills the run.

## 7. Spike (manual, before writing the real script)

In a runner pod (or `docker run` with the rootless flags from `tests/smoke.sh`), with a hand-written wrapper:

1. `bwrap --version`; `bwrap --unshare-pid --proc /proc true`; `nix shell nixpkgs#hello` inside
1a. token refresh: Claude with `CLAUDE_CODE_OAUTH_TOKEN` only (no `.credentials.json` visible) runs a task;
   Codex with the ChatGPT login: force a refresh (an old `auth.json`, or wait past the expiry) and check
   that `/home/agent/.codex/auth.json` was updated outside and nothing logged `EBUSY`. The same for a Claude
   file login, in case the runner uses one.
2. log the env and cwd the daemon passes (wrapper dumps it to a file): confirm the table in 2
3. one Claude and one Codex task on an issue, then a follow-up comment: same work dir? session resumed (daemon
   log has no `dropping prior session`)?
4. `multica repo checkout` inside: worktree's `.git` path, commit + push
5. Pi/OMP with MCP servers; Chrome via playwright MCP
6. cancel a running task: no processes left (`ps` in the pod)
6a. Docker from a sandboxed task, both DinD modes: `docker info`; `docker build` in the work dir;
   `docker run -v "$PWD:/src"` and `-v "$HOME/x:/x"` see the right files; `docker compose up` with relative
   volumes; `-p 127.0.0.1:18080:8080` reachable with `curl` and Playwright; buildx; Testcontainers (Ryuk);
   `cuda`: `docker run --device nvidia.com/gpu=all … nvidia-smi`; a cancelled task leaves its containers
   running (expected: the sandbox does not own them; note it in GUIDE.md)
7. escape checks from inside (become tests in 8): no daemon pid, no `GH_TOKEN`, no other work dir, no
   `/home/agent/.claude/.credentials.json` of the runner, writes to `~/.bashrc` absent in another agent's home

### 7r. Spike results (2026-11-10, cluster `ai-cluster`)

Run through the Flux MCP. It cannot exec into pods, so the test ran as a separate pod `sbx-spike` in `multica`
with the runner's image (`v0.8.1-cpu-96477ff`), the same security context (`hostUsers: false`,
`SYS_ADMIN`, seccomp/AppArmor `Unconfined`, `procMount: Unmasked`, `squat.ai/tun`) and the entrypoint
with `DIND_MODE=rootless`. It used emptyDirs, no PVCs and no secrets, and was deleted afterwards. The wrapper
was the `bwrap` call from section 4. The Multica runner state (fake daemon process, other task's work dir,
runner login, bare cache + worktree) was simulated, not real.

Environment: kernel 6.18.48, **bubblewrap 0.11.1**, pod userns of 65536 ids, rootless Docker 29.9.3.

| Check | Result |
|---|---|
| sandbox starts in the pod; nested userns (Chrome's sandbox) | ok |
| daemon pid / its `/proc/*/environ` from inside | not visible |
| process list inside | only `bwrap` (pid 1) and the command |
| `GH_TOKEN` exported outside | not inherited (`--clearenv`) |
| other task's work dir, runner's `.credentials.json`, `~/.multica`, `~/.bashrc` | not visible |
| own work dir, `$TMPDIR`, per-agent `HOME` at the same path; writes persist there, not in the runner home | ok |
| `claude`, `codex`, `pi`, `omp`, `multica` `--version` | ok |
| `/nix` rw: `nix-store --add`, `nix shell nixpkgs#hello` | ok |
| worktree whose cache is not bound | `fatal: not a git repository` (as expected) |
| worktree with the bare repo bound | status + commit ok |
| credential: single-file bind, write in place | ok, visible outside |
| credential: single-file bind, `rename()` over it | **fails** (as expected): directory bind needed for CLIs that rename |
| credential: directory bind, `rename()` | ok, visible outside |
| Docker: `info`, `run`, `build`, `compose` with a relative volume, `buildx` | ok |
| Docker bind mounts of `$PWD`, `$HOME`, `$TMPDIR` | ok (same-path rule works) |
| published port `127.0.0.1:…`: started outside or inside, reached from inside or outside, `curl` and Chrome | ok |
| Docker escape (`-v /home/agent:/h`) reads the runner login | yes (known and accepted, 4a) |
| headless Chrome inside | ok |
| cancel: parent of the wrapper `SIGKILL`ed, or `bwrap` `TERM`ed | sandboxed process gone |

Found in the spike:

- `PATH` contains `/home/agent/.local/bin` and `/home/agent/.nix-profile/bin`, both hidden in the sandbox.
  A CLI installed at runtime into `~/.local` (`PLAN.md` 3, *impl*) would not start. The wrapper must
  resolve the real binary outside and ro-bind its install dir (e.g. `~/.local/share/claude`) at the same
  path. Packages installed with `nix profile install` in the runner home are not visible inside; inside, `nix profile`
  works against the per-agent home.

Still open (needs the real daemon and logins, so it can't be done from a throwaway pod): items 2–5 and 1a
with real tasks: env as passed by the daemon, session resume on a follow-up, `multica repo checkout` path,
MCP servers via Multica's config, how each CLI writes its credential on refresh (in place or rename). Next
step: a second runner (own `MULTICA_DAEMON_ID`, its own home/data PVCs) with the wrapper enabled.

Separate finding: the runner pods carry `GH_TOKEN`, `MULTICA_TOKEN` and `LLM_API_KEY` as plain `value:`
env entries in the pod spec (readable by anyone who can `get pods` in `multica`). Move them to `secretKeyRef`
and rotate them.

## 8. Tests

`tests/smoke.sh`, rootless job (namespaces available there): fake task env + a stub "agent" script run through
the shim. It asserts the escape checks of 7.7, the probe case (no agent id, tmpfs home), path refusal outside
`/data`, `--die-with-parent` (kill the wrapper, child gone). Docker through the shim in both DinD jobs: the existing
end-to-end test (build → run → Playwright against the published port) runs once more from inside the sandbox,
plus a `-v "$PWD:/src"` mount check. Plus `AGENT_SANDBOX=required` fails fast when
`bwrap` cannot create a namespace (rootful/`privileged` job without userns: check, don't assume).

## 9. Order

1. spike (7), write findings back here
2. `sandbox/run` + shims + entrypoint switch + doctor, default `off`
3. tests (8)
4. cluster: `AGENT_SANDBOX=on` on `runner`, one week of real tasks, watch resume and checkout
5. default `on`, then `required` in the cluster; set `AGENT_SANDBOX_PASS_ENV` per agent (decision A)
6. later: per-task egress filter, per-agent `/nix` overlay or nix daemon, per-agent git tokens, Docker socket
   proxy (4a), upstream PR for the checkout-mode setting (6)
