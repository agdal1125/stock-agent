#!/usr/bin/env bash
# Vultr Ubuntu 24.04 LTS 신규 인스턴스 부트스트랩 (1회성)
#
# 실행 방법:
#   ssh root@<VULTR_IP>
#   curl -fsSL https://raw.githubusercontent.com/<owner>/stock-agent/main/deploy/vultr-bootstrap.sh | bash
#
# 또는 scp 로 전송 후:
#   bash vultr-bootstrap.sh
#
# 이 스크립트가 하는 일:
#   1) 시스템 업데이트 + UFW 방화벽 (22/80/443)
#   2) Docker + Compose plugin 설치
#   3) 배포용 비루트 유저 `deploy` 생성
#   4) /opt/stock-agent 에 repo clone (변수 REPO_URL 로 덮어쓰기 가능)
#   5) .env 템플릿 생성 — 수동 편집 필요
#   6) docker compose pull && up 안내

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/REPLACE-ME/stock-agent.git}"
APP_DIR="/opt/stock-agent"
DEPLOY_USER="deploy"

echo "[1/6] system update + firewall"
apt-get update -y
apt-get upgrade -y
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg ufw git

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "[2/6] Docker + compose plugin"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

echo "[3/6] deploy user"
if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${DEPLOY_USER}"
fi
usermod -aG docker "${DEPLOY_USER}"

echo "[4/6] clone repo"
mkdir -p "${APP_DIR}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}"
if [ ! -d "${APP_DIR}/.git" ]; then
    sudo -u "${DEPLOY_USER}" git clone "${REPO_URL}" "${APP_DIR}"
else
    sudo -u "${DEPLOY_USER}" git -C "${APP_DIR}" pull --ff-only
fi

echo "[5/6] .env 템플릿"
cat > "${APP_DIR}/.env.example" <<'ENV'
# 퍼블릭 HTTPS 호스트 (sslip.io 형식 권장)
PUBLIC_HOST=REPLACE-WITH-YOUR-IP-HYPHENATED.sslip.io

# OpenAI
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
OPENAI_EMBED_MODEL=text-embedding-3-small

# 운영 옵션
LLM_KILL_SWITCH=0
EAGER_TOP_N=5
DAILY_USD_CAP=5.00

# Rate limit
RATE_LIMIT_PER_MIN=30
RATE_LIMIT_PER_HOUR=400
RATE_LIMIT_ASK=10/minute
ENV
if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chown "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
fi

echo ""
echo "[6/6] 완료. 다음 단계:"
echo ""
echo "  1) vi ${APP_DIR}/.env   # PUBLIC_HOST, OPENAI_API_KEY 채우기"
echo "     PUBLIC_HOST 예: $(curl -s ifconfig.me 2>/dev/null | tr . -).sslip.io"
echo ""
echo "  2) su - ${DEPLOY_USER}"
echo "     cd ${APP_DIR}"
echo "     docker compose --profile prod up -d"
echo ""
echo "  3) docker compose --profile prod logs -f"
echo ""
echo "  4) 브라우저: https://\${PUBLIC_HOST}/"
