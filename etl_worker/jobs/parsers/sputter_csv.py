"""Ch1_log.csv 파싱 — xlsx에 없는 row만 INSERT (실패/중단/옛 데이터).

xlsx가 메인 source라 CSV는 보강 역할.
각 CSV row의 Timestamp ± 60초 이내에 sputter_runs(chamber=CH1)에 이미 있으면 skip.

멱등성: (source_file_id, row_number) UNIQUE → ON CONFLICT DO NOTHING.

Power Source 추론: CSV에는 명시 컬럼 없음. 어느 power 컬럼이 채워졌는지 보고 추론.
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


def _normalize(v):
    """pandas NaN / 빈 문자열 → None."""
    if pd.isna(v) or v == '' or v is None:
        return None
    return v


def _to_float(v):
    """숫자 변환 시도, 실패하면 None.
    CSV에 'T'/'F' 같은 문자가 숫자 컬럼에 들어오는 경우 안전 처리.
    """
    if v is None or pd.isna(v) or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _serialize(v):
    """JSON 직렬화 — datetime → ISO, NaN → None."""
    if pd.isna(v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.isoformat()
    return v


def _infer_power_source(row: dict) -> str | None:
    """CSV row에서 어느 power가 활성이었는지 추론."""
    for col, label in _POWER_SOURCE_RULES:
        v = row.get(col)
        if v is not None and not pd.isna(v):
            try:
                if float(v) != 0:
                    return label
            except (TypeError, ValueError):
                continue
    return None


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
        CAST(:raw_json AS JSONB)
    )
    ON CONFLICT (source_file_id, row_number) DO NOTHING
""")


_UPDATE_SOURCE_FILES_SQL = text("""
    UPDATE vo2.source_files
    SET row_count = :row_count,
        parser_status = :parser_status,
        parser_error = :parser_error,
        last_indexed_at = NOW()
    WHERE id = :id
""")


def _build_csv_payload(row: dict, source_file_id: int, row_number: int,
                       timestamp: datetime) -> dict:
    """CSV row → sputter_runs INSERT payload (xlsx 보강용, 정보 일부 미포함)."""
    ar = _to_float(row.get('Ar flow'))
    o2 = _to_float(row.get('O2 flow'))
    n2 = _to_float(row.get('N2 flow'))
    process_time_min = _to_float(row.get('Process Time'))
    power_source = _infer_power_source(row)

    # power_source에 따라 대표 컬럼 매핑
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
        'start_time':             timestamp,
        'end_time':               None,
        'recipe_name':            _normalize(row.get('Process Name')),
        'operator':               None,
        'note':                   None,
        'substrate':              None,
        'status':                 _normalize(row.get('Result')),
        'target':                 _normalize(row.get('G1 Target')),
        'power_source':           power_source,
        'power_select':           _normalize(row.get('Power Select')),
        'main_shutter_open':      row.get('Main Shutter') == 'T',
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
        'raw_json':               json.dumps(
            {k: _serialize(v) for k, v in row.items()}, ensure_ascii=False
        ),
    }


def parse_csv(record: SourceFileRecord) -> dict:
    """Ch1_log.csv 파싱.

    xlsx로 이미 들어간 row는 timestamp ±60초 lookup으로 skip.
    이전 처리 위치(record.previous_row_count) 이후 row만 처리.

    Returns: {"inserted": N, "skipped": M, "status": "ok"|"error"|"skipped", ...}
    """
    if record.is_race_unsafe:
        log.info(f"skip {record.file_name} (race_unsafe)")
        return {"inserted": 0, "skipped": 0, "status": "skipped"}

    prev_row_count = record.previous_row_count
    inserted = 0
    skipped = 0

    try:
        df = pd.read_csv(record.file_path)
        if 'Timestamp' not in df.columns:
            raise ValueError("Timestamp 컬럼이 없음")
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], errors='coerce')
        new_rows = df.iloc[prev_row_count:]

        row_errors = 0
        with session_scope_writer() as s:
            for offset, (_, row) in enumerate(new_rows.iterrows()):
                row_number = prev_row_count + offset
                try:
                    ts = row['Timestamp']
                    if pd.isna(ts):
                        log.warning(f"row {row_number}: Timestamp 파싱 실패, skip")
                        continue
                    ts = ts.to_pydatetime()
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_KST)

                    # xlsx에 이미 매칭되는 row 있는지 확인
                    match = s.execute(_MATCH_LOOKUP_SQL, {
                        "ch": "CH1",
                        "lo": ts - timedelta(seconds=_MATCH_TOLERANCE_SECONDS),
                        "hi": ts + timedelta(seconds=_MATCH_TOLERANCE_SECONDS),
                    }).first()
                    if match is not None:
                        skipped += 1
                        continue

                    payload = _build_csv_payload(
                        row.to_dict(), record.id, row_number, ts
                    )
                    # savepoint로 한 row 실패가 전체 transaction rollback하지 않도록
                    savepoint = s.begin_nested()
                    try:
                        s.execute(_INSERT_CSV_SQL, payload)
                        savepoint.commit()
                        inserted += 1
                    except Exception as row_e:
                        savepoint.rollback()
                        row_errors += 1
                        log.warning(
                            f"row {row_number} insert failed (data error): "
                            f"{type(row_e).__name__}: {str(row_e)[:200]}"
                        )
                except Exception as outer_e:
                    row_errors += 1
                    log.warning(
                        f"row {row_number} skipped (parse error): "
                        f"{type(outer_e).__name__}: {str(outer_e)[:200]}"
                    )

            s.execute(_UPDATE_SOURCE_FILES_SQL, {
                "id": record.id,
                "row_count": prev_row_count + len(new_rows),
                "parser_status": "ok" if row_errors == 0 else "partial",
                "parser_error": None if row_errors == 0 else f"{row_errors} rows skipped due to data errors",
            })

        log.info(
            f"csv {record.file_name}: +{inserted} inserted, "
            f"{skipped} skipped (xlsx-matched), {row_errors} errors, "
            f"{len(new_rows)} new rows scanned"
        )
        return {
            "inserted":   inserted,
            "skipped":    skipped,
            "row_errors": row_errors,
            "status":     "ok" if row_errors == 0 else "partial",
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
            "inserted": inserted,
            "skipped":  skipped,
            "status":   "error",
            "error":    error_msg,
        }
