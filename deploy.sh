#!/bin/bash
# vo2-agent 배포 스크립트 (NAS에서 실행)
#
# 사용법:
#   sudo bash deploy.sh                                 # 전체 (default)
#   sudo bash deploy.sh vo2-mcp-server                  # mcp만
#   sudo bash deploy.sh vo2-etl-worker                  # etl만
#   sudo bash deploy.sh vo2-etl-worker vo2-mcp-server   # 둘 다 (명시)
#
# - inventory-web/equipment-web와 동일 패턴
# - synowebapi로 정지하여 DSM 알림 회피
# - 새 서비스 추가 시 SERVICES_AVAILABLE 배열에만 추가하면 됨

set -e

cd /volume1/docker/vo2-agent

# 사용 가능한 서비스 정의 (새 서비스 추가 시 여기만 갱신)
SERVICES_AVAILABLE=("vo2-etl-worker" "vo2-mcp-server")

# 인자 파싱: 인자가 없으면 default = 모든 서비스
if [ "$#" -eq 0 ]; then
    SERVICES=("${SERVICES_AVAILABLE[@]}")
else
    SERVICES=("$@")

    # 유효성 체크 — 오타로 알 수 없는 서비스 건드리는 것 방지
    for SERVICE in "${SERVICES[@]}"; do
        FOUND=0
        for VALID in "${SERVICES_AVAILABLE[@]}"; do
            if [ "$SERVICE" = "$VALID" ]; then
                FOUND=1
                break
            fi
        done
        if [ "$FOUND" -eq 0 ]; then
            echo "ERROR: 알 수 없는 서비스 '$SERVICE'"
            echo "사용 가능: ${SERVICES_AVAILABLE[*]}"
            exit 1
        fi
    done
fi

echo "=========================================="
echo "[vo2-agent] 배포 시작"
echo "대상 서비스: ${SERVICES[*]}"
echo "=========================================="

# 1. 코드 동기화
echo ""
echo "[1/4] git pull..."
git pull

# 2. Buildx git info 비활성화 (build 출력 깔끔)
export BUILDX_GIT_INFO=0

# 3. 빌드 + 정지 + 시작 (서비스별로 반복)
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
for SERVICE in "${SERVICES[@]}"; do
    echo "  sudo docker logs -f $SERVICE --tail 50"
done
