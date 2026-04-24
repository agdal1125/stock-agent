# Deploy — Vultr + Caddy + sslip.io

## 사전 준비
1. Vultr Ubuntu 24.04 LTS 인스턴스 생성 (1GB / 2GB 충분). 퍼블릭 IP 확보.
2. 로컬에서 `ssh root@<IP>` 접속 확인.

## 부트스트랩
```bash
ssh root@<IP>

# 이 스크립트가 Docker, UFW, deploy user, repo clone 까지 처리
curl -fsSL https://raw.githubusercontent.com/<owner>/stock-agent/main/deploy/vultr-bootstrap.sh \
  | REPO_URL=https://github.com/<owner>/stock-agent.git bash
```

## 환경변수 설정
```bash
vi /opt/stock-agent/.env
```

- `PUBLIC_HOST` — IP의 점을 하이픈으로 바꿔 sslip.io suffix:
  `192.0.2.42` → `192-0-2-42.sslip.io`
- `OPENAI_API_KEY` — 본인 키
- `DAILY_USD_CAP` — 비상 상한 (0 = 무제한). 처음엔 `5.00` 권장
- `LLM_KILL_SWITCH=0` — 운영 시 0 (live). 사고 시 `1` 로 즉시 차단

## 기동
```bash
su - deploy
cd /opt/stock-agent
docker compose --profile prod up -d
docker compose --profile prod logs -f
```

최초 기동 시 Caddy 가 Let's Encrypt 인증서를 자동 발급합니다 (수 초). 이후:

- https://{PUBLIC_HOST}/ — 질문 UI
- https://{PUBLIC_HOST}/wiki/ — 파일 브라우저
- https://{PUBLIC_HOST}/how — 작동 원리
- https://{PUBLIC_HOST}/cost — 사용량 JSON

## 업데이트 (GHCR 이미지 사용 시)
`docker-compose.yml` 에서 `build: .` 라인을 주석 처리하고
`image: ghcr.io/<owner>/stock-agent:latest` 활성화 후:

```bash
docker compose --profile prod pull
docker compose --profile prod up -d
```

## 비상 차단
```bash
# LLM 외부 호출만 막고 싶음 (UI 는 유지, 스텁 응답)
sed -i 's/^LLM_KILL_SWITCH=.*/LLM_KILL_SWITCH=1/' /opt/stock-agent/.env
docker compose --profile prod restart app

# 전체 내리기
docker compose --profile prod down
```

## 비용 모니터링
```bash
curl -s https://{PUBLIC_HOST}/cost | python3 -m json.tool
```

`daily_usd_cap` 초과 시 `ensure_budget()` 가 `BudgetExceeded` 를 던져
LLM 호출이 자동 차단됩니다 (다음날 UTC 00:00 에 리셋).

## 방화벽·보안
- UFW 로 22/80/443 만 개방
- 앱 포트 8001 은 `127.0.0.1` 바인딩 (compose 에서 명시) → 퍼블릭 노출 X
- Caddy 가 HTTP→HTTPS 강제 + HSTS 헤더
