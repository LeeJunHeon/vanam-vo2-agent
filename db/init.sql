-- ──────────────────────────────────────────────────────────────────────────
-- vo2-agent DB schema (Phase 4 Step 16-3 시점)
--
-- 본 파일은 운영 DB(inventory-web-postgres)의 vo2 schema와 100% 일치하는
-- 기준 정의(documentation source-of-truth)다. 자동 적용되지 않는다.
-- 운영자는 DBeaver 등으로 직접 schema 변경을 적용했고, 본 파일은 그 결과를
-- 추적/재현 가능하게 기록한다.
--
-- 데이터 모델 원칙 (Phase 4 합의):
-- - ETL은 xlsx → DB 단순 복사 (해석/매칭은 agent/MCP가 처리)
-- - xlsx 그대로 보존 (중복 batch_no 허용, 사람이 적은 혼합 타입은 TEXT)
-- - 모든 source 테이블 UNIQUE는 xlsx 위치 (source_file_id, row_number)
--   (단 ald_ncd_runs는 두 시트 공유 row_number 때문에 chemistry 추가)
-- - parse_errors 메시지는 GPT/운영자 친화 자연어
-- - xlsx 변경 시 옛 데이터 보존 + 새 source_file_id로 새 row INSERT
-- - audit_logs는 보존 (감사 추적 누적)
--
-- 9개 테이블:
-- - source_files, etl_runs, mcp_audit_logs (운영 인프라)
-- - parse_errors (모든 source의 row 단위 격리)
-- - ald_ncd_runs, ald_rayvac_runs (ALD 공정)
-- - sputter_runs_human (사람 sputter log, 28 컬럼)
-- - sputter_runs_auto_main (자동 CH1 Main Process, 44 컬럼)
-- - sputter_runs_auto_plasma (자동 CH1 Plasma Cleaning, 22 컬럼)
-- ──────────────────────────────────────────────────────────────────────────

-- ────────────────────────────────────────────────────────────────────────
-- 1. source_files (모든 source의 인덱싱 root, FK 부모)
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vo2.source_files (
    id                BIGSERIAL PRIMARY KEY,
    source_type       TEXT NOT NULL,
    equipment         TEXT,
    chamber           TEXT,
    file_path         TEXT NOT NULL,
    file_name         TEXT NOT NULL,
    file_ext          TEXT,
    file_size         BIGINT,
    modified_at       TIMESTAMPTZ,
    sha256            TEXT NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ,
    last_indexed_at   TIMESTAMPTZ,
    parser_status     TEXT NOT NULL DEFAULT 'pending',
    parser_error      TEXT,
    row_count         INTEGER DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_source_files_path_sha UNIQUE (file_path, sha256)
);
CREATE INDEX IF NOT EXISTS idx_source_files_type
    ON vo2.source_files (source_type);
CREATE INDEX IF NOT EXISTS idx_source_files_status
    ON vo2.source_files (parser_status);
CREATE INDEX IF NOT EXISTS idx_source_files_sha256
    ON vo2.source_files (sha256);
CREATE INDEX IF NOT EXISTS idx_source_files_modified_at
    ON vo2.source_files (modified_at);

-- ────────────────────────────────────────────────────────────────────────
-- 2. etl_runs (5분 tick 추적)
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vo2.etl_runs (
    id                BIGSERIAL PRIMARY KEY,
    job_name          TEXT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running',
    files_seen        INTEGER DEFAULT 0,
    files_processed   INTEGER DEFAULT 0,
    rows_inserted     INTEGER DEFAULT 0,
    rows_updated      INTEGER DEFAULT 0,
    error             TEXT,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_etl_runs_job_started
    ON vo2.etl_runs (job_name, started_at DESC);

-- ────────────────────────────────────────────────────────────────────────
-- 3. mcp_audit_logs (MCP server 도구 호출 감사 — 절대 TRUNCATE 금지)
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vo2.mcp_audit_logs (
    id                BIGSERIAL PRIMARY KEY,
    called_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    caller_kind       TEXT NOT NULL,
    caller_id         TEXT,
    session_id        TEXT,
    ip_address        INET,
    tool_name         TEXT NOT NULL,
    arguments         JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary    JSONB NOT NULL DEFAULT '{}'::jsonb,
    success           BOOLEAN NOT NULL DEFAULT TRUE,
    error             TEXT,
    duration_ms       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_called
    ON vo2.mcp_audit_logs (called_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_caller
    ON vo2.mcp_audit_logs (caller_kind, caller_id);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_tool
    ON vo2.mcp_audit_logs (tool_name);

-- ────────────────────────────────────────────────────────────────────────
-- 4. parse_errors (모든 source의 row 단위 격리)
-- ────────────────────────────────────────────────────────────────────────
-- 정상 INSERT된 row와 1:N. error_type 예시:
-- 'batch_no_invalid', 'date_missing', 'timestamp_invalid', 'column_shift'
-- error_detail은 GPT/운영자 친화 자연어 (어떻게 해결할지 명시).
CREATE TABLE IF NOT EXISTS vo2.parse_errors (
    id              BIGSERIAL PRIMARY KEY,
    source_file_id  BIGINT NOT NULL REFERENCES vo2.source_files(id) ON DELETE CASCADE,
    row_number      INTEGER NOT NULL,
    error_type      TEXT NOT NULL,
    error_detail    TEXT,
    raw_data        JSONB NOT NULL,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at     TIMESTAMPTZ,
    resolved_note   TEXT,
    CONSTRAINT uq_parse_errors UNIQUE (source_file_id, row_number, error_type)
);
CREATE INDEX IF NOT EXISTS idx_parse_errors_unresolved
    ON vo2.parse_errors (source_file_id) WHERE NOT resolved;
CREATE INDEX IF NOT EXISTS idx_parse_errors_type
    ON vo2.parse_errors (error_type, detected_at DESC);

-- ────────────────────────────────────────────────────────────────────────
-- 5. ald_ncd_runs (NCD ALD, oxidant H2O, TTIP/TDMAT 두 chemistry)
-- ────────────────────────────────────────────────────────────────────────
-- NCD xlsx의 "레시피 및 결과(TTIP)" / "(TDMAT)" 두 시트 → 한 테이블 (chemistry 컬럼으로 구분).
-- batch_no는 시트 내 자체 일련번호. 같은 batch_no가 여러 row일 수 있음 (재공정 케이스).
-- UNIQUE는 (source_file_id, chemistry, row_number) — 두 시트 같은 row_number 공유하므로 chemistry 필수.
CREATE TABLE IF NOT EXISTS vo2.ald_ncd_runs (
    ald_run_id                    BIGSERIAL PRIMARY KEY,
    batch_no                      INTEGER NOT NULL,
    chemistry                     TEXT NOT NULL,    -- 'TTIP' | 'TDMAT'
    process_date                  DATE NOT NULL,
    -- 공정 파라미터 (NCD 컬럼명 그대로)
    temp_c                        REAL,
    pre_heat_delay_s              REAL,
    stable_time_s                 REAL,
    pre_heat_temp_c               REAL,
    precursor_temp_c              REAL,
    precursor_pulse_s             REAL,
    precursor_purge_s             REAL,
    h2o_temp_c                    REAL,
    h2o_pulse_s                   REAL,
    h2o_purge_s                   REAL,
    precursor_assist_flow_sccm    REAL,
    source_carrier_flow_sccm      REAL,
    h2o_carrier_flow_sccm         REAL,
    outer_flow_sccm               REAL,
    cycles                        INTEGER,
    precursor_cum_cycles          INTEGER,
    h2o_cum_cycles                INTEGER,
    chamber_clean_cum_cycles      INTEGER,
    -- 측정 결과
    gpc_a_per_cycle               REAL,
    avg_max_min_pct               REAL,
    -- 메타
    source_file_id                BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number                    INTEGER,
    raw_json                      JSONB NOT NULL,
    parse_status                  TEXT,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ald_ncd_runs UNIQUE (source_file_id, chemistry, row_number)
);
CREATE INDEX IF NOT EXISTS idx_ald_ncd_runs_date
    ON vo2.ald_ncd_runs (process_date DESC);
CREATE INDEX IF NOT EXISTS idx_ald_ncd_runs_batch
    ON vo2.ald_ncd_runs (chemistry, batch_no);

-- ────────────────────────────────────────────────────────────────────────
-- 6. ald_rayvac_runs (Rayvac ALD, oxidant O3)
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vo2.ald_rayvac_runs (
    ald_run_id                    BIGSERIAL PRIMARY KEY,
    batch_no                      INTEGER NOT NULL,
    process_date                  DATE NOT NULL,
    -- 공정 파라미터 (Rayvac 컬럼명 그대로)
    stable_time_min               REAL,
    stage_temp_c                  REAL,
    body_temp_c                   REAL,
    top_temp_c                    REAL,
    stage_height_mm               REAL,
    precursor_line_temp_c         REAL,
    reactant_line_temp_c          REAL,
    base_pressure_torr            REAL,
    throttle_pct                  REAL,
    ttip_temp_c                   REAL,
    source_base_sccm              REAL,
    ttip_assist_sccm              REAL,
    reactant_base_sccm            REAL,
    o3_conc                       REAL,
    o2_flow_sccm                  REAL,
    ttip_assist_time_s            REAL,
    ttip_pulse_s                  REAL,
    ttip_purge_s                  REAL,
    o3_pulse_s                    REAL,
    o3_purge_s                    REAL,
    cycles                        INTEGER,
    plasma_cleaning_flag          BOOLEAN,
    -- 측정 결과
    gpc_a_per_cycle               REAL,
    max_min_pct                   REAL,
    virtual_max_min_pct           REAL,
    -- 메타
    source_file_id                BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number                    INTEGER,
    raw_json                      JSONB NOT NULL,
    parse_status                  TEXT,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ald_rayvac_runs UNIQUE (source_file_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_ald_rayvac_runs_date
    ON vo2.ald_rayvac_runs (process_date DESC);
CREATE INDEX IF NOT EXISTS idx_ald_rayvac_runs_batch
    ON vo2.ald_rayvac_runs (batch_no);

-- ────────────────────────────────────────────────────────────────────────
-- 7. sputter_runs_human (사람 sputter log, 23 컬럼 매핑)
-- ────────────────────────────────────────────────────────────────────────
-- Ch1 process log xlsx의 '통합' 단일 시트 → 한 row가 한 sputter 공정.
-- C0~C22 (W까지) 매핑. C23+ 무시 (운영자 메모).
-- 사람이 손으로 적은 거라 혼합 타입 다수 → TEXT 보존.
CREATE TABLE IF NOT EXISTS vo2.sputter_runs_human (
    id                   BIGSERIAL PRIMARY KEY,
    process_date         TIMESTAMP,
    operator             TEXT,
    sub_label_raw        TEXT,
    pc_gas               TEXT,
    pc_power_w           REAL,
    pc_pressure_mtorr    REAL,
    pc_gas_flow_sccm     REAL,
    pc_time_min          REAL,
    shutter_delay_min    REAL,
    sp_power_w           REAL,
    sp_flow_sccm         REAL,
    sp_pressure_mtorr    REAL,
    sp_time              TEXT,
    thickness            TEXT,
    furnace_type         TEXT,
    annealing_temp       TEXT,
    annealing_gas_flow   TEXT,
    pulsed_dc_freq       TEXT,
    off_time_us          REAL,
    duty                 TEXT,
    depo_rate            TEXT,
    target               TEXT,
    notes                TEXT,
    -- Phase 4 Step 22: '날짜' 컬럼 다목적 처리 (forward fill + 분류)
    raw_date_value       TEXT,
    raw_date_type        TEXT,
    process_seq_in_day   INTEGER,
    row_kind             TEXT,
    source_file_id       BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number           INTEGER NOT NULL,
    raw_json             JSONB NOT NULL,
    parse_status         TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sputter_runs_human UNIQUE (source_file_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_sputter_runs_human_date
    ON vo2.sputter_runs_human (process_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_sputter_runs_human_row_kind
    ON vo2.sputter_runs_human (row_kind);

-- ────────────────────────────────────────────────────────────────────────
-- 8. sputter_runs_auto_main (자동 CH1.xlsx 'Main Process' 시트, 38 컬럼)
-- ────────────────────────────────────────────────────────────────────────
-- 자동 측정 데이터라 타입 명확.
CREATE TABLE IF NOT EXISTS vo2.sputter_runs_auto_main (
    id                   BIGSERIAL PRIMARY KEY,
    process_datetime     TIMESTAMP,
    operator             TEXT,
    process_name         TEXT,
    notes                TEXT,
    substrate            TEXT,
    main_shutter         TEXT,
    power_select         TEXT,
    g1_target            TEXT,
    g2_target            TEXT,
    g3_target            TEXT,
    deposition_rate      REAL,
    thickness_nm         INTEGER,
    chuck                TEXT,
    shutter_delay_min    REAL,
    process_time_min     REAL,
    base_pressure_torr   REAL,
    sp_ar_sccm           INTEGER,
    avg_ar_sccm          REAL,
    sp_n2_sccm           INTEGER,
    avg_n2_sccm          REAL,
    sp_o2_sccm           INTEGER,
    avg_o2_sccm          REAL,
    sp_pressure_mtorr    INTEGER,
    avg_pressure_mtorr   REAL,
    power_source         TEXT,
    sp_power_w           INTEGER,
    avg_power_w          INTEGER,
    avg_for_p_w          REAL,
    avg_ref_p_w          REAL,
    avg_load             TEXT,
    avg_tune             TEXT,
    avg_voltage_v        REAL,
    avg_current_a        REAL,
    duty_cycle_pct       INTEGER,
    frequency_khz        INTEGER,
    off_time_us          INTEGER,
    soft_arc_count       INTEGER,
    hard_arc_count       INTEGER,
    source_file_id       BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number           INTEGER NOT NULL,
    raw_json             JSONB NOT NULL,
    parse_status         TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sputter_runs_auto_main UNIQUE (source_file_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_sputter_runs_auto_main_dt
    ON vo2.sputter_runs_auto_main (process_datetime DESC NULLS LAST);

-- ────────────────────────────────────────────────────────────────────────
-- 9. sputter_runs_auto_plasma (자동 CH1.xlsx 'Plasma Cleaning' 시트, 16 컬럼)
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vo2.sputter_runs_auto_plasma (
    id                   BIGSERIAL PRIMARY KEY,
    process_datetime     TIMESTAMP,
    operator             TEXT,
    process_name         TEXT,
    notes                TEXT,
    substrate            TEXT,
    time_min             REAL,
    base_pressure_torr   REAL,
    sp_ar_sccm           INTEGER,
    avg_ar_sccm          REAL,
    sp_pressure_mtorr    INTEGER,
    avg_pressure_mtorr   REAL,
    sp_power_w           INTEGER,
    avg_for_p_w          REAL,
    avg_ref_p_w          REAL,
    avg_load             TEXT,
    avg_tune             TEXT,
    source_file_id       BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number           INTEGER NOT NULL,
    raw_json             JSONB NOT NULL,
    parse_status         TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sputter_runs_auto_plasma UNIQUE (source_file_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_sputter_runs_auto_plasma_dt
    ON vo2.sputter_runs_auto_plasma (process_datetime DESC NULLS LAST);

-- ──────────────────────────────────────────────────────────────────────────
-- 권한: vo2_reader (read-only) / vo2_writer (ETL)
-- ──────────────────────────────────────────────────────────────────────────
-- vo2_reader: MCP server가 사용 (read-only)
GRANT SELECT ON
    vo2.source_files,
    vo2.etl_runs,
    vo2.mcp_audit_logs,
    vo2.parse_errors,
    vo2.ald_ncd_runs,
    vo2.ald_rayvac_runs,
    vo2.sputter_runs_human,
    vo2.sputter_runs_auto_main,
    vo2.sputter_runs_auto_plasma
    TO vo2_reader;

-- vo2_writer: ETL worker가 사용
GRANT SELECT, INSERT, UPDATE ON
    vo2.source_files,
    vo2.etl_runs,
    vo2.mcp_audit_logs,
    vo2.parse_errors,
    vo2.ald_ncd_runs,
    vo2.ald_rayvac_runs,
    vo2.sputter_runs_human,
    vo2.sputter_runs_auto_main,
    vo2.sputter_runs_auto_plasma
    TO vo2_writer;

-- BIGSERIAL 시퀀스 USAGE
GRANT USAGE, SELECT ON SEQUENCE
    vo2.source_files_id_seq,
    vo2.etl_runs_id_seq,
    vo2.mcp_audit_logs_id_seq,
    vo2.parse_errors_id_seq,
    vo2.ald_ncd_runs_ald_run_id_seq,
    vo2.ald_rayvac_runs_ald_run_id_seq,
    vo2.sputter_runs_human_id_seq,
    vo2.sputter_runs_auto_main_id_seq,
    vo2.sputter_runs_auto_plasma_id_seq
    TO vo2_writer;
