"""get_timeseries 도구 — measurements/rga_runs의 시계열 배열 raw 반환.

한 번에 한 row만 (토큰 폭발 방지). 시계열 분석/시각화 시 사용.

지원 테이블:
- measurements: temperature_c[] + resistance_ohm[] (둘 다 같은 길이, ~500점)
- rga_runs: intensity[] (Mass 1~65, 65점)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text

from mcp_server.app.db import reader_session


SUPPORTED_TABLES = ("measurements", "rga_runs")


def run(table: Literal["measurements", "rga_runs"], row_id: int) -> dict[str, Any]:
    """get_timeseries 도구 진입.

    Args:
        table: 'measurements' 또는 'rga_runs'
        row_id: 해당 테이블의 id 컬럼 값

    Returns:
        {table, row_id, metadata: {...}, data: {column: [...]}, info: {...}}
    """
    if table not in SUPPORTED_TABLES:
        return {
            "error": (
                f"지원하지 않는 테이블: {table!r}. "
                f"지원: {SUPPORTED_TABLES}. "
                f"다른 테이블 데이터는 run_sql로 조회."
            ),
        }

    try:
        row_id = int(row_id)
    except (ValueError, TypeError):
        return {"error": f"row_id는 정수여야 합니다: {row_id!r}"}

    if table == "measurements":
        return _get_measurements_timeseries(row_id)
    else:  # rga_runs
        return _get_rga_timeseries(row_id)


def _get_measurements_timeseries(row_id: int) -> dict[str, Any]:
    """measurements 한 row의 temperature_c[] + resistance_ohm[] 반환."""
    with reader_session() as s:
        row = s.execute(
            text("""
                SELECT
                    id, file_name, file_path, file_dir,
                    year, measurement_date, process_seq,
                    process_seq_in_name, sample_seq,
                    sub_label_raw, sub_kind, sub_batch_no, suffix_n,
                    point_count, temperature_c, resistance_ohm,
                    file_size, file_mtime, sha256,
                    parse_status, raw_header, created_at
                FROM vo2.measurements
                WHERE id = :id
            """),
            {"id": row_id},
        ).mappings().one_or_none()

    if row is None:
        return {
            "error": f"measurements.id={row_id} 없음",
            "table": "vo2.measurements",
            "row_id": row_id,
        }

    temps = list(row["temperature_c"]) if row["temperature_c"] else []
    resists = list(row["resistance_ohm"]) if row["resistance_ohm"] else []

    # 시계열 요약 통계 (분석 도움)
    temp_summary = _array_summary(temps)
    res_summary = _array_summary(resists)

    return {
        "table": "vo2.measurements",
        "row_id": row_id,
        "metadata": {
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "file_dir": row["file_dir"],
            "year": row["year"],
            "measurement_date": _serialize(row["measurement_date"]),
            "process_seq": row["process_seq"],
            "process_seq_in_name": row["process_seq_in_name"],
            "sample_seq": row["sample_seq"],
            "sub_label_raw": row["sub_label_raw"],
            "sub_kind": row["sub_kind"],
            "sub_batch_no": row["sub_batch_no"],
            "suffix_n": row["suffix_n"],
            "point_count": row["point_count"],
            "file_size": row["file_size"],
            "file_mtime": _serialize(row["file_mtime"]),
            "sha256": row["sha256"],
            "parse_status": row["parse_status"],
            "raw_header": row["raw_header"],
            "created_at": _serialize(row["created_at"]),
        },
        "data": {
            "temperature_c": temps,
            "resistance_ohm": resists,
        },
        "summary": {
            "temperature_c": temp_summary,
            "resistance_ohm": res_summary,
            "is_aligned": len(temps) == len(resists),
        },
        "info": (
            "VO2 R-T 측정: temperature_c (°C) vs resistance_ohm (Ohm) — 같은 인덱스끼리 짝.\n"
            "전이온도 분석은 보통 dR/dT의 변곡점 (60~70°C 부근).\n"
            "parse_status='error'면 시계열 NULL이고 raw_header에 에러 메시지."
        ),
    }


def _get_rga_timeseries(row_id: int) -> dict[str, Any]:
    """rga_runs 한 row의 intensity[] (Mass 1~65) 반환."""
    with reader_session() as s:
        row = s.execute(
            text("""
                SELECT
                    id, source_file_id, row_number,
                    measured_at, measured_at_raw,
                    mass_count, intensity,
                    parse_status, created_at
                FROM vo2.rga_runs
                WHERE id = :id
            """),
            {"id": row_id},
        ).mappings().one_or_none()

    if row is None:
        return {
            "error": f"rga_runs.id={row_id} 없음",
            "table": "vo2.rga_runs",
            "row_id": row_id,
        }

    intensity = list(row["intensity"]) if row["intensity"] else []
    mass_count = row["mass_count"]

    # Mass별 dict 형태도 같이 제공 (agent 편의)
    by_mass = {f"Mass {i+1}": v for i, v in enumerate(intensity)}

    # 유의미한 Mass만 highlight
    notable_masses = {
        1: "H (atomic hydrogen)",
        2: "H2",
        14: "N (atomic nitrogen / fragment)",
        16: "O / CH4 fragment",
        18: "H2O (water)",
        28: "N2 또는 CO",
        32: "O2 (oxygen)",
        40: "Ar (argon, sputter gas)",
        44: "CO2 또는 N2O",
    }
    notable = {
        f"Mass {m}": {
            "value": intensity[m-1] if m <= len(intensity) else None,
            "meaning": meaning,
        }
        for m, meaning in notable_masses.items()
    }

    return {
        "table": "vo2.rga_runs",
        "row_id": row_id,
        "metadata": {
            "source_file_id": row["source_file_id"],
            "row_number": row["row_number"],
            "measured_at": _serialize(row["measured_at"]),
            "measured_at_raw": row["measured_at_raw"],
            "mass_count": mass_count,
            "parse_status": row["parse_status"],
            "created_at": _serialize(row["created_at"]),
        },
        "data": {
            "intensity": intensity,
            "by_mass": by_mass,
        },
        "summary": _array_summary(intensity),
        "notable_masses": notable,
        "info": (
            "RGA partial pressure spectrum: intensity[i] = Mass (i+1)의 partial pressure.\n"
            "주요 mass: 18=H2O, 28=N2/CO, 32=O2, 40=Ar.\n"
            "음수 값도 정상 (background subtraction 결과)."
        ),
    }


def _array_summary(arr: list) -> dict[str, Any]:
    """배열의 통계 요약."""
    if not arr:
        return {"length": 0, "all_null": True}

    nums = [x for x in arr if x is not None and isinstance(x, (int, float))]
    if not nums:
        return {"length": len(arr), "all_null": True}

    return {
        "length": len(arr),
        "min": min(nums),
        "max": max(nums),
        "first": nums[0],
        "last": nums[-1],
        "mean": sum(nums) / len(nums),
        "n_valid": len(nums),
        "n_null": len(arr) - len(nums),
    }


def _serialize(v: Any) -> Any:
    """JSON 직렬화 헬퍼."""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v