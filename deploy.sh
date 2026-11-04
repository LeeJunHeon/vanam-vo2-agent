#!/bin/bash
# vo2-agent 배포 스크립트 (NAS에서 실행)
#
# 이미지는 NAS 에서 빌드하지 않는다. GitHub Actions(.github/workflows/docker-publish.yml)가 main 커밋마다
# ghcr.io/leejunheon/<서비스>:<커밋 SHA> 로 올리고, 여기서는 git pull 한 커밋의 이미지를 받아 :deployed 로 태그한 뒤 재시작만 한다.
# (공용 함수 scripts/ghcr-deploy.sh, 배포 기록 .deploy-state)
#
# 사용법:
#   sudo bash deploy.sh                                 # 전체 (default)
#   sudo bash deploy.sh vo2-mcp-server                  # mcp만
#   sudo bash deploy.sh vo2-etl-worker                  # etl만
#   sudo bash deploy.sh vo2-etl-worker vo2-mcp-server   # 둘 다 (명시)
#   sudo bash deploy.sh rollback [서비스...]            # 바로 전 이미지(:previous)로 되돌리기 (git pull 안 함)
#   sudo bash deploy.sh --build [서비스...]             # (비상용) GitHub 없이 NAS 에서 직접 빌드
# 처음 한 번 NAS 에서: sudo docker login ghcr.io (공개 저장소 이미지면 로그인 없이도 받을 수 있다)
#
# NAS 보호(공용 함수 ghcr_begin/ghcr_end): 다른 앱 배포 중이면 멈춘다(한 번에 한 앱), NAS 부하·여유 메모리가 기준을 넘으면
# 시작하지 않는다(sudo GHCR_FORCE=1 bash deploy.sh 로만 건너뜀), 배포하는 동안 watchdog 을 일시 정지한다.
# 옛 이미지는 지우지 않는다(새벽 야간 정리 작업이 맡는다).
#
# - synowebapi로 정지하여 DSM 알림 회피
# - 새 서비스 추가 시 SERVICES_AVAILABLE 배열과 워크플로 matrix 에 추가하면 됨(이미지 이름 = 서비스 이름)

set -e

# 공용 배포 함수 — 실행 중 git pull 로 파일이 바뀌어도 이미 읽은 함수로 끝까지 돈다(스크립트 위치 기준이라 cd 전에 읽는다)
# shellcheck source=scripts/ghcr-deploy.sh
source "$(dirname "$(readlink -f "$0")")/scripts/ghcr-deploy.sh"
GHCR_REPO="LeeJunHeon/vanam-vo2-agent"
GHCR_BUILD_HINT="sudo bash deploy.sh --build"

cd /volume1/docker/vo2-agent

# 사용 가능한 서비스 정의 (새 서비스 추가 시 여기만 갱신)
SERVICES_AVAILABLE=("vo2-etl-worker" "vo2-mcp-server")

# 첫 인자가 rollback·--build 면 모드, 나머지는 서비스
case "${1:-}" in
    rollback | --build)
        ghcr_parse_mode "$1"
        shift
        ;;
esac

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

GHCR_SERVICES=("${SERVICES[@]}")
GHCR_IMAGES=()
for SERVICE in "${SERVICES[@]}"; do
    GHCR_IMAGES+=("ghcr.io/leejunheon/$SERVICE")
done

# NAS 보호(공용 함수): 동시 배포 잠금 → 상태 점검(GHCR_FORCE=1 이면 건너뜀) → watchdog 일시 정지. 끝나면 trap 이 ghcr_end
ghcr_begin

echo "=========================================="
echo "[vo2-agent] 배포 시작 ($GHCR_MODE)"
echo "대상 서비스: ${SERVICES[*]}"
echo "=========================================="

# 1. 코드 동기화 (되돌리기는 git pull 하지 않는다)
echo ""
if [ "$GHCR_MODE" = "rollback" ]; then
    echo "[1/4] 되돌리기 (git pull 안 함)"
else
    echo "[1/4] git pull..."
    # NAS 폴더가 main 이 아니면 아무것도 바꾸지 않고 멈춘다(공용 함수)
    ghcr_require_main
    git pull
fi

# 2. 이미지 준비: 그 커밋 이미지 받기 → :deployed (하나라도 없으면 아무것도 바꾸지 않고 멈춘다)
echo ""
echo "[2/4] 이미지 준비..."
ghcr_prepare_images

# 3. 정지 + 시작 (서비스별로 반복)
for SERVICE in "${SERVICES[@]}"; do
    echo ""
    echo "[3/4] $SERVICE 정지 (synowebapi)..."
    sudo /usr/syno/bin/synowebapi --exec \
        api=SYNO.Docker.Container method="stop" version=1 \
        name="$SERVICE" 2>/dev/null || echo "  (이미 정지됨)"

    echo "[4/4] $SERVICE 시작..."
    sudo docker compose up -d "$SERVICE"
done

# 옛 이미지는 지우지 않는다(안내만 — 새벽 야간 정리 작업 담당)
ghcr_cleanup

echo ""
echo "=========================================="
echo "[vo2-agent] 배포 완료!"
echo "=========================================="
echo ""
echo "로그 확인:"
for SERVICE in "${SERVICES[@]}"; do
    echo "  sudo docker logs -f $SERVICE --tail 50"
done
echo "문제가 있으면 되돌리기: sudo bash deploy.sh rollback ${SERVICES[*]}"
