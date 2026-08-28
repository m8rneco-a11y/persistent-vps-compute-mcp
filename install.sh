#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SERVICE_NAME="mcp-compute.service"
INSTALL_DIR="/opt/mcp-compute"
ENV_FILE="/etc/mcp-compute.env"
SERVICE_FILE="/etc/systemd/system/mcp-compute.service"
STATE_DIR="/var/lib/mcp-compute"
LOG_DIR="/var/log/mcp-compute"
BACKUP_ROOT="/root/mcp-compute-backups"
CADDY_MAIN="/etc/caddy/Caddyfile"
CADDY_DIR="/etc/caddy/Caddyfile.d"
CADDY_SNIPPET="${CADDY_DIR}/mcp-compute.caddy"
IMPORT_LINE="import /etc/caddy/Caddyfile.d/*.caddy"
IMPORT_BEGIN="# BEGIN persistent-vps-compute installer"
IMPORT_END="# END persistent-vps-compute installer"
DEFAULT_PORT="8766"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DOMAIN=""
PORT="${DEFAULT_PORT}"
ASSUME_YES=0
NO_CADDY=0
ROTATE_TOKEN=0
BACKUP_DIR=""

say() {
  printf '\n\033[1;34m==>\033[0m %s\n' "$*"
}

warn() {
  printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2
}

die() {
  printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Persistent VPS Compute MCP installer

Usage:
  sudo bash install.sh [options]

Options:
  --domain NAME    Public DNS name, for example mcp.example.com
  --port NUMBER    Private loopback port (default: 8766)
  --yes            Non-interactive confirmation
  --no-caddy       Install only the private MCP service; configure HTTPS yourself
  --rotate-token   Generate a new Bearer token during an upgrade
  --help            Show this help

Examples:
  sudo bash install.sh
  sudo bash install.sh --domain mcp.example.com --yes
  sudo bash install.sh --no-caddy --port 8766 --yes
EOF
}

while (($#)); do
  case "$1" in
    --domain)
      [[ $# -ge 2 ]] || die '--domain requires a value'
      DOMAIN="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || die '--port requires a value'
      PORT="$2"
      shift 2
      ;;
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    --no-caddy)
      NO_CADDY=1
      shift
      ;;
    --rotate-token)
      ROTATE_TOKEN=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

[[ ${EUID} -eq 0 ]] || die "Run this installer as root: sudo bash install.sh"
[[ -r "${SCRIPT_DIR}/server.py" ]] || die "server.py is missing; run the installer from the repository directory"
[[ -r "${SCRIPT_DIR}/requirements.txt" ]] || die "requirements.txt is missing"
[[ -r "${SCRIPT_DIR}/systemd/mcp-compute.service" ]] || die "systemd unit template is missing"
[[ -r "${SCRIPT_DIR}/tests/test_client.py" ]] || die "test client is missing"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
else
  die '/etc/os-release is missing; Debian 12 or newer is required'
fi

case "${ID:-}" in
  debian|ubuntu) ;;
  *) die "Unsupported operating system: ${PRETTY_NAME:-unknown}. Use Debian 12+ or Ubuntu 22.04+." ;;
esac

command -v apt-get >/dev/null 2>&1 || die 'apt-get is required'
[[ ${PORT} =~ ^[0-9]+$ ]] || die 'Port must be a number'
((PORT >= 1024 && PORT <= 65535)) || die 'Port must be between 1024 and 65535'

read_env_value() {
  local key="$1"
  local file="$2"
  [[ -r ${file} ]] || return 0
  sed -n "s/^${key}=//p" "${file}" | head -n 1
}

existing_domain="$(read_env_value MCP_PUBLIC_DOMAIN "${ENV_FILE}")"
existing_port="$(read_env_value MCP_PORT "${ENV_FILE}")"
existing_token="$(read_env_value MCP_TOKEN "${ENV_FILE}")"

if [[ -z ${DOMAIN} && -n ${existing_domain} ]]; then
  DOMAIN="${existing_domain}"
fi
if [[ ${PORT} == "${DEFAULT_PORT}" && -n ${existing_port} ]]; then
  PORT="${existing_port}"
fi

if ((NO_CADDY == 0)) && [[ -z ${DOMAIN} ]]; then
  if ((ASSUME_YES == 1)); then
    die '--domain is required together with --yes unless --no-caddy is used'
  fi
  printf 'Public MCP domain (example: mcp.example.com): '
  read -r DOMAIN
fi

DOMAIN="${DOMAIN,,}"
if ((NO_CADDY == 0)); then
  [[ -n ${DOMAIN} ]] || die 'A public domain is required'
  [[ ${DOMAIN} != *://* && ${DOMAIN} != */* && ${DOMAIN} != *:* ]] || die 'Enter only a DNS name, without https://, a path, or a port'
  [[ ${DOMAIN} =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die 'The domain name is not valid'
fi

cat <<EOF

Persistent VPS Compute will install a real root terminal exposed through MCP.

  Install directory : ${INSTALL_DIR}
  systemd service   : ${SERVICE_NAME}
  Private listener  : 127.0.0.1:${PORT}
  Public domain     : ${DOMAIN:-not configured by this installer}
  HTTPS proxy       : $([[ ${NO_CADDY} -eq 1 ]] && printf 'external/manual' || printf 'Caddy')

Anyone with the Bearer token can execute arbitrary commands as root.
EOF

if ((ASSUME_YES == 0)); then
  printf 'Continue? [y/N]: '
  read -r answer
  [[ ${answer,,} == y || ${answer,,} == yes ]] || die 'Installation cancelled'
fi

say 'Installing operating-system packages'
export DEBIAN_FRONTEND=noninteractive
apt-get update
packages=(ca-certificates curl openssl python3 python3-pip python3-venv)
if ((NO_CADDY == 0)); then
  packages+=(caddy)
fi
apt-get install -y --no-install-recommends "${packages[@]}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_ROOT}/${timestamp}"
mkdir -p -m 0700 "${BACKUP_DIR}"

backup_file() {
  local source="$1"
  local name="$2"
  if [[ -e ${source} || -L ${source} ]]; then
    cp -a -- "${source}" "${BACKUP_DIR}/${name}"
  fi
}

backup_file "${ENV_FILE}" mcp-compute.env
backup_file "${SERVICE_FILE}" mcp-compute.service
backup_file "${CADDY_MAIN}" Caddyfile
backup_file "${CADDY_SNIPPET}" mcp-compute.caddy
if [[ -d ${INSTALL_DIR} ]]; then
  tar --exclude='.venv' -C "$(dirname "${INSTALL_DIR}")" -czf "${BACKUP_DIR}/mcp-compute-code.tgz" "$(basename "${INSTALL_DIR}")"
fi

say 'Installing the MCP application'
install -d -o root -g root -m 0700 "${INSTALL_DIR}" "${STATE_DIR}" "${LOG_DIR}" "${INSTALL_DIR}/scripts"
install -o root -g root -m 0600 "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"
install -o root -g root -m 0600 "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
install -o root -g root -m 0600 "${SCRIPT_DIR}/tests/test_client.py" "${INSTALL_DIR}/test_client.py"
install -o root -g root -m 0700 "${SCRIPT_DIR}/scripts/show-token.sh" "${INSTALL_DIR}/scripts/show-token.sh"
install -o root -g root -m 0700 "${SCRIPT_DIR}/scripts/rotate-token.sh" "${INSTALL_DIR}/scripts/rotate-token.sh"
install -o root -g root -m 0700 "${SCRIPT_DIR}/scripts/diagnose.sh" "${INSTALL_DIR}/scripts/diagnose.sh"

if [[ ! -x ${INSTALL_DIR}/.venv/bin/python ]]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
"${INSTALL_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check --requirement "${INSTALL_DIR}/requirements.txt"

if [[ -n ${existing_token} && ${ROTATE_TOKEN} -eq 0 ]]; then
  TOKEN="${existing_token}"
else
  TOKEN="$(openssl rand -hex 32)"
fi
[[ ${#TOKEN} -ge 24 ]] || die 'Generated token is unexpectedly short'

env_temporary="$(mktemp /etc/mcp-compute.env.tmp.XXXXXX)"
trap 'rm -f -- "${env_temporary:-}"' EXIT
{
  printf 'MCP_TOKEN=%s\n' "${TOKEN}"
  printf 'MCP_HOST=127.0.0.1\n'
  printf 'MCP_PORT=%s\n' "${PORT}"
  printf 'MCP_PUBLIC_DOMAIN=%s\n' "${DOMAIN}"
  printf 'MCP_SHELL=/bin/bash\n'
  printf 'MCP_DEFAULT_CWD=/root\n'
  printf 'MCP_STATE_DIR=/var/lib/mcp-compute\n'
  printf 'MCP_LOG_DIR=/var/log/mcp-compute\n'
  printf 'MCP_MAX_SESSIONS=12\n'
  printf 'MCP_MAX_WAIT_SECONDS=30\n'
  printf 'MCP_MAX_OUTPUT_CHARS=20000\n'
  printf 'MCP_MAX_BUFFER_BYTES=2000000\n'
  printf 'MCP_FINISHED_RETENTION_SECONDS=3600\n'
  printf 'MCP_ALLOWED_ORIGINS=https://notion.so,https://www.notion.so,https://notion.com,https://www.notion.com,https://app.notion.com\n'
} >"${env_temporary}"
install -o root -g root -m 0600 "${env_temporary}" "${ENV_FILE}"
rm -f -- "${env_temporary}"
trap - EXIT

install -o root -g root -m 0644 "${SCRIPT_DIR}/systemd/mcp-compute.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

say 'Waiting for the private MCP service'
for _ in {1..30}; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
    break
  fi
  sleep 0.5
done
curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" | grep -qx 'ok' || {
  journalctl -u "${SERVICE_NAME}" -n 50 --no-pager >&2
  die 'The private MCP health check failed'
}

say 'Running the end-to-end MCP test'
MCP_TOKEN="${TOKEN}" BASE="http://127.0.0.1:${PORT}" \
  "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/test_client.py"

if ((NO_CADDY == 0)); then
  say 'Configuring automatic HTTPS with Caddy'
  install -d -o root -g root -m 0755 "${CADDY_DIR}"

  if [[ -f ${CADDY_MAIN} ]] && grep -Eq "^[[:space:]]*${DOMAIN//./\\.}[[:space:]]*\{" "${CADDY_MAIN}"; then
    die "${DOMAIN} already has a site block in ${CADDY_MAIN}. The MCP service is installed and healthy, but Caddy was not changed. Integrate reverse_proxy 127.0.0.1:${PORT} manually or remove the duplicate block and rerun."
  fi

  sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__PORT__/${PORT}/g" \
    "${SCRIPT_DIR}/caddy/mcp-compute.caddy" >"${CADDY_SNIPPET}"
  chown root:root "${CADDY_SNIPPET}"
  chmod 0644 "${CADDY_SNIPPET}"

  touch "${CADDY_MAIN}"
  if ! grep -Fqx "${IMPORT_LINE}" "${CADDY_MAIN}"; then
    {
      printf '\n%s\n' "${IMPORT_BEGIN}"
      printf '%s\n' "${IMPORT_LINE}"
      printf '%s\n' "${IMPORT_END}"
    } >>"${CADDY_MAIN}"
  fi

  caddy fmt --overwrite "${CADDY_SNIPPET}"
  if ! caddy validate --config "${CADDY_MAIN}"; then
    if [[ -f ${BACKUP_DIR}/Caddyfile ]]; then
      cp -a -- "${BACKUP_DIR}/Caddyfile" "${CADDY_MAIN}"
    fi
    if [[ -f ${BACKUP_DIR}/mcp-compute.caddy ]]; then
      cp -a -- "${BACKUP_DIR}/mcp-compute.caddy" "${CADDY_SNIPPET}"
    else
      rm -f -- "${CADDY_SNIPPET}"
    fi
    die "Caddy validation failed; previous Caddy files were restored. The private MCP service remains healthy on 127.0.0.1:${PORT}."
  fi

  systemctl enable --now caddy
  systemctl reload caddy

  say 'Checking the public HTTPS endpoint'
  public_ok=0
  for _ in {1..20}; do
    if [[ $(curl -fsS --max-time 5 "https://${DOMAIN}/healthz" 2>/dev/null || true) == ok ]]; then
      public_ok=1
      break
    fi
    sleep 2
  done
  if ((public_ok == 0)); then
    warn "Private MCP passed all tests, but https://${DOMAIN}/healthz is not ready yet. Verify the DNS A/AAAA record and that TCP 80/443 are open."
  fi
fi

say 'Installation complete'
cat <<EOF
Service       : ${SERVICE_NAME} (active and enabled)
Private bind  : 127.0.0.1:${PORT}
MCP URL       : $([[ -n ${DOMAIN} ]] && printf 'https://%s/mcp' "${DOMAIN}" || printf 'configure your HTTPS proxy to /mcp')
Health URL    : $([[ -n ${DOMAIN} ]] && printf 'https://%s/healthz' "${DOMAIN}" || printf 'http://127.0.0.1:%s/healthz' "${PORT}")
Backup        : ${BACKUP_DIR}

Show the bare Bearer token only on the VPS:
  sudo ${INSTALL_DIR}/scripts/show-token.sh

Run diagnostics:
  sudo ${INSTALL_DIR}/scripts/diagnose.sh

The token is equivalent to a root password. Do not paste it into README files,
Skill pages, screenshots, issues, or chat messages.
EOF
