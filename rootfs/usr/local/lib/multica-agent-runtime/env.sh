# shellcheck shell=bash
# Caches and the internal CA. The cache variables are read natively (NIX_CONFIG, PIP_INDEX_URL,
# UV_DEFAULT_INDEX, HF_ENDPOINT, NPM_CONFIG_REGISTRY); this only spreads a custom CA bundle
# (SSL_CERT_FILE) to the tools that read their own variable.
setup_ca_env() {
  local ca=${SSL_CERT_FILE:-}
  [ -n "$ca" ] && [ -r "$ca" ] || return 0
  local v
  for v in NODE_EXTRA_CA_CERTS GIT_SSL_CAINFO REQUESTS_CA_BUNDLE CURL_CA_BUNDLE PIP_CERT NIX_SSL_CERT_FILE; do
    if [ -z "${!v:-}" ]; then
      agent_env "$v" "$ca"
    else
      agent_env "$v" "${!v}"
    fi
  done
  agent_env SSL_CERT_FILE "$ca"
}
