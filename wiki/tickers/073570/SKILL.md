---
ticker: "073570"
name_ko: "리튬포어스"
kind: skill
updated_at: "2026-04-27T02:05:36+00:00"
---

# 리튬포어스 (073570) — SKILL

종목 Agent가 이 종목 관련 질의를 처리할 때 참조하는 **정책 파일**입니다.
(router.py 의 intent→sections 기본값과 동일)

## 별칭 (aliases)
- 리튬포어스
- 포어스


## 지원 intent → 우선 섹션

| intent | 우선 섹션 |
|---|---|
| latest_issue   | 10_latest_events.md, 20_business.md |
| sns_buzz       | 11_sns_events.md, 10_latest_events.md |
| business_model | 00_profile.md, 20_business.md |
| finance        | 30_finance.md, 10_latest_events.md |
| relations      | 40_relations.md, 20_business.md |
| theme          | 50_theme.md, 20_business.md |
| generic        | 00_profile.md, 10_latest_events.md, 20_business.md |

## Freshness SLA

| 섹션 | 갱신 주기 |
|---|---|
| latest_events  | 15분 (뉴스·공시 ingest 시) |
| sns_events     | 15분 (SNS/종토방 피드 ingest 시) |
| profile / business / finance / relations / theme | 일 1회 배치 |

## 섹션 목록 (링크)
- [`00_profile.md`](00_profile.md)
- [`10_latest_events.md`](10_latest_events.md)
- [`11_sns_events.md`](11_sns_events.md)
- [`20_business.md`](20_business.md)
- [`30_finance.md`](30_finance.md)
- [`40_relations.md`](40_relations.md)
- [`50_theme.md`](50_theme.md)
