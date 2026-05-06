#!/bin/bash
# vo2-agent 배포 스크립트 (NAS에서 실행)
# 사용법: bash deploy.sh
# - inventory-web/equipment-web와 동일 패턴
# - synowebapi로 정지하여 DSM 알림 회피
# - 새 서비스 추가 시 SERVICES 배열에만 추가하면 됨

set -e  # 에러 발생 시 즉시 중단

cd /volume1/docker/vo2-agent

echo "=========================================="
echo "[vo2-agent] 배포 시작"
echo "=========================================="

# 1. 코드 동기화
echo "[1/4] git pull..."
git pull

# 2. Buildx git info 비활성화 (build 출력 깔끔)
export BUILDX_GIT_INFO=0

# 3. 빌드 + 정지 + 시작 (서비스별로 반복)
SERVICES=("vo2-etl-worker" "vo2-mcp-server")
# MCP server 추가 시: SERVICES=("vo2-etl-worker" "vo2-mcp-server")

for SERVICE in "${SERVICES[@]}"; do
    echo ""
    echo "[2/4] $SERVICE 빌드 중..."
    sudo docker compose build "$SERVICE"

    echo "[3/4] $SERVICE 정지 (synowebapi)..."
    sudo /usr/syno/bin/synowebapi --exec \
        api=SYNO.Docker.Container method="stop" version=1 \
        name="$SERVICE" 2>/dev/null || echo "  (이미 정지됨)"

    echo "[4/4] $SERVICE 시작..."
    sudo docker compose up -d "$SERVICE"
done

echo ""
echo "=========================================="
echo "[vo2-agent] 배포 완료!"
echo "=========================================="
echo ""
echo "로그 확인:"
echo "  sudo docker logs -f vo2-etl-worker --tail 50"
