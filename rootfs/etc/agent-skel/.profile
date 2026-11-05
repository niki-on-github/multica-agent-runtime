# ~/.profile of the agent user (login shells). Seeded once from /etc/agent-skel.
if [ -n "$BASH_VERSION" ] && [ -r "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
