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

## 배포 (NAS)

이미지는 NAS 에서 빌드하지 않는다. main 에 push 하면 GitHub Actions(`.github/workflows/docker-publish.yml`)가 이미지를 빌드해
이미지 두 개(`ghcr.io/leejunheon/vo2-etl-worker`·`vo2-mcp-server`, 태그 `<커밋 SHA>`·`:latest`) 로 올리고, NAS 는 그 이미지를 받아 재시작만 한다.

1. push 뒤 Actions 가 성공했는지 본다: https://github.com/LeeJunHeon/vanam-vo2-agent/actions
2. NAS `/volume1/docker/vo2-agent` 에서 `sudo bash deploy.sh [서비스...]`(서비스를 안 주면 둘 다) — git pull → 그 커밋 이미지 받기 → `:deployed` 로 바꾸기 → `docker compose up -d` → 실행 확인(옛 이미지는 지우지 않는다).
   그 커밋 이미지가 아직 없으면(빌드 중·실패·로그인 안 됨) Actions 주소를 안내하고 아무것도 바꾸지 않은 채 멈춘다.
3. 문제가 있으면 `rollback` 인자 — 바로 전 이미지(`:previous`)와 맞바꾼다(git pull 하지 않음).
4. GitHub 을 쓸 수 없을 때만 `--build` 인자(비상용) — 예전처럼 NAS 에서 직접 빌드해 `:deployed` 로 쓴다.
5. **한 번에 한 앱씩 배포한다**(다른 앱 배포 중이면 멈춘다). NAS 1분 평균 부하가 6 초과이거나 여유 메모리가 500MB 미만이면
   시작하지 않는다 — 확인하고 꼭 해야 할 때만 `sudo GHCR_FORCE=1 bash deploy.sh`(rollback·--build 와 같이 쓸 수 있다). 옛 이미지는 새벽 야간 정리 작업이 지운다.
- 인자 예: `sudo bash deploy.sh rollback vo2-mcp-server`, `sudo bash deploy.sh --build vo2-etl-worker`. 정지는 지금처럼 synowebapi 로 한다(DSM 알림 회피).

- 처음 한 번: NAS 에서 `sudo git pull`(새 배포 스크립트 받기) → `sudo docker login ghcr.io`(GitHub 사용자 이름 + `read:packages` 권한
  토큰. 공개 저장소 이미지는 로그인 없이도 받을 수 있다) → 배포. 처음에는 `:previous` 가 없어 되돌리기는 `--build` 로 한다.
- 배포 기록은 앱 폴더의 `.deploy-state`(git 에 넣지 않음)에 이미지마다 현재·이전 커밋 SHA 와 시각으로 남는다. 공용 함수는 `scripts/ghcr-deploy.sh`.
- 예전에 NAS 에서 빌드한 이미지(compose 기본 이름)는 자동으로 지우지 않는다. `sudo docker images` 로 확인해 쓰지 않는 것만 지운다.

## 자세한 문서

설계, 의사결정, 배포 절차, ETL/MCP 상세는 운영자의 Obsidian vault `01_Projects/VO2_Agent/` 참조.

## 현재 단계

Phase 1b — CH1 두 파일 (`Ch1_log.csv`, `CH1.xlsx`) 만으로 ETL → DB → MCP → portal 종단간 검증.
다른 source (OES, ALD, anneal, measurement) 는 Phase 2 이후 추가.
