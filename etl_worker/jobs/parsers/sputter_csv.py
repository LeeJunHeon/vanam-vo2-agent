"""Ch1_log.csv 파싱 — 데이터 보존 강화 (Step 4-fix4).

기본 정책 변경:
- 모든 row를 sputter_runs에 INSERT 시도 (skip 금지).
- timestamp 깨진 row → start_time=NULL + parse_status='timestamp_missing'
  + vo2.parse_errors에 'timestamp_invalid' 기록 + raw_json 보존.
- 숫자 컬럼이 'T'/'F' 외 변환 불가값이면 column_shift 의심 → parse_status='partial'
  + vo2.parse_errors에 'column_shift' 기록 (sputter_runs는 best-effort INSERT).
- xlsx 매칭 row만 skip (정상 동작, ts 정상일 때만 lookup).

read_csv 변경:
- dtype=str + keep_default_na=False + na_values=[""] : 자동 dtype 추론 차단.
- to_datetime(format="ISO8601") : 길이 19 vs 26 mixed에서 NaT 발생하던 버그 fix.

멱등성: (source_file_id, row_number) UNIQUE → ON CONFLICT DO NOTHING.
parse_errors도 (source_file_id, row_number, error_type) UNIQUE.
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from shared.db import session_scope_writer
from etl_worker.jobs.scan_files import SourceFileRecord

log = logging.getLogger("etl.parsers.sputter_csv")
_KST = ZoneInfo("Asia/Seoul")

# power_source 추론 우선순위 (Pulse가 CW보다 먼저 — Pulse 컬럼 채워졌으면 Pulse)
_POWER_SOURCE_RULES = [
    ('RF Pulse: P',  'RF Pulse'),
    ('DC Pulse: P',  'DC Pulse'),
    ('RF: For.P',    'RF'),
    ('DC: P',        'DC'),
]

_MATCH_TOLERANCE_SECONDS = 60

# 숫자가 들어와야 정상인 컬럼들 — 그 외 값(빈값/T/F 제외)이 오면 column_shift 의심.
NUMERIC_COLUMNS = [
    "Base Pressure", "Integration Time", "Ar flow", "O2 flow", "N2 flow",
    "Working Pressure", "Process Time",
    "RF: For.P", "RF: Ref. P",
    "DC: V", "DC: I", "DC: P",
    "RF Pulse: P", "RF Pulse: Freq", "RF Pulse: Duty Cycle",
    "DC Pulse: P", "DC Pulse: V", "DC Pulse: I",
    "DC Pulse: Freq", "DC Pulse: Duty Cycle",
    "RF Pulse: For.P", "RF Pulse: Ref.P",
    "Shutter Delay", "Chuck Position",
]


# ─────────────────────────── 헬퍼 ───────────────────────────

def _is_na(v) -> bool:
    """pd.isna 안전 wrapper — list/dict 등 비교 불가 타입은 False 처리."""
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _normalize(v):
    """문자열 strip + 빈값/NaN → None."""
    if _is_na(v):
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return v


def _to_float(v):
    """숫자 변환. 'T'/'F'/빈값/변환 불가 → None."""
    if _is_na(v):
        return None
    s = str(v).strip()
    if s == "" or s.upper() in ("T", "F", "TRUE", "FALSE"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _serialize(v):
    """JSON 직렬화 — NaN → None, datetime → ISO."""
    if _is_na(v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.isoformat()
    return v


def _infer_power_source(row: dict):
    """CSV row에서 어느 power가 활성이었는지 추론."""
    for col, label in _POWER_SOURCE_RULES:
        v = row.get(col)
        if _is_na(v):
            continue
        try:
            if float(str(v).strip()) != 0:
                return label
        except (TypeError, ValueError):
            continue
    return None


def _detect_suspicious_columns(row: dict) -> list:
    """숫자 컬럼에 변환 불가 값이 있으면 의심 컬럼 목록 반환.

    NUMERIC_COLUMNS는 모두 숫자가 와야 정상인 컬럼들. 'T'/'F' 같은 boolean
    문자가 들어오는 것 자체가 column_shift 신호이므로 면제 안 함.
    (boolean이 정상인 컬럼 main_shutter_open / target_cleaning_flag 등은
    애초에 NUMERIC_COLUMNS에 포함돼 있지 않음.)
    """
    suspicious = []
    for col in NUMERIC_COLUMNS:
        raw = row.get(col)
        if _is_na(raw):
            continue
        s = str(raw).strip()
        if s == "":
            continue
        try:
            float(s)
        except (TypeError, ValueError):
            suspicious.append(f"{col}='{s}'")
    return suspicious


# ─────────────────────────── SQL ───────────────────────────

_MATCH_LOOKUP_SQL = text("""
    SELECT 1 FROM vo2.sputter_runs
    WHERE chamber = :ch
      AND start_time BETWEEN :lo AND :hi
    LIMIT 1
""")


_INSERT_CSV_SQL = text("""
    INSERT INTO vo2.sputter_runs (
        sputter_run_id, source_file_id, row_number, chamber, sample_id,
        start_time, end_time, recipe_name, operator, note, substrate, status,
        target, power_source, power_select, main_shutter_open, chuck_position,
        shutter_delay_min, process_time_min, integration_time_s,
        sp_ar_sccm, sp_o2_sccm, sp_n2_sccm, sp_pressure_mtorr, sp_power_w,
        base_pressure_torr, avg_pressure_mtorr,
        avg_ar_sccm, avg_o2_sccm, avg_n2_sccm,
        avg_power_w, avg_for_p_w, avg_ref_p_w, avg_voltage_v, avg_current_a,
        avg_load_au, avg_tune_au,
        pulse_freq_khz, pulse_duty_cycle_pct, pulse_off_time_us,
        dep_rate_nm_per_s, thickness_nm, soft_arc_count, hard_arc_count,
        o2_ratio, total_flow_sccm, power_time_ws,
        substrate_temperature_c, target_cleaning_flag,
        parse_status,
        raw_json
    )
    VALUES (
        :sputter_run_id, :source_file_id, :row_number, :chamber, :sample_id,
        :start_time, :end_time, :recipe_name, :operator, :note, :substrate, :status,
        :target, :power_source, :power_select, :main_shutter_open, :chuck_position,
        :shutter_delay_min, :process_time_min, :integration_time_s,
        :sp_ar_sccm, :sp_o2_sccm, :sp_n2_sccm, :sp_pressure_mtorr, :sp_power_w,
        :base_pressure_torr, :avg_pressure_mtorr,
        :avg_ar_sccm, :avg_o2_sccm, :avg_n2_sccm,
        :avg_power_w, :avg_for_p_w, :avg_ref_p_w, :avg_voltage_v, :avg_current_a,
        :avg_load_au, :avg_tune_au,
        :pulse_freq_khz, :pulse_duty_cycle_pct, :pulse_off_time_us,
        :dep_rate_nm_per_s, :thickness_nm, :soft_arc_count, :hard_arc_count,
        :o2_ratio, :total_flow_sccm, :power_time_ws,
        :substrate_temperature_c, :target_cleaning_flag,
        :parse_status,
        CAST(:raw_json AS JSONB)
    )
    ON CONFLICT (source_file_id, row_number) DO NOTHING
""")


_INSERT_PARSE_ERROR_SQL = text("""
    INSERT INTO vo2.parse_errors
        (source_file_id, row_number, error_type, error_detail, raw_data)
    VALUES
        (:sfid, :rn, :etype, :edetail, CAST(:raw AS JSONB))
    ON CONFLICT (source_file_id, row_number, error_type) DO NOTHING
""")


_UPDATE_SOURCE_FILES_SQL = text("""
    UPDATE vo2.source_files
    SET row_count = :row_count,
        parser_status = :parser_status,
        parser_error = :parser_error,
        last_indexed_at = NOW()
    WHERE id = :id
""")


def _insert_parse_error(
    session, source_file_id: int, row_number: int,
    error_type: str, error_detail: str, raw_data: dict,
) -> bool:
    """parse_errors INSERT — savepoint로 격리, 중복(UNIQUE)은 silently skip.

    Returns: True if INSERT 시도 성공 (실제 row 생성 여부와 무관), False if 예외.
    """
    try:
        with session.begin_nested():
            session.execute(_INSERT_PARSE_ERROR_SQL, {
                "sfid": source_file_id,
                "rn": row_number,
                "etype": error_type,
                "edetail": error_detail,
                "raw": json.dumps(raw_data, ensure_ascii=False, default=str),
            })
        return True
    except Exception as e:
        log.warning(
            f"parse_errors INSERT 실패 row {row_number}/{error_type}: "
            f"{type(e).__name__}: {str(e)[:200]}"
        )
        return False


def _build_csv_payload(
    row: dict, source_file_id: int, row_number: int,
    timestamp, parse_status,
) -> dict:
    """CSV row → sputter_runs INSERT payload.

    timestamp가 None이면 start_time=NULL.
    parse_status는 caller가 결정 ('timestamp_missing' | 'partial' | None).
    """
    ar = _to_float(row.get('Ar flow'))
    o2 = _to_float(row.get('O2 flow'))
    n2 = _to_float(row.get('N2 flow'))
    process_time_min = _to_float(row.get('Process Time'))
    power_source = _infer_power_source(row)

    avg_power = None
    avg_for_p = None
    avg_ref_p = None
    avg_voltage = None
    avg_current = None
    pulse_freq = None
    pulse_duty = None

    if power_source == 'DC':
        avg_power = _to_float(row.get('DC: P'))
        avg_voltage = _to_float(row.get('DC: V'))
        avg_current = _to_float(row.get('DC: I'))
    elif power_source == 'RF':
        avg_for_p = _to_float(row.get('RF: For.P'))
        avg_ref_p = _to_float(row.get('RF: Ref. P'))
    elif power_source == 'RF Pulse':
        avg_power = _to_float(row.get('RF Pulse: P'))
        avg_for_p = _to_float(row.get('RF Pulse: For.P'))
        avg_ref_p = _to_float(row.get('RF Pulse: Ref.P'))
        pulse_freq = _to_float(row.get('RF Pulse: Freq'))
        pulse_duty = _to_float(row.get('RF Pulse: Duty Cycle'))
    elif power_source == 'DC Pulse':
        avg_power = _to_float(row.get('DC Pulse: P'))
        avg_voltage = _to_float(row.get('DC Pulse: V'))
        avg_current = _to_float(row.get('DC Pulse: I'))
        pulse_freq = _to_float(row.get('DC Pulse: Freq'))
        pulse_duty = _to_float(row.get('DC Pulse: Duty Cycle'))

    # 파생값
    o2_ratio = None
    if isinstance(ar, (int, float)) and isinstance(o2, (int, float)) and (ar + o2) > 0:
        o2_ratio = o2 / (ar + o2)
    total_flow = None
    flows = [x for x in (ar, o2, n2) if isinstance(x, (int, float))]
    if flows:
        total_flow = sum(flows)
    power_time = None
    if isinstance(avg_power, (int, float)) and isinstance(process_time_min, (int, float)):
        power_time = avg_power * process_time_min * 60.0

    sputter_run_id = f"CH1-csv-{source_file_id}-{row_number}"

    return {
        'sputter_run_id':         sputter_run_id,
        'source_file_id':         source_file_id,
        'row_number':             row_number,
        'chamber':                'CH1',
        'sample_id':              None,
        'start_time':             timestamp,   # None이면 컬럼이 NULL
        'end_time':               None,
        'recipe_name':            _normalize(row.get('Process Name')),
        'operator':               None,
        'note':                   None,
        'substrate':              None,
        'status':                 _normalize(row.get('Result')),
        'target':                 _normalize(row.get('G1 Target')),
        'power_source':           power_source,
        'power_select':           _normalize(row.get('Power Select')),
        'main_shutter_open':      _normalize(row.get('Main Shutter')) == 'T',
        'chuck_position':         _normalize(row.get('Chuck Position')),
        'shutter_delay_min':      _to_float(row.get('Shutter Delay')),
        'process_time_min':       process_time_min,
        'integration_time_s':     _to_float(row.get('Integration Time')),
        'sp_ar_sccm':             None,
        'sp_o2_sccm':             None,
        'sp_n2_sccm':             None,
        'sp_pressure_mtorr':      None,
        'sp_power_w':             None,
        'base_pressure_torr':     _to_float(row.get('Base Pressure')),
        'avg_pressure_mtorr':     _to_float(row.get('Working Pressure')),
        'avg_ar_sccm':            ar,
        'avg_o2_sccm':            o2,
        'avg_n2_sccm':            n2,
        'avg_power_w':            avg_power,
        'avg_for_p_w':            avg_for_p,
        'avg_ref_p_w':            avg_ref_p,
        'avg_voltage_v':          avg_voltage,
        'avg_current_a':          avg_current,
        'avg_load_au':            None,
        'avg_tune_au':            None,
        'pulse_freq_khz':         pulse_freq,
        'pulse_duty_cycle_pct':   pulse_duty,
        'pulse_off_time_us':      None,
        'dep_rate_nm_per_s':      None,
        'thickness_nm':           None,
        'soft_arc_count':         None,
        'hard_arc_count':         None,
        'o2_ratio':               o2_ratio,
        'total_flow_sccm':        total_flow,
        'power_time_ws':          power_time,
        'substrate_temperature_c': None,
        'target_cleaning_flag':   False,
        'parse_status':           parse_status,
        'raw_json':               json.dumps(
            {k: _serialize(v) for k, v in row.items()},
            ensure_ascii=False, default=str,
        ),
    }


# ─────────────────────────── 메인 ───────────────────────────

def parse_csv(record: SourceFileRecord) -> dict:
    """Ch1_log.csv 파싱.

    데이터 보존 정책 (Step 4-fix4):
    - 모든 row를 sputter_runs에 INSERT 시도 (skip 금지)
    - timestamp 깨짐 → start_time=NULL + parse_status='timestamp_missing'
      + parse_errors에 'timestamp_invalid' 기록
    - 숫자 컬럼 의심 → parse_status='partial' + parse_errors에 'column_shift' 기록
    - xlsx ±60초 매칭 → skip (정상 timestamp일 때만)

    Returns: dict with both legacy keys (inserted/skipped/row_errors/status)
             and new keys (rows_inserted/rows_skipped_xlsx/rows_with_errors/row_count).
    """
    if record.is_race_unsafe:
        log.info(f"skip {record.file_name} (race_unsafe)")
        return {
            "inserted": 0, "skipped": 0, "row_errors": 0,
            "rows_inserted": 0, "rows_skipped_xlsx": 0,
            "rows_with_errors": 0, "row_count": 0,
            "status": "skipped",
        }

    prev_row_count = record.previous_row_count
    rows_inserted = 0
    rows_skipped_xlsx = 0
    rows_with_errors = 0  # parse_errors에 한 건이라도 기록된 row 수

    try:
        # dtype=str로 자동 추론 차단 (timestamp 길이 mixed 시 NaT 버그 방지).
        # keep_default_na=False + na_values=[""] : 빈 셀만 NaN, 'NA'/'NaN' 같은 문자열은 그대로.
        df = pd.read_csv(
            record.file_path,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
        )
        if 'Timestamp' not in df.columns:
            raise ValueError("Timestamp 컬럼이 없음")
        # ISO8601 명시 — 길이 19 vs 26 mixed 케이스에서 NaT 발생하던 버그 fix.
        df['_ts_parsed'] = pd.to_datetime(
            df['Timestamp'], format="ISO8601", errors='coerce'
        )
        new_rows = df.iloc[prev_row_count:]

        with session_scope_writer() as s:
            for offset, (_, row) in enumerate(new_rows.iterrows()):
                row_number = prev_row_count + offset
                # raw_data용 dict — _ts_parsed (derived) 제거
                row_dict = {k: v for k, v in row.to_dict().items() if k != '_ts_parsed'}

                try:
                    parse_errors_to_record = []  # list[(error_type, error_detail)]
                    parse_status = None

                    # === Timestamp 처리 ===
                    ts_parsed = row['_ts_parsed']
                    if pd.notna(ts_parsed):
                        ts = ts_parsed.to_pydatetime()
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=_KST)
                    else:
                        ts = None
                        parse_status = "timestamp_missing"
                        raw_ts = row_dict.get('Timestamp', '')
                        parse_errors_to_record.append(
                            ("timestamp_invalid", f"raw='{raw_ts}'")
                        )

                    # === xlsx 매칭 skip — 정상 ts일 때만 ===
                    if ts is not None:
                        match = s.execute(_MATCH_LOOKUP_SQL, {
                            "ch": "CH1",
                            "lo": ts - timedelta(seconds=_MATCH_TOLERANCE_SECONDS),
                            "hi": ts + timedelta(seconds=_MATCH_TOLERANCE_SECONDS),
                        }).first()
                        if match is not None:
                            rows_skipped_xlsx += 1
                            continue

                    # === 숫자 컬럼 의심 감지 ===
                    suspicious = _detect_suspicious_columns(row_dict)
                    if suspicious:
                        # timestamp_missing이 우선 — 더 심각한 상태이므로 덮어쓰지 않음.
                        if parse_status is None:
                            parse_status = "partial"
                        parse_errors_to_record.append(
                            ("column_shift", "; ".join(suspicious[:5]))
                        )

                    # === sputter_runs INSERT (savepoint로 격리, best-effort) ===
                    payload = _build_csv_payload(
                        row_dict, record.id, row_number, ts, parse_status,
                    )
                    insert_ok = False
                    savepoint = s.begin_nested()
                    try:
                        s.execute(_INSERT_CSV_SQL, payload)
                        savepoint.commit()
                        rows_inserted += 1
                        insert_ok = True
                    except Exception as row_e:
                        savepoint.rollback()
                        log.warning(
                            f"row {row_number} sputter_runs INSERT 실패: "
                            f"{type(row_e).__name__}: {str(row_e)[:200]}"
                        )

                    # === parse_errors INSERT — sputter_runs 성공 시에만 (FK 보장) ===
                    if insert_ok and parse_errors_to_record:
                        for error_type, error_detail in parse_errors_to_record:
                            _insert_parse_error(
                                s, record.id, row_number,
                                error_type, error_detail, row_dict,
                            )
                        rows_with_errors += 1

                except Exception as outer_e:
                    log.warning(
                        f"row {row_number} 처리 중 예외: "
                        f"{type(outer_e).__name__}: {str(outer_e)[:200]}"
                    )

            s.execute(_UPDATE_SOURCE_FILES_SQL, {
                "id": record.id,
                "row_count": prev_row_count + len(new_rows),
                "parser_status": "ok" if rows_with_errors == 0 else "partial",
                "parser_error": (
                    None if rows_with_errors == 0
                    else f"{rows_with_errors} rows recorded with parse_errors"
                ),
            })

        log.info(
            f"csv {record.file_name}: +{rows_inserted} inserted, "
            f"{rows_skipped_xlsx} skipped (xlsx-matched), "
            f"{rows_with_errors} parse_errors recorded, "
            f"{len(new_rows)} new rows scanned"
        )
        return {
            # 신규 키 (Step 4-fix4)
            "rows_inserted":     rows_inserted,
            "rows_skipped_xlsx": rows_skipped_xlsx,
            "rows_with_errors":  rows_with_errors,
            "row_count":         len(new_rows),
            # 기존 키 (sync_sputter caller 호환)
            "inserted":   rows_inserted,
            "skipped":    rows_skipped_xlsx,
            "row_errors": rows_with_errors,
            "status":     "ok" if rows_with_errors == 0 else "partial",
        }

    except Exception as e:
        error_msg = str(e)
        log.error(f"csv {record.file_name} parse failed: {error_msg}", exc_info=True)
        with session_scope_writer() as s:
            s.execute(_UPDATE_SOURCE_FILES_SQL, {
                "id": record.id,
                "row_count": prev_row_count,
                "parser_status": "error",
                "parser_error": error_msg[:1000],
            })
        return {
            "rows_inserted":     rows_inserted,
            "rows_skipped_xlsx": rows_skipped_xlsx,
            "rows_with_errors":  rows_with_errors,
            "row_count":         0,
            "inserted":   rows_inserted,
            "skipped":    rows_skipped_xlsx,
            "row_errors": rows_with_errors,
            "status":     "error",
            "error":      error_msg,
        }
