"""RGA CSV 파서 — /data/RGA/Ch.1/RGA_spectrums.csv → vo2.rga_runs.

데이터 구조:
- BOM (\\ufeff)으로 시작 (utf-8-sig 인코딩으로 처리)
- R1 헤더: Time, Mass 1, Mass 2, ..., Mass 65 (총 66 컬럼)
- R2~ 데이터: 각 row가 한 측정 시점
- Time 두 형식:
  - "2024-05-17 9:53" (옛, MM only, 1자리 시간 가능)
  - "2026-05-08 16:03:15" (최근, HH:MM:SS)
- Mass 값: 과학 표기 (1.69E-09 등), 음수 가능
- 누적 단일 파일 (운영자가 sputter 공정 후 row 추가)

Phase 4 Step 27 — DELETE + INSERT 동기화 정책:
- record.metadata에 'all_processed' 있으면 skip (sha 같음)
- 새 sha → 같은 source_type+file_path 의 옛 row 모두 DELETE 후 모든 row 새로 INSERT
  → csv 의 현재 상태 = DB 의 현재 상태 (수정/삭제 모두 반영)
- 한 트랜잭션 안에서 DELETE + INSERT (원자성, 부분 실패 시 자동 rollback)
- savepoint(begin_nested) 로 한 row 실패가 batch 영향 안 줌
"""

import csv
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from shared.db import session_scope_writer
from etl_worker.jobs.scan_files import SourceFileRecord

log = logging.getLogger("etl.parsers.rga_csv")

# Time 파싱 형식 (둘 다 시도)
TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",  # 2026-05-08 16:03:15
    "%Y-%m-%d %H:%M",     # 2024-05-17 9:53
]

# Batch commit 단위
BATCH_SIZE = 200


def _parse_time(s: str) -> Optional[datetime]:
    """Time 컬럼 파싱. 두 형식 시도. 실패 시 None."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_intensity(s: str) -> Optional[float]:
    """Mass 값 파싱. 과학 표기. 실패 시 None."""
    if s is None:
        return None
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    try:
        f = float(s)
        if f != f or f == float('inf') or f == float('-inf'):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _serialize(v):
    """JSON 직렬화 헬퍼."""
    if isinstance(v, datetime):
        return v.isoformat()
    return v


_INSERT_SQL = text("""
    INSERT INTO vo2.rga_runs (
        source_file_id, row_number,
        measured_at, measured_at_raw,
        mass_count, intensity,
        raw_json, parse_status
    ) VALUES (
        :source_file_id, :row_number,
        :measured_at, :measured_at_raw,
        :mass_count,
        CAST(:intensity AS DOUBLE PRECISION[]),
        CAST(:raw_json AS JSONB),
        :parse_status
    )
    ON CONFLICT (source_file_id, row_number) DO NOTHING
""")

_UPDATE_METADATA_SQL = text("""
    UPDATE vo2.source_files
    SET metadata = CAST(:metadata AS JSONB),
        row_count = :row_count,
        parser_status = :parser_status,
        parser_error = :parser_error,
        last_indexed_at = NOW()
    WHERE id = :id
""")


def _build_payload(
    source_file_id: int,
    row_number: int,
    time_raw: str,
    measured_at: Optional[datetime],
    mass_count: int,
    intensity: list,
    header: list,
    row: list,
    parse_status: str,
) -> dict:
    """RGA row payload 구성. raw_json에 전체 원본 보존."""
    raw = {}
    for i, val in enumerate(row):
        key = header[i] if i < len(header) and header[i] else f'col_{i}'
        if isinstance(key, str):
            key = key.strip()
        raw[key] = val
    return {
        'source_file_id': source_file_id,
        'row_number': row_number,
        'measured_at': measured_at,
        'measured_at_raw': time_raw,
        'mass_count': mass_count,
        'intensity': intensity,
        'raw_json': json.dumps(
            {k: _serialize(v) for k, v in raw.items()},
            ensure_ascii=False,
        ),
        'parse_status': parse_status,
    }


def parse_rga_csv(record: SourceFileRecord) -> dict:
    """RGA CSV 메인 진입.

    Returns:
        {"status": "ok"|"error"|"skipped", "inserted": N, "errors": E, ...}
    """
    if record.is_race_unsafe:
        log.info(f"skip {record.file_name} (race_unsafe)")
        return {"status": "skipped", "reason": "race_unsafe", "inserted": 0, "errors": 0}

    if record.metadata and record.metadata.get("all_processed"):
        log.info(f"skip {record.file_name} (sha already processed)")
        return {"status": "skipped", "reason": "already_processed", "inserted": 0, "errors": 0}

    inserted = 0
    errors = 0

    try:
        # BOM 처리: utf-8-sig로 읽기 (BOM이 있으면 자동 제거)
        with open(record.file_path, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.reader(f)

            # 헤더
            try:
                header = next(reader)
            except StopIteration:
                log.error(f"RGA file empty: {record.file_path}")
                with session_scope_writer() as s:
                    s.execute(_UPDATE_METADATA_SQL, {
                        "id": record.id,
                        "metadata": json.dumps(record.metadata or {}, ensure_ascii=False),
                        "row_count": 0,
                        "parser_status": "error",
                        "parser_error": "RGA file empty",
                    })
                return {"status": "error", "error": "RGA file empty", "inserted": 0, "errors": 0}

            # 헤더 정리 (앞뒤 공백 제거)
            header = [(h.strip() if isinstance(h, str) else h) for h in header]

            # Mass 컬럼 수 카운트 (Time 제외)
            mass_count = sum(
                1 for h in header[1:]
                if isinstance(h, str) and h.lower().startswith('mass')
            )

            if mass_count == 0:
                log.error(f"RGA header missing Mass columns: {header[:5]}")
                with session_scope_writer() as s:
                    s.execute(_UPDATE_METADATA_SQL, {
                        "id": record.id,
                        "metadata": json.dumps(record.metadata or {}, ensure_ascii=False),
                        "row_count": 0,
                        "parser_status": "error",
                        "parser_error": "header missing Mass columns",
                    })
                return {
                    "status": "error",
                    "error": "header missing Mass columns",
                    "inserted": 0,
                    "errors": 0,
                }

            log.info(f"RGA file: {mass_count} mass columns detected")

            # 모든 데이터 row 수집 (batch 처리 위해)
            data_rows = list(reader)
            total_rows = len(data_rows)
            log.info(f"RGA file: {total_rows} data rows to process")

            # Phase 4 Step 27 — DELETE + INSERT 동기화 (한 트랜잭션 안에서 원자성)
            with session_scope_writer() as s:
                # 같은 source_type + file_path 의 옛 row 모두 DELETE
                delete_result = s.execute(text("""
                    DELETE FROM vo2.rga_runs
                    WHERE source_file_id IN (
                        SELECT id FROM vo2.source_files
                        WHERE source_type = 'rga_csv' AND file_path = :fp
                    )
                """), {"fp": str(record.file_path)})
                log.info(f"RGA DELETE 옛 row: {delete_result.rowcount}")

                # batch INSERT (한 트랜잭션 안에서)
                for batch_idx in range(0, total_rows, BATCH_SIZE):
                    batch = data_rows[batch_idx : batch_idx + BATCH_SIZE]
                    for offset, row in enumerate(batch):
                        # row_idx: CSV 줄 번호 (1=header, 2부터 데이터)
                        row_idx = batch_idx + offset + 2

                        if not row or len(row) < 2:
                            continue  # 완전 빈 줄 skip

                        time_raw = row[0] if row else ""
                        measured_at = _parse_time(time_raw)

                        # mass 값 파싱 (Time 제외, mass_count개)
                        intensity = []
                        for i in range(1, min(mass_count + 1, len(row))):
                            intensity.append(_parse_intensity(row[i]))

                        parse_status = 'ok' if measured_at else 'time_invalid'

                        payload = _build_payload(
                            record.id, row_idx, time_raw, measured_at,
                            mass_count, intensity, header, row, parse_status,
                        )

                        # savepoint로 한 row 실패 격리
                        try:
                            with s.begin_nested():
                                s.execute(_INSERT_SQL, payload)
                            inserted += 1
                        except Exception as e:
                            log.warning(
                                f"RGA row {row_idx} insert failed: "
                                f"{type(e).__name__}: {str(e)[:200]}"
                            )
                            errors += 1

                    if batch_idx % 1000 == 0 and batch_idx > 0:
                        log.info(f"RGA progress: {batch_idx + len(batch)}/{total_rows}")

        # metadata 갱신
        new_metadata = {
            "all_processed": True,
            "inserted": inserted,
            "errors": errors,
            "mass_count": mass_count,
            "policy": "delete_insert_sync",  # Phase 4 Step 27
        }
        with session_scope_writer() as s:
            s.execute(_UPDATE_METADATA_SQL, {
                "id": record.id,
                "metadata": json.dumps(new_metadata, ensure_ascii=False),
                "row_count": inserted,
                "parser_status": "ok",
                "parser_error": None,
            })

        log.info(
            f"rga_csv {record.file_name}: +{inserted} rows, {errors} errors, "
            f"{mass_count} mass columns (DELETE+INSERT sync)"
        )
        return {
            "status": "ok",
            "inserted": inserted,
            "errors": errors,
            "mass_count": mass_count,
        }

    except Exception as e:
        error_msg = str(e)
        log.error(f"rga_csv {record.file_name} parse failed: {error_msg}", exc_info=True)
        with session_scope_writer() as s:
            s.execute(_UPDATE_METADATA_SQL, {
                "id": record.id,
                "metadata": json.dumps(record.metadata or {}, ensure_ascii=False),
                "row_count": inserted,
                "parser_status": "error",
                "parser_error": error_msg[:1000],
            })
        return {
            "status": "error",
            "error": error_msg,
            "inserted": inserted,
            "errors": errors,
        }