"""VO2 측정 .dat 파서 — 트리 traversal → vo2.measurements.

전략: ETL은 트리 traversal + 파일 → DB 단순 복사. 해석은 agent.

폴더 구조 (NAS):
  /data_vo2/VO2 data (NAS)/data/
    └── {year, 4자리}/         예: 2026
          └── {date, 8자리 YYYYMMDD}/   예: 20260506
                ├── {seq, 1~3자리 숫자}/   예: 1, 2, 3 (공정 순번 — 옵션)
                │   └── *.dat
                └── *.dat                 (공정 폴더 없는 케이스도 있음)

파일명 규칙:
  채택: ^(.+)_(\\d+)\\.dat$
    예: 1-1-ncd(236)_p_big-F_0.10V_0.dat → base="...0.10V", n=0
        같은 base에 _0, _1, _2가 있으면 max(n)만 채택
  거부: ..._merged.dat, ..._applied_TCR.dat, dataset.dat 등 (정규식 안 맞음)

파일명 추출 (best-effort):
  ^(\\d+)-(\\d+)-(.+?)$ 매칭
    예: "1-1-ncd(236)_p_big-F_0.10V" → process_seq=1, sample_seq=1, rest="ncd(236)_p_big-F_0.10V"

  rest에서 sub_label 추출:
    [Nn][Cc][Dd] → sub_kind='ncd'
    [Rr]         → sub_kind='rayvac'
    매칭 안 되면 NULL

.dat 내용:
  R1: 헤더 ("Temp   Resistance")
  R2~: 두 컬럼 (탭/공백 구분), 과학 표기
  ASCII, CRLF

실패 row 정책 (Phase 4 Step 17 fix):
  - savepoint(begin_nested)로 한 row 실패가 batch 나머지에 영향 안 줌
  - 실패 row도 메타는 INSERT (시계열 NULL, parse_status='error', raw_header에 에러 메시지)
  - 데이터 유실 0 — 운영자/agent가 SELECT WHERE parse_status='error'로 조회 가능
"""

import hashlib
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from shared.db import session_scope_writer

log = logging.getLogger("etl.parsers.measurement_dat")

# 마운트 매핑: /volume1/VanaM_VO2 → /data_vo2:ro (docker-compose)
MEASUREMENT_ROOT = "/data_vo2/VO2 data (NAS)/data"

# 파일명 정규식
SUFFIX_RE = re.compile(r'^(.+)_(\d+)\.dat$')
PREFIX_RE = re.compile(r'^(\d+)-(\d+)-(.+?)$')
SUB_NCD_RE = re.compile(r'(?i)\bn[c]d\s*\(\s*(\d+)\s*\)')
SUB_R_RE = re.compile(r'(?<![A-Za-z_])[Rr]\s*\(\s*(\d+)\s*\)')

# 폴더 정규식 (skip 대상 필터링)
YEAR_RE = re.compile(r'^\d{4}$')
DATE_RE = re.compile(r'^\d{8}$')

# Batch commit 단위 (메모리 안전)
BATCH_SIZE = 100

# 방어막 ①: 후보 단계 race guard (header_only 중복 적재 방지)
MIN_DAT_SIZE_BYTES = 100              # 헤더만 적힌 상태(~22 byte) 차단. 정상 측정 최소 5.8KB라 안전
DISCOVER_MTIME_GRACE_SECONDS = 60     # 60초 이내 mtime은 장비 쓰기 진행 중 가능성

# 방어막 ②: INSERT 직전 안전망
HEADER_ONLY_GRACE_HOURS = 24          # 24h 안쪽 header_only는 race로 간주 skip, 24h+ 는 진짜 실패로 error 격리

# sha256 chunk
_CHUNK = 64 * 1024


# ─────────── 헬퍼 ───────────

def _compute_sha256(p: Path) -> str:
    """파일 sha256 hex digest."""
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _parse_dat_content(file_path: Path) -> tuple[str, list, list]:
    """.dat 파일 → (raw_header, temps[], resistances[]).

    R1: 헤더 ("Temp   Resistance")
    R2~: 두 컬럼 (탭/공백). 변환 실패 줄은 skip.
    인코딩: ASCII (latin-1로 fallback).
    """
    try:
        with open(file_path, 'r', encoding='ascii') as f:
            lines = f.read().splitlines()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='latin-1') as f:
            lines = f.read().splitlines()

    if not lines:
        return ('', [], [])

    raw_header = lines[0].strip()
    temps = []
    resistances = []

    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            t = float(parts[0])
            r = float(parts[1])
            # NaN/Inf 차단
            if t != t or r != r or t == float('inf') or r == float('inf') or t == float('-inf') or r == float('-inf'):
                continue
            temps.append(t)
            resistances.append(r)
        except (ValueError, TypeError):
            continue

    return (raw_header, temps, resistances)


def _extract_folder_meta(file_path: Path) -> tuple[Optional[int], Optional[date], Optional[int]]:
    """파일 경로에서 (year, measurement_date, process_seq) 추출."""
    try:
        rel = file_path.relative_to(MEASUREMENT_ROOT)
    except ValueError:
        return (None, None, None)

    parts = rel.parts

    year = None
    measurement_date = None
    process_seq = None

    if len(parts) >= 1 and YEAR_RE.match(parts[0]):
        year = int(parts[0])

    if len(parts) >= 2 and DATE_RE.match(parts[1]):
        try:
            measurement_date = date(
                year=int(parts[1][:4]),
                month=int(parts[1][4:6]),
                day=int(parts[1][6:8]),
            )
        except ValueError:
            pass

    # 직속 부모가 1~3자리 숫자면 공정 순번
    if len(parts) >= 3:
        parent = parts[-2]
        if parent.isdigit() and len(parent) <= 3:
            process_seq = int(parent)

    return (year, measurement_date, process_seq)


def _extract_filename_meta(file_name: str, suffix_n: int) -> dict:
    """파일명에서 process_seq_in_name, sample_seq, sub_label/kind/batch 추출.

    매칭 실패한 부분은 NULL.
    """
    result = {
        'process_seq_in_name': None,
        'sample_seq': None,
        'sub_label_raw': None,
        'sub_kind': None,
        'sub_batch_no': None,
    }

    # 1단계: suffix 제거된 base 부분
    suffix_part = f'_{suffix_n}.dat'
    if file_name.endswith(suffix_part):
        base = file_name[:-len(suffix_part)]
    else:
        return result

    # 2단계: prefix `^N-M-rest$`
    prefix_m = PREFIX_RE.match(base)
    if not prefix_m:
        return result

    result['process_seq_in_name'] = int(prefix_m.group(1))
    result['sample_seq'] = int(prefix_m.group(2))
    rest = prefix_m.group(3)

    # 3단계: rest에서 NCD 우선 매칭, 그 다음 R
    ncd_m = SUB_NCD_RE.search(rest)
    if ncd_m:
        result['sub_label_raw'] = ncd_m.group(0)
        result['sub_kind'] = 'ncd'
        result['sub_batch_no'] = int(ncd_m.group(1))
        return result

    r_m = SUB_R_RE.search(rest)
    if r_m:
        result['sub_label_raw'] = r_m.group(0)
        result['sub_kind'] = 'rayvac'
        result['sub_batch_no'] = int(r_m.group(1))

    return result


def _discover_dat_files(base_path: Path) -> list[tuple[Path, str, int]]:
    """트리 traversal + 그룹화.

    Returns: [(file_path, base_name, suffix_n), ...] — 가장 큰 N만 남음.
    """
    candidates = []

    for dirpath, dirnames, filenames in os.walk(base_path):
        groups = {}  # base → (max_n, filename)
        for fname in filenames:
            m = SUFFIX_RE.match(fname)
            if not m:
                continue
            base, n_str = m.group(1), m.group(2)
            try:
                n = int(n_str)
            except ValueError:
                continue

            # 방어막 ① — race / 쓰기 진행 중 후보 제외
            full_path = Path(dirpath) / fname
            try:
                st = full_path.stat()
            except OSError as e:
                log.warning(f"stat failed for {full_path}: {type(e).__name__}: {e}")
                continue

            file_size = st.st_size
            file_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            now = datetime.now(timezone.utc)
            age_seconds = (now - file_mtime).total_seconds()

            # 1A: 매우 작은 파일이고 mtime이 24h 안쪽이면 race 가능성 → 후보 제외
            # (24h+ 이면 진짜 실패 가능성이라 후보에 넣어 방어막 ②가 error로 격리)
            if file_size < MIN_DAT_SIZE_BYTES and age_seconds < HEADER_ONLY_GRACE_HOURS * 3600:
                log.debug(
                    f"discover skip race: {full_path} "
                    f"size={file_size} byte, age={age_seconds/60:.1f} min"
                )
                continue

            # 1B: mtime이 60초 안쪽이면 장비가 쓰기 진행 중 → 후보 제외
            if age_seconds < DISCOVER_MTIME_GRACE_SECONDS:
                log.debug(
                    f"discover skip recent mtime: {full_path} age={age_seconds:.1f}s"
                )
                continue

            existing = groups.get(base)
            if existing is None or existing[0] < n:
                groups[base] = (n, fname)

        for base, (n, fname) in groups.items():
            candidates.append((Path(dirpath) / fname, base, n))

    return candidates


def _build_payload(
    file_path: Path,
    suffix_n: int,
    sha: str,
    file_size: Optional[int],
    file_mtime: Optional[datetime],
    raw_header: str,
    temps: list,
    resistances: list,
    parse_status: str,
) -> dict:
    """공통 payload 구성. 정상/에러 모두 사용."""
    year, measurement_date, process_seq = _extract_folder_meta(file_path)
    fname_meta = _extract_filename_meta(file_path.name, suffix_n)

    return {
        'file_path': str(file_path),
        'file_name': file_path.name,
        'file_dir': str(file_path.parent),
        'year': year,
        'measurement_date': measurement_date,
        'process_seq': process_seq,
        'process_seq_in_name': fname_meta['process_seq_in_name'],
        'sample_seq': fname_meta['sample_seq'],
        'sub_label_raw': fname_meta['sub_label_raw'],
        'sub_kind': fname_meta['sub_kind'],
        'sub_batch_no': fname_meta['sub_batch_no'],
        'suffix_n': suffix_n,
        'point_count': len(temps),
        'temperature_c': temps if temps else None,
        'resistance_ohm': resistances if resistances else None,
        'file_size': file_size,
        'file_mtime': file_mtime,
        'sha256': sha,
        'parse_status': parse_status,
        'raw_header': raw_header,
    }


# ─────────── SQL ───────────

_INSERT_SQL = text("""
    INSERT INTO vo2.measurements (
        file_path, file_name, file_dir,
        year, measurement_date, process_seq,
        process_seq_in_name, sample_seq, sub_label_raw, sub_kind, sub_batch_no, suffix_n,
        point_count, temperature_c, resistance_ohm,
        file_size, file_mtime, sha256,
        parse_status, raw_header
    ) VALUES (
        :file_path, :file_name, :file_dir,
        :year, :measurement_date, :process_seq,
        :process_seq_in_name, :sample_seq, :sub_label_raw, :sub_kind, :sub_batch_no, :suffix_n,
        :point_count,
        CAST(:temperature_c AS DOUBLE PRECISION[]),
        CAST(:resistance_ohm AS DOUBLE PRECISION[]),
        :file_size, :file_mtime, :sha256,
        :parse_status, :raw_header
    )
    ON CONFLICT (file_path, sha256) DO NOTHING
""")


# ─────────── 한 row 처리 ───────────

def _process_one_file(s, file_path: Path, base_name: str, suffix_n: int, counters: dict) -> None:
    """한 .dat 처리. 정상/에러 격리/race skip 분기 모두 처리.

    counters: {'files_inserted', 'files_with_error', 'fully_failed', 'skipped_header_only'}
    """
    # 메타 추출 (파일 stat + sha — 거의 실패 안 함)
    file_size = None
    file_mtime = None
    sha = None
    try:
        stat = file_path.stat()
        file_size = stat.st_size
        file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        sha = _compute_sha256(file_path)
    except Exception as e:
        stat_error = f"{type(e).__name__}: {e}"
        log.warning(f"cannot stat/sha {file_path}: {stat_error[:200]}")
        # sha 없으면 격리 INSERT도 못 함 (sha NOT NULL).
        counters['fully_failed'] += 1
        return

    # 1단계: 파싱 (savepoint 1)
    error_msg = None
    parse_status = None
    raw_header = ''
    temps: list = []
    resistances: list = []

    try:
        with s.begin_nested():
            raw_header, temps, resistances = _parse_dat_content(file_path)
        parse_status = 'ok' if temps else 'header_only'
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        log.warning(f"failed to parse {file_path}: {error_msg[:200]}")
        parse_status = 'error'

    # 1.5단계: 방어막 ② — header_only race-aware 분기
    if parse_status == 'header_only':
        if file_mtime is None:
            log.debug(f"skip header_only {file_path} (mtime unknown)")
            counters['skipped_header_only'] += 1
            return
        age_hours = (datetime.now(timezone.utc) - file_mtime).total_seconds() / 3600
        if age_hours < HEADER_ONLY_GRACE_HOURS:
            log.info(
                f"skip header_only {file_path.name} "
                f"(age {age_hours:.1f}h < {HEADER_ONLY_GRACE_HOURS}h, likely race)"
            )
            counters['skipped_header_only'] += 1
            return
        # 24h+ 지속 → 진짜 측정 실패로 간주, error 격리로 fall through
        error_msg = (
            f"header_only persisted >{HEADER_ONLY_GRACE_HOURS}h "
            f"(mtime={file_mtime.isoformat()}, size={file_size}). "
            f"진짜 측정 실패 가능성 — 운영자 확인 필요."
        )
        parse_status = 'error'

    # 2단계: ok 만 정상 INSERT
    if parse_status == 'ok':
        try:
            with s.begin_nested():
                payload = _build_payload(
                    file_path, suffix_n, sha, file_size, file_mtime,
                    raw_header, temps, resistances, 'ok',
                )
                s.execute(_INSERT_SQL, payload)
                counters['files_inserted'] += 1
                return
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            log.warning(f"failed to insert {file_path}: {error_msg[:200]}")
            # fall through 격리 INSERT

    # 3단계: 격리 INSERT (parse_status='error', 시계열 NULL) — 기존 2단계 로직 그대로
    try:
        with s.begin_nested():
            payload = _build_payload(
                file_path, suffix_n, sha, file_size, file_mtime,
                error_msg[:500] if error_msg else "unknown error",
                [], [],
                'error',
            )
            s.execute(_INSERT_SQL, payload)
            counters['files_with_error'] += 1
    except Exception as e2:
        log.error(f"isolation INSERT also failed for {file_path}: {type(e2).__name__}: {e2}")
        counters['fully_failed'] += 1


# ─────────── 메인 ───────────

def parse_measurements_tree() -> dict:
    """측정 .dat 트리 traversal + 적재. sync_sputter에서 호출.

    실패 row 정책 (Phase 4 Step 17 fix):
    - savepoint로 한 row 실패가 batch 나머지에 영향 안 줌
    - 실패 row는 메타만 INSERT (시계열 NULL, parse_status='error')
    """
    base = Path(MEASUREMENT_ROOT)
    if not base.exists():
        log.error(f"MEASUREMENT_ROOT not found: {base}")
        return {
            "status": "error",
            "error": f"MEASUREMENT_ROOT not found: {base}",
            "files_seen": 0,
            "files_inserted": 0,
            "files_with_error": 0,
            "fully_failed": 0,
            "skipped_header_only": 0,
        }

    log.info(f"=== measurements_tree start: {base} ===")
    log.info("discovering .dat files...")

    try:
        candidates = _discover_dat_files(base)
    except Exception as e:
        log.error(f"discover failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error": f"discover failed: {e}",
            "files_seen": 0,
            "files_inserted": 0,
            "files_with_error": 0,
            "fully_failed": 0,
            "skipped_header_only": 0,
        }

    files_seen = len(candidates)
    log.info(f"discovered {files_seen} candidate .dat files (max-N selected per group)")

    counters = {
        'files_inserted': 0,        # parse_status='ok'
        'files_with_error': 0,      # parse_status='error' (격리 INSERT 성공)
        'fully_failed': 0,          # 격리 INSERT도 실패 (sha 못 채운 경우 등)
        'skipped_header_only': 0,   # 방어막 ② skip 카운트 (24h 안쪽 header_only race)
    }

    for batch_idx in range(0, files_seen, BATCH_SIZE):
        batch = candidates[batch_idx : batch_idx + BATCH_SIZE]

        with session_scope_writer() as s:
            for file_path, base_name, suffix_n in batch:
                _process_one_file(s, file_path, base_name, suffix_n, counters)

        log.info(f"batch {batch_idx + len(batch)}/{files_seen} processed")

    log.info(
        f"=== measurements_tree done: "
        f"+{counters['files_inserted']} ok, "
        f"+{counters['files_with_error']} error-isolated, "
        f"{counters['fully_failed']} fully-failed, "
        f"{counters['skipped_header_only']} skipped-header-only, "
        f"(of {files_seen} candidates) ==="
    )
    return {
        "status": "ok",
        "files_seen": files_seen,
        "files_inserted": counters['files_inserted'],
        "files_with_error": counters['files_with_error'],
        "fully_failed": counters['fully_failed'],
        "skipped_header_only": counters['skipped_header_only'],
    }