#!/bin/sh
# Chrome for Testing at the default Chrome path (/opt/google/chrome/chrome), used by Playwright
# (channel "chrome"), chrome-devtools-mcp and puppeteer.
# - no usable user-namespace sandbox (seccomp/AppArmor): add --no-sandbox instead of failing
# - small /dev/shm (container default 64 MiB): add --disable-dev-shm-usage
# - CHROME_EXTRA_ARGS: extra flags, split on whitespace
# shellcheck disable=SC2086
set -- ${CHROME_EXTRA_ARGS:-} "$@"
if ! unshare --user true 2>/dev/null; then
  set -- --no-sandbox "$@"
fi
shm_kb=$(df -Pk /dev/shm 2>/dev/null | awk 'NR==2 {print $2}')
if [ "${shm_kb:-0}" -lt 1048576 ]; then
  set -- --disable-dev-shm-usage "$@"
fi
exec /opt/chrome-for-testing/chrome "$@"
