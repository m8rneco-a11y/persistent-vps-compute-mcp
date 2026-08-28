#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${MCP_ENV_FILE:-/etc/mcp-compute.env}"
SERVICE_NAME="${MCP_SERVICE_NAME:-mcp-compute.service}"

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run as root: sudo %s\n' "$0" >&2
  exit 1
fi

if [[ ! -r ${ENV_FILE} ]]; then
  printf 'Environment file not found: %s\n' "${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "${ENV_FILE}"

printf '%-24s %s\n' 'MCP service:' "$(systemctl is-active "${SERVICE_NAME}" 2>/dev/null || true)"
printf '%-24s %s\n' 'MCP autostart:' "$(systemctl is-enabled "${SERVICE_NAME}" 2>/dev/null || true)"
printf '%-24s %s:%s\n' 'Private listener:' "${MCP_HOST:-127.0.0.1}" "${MCP_PORT:-8766}"
printf '%-24s %s\n' 'Local health:' "$(curl -fsS --max-time 5 "http://${MCP_HOST:-127.0.0.1}:${MCP_PORT:-8766}/healthz" 2>/dev/null || printf 'FAILED')"

if command -v caddy >/dev/null 2>&1; then
  printf '%-24s %s\n' 'Caddy:' "$(systemctl is-active caddy 2>/dev/null || true)"
fi

if [[ -n ${MCP_PUBLIC_DOMAIN:-} ]]; then
  printf '%-24s %s\n' 'Public endpoint:' "https://${MCP_PUBLIC_DOMAIN}/mcp"
  public_health="$(curl -fsS --max-time 10 "https://${MCP_PUBLIC_DOMAIN}/healthz" 2>/dev/null || printf 'FAILED')"
  printf '%-24s %s\n' 'Public health:' "${public_health}"
fi

printf '\nRecent service log (secrets are never printed by this script):\n'
journalctl -u "${SERVICE_NAME}" -n 25 --no-pager
