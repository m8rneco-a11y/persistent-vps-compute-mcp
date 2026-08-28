#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="mcp-compute.service"
INSTALL_DIR="/opt/mcp-compute"
ENV_FILE="/etc/mcp-compute.env"
SERVICE_FILE="/etc/systemd/system/mcp-compute.service"
STATE_DIR="/var/lib/mcp-compute"
LOG_DIR="/var/log/mcp-compute"
CADDY_MAIN="/etc/caddy/Caddyfile"
CADDY_SNIPPET="/etc/caddy/Caddyfile.d/mcp-compute.caddy"
IMPORT_BEGIN="# BEGIN persistent-vps-compute installer"
IMPORT_END="# END persistent-vps-compute installer"
ASSUME_YES=0
PURGE=0

usage() {
  cat <<'EOF'
Usage: sudo bash uninstall.sh [--yes] [--purge]

Without --purge, the Bearer token, audit state, and logs are preserved.
Projects created through the MCP are never removed by this script.
EOF
}

while (($#)); do
  case "$1" in
    --yes|-y) ASSUME_YES=1 ;;
    --purge) PURGE=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run as root: sudo bash uninstall.sh\n' >&2
  exit 1
fi

cat <<EOF
This will stop and remove ${SERVICE_NAME}, ${INSTALL_DIR}, and its Caddy snippet.
Projects, Docker containers, databases, and unrelated services will not be touched.
$([[ ${PURGE} -eq 1 ]] && printf 'The token, MCP state, and MCP logs will also be deleted.' || printf 'The token, MCP state, and MCP logs will be preserved.')
EOF

if ((ASSUME_YES == 0)); then
  printf 'Continue? [y/N]: '
  read -r answer
  [[ ${answer,,} == y || ${answer,,} == yes ]] || exit 0
fi

systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
rm -f -- "${SERVICE_FILE}"
systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true

rm -f -- "${CADDY_SNIPPET}"
if [[ -f ${CADDY_MAIN} ]] && ! compgen -G '/etc/caddy/Caddyfile.d/*.caddy' >/dev/null; then
  temporary="$(mktemp /etc/caddy/Caddyfile.tmp.XXXXXX)"
  awk -v begin="${IMPORT_BEGIN}" -v end="${IMPORT_END}" '
    $0 == begin { skipping = 1; next }
    $0 == end { skipping = 0; next }
    !skipping { print }
  ' "${CADDY_MAIN}" >"${temporary}"
  install -o root -g root -m 0644 "${temporary}" "${CADDY_MAIN}"
  rm -f -- "${temporary}"
fi

if command -v caddy >/dev/null 2>&1 && caddy validate --config "${CADDY_MAIN}" >/dev/null 2>&1; then
  systemctl reload caddy 2>/dev/null || true
fi

rm -rf -- "${INSTALL_DIR}"

if ((PURGE == 1)); then
  rm -f -- "${ENV_FILE}"
  rm -rf -- "${STATE_DIR}" "${LOG_DIR}"
fi

printf 'Persistent VPS Compute MCP removed. Unrelated projects and services were preserved.\n'
