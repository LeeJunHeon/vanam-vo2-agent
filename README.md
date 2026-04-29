# vanam-vo2-agent

VO2 박막 공정 데이터 통합 분석 시스템.
Synology NAS의 Docker 컨테이너로 배포되는 Python ETL/MCP 서비스.

## 구조

```
.
├── etl-worker/          # 5분마다 SMB 파일 → vo2 schema 적재
├── mcp-server/          # FastAPI read-only API (portal-nextjs SSR이 호출)
├── analysis-worker/     # daily batch (Phase 2 이후)
├── shared/              # 공통 SQLAlchemy 모델, config, logger
├── db/                  # init.sql + Alembic migrations
└── docker-compose.yml
```

## 의존 인프라

- PostgreSQL: 기존 컨테이너 `inventory-web-postgres` 의 `vo2` schema 사용
- Docker network: `vanam-db-net` (external, 이미 존재)
- 포털: `portal-nextjs` 의 `/vo2` 라우트로 통합 (Phase 1)
- 외부 노출: Phase 3에서 `vo2-mcp.vanam.synology.me` (보안 hardening 후)

## 자세한 문서

설계, 의사결정, 배포 절차, ETL/MCP 상세는 운영자의 Obsidian vault `01_Projects/VO2_Agent/` 참조.

## 현재 단계

Phase 1b — CH1 두 파일 (`Ch1_log.csv`, `CH1.xlsx`) 만으로 ETL → DB → MCP → portal 종단간 검증.
다른 source (OES, ALD, anneal, measurement) 는 Phase 2 이후 추가.
