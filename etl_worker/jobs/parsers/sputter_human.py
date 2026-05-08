"""사람 sputter log xlsx 파서 — '통합' 단일 시트 → sputter_runs_human.

전략: ETL은 xlsx → DB 단순 복사. 해석은 agent.

xlsx 구조:
- '통합' 시트: R1=헤더, R2=단위, R3부터 데이터
- C0~C22 (W까지) 23 컬럼 매핑. C23+ 무시.
- 사람이 손으로 적어 혼합 타입 다수 → 모두 TEXT 보존.

빈 row 판정:
- 23 컬럼 모두 비어있어야 진짜 빈 row.
- 5 연속이면 데이터 끝.
"""

import json
import logging
from datetime import datetime, date

from openpyxl import load_workbook
from sqlalchemy import text

from shared.db import session_scope_writer
from etl_worker.jobs.scan_files import SourceFileRecord

log = logging.getLogger("etl.parsers.sputter_human")

SHEET_NAME = "통합"

# (col_idx, db_col, type)  type: 'timestamp' | 'real' | 'text'
HUMAN_COL_MAP = [
    (0,  'process_date',       'timestamp'),
    (1,  'operator',           'text'),
    (2,  'sub_label_raw',      'text'),
    (3,  'pc_gas',             'text'),
    (4,  'pc_power_w',         'real'),
    (5,  'pc_pressure_mtorr',  'real'),
    (6,  'pc_gas_flow_sccm',   'real'),
    (7,  'pc_time_min',        'real'),
    (8,  'shutter_delay_min',  'real'),
    (9,  'sp_power_w',         'real'),
    (10, 'sp_flow_sccm',       'real'),
    (11, 'sp_pressure_mtorr',  'real'),
    (12, 'sp_time',            'text'),
    (13, 'thickness',          'text'),
    (14, 'furnace_type',       'text'),
    (15, 'annealing_temp',     'text'),
    (16, 'annealing_gas_flow', 'text'),
    (17, 'pulsed_dc_freq',     'text'),
    (18, 'off_time_us',        'real'),
    (19, 'duty',               'text'),
    (20, 'depo_rate',          'text'),
    (21, 'target',             'text'),
    (22, 'notes',              'text'),
]


def _to_real(v):
    if v is None or v == '':
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        f = float(v)
        if f != f or f == float('inf') or f == float('-inf'):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _to_text(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _to_timestamp(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
    return None


def _serialize(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _row_to_full_dict(row, header):
    result = {}
    for i, val in enumerate(row):
        key = header[i] if i < len(header) and header[i] else f'col_{i}'
        if isinstance(key, str):
            key = key.replace('\n', ' ').strip()
        result[key] = val
    return result


def _is_truly_empty_row(row, max_col=23):
    for i in range(max_col):
        if i < len(row):
            v = row[i]
            if v is not None and not (isinstance(v, str) and not v.strip()):
                return False
    return True


_DB_COLS = [m[1] for m in HUMAN_COL_MAP]
_INSERT_HUMAN_SQL = text(f"""
    INSERT INTO vo2.sputter_runs_human (
        {', '.join(_DB_COLS)},
        source_file_id, row_number, raw_json, parse_status
    ) VALUES (
        {', '.join(':' + c for c in _DB_COLS)},
        :source_file_id, :row_number, CAST(:raw_json AS JSONB), :parse_status
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


def _build_payload(row, source_file_id, row_number, header):
    payload = {}
    for col_idx, db_col, dtype in HUMAN_COL_MAP:
        v = row[col_idx] if col_idx < len(row) else None
        if dtype == 'real':
            payload[db_col] = _to_real(v)
        elif dtype == 'timestamp':
            payload[db_col] = _to_timestamp(v)
        else:
            payload[db_col] = _to_text(v)

    payload['source_file_id'] = source_file_id
    payload['row_number'] = row_number
    payload['parse_status'] = None

    raw = _row_to_full_dict(row, header)
    payload['raw_json'] = json.dumps(
        {k: _serialize(v) for k, v in raw.items()},
        ensure_ascii=False,
    )
    return payload


def parse_sputter_human(record: SourceFileRecord) -> dict:
    if record.is_race_unsafe:
        log.info(f"skip {record.file_name} (race_unsafe)")
        return {"status": "skipped", "reason": "race_unsafe", "inserted": 0}

    if record.metadata and record.metadata.get("all_processed"):
        log.info(f"skip {record.file_name} (sha already processed)")
        return {"status": "skipped", "reason": "already_processed", "inserted": 0}

    inserted = 0

    try:
        wb = load_workbook(record.file_path, read_only=True, data_only=True)
        try:
            if SHEET_NAME not in wb.sheetnames:
                log.error(
                    f"sheet '{SHEET_NAME}' not found in {record.file_name}. "
                    f"available: {wb.sheetnames}"
                )
                with session_scope_writer() as s:
                    s.execute(_UPDATE_METADATA_SQL, {
                        "id": record.id,
                        "metadata": json.dumps(record.metadata or {}, ensure_ascii=False),
                        "row_count": 0,
                        "parser_status": "error",
                        "parser_error": f"sheet '{SHEET_NAME}' not found",
                    })
                return {"status": "error", "error": f"sheet '{SHEET_NAME}' not found", "inserted": 0}

            ws = wb[SHEET_NAME]

            header_data = list(ws.iter_rows(min_row=1, max_row=1, values_only=True))
            header = list(header_data[0]) if header_data else []
            header = [
                (h.replace('\n', ' ').strip() if isinstance(h, str) else h)
                for h in header
            ]

            empty_streak = 0
            with session_scope_writer() as s:
                for row_idx, row in enumerate(
                    ws.iter_rows(min_row=3, values_only=True), start=3
                ):
                    if _is_truly_empty_row(row, max_col=23):
                        empty_streak += 1
                        if empty_streak >= 5:
                            break
                        continue
                    empty_streak = 0

                    payload = _build_payload(row, record.id, row_idx, header)
                    s.execute(_INSERT_HUMAN_SQL, payload)
                    inserted += 1
        finally:
            wb.close()

        new_metadata = {"all_processed": True, "inserted": inserted}
        with session_scope_writer() as s:
            s.execute(_UPDATE_METADATA_SQL, {
                "id": record.id,
                "metadata": json.dumps(new_metadata, ensure_ascii=False),
                "row_count": inserted,
                "parser_status": "ok",
                "parser_error": None,
            })

        log.info(f"sputter_human {record.file_name}: +{inserted} rows")
        return {"status": "ok", "inserted": inserted}

    except Exception as e:
        error_msg = str(e)
        log.error(f"sputter_human {record.file_name} parse failed: {error_msg}", exc_info=True)
        with session_scope_writer() as s:
            s.execute(_UPDATE_METADATA_SQL, {
                "id": record.id,
                "metadata": json.dumps(record.metadata or {}, ensure_ascii=False),
                "row_count": inserted,
                "parser_status": "error",
                "parser_error": error_msg[:1000],
            })
        return {"status": "error", "error": error_msg, "inserted": inserted}
