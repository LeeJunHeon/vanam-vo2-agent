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
    """파일 경로에서 (year, measurement_date, process_seq) 추출.

    /data_vo2/VO2 data (NAS)/data/2026/20260506/1/file.dat
                                  ^^^^ ^^^^^^^^ ^
                                  year date     seq
    """
    try:
        rel = file_path.relative_to(MEASUREMENT_ROOT)
    except ValueError:
        return (None, None, None)

    parts = rel.parts  # ('2026', '20260506', '1', 'file.dat') 등

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
        parent = parts[-2]  # 파일의 직속 부모
        if parent.isdigit() and len(parent) <= 3:
            process_seq = int(parent)

    return (year, measurement_date, process_seq)


def _extract_filename_meta(file_name: str, suffix_n: int) -> dict:
    """파일명에서 process_seq_in_name, sample_seq, sub_label/kind/batch 추출.

    "1-1-ncd(236)_p_big-F_0.10V_0.dat" (suffix_n=0)
       ↓
    {
      'process_seq_in_name': 1,
      'sample_seq': 1,
      'sub_label_raw': 'ncd(236)',
      'sub_kind': 'ncd',
      'sub_batch_no': 236,
    }

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
        # 폴더 안 .dat 파일들을 base별로 그룹화
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

            existing = groups.get(base)
            if existing is None or existing[0] < n:
                groups[base] = (n, fname)

        # 그룹별 max만 candidates에 추가
        for base, (n, fname) in groups.items():
            candidates.append((Path(dirpath) / fname, base, n))

    return candidates


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


# ─────────── 메인 ───────────

def parse_measurements_tree() -> dict:
    """측정 .dat 트리 traversal + 적재. sync_sputter에서 호출.

    Returns:
        {"status": "ok"|"error", "files_seen": N, "files_inserted": M,
         "files_skipped": K, "errors": E, ...}
    """
    base = Path(MEASUREMENT_ROOT)
    if not base.exists():
        log.error(f"MEASUREMENT_ROOT not found: {base}")
        return {
            "status": "error",
            "error": f"MEASUREMENT_ROOT not found: {base}",
            "files_seen": 0, "files_inserted": 0,
            "files_skipped": 0, "errors": 0,
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
            "files_seen": 0, "files_inserted": 0,
            "files_skipped": 0, "errors": 0,
        }

    files_seen = len(candidates)
    log.info(f"discovered {files_seen} candidate .dat files (max-N selected per group)")

    files_inserted = 0
    files_skipped = 0
    errors = 0

    # batch commit
    for batch_idx in range(0, files_seen, BATCH_SIZE):
        batch = candidates[batch_idx : batch_idx + BATCH_SIZE]

        with session_scope_writer() as s:
            for file_path, base_name, suffix_n in batch:
                try:
                    # 파일 메타
                    stat = file_path.stat()
                    file_size = stat.st_size
                    file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

                    # sha256
                    sha = _compute_sha256(file_path)

                    # .dat 내용 파싱
                    raw_header, temps, resistances = _parse_dat_content(file_path)
                    point_count = len(temps)
                    parse_status = 'ok' if point_count > 0 else 'header_only'

                    # 폴더 메타
                    year, measurement_date, process_seq = _extract_folder_meta(file_path)

                    # 파일명 메타
                    fname_meta = _extract_filename_meta(file_path.name, suffix_n)

                    # INSERT
                    s.execute(_INSERT_SQL, {
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
                        'point_count': point_count,
                        'temperature_c': temps if temps else None,
                        'resistance_ohm': resistances if resistances else None,
                        'file_size': file_size,
                        'file_mtime': file_mtime,
                        'sha256': sha,
                        'parse_status': parse_status,
                        'raw_header': raw_header,
                    })
                    files_inserted += 1

                except Exception as e:
                    log.warning(f"failed to process {file_path}: {type(e).__name__}: {e}")
                    errors += 1
                    continue

        log.info(f"batch {batch_idx + len(batch)}/{files_seen} processed")

    log.info(
        f"=== measurements_tree done: "
        f"+{files_inserted} inserted, {files_skipped} skipped, {errors} errors "
        f"(of {files_seen} candidates) ==="
    )
    return {
        "status": "ok",
        "files_seen": files_seen,
        "files_inserted": files_inserted,
        "files_skipped": files_skipped,
        "errors": errors,
    }
