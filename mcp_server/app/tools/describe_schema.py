"""describe_schema 도구 — DB schema + 도메인 지식 + 예시 row.

호출 패턴:
- describe_schema()         → 전체 18개 테이블 요약 (vo2 11 + equipment 7) + 관계 + 도메인 overview
- describe_schema(table=X)  → 특정 테이블 모든 컬럼 + 예시 5 row (raw_json/배열/file_data 제외)
                              bare name이면 스키마 자동 추론 ('vo2.X' / 'equipment.X' 명시도 가능)

agent가 SQL 짜기 전 "이 DB가 뭘 담고 있나?" 이해할 수 있도록 설계.
hybrid 패턴: agent system prompt에 요약 박혀있고, 깊이 분석 시 이 도구로 상세 조회.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from mcp_server.app.db import reader_session


# ─────────── 도메인 지식 (정적 — Decisions.md 기반) ───────────

DOMAIN_OVERVIEW = """
VANAM의 VO2 박막 공정 데이터 통합 분석 시스템.

VO2 (vanadium dioxide)는 박막 상전이 물질로, 60~70°C 부근에서 절연체→금속 전이를 보임 (resistance 급격히 감소).

공정 흐름:
1. ALD (Atomic Layer Deposition)로 TiO2 박막 증착
   - NCD 장비: TTIP+H2O 또는 TDMAT+H2O chemistry (table: ald_ncd_runs)
   - Rayvac 장비: TTIP+O3 chemistry (table: ald_rayvac_runs)
2. Sputter chamber에서 V (vanadium) 추가
   - 사람 기록: 운영자가 직접 손으로 적은 log (table: sputter_runs_human)
   - 자동 기록: CH1 sputter 장비가 자동 기록 (table: sputter_runs_auto_main + sputter_runs_auto_plasma)
3. Sputter 직전/직후 RGA로 chamber 잔류 가스 분석 (table: rga_runs)
   - 주요 mass: Mass 18=H2O, Mass 28=N2/CO, Mass 32=O2, Mass 40=Ar
4. 완성된 VO2 박막을 4-probe 측정 → R-T curve .dat 파일 (table: measurements)

ETL 원칙:
- ETL은 단순 복사기 — xlsx/csv ground truth 그대로 DB에 적재
- 사람이 적은 혼합 표기 ('530(b)', '3L/min', 'pulsed DC,25' 등) TEXT로 보존
- 해석/매칭은 agent (GPT) 영역
- 실패 row도 메타 살림 (parse_errors 격리 또는 parse_status='error')
- 멱등성: 5분 tick + sha256 + ON CONFLICT

장비 유지보수 데이터 (equipment 스키마, 별도 웹앱이 운영):
- equipment.equipments: 장비 마스터 (VO2 공정 관련은 id 3=CH1 Sputter, 7=Rayvac ALD, 8=NCD ALD)
- equipment.equipment_logs: 수리/벤트/클리닝 이벤트 로그 (event_type='repair'|'vent'|'cleaning')
- 공정 데이터와 직접 FK 없음. timestamp(KST) 기반 연관 분석만 가능.
- 활용 예: 'sputter run 직전 N일 내 장비 유지보수 있었나'를
  equipment_logs.occurred_at 과 sputter_runs_auto_main.process_datetime 비교로 판단.
"""

SAMPLE_MAPPING_GUIDE = """
Sample 추적 매핑 — 측정 파일 ↔ ALD batch:

측정 파일명 규칙: ^(\\d+)-(\\d+)-(ncd|R)\\((\\d+)\\)_..._N.dat
  예: "1-1-ncd(236)_p_big-F_0.10V_0.dat"
       - process_seq_in_name = 1
       - sample_seq = 1
       - sub_kind = 'ncd' → ald_ncd_runs와 연결
       - sub_batch_no = 236 → ald_ncd_runs.batch_no = 236

  예: "1-4-R(323)_big-F_0.10V_0.dat"
       - sub_kind = 'rayvac' → ald_rayvac_runs와 연결
       - sub_batch_no = 323 → ald_rayvac_runs.batch_no = 323

NULL 매칭 케이스 (6,643 중 약 3,645개, 55%):
  옛 운영자가 자유롭게 명명한 파일들 — 정규식 매칭 실패.
  예: '#1_4probe0.10V_0.dat', '(re)3-1_0.10V_2.dat', '0204_2-52_0.10V_0.dat', '5-5_0.10V_2.dat'
  agent가 file_path/file_name raw로 의미 추론 필요.
  운영자에게 직접 물어볼 수도 있음 (자연어로 "이 파일이 무슨 sample인지?").

JOIN 패턴 예시:
  -- 특정 ALD batch 박막의 모든 측정값
  SELECT m.file_name, m.point_count, m.parse_status
  FROM vo2.measurements m
  JOIN vo2.ald_ncd_runs a 
    ON m.sub_kind='ncd' 
   AND m.sub_batch_no=a.batch_no
  WHERE a.chemistry='TTIP' AND a.batch_no=236;

  -- 특정 batch의 sputter 공정 일자 찾기 (timestamp + sample_label로 추론)
  -- sputter_runs는 batch_no 컬럼 없음. agent가 sub_label_raw에서 의미 매칭.
"""

COMMON_QUERIES = [
    {
        "question": "DB 전체 현황 한눈에 보기",
        "sql": """SELECT 'ald_ncd_runs' AS tbl, COUNT(*) AS rows, MIN(process_date) AS earliest, MAX(process_date) AS latest FROM vo2.ald_ncd_runs
UNION ALL SELECT 'ald_rayvac_runs', COUNT(*), MIN(process_date), MAX(process_date) FROM vo2.ald_rayvac_runs
UNION ALL SELECT 'sputter_runs_human', COUNT(*), MIN(process_date)::date, MAX(process_date)::date FROM vo2.sputter_runs_human
UNION ALL SELECT 'sputter_runs_auto_main', COUNT(*), MIN(process_datetime)::date, MAX(process_datetime)::date FROM vo2.sputter_runs_auto_main
UNION ALL SELECT 'measurements', COUNT(*), MIN(measurement_date), MAX(measurement_date) FROM vo2.measurements
UNION ALL SELECT 'rga_runs', COUNT(*), MIN(measured_at)::date, MAX(measured_at)::date FROM vo2.rga_runs;""",
    },
    {
        "question": "특정 ALD batch의 박막 측정 결과 (NCD 236번)",
        "sql": """SELECT m.id, m.file_name, m.measurement_date, m.point_count, m.parse_status
FROM vo2.measurements m
WHERE m.sub_kind='ncd' AND m.sub_batch_no=236
ORDER BY m.measurement_date DESC, m.suffix_n DESC;""",
    },
    {
        "question": "지난 한 달 sputter 공정 (사람+자동 통합)",
        "sql": """SELECT 'human' AS source, process_date AS dt, sub_label_raw AS sample, sp_power_w, thickness AS thick_raw
FROM vo2.sputter_runs_human
WHERE process_date > NOW() - INTERVAL '30 days'
UNION ALL
SELECT 'auto', process_datetime, process_name, sp_power_w, thickness_nm::text
FROM vo2.sputter_runs_auto_main
WHERE process_datetime > NOW() - INTERVAL '30 days'
ORDER BY dt DESC;""",
    },
    {
        "question": "RGA에서 H2O(Mass 18) 평소 대비 높았던 시점",
        "sql": """WITH stats AS (
  SELECT AVG(intensity[18]) AS mean, STDDEV(intensity[18]) AS std
  FROM vo2.rga_runs
)
SELECT r.measured_at, r.intensity[18] AS h2o, r.intensity[28] AS n2_co, r.intensity[32] AS o2, r.intensity[40] AS ar
FROM vo2.rga_runs r, stats s
WHERE r.intensity[18] > s.mean + 2*s.std
ORDER BY r.measured_at DESC
LIMIT 50;""",
    },
    {
        "question": "측정 시계열의 전이 특성 요약 (시계열 raw는 get_timeseries로)",
        "sql": """SELECT 
  id, file_name, measurement_date, sub_kind, sub_batch_no, point_count,
  (SELECT MIN(t) FROM unnest(temperature_c) AS t) AS temp_min,
  (SELECT MAX(t) FROM unnest(temperature_c) AS t) AS temp_max,
  (SELECT MIN(r) FROM unnest(resistance_ohm) AS r) AS r_min,
  (SELECT MAX(r) FROM unnest(resistance_ohm) AS r) AS r_max
FROM vo2.measurements
WHERE parse_status='ok' AND sub_kind IS NOT NULL
ORDER BY id DESC
LIMIT 20;""",
    },
    {
        "question": "ETL이 처리 못 한 격리 row (자연어 에러 메시지 포함)",
        "sql": """SELECT sf.source_type, sf.file_name, pe.error_type, pe.error_detail, pe.detected_at
FROM vo2.parse_errors pe
JOIN vo2.source_files sf ON sf.id=pe.source_file_id
WHERE pe.resolved=false
ORDER BY pe.detected_at DESC;""",
    },
    {
        "question": "measurement 격리된 row (시계열 NULL)",
        "sql": """SELECT id, file_path, raw_header AS error_msg
FROM vo2.measurements
WHERE parse_status='error'
ORDER BY id DESC;""",
    },
    {
        "question": "VO2 공정 장비(CH1 Sputter, Rayvac/NCD ALD)의 최근 수리/벤트/클리닝 이력",
        "sql": """SELECT l.id, e.name AS equipment, l.event_type, l.occurred_at,
       l.operator, l.status, l.description,
       l.symptom, l.replaced_parts, l.is_external, l.vendor_name,
       l.vent_reason, l.cleaning_type, l.next_scheduled_at, l.completed_at
FROM equipment.equipment_logs l
JOIN equipment.equipments e ON e.id = l.equipment_id
WHERE l.equipment_id IN (3, 7, 8)
  AND l.status = '완료'
ORDER BY l.occurred_at DESC
LIMIT 50;""",
    },
    {
        "question": "특정 sputter run 직전 7일간 CH1 Sputter 유지보수 이력 (timestamp 기반 연관)",
        "sql": """WITH target_run AS (
    SELECT id, process_datetime FROM vo2.sputter_runs_auto_main WHERE id = 1
)
SELECT l.event_type, l.occurred_at, l.status, l.description,
       l.vent_reason, l.cleaning_type, l.symptom
FROM equipment.equipment_logs l, target_run r
WHERE l.equipment_id = 3
  AND l.status = '완료'
  AND l.occurred_at BETWEEN r.process_datetime - INTERVAL '7 days'
                        AND r.process_datetime
ORDER BY l.occurred_at DESC;""",
    },
]


# ─────────── 테이블별 메타데이터 ───────────

TABLE_META = {
    "source_files": {
        "purpose": "모든 xlsx/csv source 파일 인덱싱. 다른 모든 _runs 테이블의 FK 부모.",
        "domain_notes": "5분 tick마다 NAS의 SOURCE_FILES (sputter_human_xlsx, sputter_auto_xlsx, ald_ncd_xlsx, ald_rayvac_xlsx, rga_csv) 5종이 sha256 기반으로 등록됨. measurements는 SOURCE_FILES 거치지 않고 트리 traversal로 직접 적재됨 (file_path UNIQUE).",
        "key_columns": ["source_type", "file_path", "sha256", "parser_status"],
    },
    "etl_runs": {
        "purpose": "5분 tick마다 도는 ETL의 실행 기록. 어떤 source 처리됐는지 +rows.",
        "domain_notes": "metadata.parsers에 각 파서 결과 JSON 포함. status='ok'|'error'|'running'.",
        "key_columns": ["started_at", "status", "rows_inserted", "metadata"],
    },
    "mcp_audit_logs": {
        "purpose": "MCP server 도구 호출 감사. 누가/언제/뭘 호출했는지 + SQL 본문.",
        "domain_notes": "caller_kind='portal'|'chatgpt_connector'. run_sql 호출 시 arguments.sql에 전체 SQL 본문 저장. result_summary에 row 수 등 결과 요약.",
        "key_columns": ["called_at", "caller_kind", "tool_name", "arguments", "success", "duration_ms"],
    },
    "parse_errors": {
        "purpose": "xlsx row 단위 파싱 실패 격리. 자연어 에러 메시지로 운영자 안내.",
        "domain_notes": "error_detail이 GPT/운영자 친화 자연어 (예: 'NCD TTIP 시트 R36 row의 공정 번호가 test로 정수가 아닙니다...'). resolved=false인 row가 운영자 액션 필요.",
        "key_columns": ["source_file_id", "error_type", "error_detail", "resolved"],
    },
    "ald_ncd_runs": {
        "purpose": "ALD NCD 장비 공정 로그. TiO2 박막 증착 (TTIP/TDMAT precursor + H2O oxidant).",
        "domain_notes": "chemistry='TTIP'|'TDMAT'. batch_no는 운영자가 적은 공정 번호 (재공정 케이스 있어 같은 batch_no 여러 row 가능). raw_json에 xlsx 모든 컬럼 원본 보존.",
        "key_columns": ["batch_no", "chemistry", "process_date", "cycles", "gpc_a_per_cycle"],
    },
    "ald_rayvac_runs": {
        "purpose": "ALD Rayvac 장비 공정 로그. TiO2 박막 증착 (TTIP + O3 oxidant).",
        "domain_notes": "NCD와 달리 oxidant가 O3 (오존). plasma_cleaning_flag로 챔버 클리닝 사이클 구분.",
        "key_columns": ["batch_no", "process_date", "cycles", "gpc_a_per_cycle", "o3_conc"],
    },
    "sputter_runs_human": {
        "purpose": "사람이 손으로 적은 sputter 공정 log (Ch1 process log xlsx의 통합 시트).",
        "domain_notes": "거의 대부분 TEXT 컬럼 — 사람이 '530(b)', '3L/min', 'pulsed DC,25', '30 nm' 같은 혼합 표기 적음. agent가 자연어로 해석. sub_label_raw에 sample 식별자. thickness는 TEXT (자동과 다름!).",
        "key_columns": ["process_date", "operator", "sub_label_raw", "sp_power_w", "thickness", "notes"],
    },
    "sputter_runs_auto_main": {
        "purpose": "CH1 sputter 장비 자동 기록 Main Process (실제 박막 증착 단계).",
        "domain_notes": "자동 기록이라 타입 명확. process_datetime은 timestamp (사람 sputter는 process_date). thickness_nm은 INTEGER. sp_*는 setpoint, avg_*는 실측 평균. ABS RF/DC power, arc count 등.",
        "key_columns": ["process_datetime", "process_name", "thickness_nm", "sp_power_w", "avg_power_w", "sp_o2_sccm"],
    },
    "sputter_runs_auto_plasma": {
        "purpose": "CH1 sputter 자동 기록 Plasma Cleaning 단계 (Main 전 챔버 청소).",
        "domain_notes": "Main과 같은 공정의 사전 단계. process_datetime으로 Main과 연결 가능.",
        "key_columns": ["process_datetime", "process_name", "time_min", "sp_power_w", "avg_pressure_mtorr"],
    },
    "measurements": {
        "purpose": "VO2 박막 4-probe R-T 측정 .dat 파일. 한 row = 한 파일 = 한 측정 (시계열 평균 500점).",
        "domain_notes": (
            "트리 traversal로 적재 (source_files 안 거침). file_path UNIQUE.\n"
            "temperature_c[] (°C) + resistance_ohm[] (Ohm) 같은 길이 배열. point_count에 길이 저장.\n"
            "sub_kind/sub_batch_no가 ALD batch와 연결 (NULL 55% = 옛 자유 파일명).\n"
            "suffix_n: 같은 sample 여러 번 측정 시 _0, _1, _2... 중 가장 큰 N만 적재 (최종 측정).\n"
            "parse_status='ok'|'header_only'|'error'. error인 row는 시계열 NULL + raw_header에 에러 메시지.\n"
            "VO2 전이온도 분석은 dR/dT의 변곡점 위치 (보통 60~70°C). 시계열 raw 보려면 get_timeseries(table='measurements', row_id=ID).\n"
            "주의: 시계열 컬럼이 매우 큼. run_sql에서는 length만 표시됨. 분석은 SQL의 unnest()로 가능."
        ),
        "key_columns": ["measurement_date", "sub_kind", "sub_batch_no", "suffix_n", "point_count", "parse_status", "file_name"],
    },
    "rga_runs": {
        "purpose": "RGA (Residual Gas Analyzer) chamber 잔류 가스 spectrum. 시간별 Mass 1~65 partial pressure.",
        "domain_notes": (
            "한 row = 한 측정 시점. intensity[65] 배열 (Mass 1=H, Mass 2=H2, Mass 18=H2O, Mass 28=N2/CO, Mass 32=O2, Mass 40=Ar 등).\n"
            "운영자가 sputter 공정 전후로 수동 측정 (2024-05 ~ 현재 누적 2544+ row).\n"
            "intensity[18]은 1-based PostgreSQL 배열 (Python과 다름).\n"
            "raw_json에 전체 65 mass + Time 원본 보존."
        ),
        "key_columns": ["measured_at", "mass_count", "parse_status"],
    },
}


# ─────────── equipment 스키마 (외부 앱 소유, read-only) ───────────
# 가공/매핑 없이 그대로 노출. GPT가 raw로 판단.

EQUIPMENT_TABLE_META = {
    "equipments": {
        "purpose": "장비 마스터. sputter/ALD/evaporator/furnace 등 8장비 등록.",
        "domain_notes": (
            "한 row = 한 장비. category로 그룹 (Sputter/ALD/Evaporator/Furnace).\n"
            "VO2 공정 관련 장비: id=3 CH1 Sputter (메인), id=7 Rayvac ALD, id=8 NCD ALD.\n"
            "VO2 비관련: id=4 CH2 Sputter, id=5 CHK Sputter, id=6 In-Line Sputter, id=9 Evaporator, id=10 Furnace.\n"
            "vent_interval_days/cleaning_interval_days: 현재 모두 0으로 미설정 — 주기 기반 분석에 사용 불가.\n"
            "description: 보통 NULL (운영자가 채우지 않음)."
        ),
        "key_columns": ["id", "name", "category", "is_active", "is_vent_target", "is_cleaning_target"],
    },
    "equipment_logs": {
        "purpose": "장비 이벤트(수리/벤트/클리닝) 메인 로그. 한 row = 한 이벤트.",
        "domain_notes": (
            "event_type 값:\n"
            "  - 'repair'   : 수리 (symptom/replaced_parts/is_external/vendor_name 사용)\n"
            "  - 'vent'     : 벤트 (vent_reason 사용)\n"
            "  - 'cleaning' : 클리닝 (cleaning_type 사용)\n"
            "status: '처리중'(default) / '완료'. '완료'만 신뢰할 만한 이벤트.\n"
            "occurred_at: timestamp WITHOUT time zone — KST 가정 (운영자 직접 입력).\n"
            "completed_at: timestamp WITH time zone — '완료' 처리한 시각.\n"
            "is_external=true면 외주 수리 (vendor_name에 업체명).\n"
            "vent_reason/cleaning_type은 자유 입력 VARCHAR (FK 아님). "
            "vent_reason_options/cleaning_type_options에 권장 라벨 있으나 강제 안 됨 — 필터 시 fuzzy 매칭.\n"
            "description에 운영자가 자연어로 풍부한 메모 남김 (수리 원인/조치, 무게 변화 등). 분석 시 핵심.\n"
            "VO2 분석은 equipment_id IN (3, 7, 8) 으로 필터."
        ),
        "key_columns": ["id", "equipment_id", "event_type", "occurred_at", "status", "operator", "description"],
    },
    "equipment_log_entries": {
        "purpose": "equipment_logs의 진행 메모(시간순). '처리중' 상태에서 경과 기록용.",
        "domain_notes": (
            "한 log_id 당 0개 이상의 메모. 현재 운영에서 거의 사용 안 됨 (0건)."
        ),
        "key_columns": ["id", "log_id", "occurred_at"],
    },
    "equipment_photos": {
        "purpose": "equipment_logs에 첨부된 사진 메타데이터.",
        "domain_notes": (
            "메타만 노출 (file_name/mime_type/file_size/created_at). "
            "원본 base64(file_data)는 vo2_reader 권한에서 차단 — 토큰 폭발 방지."
        ),
        "key_columns": ["id", "log_id", "file_name", "file_size"],
    },
    "equipment_entry_photos": {
        "purpose": "equipment_log_entries에 첨부된 사진 메타데이터.",
        "domain_notes": (
            "log_entries가 거의 미사용이라 사실상 빈 상태. file_data 차단됨."
        ),
        "key_columns": ["id", "entry_id", "file_name"],
    },
    "cleaning_type_options": {
        "purpose": "클리닝 종류 권장 라벨. equipment_logs.cleaning_type의 후보값.",
        "domain_notes": (
            "FK 아님 — 자유 VARCHAR. 현재 4종: '정기 클리닝', '챔버 세정', '비정기', '기타'."
        ),
        "key_columns": ["id", "label"],
    },
    "vent_reason_options": {
        "purpose": "벤트 사유 권장 라벨. equipment_logs.vent_reason의 후보값.",
        "domain_notes": (
            "FK 아님 — 자유 VARCHAR. 현재 5종: '타겟 교체', '정기 점검', '수리', '클리닝', '기타'."
        ),
        "key_columns": ["id", "label"],
    },
}


# vo2 + equipment 통합 lookup
ALL_TABLE_META = {
    **{f"vo2.{k}": v for k, v in TABLE_META.items()},
    **{f"equipment.{k}": v for k, v in EQUIPMENT_TABLE_META.items()},
}


def _resolve_qualified(table: str) -> str | None:
    """'equipments' → 'equipment.equipments', 'measurements' → 'vo2.measurements'.
    'vo2.X' / 'equipment.X' 형태는 그대로. 없으면 None."""
    if "." in table:
        return table if table in ALL_TABLE_META else None
    for cand in (f"vo2.{table}", f"equipment.{table}"):
        if cand in ALL_TABLE_META:
            return cand
    return None


# ─────────── 메인 ───────────

def run(table: str | None = None) -> dict[str, Any]:
    """describe_schema 도구 진입.

    Args:
        table: None이면 전체 요약. 테이블명 주면 그 테이블 상세.

    Returns:
        {schema_overview: {...}, tables: [...] 또는 table: {...}, ...}
    """
    if table is None:
        return _describe_all()
    else:
        return _describe_one(table)


def _describe_all() -> dict[str, Any]:
    """전체 테이블 요약 (vo2 11개 + equipment 7개 = 18개)."""
    table_summaries = []

    with reader_session() as s:
        for qualified, meta in ALL_TABLE_META.items():
            schema_name, tbl = qualified.split(".", 1)
            row_count = s.execute(
                text(f"SELECT COUNT(*) FROM {qualified}")
            ).scalar_one()
            col_count = s.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema=:s AND table_name=:t"
                ),
                {"s": schema_name, "t": tbl},
            ).scalar_one()
            unique_def = s.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE contype='u' AND connamespace=(:s)::regnamespace "
                    "AND conrelid = (:s || '.' || :t)::regclass LIMIT 1"
                ),
                {"s": schema_name, "t": tbl},
            ).scalar_one_or_none()

            table_summaries.append({
                "table": qualified,
                "purpose": meta["purpose"],
                "domain_notes": meta["domain_notes"],
                "key_columns": meta["key_columns"],
                "row_count": row_count,
                "column_count": col_count,
                "unique_key": unique_def,
            })

    return {
        "schemas": ["vo2", "equipment"],
        "database": "inventory",
        "total_tables": len(ALL_TABLE_META),
        "domain_overview": DOMAIN_OVERVIEW.strip(),
        "sample_mapping_guide": SAMPLE_MAPPING_GUIDE.strip(),
        "common_queries": COMMON_QUERIES,
        "tables": table_summaries,
        "relationships": [
            "source_files.id ← parse_errors.source_file_id (CASCADE)",
            "source_files.id ← ald_ncd_runs.source_file_id (SET NULL)",
            "source_files.id ← ald_rayvac_runs.source_file_id (SET NULL)",
            "source_files.id ← sputter_runs_human.source_file_id (SET NULL)",
            "source_files.id ← sputter_runs_auto_main.source_file_id (SET NULL)",
            "source_files.id ← sputter_runs_auto_plasma.source_file_id (SET NULL)",
            "source_files.id ← rga_runs.source_file_id",
            "measurements: NO FK — 자체 트리 traversal로 적재, file_path UNIQUE",
            "equipment.equipments.id ← equipment.equipment_logs.equipment_id (외부 앱 FK)",
            "equipment.equipment_logs.id ← equipment.equipment_photos.log_id",
            "equipment.equipment_logs.id ← equipment.equipment_log_entries.log_id",
            "equipment.equipment_log_entries.id ← equipment.equipment_entry_photos.entry_id",
            "vo2 ↔ equipment: FK 없음. timestamp(KST) 기반 연관 분석만 가능.",
        ],
        "next_step_hint": (
            "특정 테이블 상세 (모든 컬럼 타입 + 예시 5 row)는 "
            "describe_schema(table='ald_ncd_runs') 또는 "
            "describe_schema(table='equipment_logs') 형태로 호출. "
            "분석 SQL은 run_sql(sql='SELECT ...'). "
            "시계열 raw 배열은 get_timeseries(table='measurements'|'rga_runs', row_id=N)."
        ),
    }


def _describe_one(table: str) -> dict[str, Any]:
    """특정 테이블 상세. vo2 / equipment 둘 다 지원.
    bare name 주면 자동으로 스키마 추론. 'vo2.X' 또는 'equipment.X'도 OK."""
    qualified = _resolve_qualified(table)
    if qualified is None:
        return {
            "error": f"unknown table: {table!r}. "
                     f"available: {list(ALL_TABLE_META.keys())}",
        }

    meta = ALL_TABLE_META[qualified]
    schema_name, table_name = qualified.split(".", 1)

    with reader_session() as s:
        # 컬럼 정보
        columns = s.execute(
            text("""
                SELECT
                    column_name,
                    ordinal_position,
                    data_type,
                    udt_name,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_schema=:s AND table_name=:t
                ORDER BY ordinal_position
            """),
            {"s": schema_name, "t": table_name},
        ).mappings().all()

        # 제약
        unique_def = s.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype='u' AND connamespace=(:s)::regnamespace "
                "AND conrelid = (:s || '.' || :t)::regclass LIMIT 1"
            ),
            {"s": schema_name, "t": table_name},
        ).scalar_one_or_none()

        fk_defs = s.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype='f' AND connamespace=(:s)::regnamespace "
                "AND conrelid = (:s || '.' || :t)::regclass"
            ),
            {"s": schema_name, "t": table_name},
        ).scalars().all()

        # row 수
        row_count = s.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar_one()

        # 예시 5 row — 큰 컬럼 (raw_json, raw_data, jsonb, 배열, 시계열, base64 사진) 제외
        large_cols = {
            "raw_json", "raw_data", "temperature_c", "resistance_ohm", "intensity",
            "metadata", "arguments", "result_summary",
            "file_data",  # equipment_photos / equipment_entry_photos의 base64
        }
        select_cols = [c["column_name"] for c in columns if c["column_name"] not in large_cols]
        select_clause = ", ".join(f'"{c}"' for c in select_cols) if select_cols else "*"

        sample_rows = s.execute(
            text(f"SELECT {select_clause} FROM {qualified} ORDER BY 1 DESC LIMIT 5")
        ).mappings().all()

    return {
        "table": qualified,
        "purpose": meta["purpose"],
        "domain_notes": meta["domain_notes"],
        "row_count": row_count,
        "unique_constraint": unique_def,
        "foreign_keys": fk_defs,
        "key_columns": meta["key_columns"],
        "columns": [
            {
                "name": c["column_name"],
                "position": c["ordinal_position"],
                "type": c["data_type"],
                "udt": c["udt_name"],
                "nullable": c["is_nullable"] == "YES",
                "default": c["column_default"],
            }
            for c in columns
        ],
        "sample_rows": [
            {k: _serialize(v) for k, v in row.items()}
            for row in sample_rows
        ],
        "sample_note": (
            "raw_json/raw_data/배열/시계열/file_data(base64) 컬럼은 예시에서 제외됨. "
            "전체 보려면 run_sql 또는 get_timeseries 사용."
        ),
    }


def _serialize(v: Any) -> Any:
    """JSON 직렬화 헬퍼."""
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v