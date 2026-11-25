#!/usr/bin/env bash
# One-shot installer for the DeepAI Code gateway + its signup worker on Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/addxiaoyi/deepai2api/main/deploy/install.sh | sudo bash
#   sudo bash deploy/install.sh --dir /opt/deepai2api --port 8787 --with-docker
#
# What it does:
#   1. installs git/python3/nodejs (+ docker when asked)
#   2. copies this checkout to /opt/deepai2api (or clones --repo)
#   3. npm ci (gateway deps) and a venv with the signup tool's requirements
#   4. writes /etc/deepai2api/env with a generated admin token
#   5. installs + enables deepai-gateway.service (and deepai-webui.service)
#
# Idempotent: re-running keeps the existing token unless --rotate-token is given.
set -euo pipefail

REPO_URL="${DEEPAI_REPO_URL:-https://github.com/addxiaoyi/deepai2api.git}"
REPO_BRANCH="${DEEPAI_REPO_BRANCH:-main}"
INSTALL_DIR="${DEEPAI_INSTALL_DIR:-/opt/deepai2api}"
DATA_DIR="${DEEPAI_DATA_DIR:-/var/lib/deepai2api/data}"
ENV_DIR="/etc/deepai2api"
ENV_FILE="${ENV_DIR}/env"
SERVICE_USER="${DEEPAI_SERVICE_USER:-deepai}"
PORT=8787
WITH_DOCKER=0
WITH_CAMOUFOX=0
ROTATE_TOKEN=0
SOURCE_DIR=""

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --data) DATA_DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) REPO_BRANCH="$2"; shift 2 ;;
    --source) SOURCE_DIR="$2"; shift 2 ;;
    --with-docker) WITH_DOCKER=1; shift ;;
    --with-camoufox) WITH_CAMOUFOX=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 …)"
command -v systemctl >/dev/null || die "this installer needs systemd"

# --------------------------------------------------------------------- packages
if command -v apt-get >/dev/null; then
  PKG=apt
elif command -v dnf >/dev/null; then
  PKG=dnf
elif command -v yum >/dev/null; then
  PKG=yum
elif command -v apk >/dev/null; then
  PKG=apk
elif command -v pacman >/dev/null; then
  PKG=pacman
else
  die "unsupported distribution (no apt/dnf/yum/apk/pacman)"
fi

install_pkgs() {
  log "installing: $*"
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y -qq "$@" ;;
    dnf|yum) "$PKG" install -y -q "$@" ;;
    apk) apk add --no-cache "$@" ;;
    pacman) pacman -Sy --noconfirm --needed "$@" ;;
  esac
}

need=()
command -v git >/dev/null || need+=(git)
command -v curl >/dev/null || need+=(curl)
command -v python3 >/dev/null || need+=(python3)
python3 -c 'import venv' >/dev/null 2>&1 || need+=(python3-venv)
[ "$PKG" = apt ] && need+=(python3-pip ca-certificates)
if [ "${#need[@]}" -gt 0 ]; then
  install_pkgs "${need[@]}"
fi

node_ok() { command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 20 ]; }
if ! node_ok; then
  log "installing Node.js 20"
  case "$PKG" in
    apt)
      curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
      apt-get install -y -qq nodejs
      ;;
    dnf) dnf module install -y nodejs:20/common ;;
    yum) curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - && yum install -y nodejs ;;
    apk) apk add --no-cache nodejs npm ;;
    pacman) pacman -Sy --noconfirm nodejs npm ;;
  esac
fi
node_ok || die "Node.js >= 20 is required (install it manually, then re-run)"

if [ "$WITH_DOCKER" = 1 ] && ! command -v docker >/dev/null; then
  log "installing Docker"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

# ------------------------------------------------------------------------ user
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  log "creating service user $SERVICE_USER"
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null ||
    adduser -S -h "$INSTALL_DIR" -s /sbin/nologin "$SERVICE_USER"
fi

# ------------------------------------------------------------------------ code
if [ -n "$SOURCE_DIR" ]; then
  log "copying $SOURCE_DIR -> $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  tar -C "$SOURCE_DIR" --exclude .git --exclude node_modules --exclude vendor \
      --exclude 'tools/deepai-reg/out' -cf - . | tar -C "$INSTALL_DIR" -xf -
elif [ -d "$INSTALL_DIR/.git" ]; then
  log "updating $INSTALL_DIR from origin/$REPO_BRANCH"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"
else
  log "cloning $REPO_URL -> $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
mkdir -p "$DATA_DIR"

log "installing gateway dependencies (npm ci)"
( cd "$INSTALL_DIR" && npm ci --omit=dev --no-audit --no-fund )

log "creating python venv for the signup workers"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/tools/deepai-reg/requirements.txt" || {
  warn "camoufox wheel install failed; the protocol engine still works"
}
if [ -f "$INSTALL_DIR/tools/poe-reg/requirements.txt" ]; then
  "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/tools/poe-reg/requirements.txt" || true
fi
if [ "$WITH_CAMOUFOX" = 1 ]; then
  log "fetching the Camoufox browser (~1.3 GB)"
  "$INSTALL_DIR/.venv/bin/python" -m camoufox fetch || warn "camoufox fetch failed — browser engine unavailable"
fi

# ------------------------------------------------------------------------- env
mkdir -p "$ENV_DIR"
if [ "$ROTATE_TOKEN" = 1 ] || [ ! -f "$ENV_FILE" ]; then
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  log "writing $ENV_FILE with a fresh admin token"
  cat > "$ENV_FILE" <<EOF
DEEPAI_GATEWAY_HOST=0.0.0.0
DEEPAI_GATEWAY_PORT=${PORT}
DEEPAI_GATEWAY_TOKEN=${TOKEN}
DEEPAI_GATEWAY_DATA=${DATA_DIR}
DEEPAI_WEBUI_URL=http://127.0.0.1:3000
DEEPAI_PYTHON=${INSTALL_DIR}/.venv/bin/python
DEEPAI_REGISTER_DIR=${INSTALL_DIR}/tools/deepai-reg
POE_PYTHON=${INSTALL_DIR}/.venv/bin/python
POE_REGISTER_DIR=${INSTALL_DIR}/tools/poe-reg
EOF
  chmod 600 "$ENV_FILE"
else
  log "keeping existing $ENV_FILE"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"

# ---------------------------------------------------------------------- systemd
log "installing systemd units"
install -m 644 "$INSTALL_DIR/deploy/deepai-gateway.service" /etc/systemd/system/deepai-gateway.service
systemctl daemon-reload
systemctl enable deepai-gateway.service
systemctl restart deepai-gateway.service

if [ "$WITH_DOCKER" = 1 ] && command -v docker >/dev/null; then
  install -m 644 "$INSTALL_DIR/deploy/deepai-webui.service" /etc/systemd/system/deepai-webui.service
  systemctl daemon-reload
  systemctl enable deepai-webui.service
  log "building the Open WebUI fork (first build takes a few minutes)"
  systemctl restart deepai-webui.service
else
  warn "skipping WebUI unit (no docker); re-run with --with-docker, or use: docker compose up -d --build"
fi

TOKEN_VALUE="$(grep '^DEEPAI_GATEWAY_TOKEN=' "$ENV_FILE" | cut -d= -f2)"
cat <<EOF

Done.
  gateway    http://127.0.0.1:${PORT}          (systemctl status deepai-gateway)
  WebUI      http://127.0.0.1:3000             (systemctl status deepai-webui)
  admin token ${TOKEN_VALUE}
  config     ${ENV_FILE}
  data       ${DATA_DIR}

Next:
  1. open the WebUI, register the first account (it becomes admin)
  2. Settings → Admin → DeepAI: paste the gateway URL + token → 保存
  3. 批量注册 · 自动入池: pick count/engine → 开始注册

Logs: journalctl -u deepai-gateway -f
EOF
