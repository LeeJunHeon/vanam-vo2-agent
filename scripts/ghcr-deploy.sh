#!/bin/bash
# GHCR 이미지 배포 공용 함수 — deploy*.sh 가 source 한다(직접 실행하지 않는다).
#
# GitHub Actions(.github/workflows/docker-publish.yml)가 main 커밋마다 이미지를 빌드해
# ghcr.io/leejunheon/<이미지>:<커밋 SHA> 와 :latest 로 올린다. NAS 는 빌드하지 않고 그 이미지를 받아 재시작만 한다.
#   :<SHA>     GitHub Actions 가 올린 그 커밋의 이미지(받아 온 것)
#   :deployed  docker-compose.yml 이 쓰는 이미지(pull_policy: never — compose 가 스스로 받지 않는다)
#   :previous  바로 전에 배포했던 이미지(rollback 용)
# 배포 기록은 앱 폴더의 .deploy-state(git 에 넣지 않음)에 이미지마다 한 줄씩 남긴다: 이미지 현재SHA 이전SHA 시각
#
# 모드(GHCR_MODE, ghcr_parse_mode 가 정한다)
#   deploy   : git pull 한 HEAD 의 :<SHA> 를 모두 받은 뒤 :deployed → :previous, :<SHA> → :deployed (하나라도 없으면 아무것도 바꾸지 않고 멈춘다)
#   rollback : :previous ↔ :deployed 를 맞바꾼다(:previous 가 없으면 멈춘다). git pull 하지 않는다
#   build    : (비상용, --build) GitHub 을 쓰지 않고 NAS 에서 docker compose build 해 :deployed 로 쓴다(:previous 는 보관)
#
# source 하기 전에 정하는 값
#   GHCR_REPO      GitHub 저장소(LeeJunHeon/xxx) — 안내 주소와 옛 이미지 정리(라벨)에 쓴다
#   GHCR_IMAGES    이번에 배포할 이미지(태그 없는 전체 이름) 배열
#   GHCR_SERVICES  그 이미지를 쓰는 compose 서비스 배열(build 모드에서 빌드할 서비스)
# 선택 값
#   GHCR_COMPOSE        compose 명령 배열(기본: docker compose)
#   GHCR_PREVIOUS_HOOK  :previous 를 남기는 함수 이름(기본 ghcr_keep_previous). 바꾸는 쪽이 GHCR_PREV_SHA 도 정한다
#   GHCR_STATE_FILE     배포 기록 파일(기본 .deploy-state, 앱 폴더 기준)
#   GHCR_BUILD_HINT     안내 문구에 쓸 비상 빌드 명령(기본: sudo <스크립트> --build)

GHCR_STATE_FILE="${GHCR_STATE_FILE:-.deploy-state}"
GHCR_MODE="deploy"
GHCR_OLD_IDS=()   # 태그를 잃을 수 있는 옛 이미지 ID — 정리 단계에서 태그가 하나도 없으면 지운다
GHCR_PREV_SHA="-" # 새 :previous 가 가리키는 SHA(모르면 -)
GHCR_BUILD_HINT="${GHCR_BUILD_HINT:-sudo $0 --build}"

ghcr_log() {
  echo "  [ghcr] $*"
}

ghcr_compose() {
  if [ -n "${GHCR_COMPOSE+x}" ] && [ "${#GHCR_COMPOSE[@]}" -gt 0 ]; then
    "${GHCR_COMPOSE[@]}" "$@"
  else
    docker compose "$@"
  fi
}

# 인자 하나(빈 값·rollback·--build)로 모드를 정한다. 그 밖의 값이면 종료 코드 2.
ghcr_parse_mode() {
  case "${1:-}" in
    "") GHCR_MODE="deploy" ;;
    rollback) GHCR_MODE="rollback" ;;
    --build) GHCR_MODE="build" ;;
    *)
      echo "알 수 없는 인자: $1" >&2
      echo "사용법: sudo $0 [rollback|--build]" >&2
      exit 2
      ;;
  esac
}

ghcr_has_image() {
  docker image inspect "$1" >/dev/null 2>&1
}

ghcr_image_id() {
  docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true
}

# 배포 기록에서 그 이미지의 칸 하나(2=현재 SHA, 3=이전 SHA). 없으면 -
ghcr_state_get() {
  local image="$1" field="$2" value=""
  if [ -f "$GHCR_STATE_FILE" ]; then
    value="$(awk -v img="$image" -v f="$field" '$1 == img { v = $f } END { print v }' "$GHCR_STATE_FILE")"
  fi
  echo "${value:--}"
}

# 그 이미지의 줄을 새 값으로 바꾼다(다른 이미지 줄은 그대로)
ghcr_state_set() {
  local image="$1" current="$2" previous="$3" now tmp
  now="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  tmp="${GHCR_STATE_FILE}.tmp"
  {
    echo "# 배포 기록(deploy 스크립트가 쓴다, git 에 넣지 않음): 이미지 현재SHA 이전SHA 시각"
    if [ -f "$GHCR_STATE_FILE" ]; then
      awk -v img="$image" '$1 != img && $1 !~ /^#/' "$GHCR_STATE_FILE"
    fi
    echo "$image $current $previous $now"
  } >"$tmp"
  mv "$tmp" "$GHCR_STATE_FILE"
}

ghcr_actions_hint() {
  echo "GitHub Actions 빌드가 아직 끝나지 않았거나 실패했습니다: https://github.com/${GHCR_REPO}/actions" >&2
  echo "  - 빌드 중이면 끝난 뒤 다시 실행하세요." >&2
  echo "  - 로그인 문제(denied·unauthorized)면 NAS 에서 한 번: sudo docker login ghcr.io  (사용자 이름 + read:packages 권한 토큰)" >&2
  echo "  - 급하면 NAS 에서 직접 빌드: $GHCR_BUILD_HINT" >&2
}

# 모든 이미지의 :<SHA> 를 받는다(이미 있으면 건너뜀). 하나라도 없으면 아무것도 바꾸지 않고 종료 1.
ghcr_pull_all() {
  local sha="$1" image
  for image in "${GHCR_IMAGES[@]}"; do
    if ghcr_has_image "$image:$sha"; then
      ghcr_log "$image:${sha:0:12} 이미 있음"
      continue
    fi
    ghcr_log "docker pull $image:$sha"
    if ! docker pull "$image:$sha"; then
      echo "배포 중단: $image:$sha 를 받지 못했습니다. 실행 중인 서비스는 바꾸지 않았습니다." >&2
      ghcr_actions_hint
      exit 1
    fi
  done
}

# 기본 :previous 보관 — 지금 :deployed 가 있으면 :previous 로 태그한다(처음 배포면 건너뜀).
# 덮어써질 옛 :previous 의 ID 는 정리 후보로 남긴다.
ghcr_keep_previous() {
  local image="$1" old_prev
  if ! ghcr_has_image "$image:deployed"; then
    ghcr_log "$image:deployed 가 없어 :previous 를 남기지 않습니다(처음 배포 — 되돌릴 때는 --build)"
    GHCR_PREV_SHA="-"
    return 0
  fi
  old_prev="$(ghcr_image_id "$image:previous")"
  [ -n "$old_prev" ] && GHCR_OLD_IDS+=("$old_prev")
  docker tag "$image:deployed" "$image:previous"
  GHCR_PREV_SHA="$(ghcr_state_get "$image" 2)"
  ghcr_log "지금 이미지를 $image:previous 로 보관 (${GHCR_PREV_SHA:0:12})"
}

# 모드에 맞게 :deployed 를 준비한다(컨테이너는 아직 그대로). 끝나면 docker compose up -d 하면 된다.
ghcr_prepare_images() {
  local image sha hook="${GHCR_PREVIOUS_HOOK:-ghcr_keep_previous}"
  case "$GHCR_MODE" in
    deploy)
      sha="$(git rev-parse HEAD)"
      ghcr_pull_all "$sha"
      for image in "${GHCR_IMAGES[@]}"; do
        if [ "$(ghcr_image_id "$image:deployed")" = "$(ghcr_image_id "$image:$sha")" ]; then
          ghcr_log "$image:${sha:0:12} 는 이미 :deployed — 태그는 그대로 두고 다시 띄운다"
          continue
        fi
        "$hook" "$image"
        docker tag "$image:$sha" "$image:deployed"
        ghcr_state_set "$image" "$sha" "$GHCR_PREV_SHA"
        ghcr_log "$image:${sha:0:12} → :deployed"
      done
      ;;
    build)
      sha="$(git rev-parse HEAD)"
      for image in "${GHCR_IMAGES[@]}"; do
        "$hook" "$image"
      done
      ghcr_log "NAS 에서 직접 빌드(--build): ${GHCR_SERVICES[*]}"
      export BUILDX_GIT_INFO=0
      if ! ghcr_compose build "${GHCR_SERVICES[@]}"; then
        echo "배포 중단: 빌드에 실패했습니다. 실행 중인 서비스는 바꾸지 않았습니다." >&2
        exit 1
      fi
      for image in "${GHCR_IMAGES[@]}"; do
        ghcr_state_set "$image" "${sha}-nas" "$GHCR_PREV_SHA"
      done
      ;;
    rollback)
      ghcr_rollback
      ;;
  esac
}

# :previous ↔ :deployed 맞바꾸기. 하나라도 :previous 가 없으면 아무것도 바꾸지 않고 종료 1.
ghcr_rollback() {
  local image current previous
  for image in "${GHCR_IMAGES[@]}"; do
    if ! ghcr_has_image "$image:previous"; then
      echo "되돌리기 중단: $image:previous 가 없습니다. 아무것도 바꾸지 않았습니다." >&2
      echo "  (처음 배포 뒤라면 이전 이미지가 없습니다. 고친 코드를 push 해 다시 배포하거나 $GHCR_BUILD_HINT)" >&2
      exit 1
    fi
  done
  for image in "${GHCR_IMAGES[@]}"; do
    current="$(ghcr_state_get "$image" 2)"
    previous="$(ghcr_state_get "$image" 3)"
    if ghcr_has_image "$image:deployed"; then
      docker tag "$image:deployed" "$image:rollback-tmp"
      docker tag "$image:previous" "$image:deployed"
      docker tag "$image:rollback-tmp" "$image:previous"
      docker rmi "$image:rollback-tmp" >/dev/null
    else
      docker tag "$image:previous" "$image:deployed"
    fi
    ghcr_state_set "$image" "$previous" "$current"
    ghcr_log "$image:previous(${previous:0:12}) → :deployed, 되돌리기 전 이미지(${current:0:12})는 :previous 로"
  done
}

# 옛 이미지 정리: 이 앱 이미지 이름의 태그 중 :deployed·:previous 가 가리키지 않는 것을 지우고,
# 태그를 잃은 옛 이미지와 이 저장소 라벨이 붙은 dangling 이미지를 지운다(다른 앱 이미지는 건드리지 않는다).
ghcr_cleanup() {
  local image keep tag id old
  for image in "${GHCR_IMAGES[@]}"; do
    keep=" $(ghcr_image_id "$image:deployed") $(ghcr_image_id "$image:previous") "
    while read -r tag id; do
      [ -z "$tag" ] && continue
      case "$tag" in deployed | previous | "<none>") continue ;; esac
      if [[ "$keep" != *" $id "* ]]; then
        docker rmi "$image:$tag" >/dev/null 2>&1 && ghcr_log "옛 이미지 지움: $image:${tag:0:12}"
      fi
    done < <(docker image ls "$image" --no-trunc --format '{{.Tag}} {{.ID}}' 2>/dev/null)
  done
  for old in ${GHCR_OLD_IDS[@]+"${GHCR_OLD_IDS[@]}"}; do
    if [ "$(docker image inspect -f '{{len .RepoTags}}' "$old" 2>/dev/null || echo 1)" = "0" ]; then
      docker rmi "$old" >/dev/null 2>&1 && ghcr_log "태그 없는 옛 이미지 지움: ${old:7:12}"
    fi
  done
  docker image prune -f --filter "label=org.opencontainers.image.source=https://github.com/${GHCR_REPO}" >/dev/null 2>&1 || true
}

# NAS 폴더가 main 브랜치일 때만 배포한다(git pull 전에 부른다, --build 도 같다). 다른 브랜치면 아무것도 바꾸지 않고 종료 1.
# rollback 은 git 을 쓰지 않으므로 보지 않는다.
ghcr_require_main() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")"
  if [ "$branch" != "main" ]; then
    echo "NAS 폴더가 $branch 에 있습니다. main 만 배포합니다: sudo git checkout main 후 다시 실행" >&2
    exit 1
  fi
}

# 실패했을 때 되돌리기 안내
ghcr_rollback_hint() {
  local image
  for image in "${GHCR_IMAGES[@]}"; do
    if ! ghcr_has_image "$image:previous"; then
      echo "  (보관된 $image:previous 가 없습니다. 고친 코드를 push 해 다시 배포하거나 $GHCR_BUILD_HINT)" >&2
      return 0
    fi
  done
  echo "  이전 이미지로 되돌리기: sudo $0 rollback" >&2
}

# 서비스 하나짜리 배포 흐름(포털·재고·장비·인사 네 스크립트·경영지원이 같이 쓴다)
#   필요한 값: APP_DIR SERVICE CONTAINER (선택) DONE_MSG WAIT_SECONDS WAIT_INTERVAL
#   GHCR_CHECK_LOCAL_CHANGES=1 이면 git pull 전에 NAS 에서 직접 고친 추적 파일을 검사한다
#   GHCR_UP_ARGS: docker compose up -d 에 더할 인자(예: --no-deps)
# 실행 중 git pull 로 이 파일·배포 스크립트가 바뀌어도 이미 읽은 함수로 끝까지 돈다.
ghcr_standard_main() {
  # root 가 아니면 sudo -H 로 다시 실행 (폴더 소유자가 달라 root 의 git 설정 /root/.gitconfig·docker 를 쓴다)
  if [ "$(id -u)" -ne 0 ]; then
    exec sudo -H bash "$0" "$@"
  fi
  ghcr_parse_mode "${1:-}"
  local wait_seconds="${WAIT_SECONDS:-60}" wait_interval="${WAIT_INTERVAL:-5}"
  local up_args=()
  if [ -n "${GHCR_UP_ARGS+x}" ]; then
    up_args=("${GHCR_UP_ARGS[@]}")
  fi

  cd "$APP_DIR" || exit 1

  if [ "$GHCR_MODE" = "rollback" ]; then
    echo "[1/4] 되돌리기 (git pull 하지 않음)"
  else
    echo "[1/4] git pull"
    # 폴더 소유자가 달라 생기는 dubious ownership 방지 — 목록에 없을 때만 추가
    local safe_dirs
    safe_dirs="$(git config --global --get-all safe.directory || true)"
    if ! grep -qxF -- "$APP_DIR" <<<"$safe_dirs"; then
      echo "      safe.directory 추가: $APP_DIR"
      git config --global --add safe.directory "$APP_DIR"
    fi
    ghcr_require_main
    # NAS 에서 직접 고친 추적 파일이 있으면 멈춘다 (추적하지 않는 새 파일은 괜찮다)
    if [ "${GHCR_CHECK_LOCAL_CHANGES:-0}" = "1" ] && { ! git diff --quiet || ! git diff --cached --quiet; }; then
      echo "배포 중단: NAS 에서 직접 고친 파일이 있습니다"
      git status --short --untracked-files=no
      echo "NAS 에서 직접 고친 내용은 GitHub 코드에 먼저 옮긴 뒤 git checkout -- <파일> 로 되돌리고 다시 배포하세요"
      exit 1
    fi
    git pull
  fi

  echo "[2/4] 이미지 준비 ($GHCR_MODE: ${GHCR_IMAGES[*]})"
  ghcr_prepare_images

  # stop 없이 up -d — 이미지가 바뀌면 compose 가 컨테이너를 교체한다
  # (stop 을 먼저 하면 "Running" 만 찍고 안 켜진 적이 있다)
  echo "[3/4] docker compose up -d ${up_args[*]+${up_args[*]} }$SERVICE"
  ghcr_compose up -d ${up_args[@]+"${up_args[@]}"} "$SERVICE"

  echo "[4/4] 실행 확인 ($CONTAINER, 최대 ${wait_seconds}초)"
  local waited=0 retried=0 running
  while true; do
    running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
    [ "$running" = "true" ] && break
    # 첫 확인에서 꺼져 있으면 한 번 더 켠다 (이미지가 그대로일 때 up -d 가 켜지 않은 경우 대비)
    if [ "$retried" -eq 0 ]; then
      retried=1
      echo "      실행 중이 아님 — docker compose up -d $SERVICE 다시 실행"
      ghcr_compose up -d ${up_args[@]+"${up_args[@]}"} "$SERVICE" || true
    fi
    if [ "$waited" -ge "$wait_seconds" ]; then
      docker ps -a --filter "name=$CONTAINER" || true
      docker logs --tail 30 "$CONTAINER" || true
      echo "배포 실패: 컨테이너가 실행 중이 아님"
      ghcr_rollback_hint
      exit 1
    fi
    sleep "$wait_interval"
    waited=$((waited + wait_interval))
  done

  ghcr_cleanup
  git log --oneline -1
  docker ps --filter "name=$CONTAINER" --format '{{.Names}}  {{.Status}}  {{.Image}}'
  echo "${DONE_MSG:-배포 완료!}"
}
