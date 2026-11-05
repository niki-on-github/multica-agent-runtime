# shellcheck shell=bash
# git identity and credentials from env, re-applied on every start (no tokens in git config files:
# the credential helpers read the variables at use time).
setup_git() {
  git config --global init.defaultBranch main
  if is_set GIT_AUTHOR_NAME; then git config --global user.name "$GIT_AUTHOR_NAME"; fi
  if is_set GIT_AUTHOR_EMAIL; then git config --global user.email "$GIT_AUTHOR_EMAIL"; fi

  if is_set GH_TOKEN; then
    # git over https://github.com through gh (reads GH_TOKEN)
    gh auth setup-git --hostname github.com >/dev/null 2>&1 || warn "gh auth setup-git failed"
  fi

  if is_set GITEA_URL && is_set GITEA_TOKEN; then
    local url=${GITEA_URL%/}
    # Gitea accepts a personal access token as the password; the user name is not checked
    # shellcheck disable=SC2016
    git config --global "credential.$url.helper" \
      '!f() { test "$1" = get && printf "username=%s\npassword=%s\n" "${GITEA_USER:-git}" "$GITEA_TOKEN"; }; f'
    tea login delete gitea >/dev/null 2>&1 || true
    if tea login add --name gitea --url "$url" --token "$GITEA_TOKEN" >/dev/null 2>&1; then
      tea login default gitea >/dev/null 2>&1 || true
    else
      warn "tea login add failed for $url"
    fi
  fi

  # optional SSH key/config from a mounted secret
  local ssh_src=${AGENT_SSH_DIR:-/etc/agent-ssh}
  if [ -d "$ssh_src" ] && [ -n "$(ls -A "$ssh_src" 2>/dev/null)" ]; then
    install -d -m 0700 "$HOME/.ssh"
    local f
    for f in "$ssh_src"/*; do
      [ -f "$f" ] || continue
      install -m 0600 "$f" "$HOME/.ssh/$(basename "$f")"
    done
  fi
}
