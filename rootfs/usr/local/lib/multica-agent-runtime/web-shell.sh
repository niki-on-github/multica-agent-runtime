#!/usr/bin/env bash
# Session of the web terminal (ttyd --url-arg: `?arg=tmux` in the URL arrives as $1).
#   (default)   plain login shell, one per browser tab. Long lines (OAuth URLs) are soft-wrapped by
#               the browser terminal itself, so clicking or copying them gets the whole URL.
#   ?arg=tmux   persistent tmux session `main`, shared by all tabs and kept across reconnects. tmux
#               redraws wrapped lines row by row, so long URLs are cut at the row end there.
# Anything else in the URL is ignored.
case "${1:-}" in
  tmux) exec tmux new -A -s main ;;
  *) exec bash -l ;;
esac
