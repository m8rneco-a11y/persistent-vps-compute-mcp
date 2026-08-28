#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ENV_FILE="${MCP_ENV_FILE:-/etc/mcp-compute.env}"
SERVICE_NAME="${MCP_SERVICE_NAME:-mcp-compute.service}"

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run as root: sudo %s\n' "$0" >&2
  exit 1
fi

if [[ ! -f ${ENV_FILE} ]]; then
  printf 'Environment file not found: %s\n' "${ENV_FILE}" >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || {
  printf 'openssl is required\n' >&2
  exit 1
}

backup="${ENV_FILE}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
trap 'rm -f -- "${temporary}"' EXIT

cp -a -- "${ENV_FILE}" "${backup}"
new_token="$(openssl rand -hex 32)"

awk -v replacement="MCP_TOKEN=${new_token}" '
  BEGIN { replaced = 0 }
  /^MCP_TOKEN=/ {
    if (!replaced) {
      print replacement
      replaced = 1
    }
    next
  }
  { print }
  END {
    if (!replaced) print replacement
  }
' "${ENV_FILE}" >"${temporary}"

install -o root -g root -m 0600 "${temporary}" "${ENV_FILE}"
systemctl restart "${SERVICE_NAME}"
systemctl is-active --quiet "${SERVICE_NAME}"

printf 'Bearer token rotated. Previous file: %s\n' "${backup}"
printf 'All MCP clients must be updated. Show the new bare token with:\n'
printf '  sudo /opt/mcp-compute/scripts/show-token.sh\n'
