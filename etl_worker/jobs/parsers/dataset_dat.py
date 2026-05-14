"""dataset.dat 파서 — Phase 4 Step 26.

측정 폴더 안의 dataset.dat = 측정 raw 들의 계산 결과 summary.
한 파일 = N sample (raw + applied 두 row 씩) + day-level Tol 6 값.

운영 패턴:
- 사용자가 raw 측정 후 별도 계산해 dataset.dat 생성
- 수정 없음. 한 번 만들면 끝
- raw 폴더에 나중에 추가될 수 있음 (선택 적재)

파일 구조:
- Tab 구분, CRLF, ASCII
- 줄 1: Tol_RR=... Tol_TCR_h=... 등 6 Tol 값
- 줄 2: ID Label R25 R85 R25_R85 TMI dT TCR_s TCR_h TCR_c T_h T_c Bse Bsw DeltaT Status Message
- 줄 3~: 데이터 (한 sample 당 raw row + applied row 2개, 빈 줄로 구분 가능)

적재 규칙:
- dT 만 applied row 의 값
- 나머지 11 metric (R25, R85, R25_R85, TMI, TCR_s, TCR_h, TCR_c, T_h, T_c, Bse, Bsw) → raw row 의 값
- DeltaT, Status, Message → SKIP
- Tol 6 값 → 모든 sample row 에 복사 저장

전략 (measurement_dat 와 같은 패턴):
- 트리 traversal: /data_vo2/.../<YYYY>/<YYYYMMDD>/<N>?/dataset.dat
- source_files 안 거침 (file_path + sha256 UNIQUE 로 멱등성)
- savepoint(begin_nested) 로 sample row 단위 격리
- file-level 깨짐 (헤더 잘못, 17 컬럼 안 맞음 등) → log warning + INSERT 안 함
- sample row 깨짐 (raw/applied 한 쪽만 / Label 값 이상 / 숫자 변환 실패) → 격리 INSERT, parse_status='error', raw_json 보존
"""

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from shared.db import session_scope_writer

log = logging.getLogger("etl.parsers.dataset_dat")

# measurement_dat 와 같은 ROOT 사용
DATASET_ROOT = "/data_vo2/VO2 data (NAS)/data"

DATASET_FILENAME = "dataset.dat"

# 폴더 정규식 (measurement_dat 와 동일)
YEAR_RE = re.compile(r'^\d{4}$')
DATE_RE = re.compile(r'^\d{8}$')

# Tol 헤더 정규식 — Tol_KEY=VALUE 형식
TOL_PATTERN = re.compile(r'(Tol_\w+)=([0-9.\-+eE]+)')

# 기대하는 17 컬럼 헤더
EXPECTED_HEADER = [
    'ID', 'Label', 'R25', 'R85', 'R25_R85', 'TMI', 'dT',
    'TCR_s', 'TCR_h', 'TCR_c', 'T_h', 'T_c', 'Bse', 'Bsw',
    'DeltaT', 'Status', 'Message'
]

# Tol 키 → DB 컬럼 매핑
TOL_COLUMN_MAP = {
    'Tol_RR': 'tol_rr',
    'Tol_TCR_h': 'tol_tcr_h',
    'Tol_TCR_c': 'tol_tcr_c',
    'Tol_dT': 'tol_dt',
    'Tol_Bse': 'tol_bse',
    'Tol_sw': 'tol_sw',
}

# raw row 에서 가져올 metric 컬럼 (DeltaT 이전 = index 2~13)
# Label column (index 1) 제외하고 R25 부터 Bsw 까지 (12 컬럼 중 dT 제외하면 11)
RAW_METRICS = {
    'R25': 'r25',
    'R85': 'r85',
    'R25_R85': 'r25_r85_ratio',
    'TMI': 'tmi',
    'TCR_s': 'tcr_s',
    'TCR_h': 'tcr_h',
    'TCR_c': 'tcr_c',
    'T_h': 't_h',
    'T_c': 't_c',
    'Bse': 'bse',
    'Bsw': 'bsw',
}

# applied row 에서 가져올 metric (dT 만)
APPLIED_METRICS = {
    'dT': 'dt',
}

BATCH_SIZE = 50
_CHUNK = 64 * 1024


def _compute_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _extract_folder_meta(file_path: Path) -> tuple[Optional[int], Optional[date], Optional[int]]:
    """파일 경로에서 (year, measurement_date, process_seq) 추출.
    measurement_dat 의 _extract_folder_meta 와 같은 로직.
    """
    try:
        rel = file_path.relative_to(DATASET_ROOT)
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


def _discover_dataset_files() -> list[Path]:
    """트리 traversal — 모든 dataset.dat 파일 찾기.

    /data_vo2/.../<YYYY>/<YYYYMMDD>/<N>?/dataset.dat

    N 폴더 있을 수도 / 없을 수도 — 둘 다 지원.
    """
    base = Path(DATASET_ROOT)
    if not base.exists():
        return []

    results = []
    for dirpath, dirnames, filenames in os.walk(base):
        if DATASET_FILENAME in filenames:
            results.append(Path(dirpath) / DATASET_FILENAME)

    return results


def _parse_tol_header(line: str) -> Optional[dict]:
    """줄 1 (Tol 헤더) 파싱 → {'tol_rr': 44.036, ...} dict.

    형식: Tol_RR=44.036\tTol_TCR_h=20.211\t...
    """
    tols = {}
    for match in TOL_PATTERN.finditer(line):
        key = match.group(1)
        if key in TOL_COLUMN_MAP:
            try:
                tols[TOL_COLUMN_MAP[key]] = float(match.group(2))
            except ValueError:
                continue

    if len(tols) != len(TOL_COLUMN_MAP):
        return None  # 6개 Tol 모두 못 찾으면 파일 깨짐
    return tols


def _is_already_processed(file_path: str, sha: str) -> bool:
    """같은 (file_path, sha256) 가 이미 DB 에 있으면 True."""
    sql = text("""
        SELECT 1 FROM vo2.measurement_summary
        WHERE file_path = :fp AND sha256 = :sha
        LIMIT 1
    """)
    with session_scope_writer() as s:
        row = s.execute(sql, {"fp": file_path, "sha": sha}).fetchone()
    return row is not None


def _build_payload(
    file_path: Path,
    sha: str,
    file_size: int,
    file_mtime: datetime,
    folder_meta: tuple,
    sample_id: int,
    raw_values: dict,
    applied_values: dict,
    tols: dict,
    parse_status: str,
    parse_error: Optional[str],
    raw_json_obj: dict,
) -> dict:
    """sample 한 개 payload."""
    year, measurement_date, process_seq = folder_meta

    payload = {
        "file_path": str(file_path),
        "file_name": file_path.name,
        "file_dir": str(file_path.parent),
        "sha256": sha,
        "year": year,
        "measurement_date": measurement_date,
        "process_seq": process_seq,
        "sample_id": sample_id,
        "file_size": file_size,
        "file_mtime": file_mtime,
        "parse_status": parse_status,
        "parse_error": parse_error,
        "raw_json": json.dumps(raw_json_obj, ensure_ascii=False, default=str),
    }

    # raw row 에서 추출 (None 가능 — 격리 INSERT 케이스)
    for src_col, db_col in RAW_METRICS.items():
        payload[db_col] = raw_values.get(src_col) if raw_values else None

    # applied row 에서 추출 (dT 만)
    for src_col, db_col in APPLIED_METRICS.items():
        payload[db_col] = applied_values.get(src_col) if applied_values else None

    # Tol 6 값 (모든 row 동일)
    for db_col in TOL_COLUMN_MAP.values():
        payload[db_col] = tols.get(db_col) if tols else None

    return payload


_INSERT_SQL = text("""
INSERT INTO vo2.measurement_summary (
    file_path, file_name, file_dir, sha256,
    year, measurement_date, process_seq, sample_id,
    r25, r85, r25_r85_ratio, tmi, tcr_s, tcr_h, tcr_c,
    t_h, t_c, bse, bsw,
    dt,
    tol_rr, tol_tcr_h, tol_tcr_c, tol_dt, tol_bse, tol_sw,
    raw_json, parse_status, parse_error,
    file_size, file_mtime
) VALUES (
    :file_path, :file_name, :file_dir, :sha256,
    :year, :measurement_date, :process_seq, :sample_id,
    :r25, :r85, :r25_r85_ratio, :tmi, :tcr_s, :tcr_h, :tcr_c,
    :t_h, :t_c, :bse, :bsw,
    :dt,
    :tol_rr, :tol_tcr_h, :tol_tcr_c, :tol_dt, :tol_bse, :tol_sw,
    CAST(:raw_json AS JSONB), :parse_status, :parse_error,
    :file_size, :file_mtime
)
ON CONFLICT (file_path, sha256, sample_id) DO NOTHING
""")


def _try_float(v: str) -> Optional[float]:
    """안전한 float 변환. 실패 시 None."""
    if v is None:
        return None
    try:
        f = float(v.strip())
        if f != f or f == float('inf') or f == float('-inf'):
            return None
        return f
    except (ValueError, AttributeError):
        return None


def _parse_one_file(file_path: Path, counters: dict) -> None:
    """한 dataset.dat 파일 처리."""
    # 메타 + sha
    try:
        stat = file_path.stat()
        file_size = stat.st_size
        file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        sha = _compute_sha256(file_path)
    except Exception as e:
        log.warning(f"cannot stat/sha {file_path}: {type(e).__name__}: {e}")
        counters['files_failed'] += 1
        return

    # 이미 처리된 파일 (incremental skip)
    if _is_already_processed(str(file_path), sha):
        counters['files_skipped'] += 1
        return

    # 파일 읽기
    try:
        with open(file_path, 'r', encoding='ascii') as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='latin-1') as f:
                content = f.read()
        except Exception as e:
            log.warning(f"cannot read {file_path}: {type(e).__name__}: {e}")
            counters['files_failed'] += 1
            return

    lines = content.splitlines()
    if len(lines) < 3:
        log.warning(f"dataset.dat too short ({len(lines)} lines): {file_path}")
        counters['files_failed'] += 1
        return

    # 줄 1: Tol 헤더
    tols = _parse_tol_header(lines[0])
    if tols is None:
        log.warning(f"dataset.dat Tol header parse failed: {file_path}")
        counters['files_failed'] += 1
        return

    # 줄 2: 컬럼 헤더 검증
    header_cols = lines[1].split('\t')
    if header_cols != EXPECTED_HEADER:
        log.warning(
            f"dataset.dat header mismatch in {file_path}: "
            f"got {header_cols[:5]}... expected {EXPECTED_HEADER[:5]}..."
        )
        counters['files_failed'] += 1
        return

    # 줄 3~: 데이터. ID 별로 raw + applied 묶음
    folder_meta = _extract_folder_meta(file_path)
    samples = {}  # sample_id → {'raw': dict, 'applied': dict, 'raw_row': str, 'applied_row': str}

    for line in lines[2:]:
        line = line.strip()
        if not line:
            continue  # 빈 줄 skip

        cols = line.split('\t')
        if len(cols) < len(EXPECTED_HEADER):
            log.warning(f"row column count mismatch in {file_path}: {len(cols)}/{len(EXPECTED_HEADER)}")
            continue

        # 컬럼 매핑
        row_dict = dict(zip(EXPECTED_HEADER, cols))
        try:
            sample_id = int(row_dict['ID'])
        except (ValueError, KeyError):
            log.warning(f"sample_id parse failed in {file_path}: ID={row_dict.get('ID')}")
            continue

        label = row_dict.get('Label', '').strip().lower()
        if label not in ('raw', 'applied'):
            log.warning(f"unknown Label '{label}' in {file_path} ID={sample_id}")
            continue

        # numeric 변환
        numeric = {}
        for col in RAW_METRICS.keys():
            numeric[col] = _try_float(row_dict.get(col))
        for col in APPLIED_METRICS.keys():
            numeric[col] = _try_float(row_dict.get(col))

        if sample_id not in samples:
            samples[sample_id] = {}
        samples[sample_id][label] = numeric
        samples[sample_id][f'{label}_row'] = row_dict

    # 각 sample INSERT (savepoint 격리)
    if not samples:
        log.info(f"dataset.dat no valid samples: {file_path}")
        counters['files_no_samples'] += 1
        return

    inserted = 0
    errors = 0

    with session_scope_writer() as s:
        for sample_id, parts in sorted(samples.items()):
            raw_values = parts.get('raw')
            applied_values = parts.get('applied')
            raw_row = parts.get('raw_row')
            applied_row = parts.get('applied_row')

            # 정상: raw + applied 둘 다 있음
            if raw_values and applied_values:
                parse_status = 'ok'
                parse_error = None
            else:
                parse_status = 'error'
                missing = []
                if not raw_values:
                    missing.append('raw')
                if not applied_values:
                    missing.append('applied')
                parse_error = f"missing: {', '.join(missing)}"

            raw_json_obj = {
                'raw_row': raw_row,
                'applied_row': applied_row,
            }

            payload = _build_payload(
                file_path, sha, file_size, file_mtime, folder_meta,
                sample_id, raw_values, applied_values, tols,
                parse_status, parse_error, raw_json_obj,
            )

            try:
                with s.begin_nested():
                    s.execute(_INSERT_SQL, payload)
                if parse_status == 'ok':
                    inserted += 1
                else:
                    errors += 1
            except Exception as e:
                log.warning(
                    f"sample INSERT failed in {file_path} id={sample_id}: "
                    f"{type(e).__name__}: {e}"
                )
                errors += 1

    counters['samples_inserted'] += inserted
    counters['samples_with_error'] += errors
    counters['files_inserted'] += 1
    log.info(
        f"dataset.dat {file_path}: +{inserted} samples, "
        f"{errors} errors (n_samples={len(samples)})"
    )


def parse_datasets_tree() -> dict:
    """dataset.dat 트리 traversal + 적재. sync_sputter 에서 호출.

    measurement_dat 의 parse_measurements_tree() 와 같은 패턴.

    Returns: {"status": "ok"|"error", "files_seen": N, ...}
    """
    base = Path(DATASET_ROOT)
    if not base.exists():
        log.error(f"DATASET_ROOT not found: {base}")
        return {
            "status": "error",
            "error": f"DATASET_ROOT not found: {base}",
            "files_seen": 0,
            "files_inserted": 0,
            "samples_inserted": 0,
            "samples_with_error": 0,
            "files_skipped": 0,
            "files_failed": 0,
        }

    log.info(f"=== datasets_tree start: {base} ===")

    try:
        files = _discover_dataset_files()
    except Exception as e:
        log.error(f"discover failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error": f"discover failed: {e}",
            "files_seen": 0,
            "files_inserted": 0,
            "samples_inserted": 0,
            "samples_with_error": 0,
            "files_skipped": 0,
            "files_failed": 0,
        }

    files_seen = len(files)
    log.info(f"discovered {files_seen} dataset.dat files")

    counters = {
        'files_inserted': 0,
        'files_skipped': 0,
        'files_no_samples': 0,
        'files_failed': 0,
        'samples_inserted': 0,
        'samples_with_error': 0,
    }

    for file_path in files:
        try:
            _parse_one_file(file_path, counters)
        except Exception as e:
            log.exception(f"unexpected error processing {file_path}")
            counters['files_failed'] += 1

    log.info(
        f"=== datasets_tree done: "
        f"{counters['files_inserted']} files OK ({counters['samples_inserted']} samples + "
        f"{counters['samples_with_error']} err), "
        f"{counters['files_skipped']} skipped (already), "
        f"{counters['files_no_samples']} no-samples, "
        f"{counters['files_failed']} failed "
        f"(of {files_seen} discovered) ==="
    )

    return {
        "status": "ok",
        "files_seen": files_seen,
        **counters,
    }
