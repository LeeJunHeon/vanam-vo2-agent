-- vo2-agent DB schema (Step 4-fix4 시점)
--
-- 주의: 본 파일은 운영 DB(inventory-web-postgres)의 vo2 schema와 일치하는
-- 기준 정의(documentation source-of-truth)일 뿐, 자동 적용되지 않는다.
-- 운영자는 DBeaver 등으로 직접 schema 변경을 적용했고, 본 파일은 그 결과를
-- 추적/재현 가능하게 기록한다. Phase 2 이후 Alembic으로 마이그레이션 관리 예정.
--
-- Step 4-fix4 변경:
-- 1. vo2.parse_errors 테이블 신규 (파싱 단계의 row 단위 이상 기록)
-- 2. vo2.sputter_runs.start_time → NULLable (timestamp 깨진 row도 보존)
-- 3. vo2.sputter_runs.parse_status TEXT 컬럼 추가 (파싱 상태 라벨)


-- ──────────────────────────────────────────────────────────────────────────
-- Step 4-fix4: parse_errors 테이블
-- ──────────────────────────────────────────────────────────────────────────
-- 한 source_file의 한 row에서 발견된 파싱 단계 이상을 기록.
-- 정상 INSERT된 sputter_runs row와 1:N (한 row가 여러 종류의 이상을 가질 수 있음).
-- error_type 예시: 'timestamp_invalid', 'column_shift', 'unknown_recipe'
-- (source_file_id, row_number, error_type) UNIQUE → 같은 tick에서 중복 기록 방지

CREATE TABLE IF NOT EXISTS vo2.parse_errors (
  id BIGSERIAL PRIMARY KEY,
  source_file_id BIGINT NOT NULL
    REFERENCES vo2.source_files(id) ON DELETE CASCADE,
  row_number INTEGER NOT NULL,
  error_type TEXT NOT NULL,
  error_detail TEXT,
  raw_data JSONB NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved BOOLEAN NOT NULL DEFAULT FALSE,
  resolved_at TIMESTAMPTZ,
  resolved_note TEXT,
  CONSTRAINT uq_parse_errors UNIQUE (source_file_id, row_number, error_type)
);

CREATE INDEX IF NOT EXISTS idx_parse_errors_unresolved
  ON vo2.parse_errors (source_file_id) WHERE NOT resolved;

CREATE INDEX IF NOT EXISTS idx_parse_errors_type
  ON vo2.parse_errors (error_type, detected_at DESC);


-- ──────────────────────────────────────────────────────────────────────────
-- Step 4-fix4: sputter_runs 변경
-- ──────────────────────────────────────────────────────────────────────────
-- start_time NULL 허용: timestamp 파싱 실패한 row도 raw_json 보존하며 INSERT.
-- parse_status: 'timestamp_missing' | 'partial' | NULL(clean).

ALTER TABLE vo2.sputter_runs ALTER COLUMN start_time DROP NOT NULL;
ALTER TABLE vo2.sputter_runs ADD COLUMN IF NOT EXISTS parse_status TEXT;


-- ──────────────────────────────────────────────────────────────────────────
-- Phase 4 Step 11+12.5: ALD/Sample/측정 데이터 모델
-- ──────────────────────────────────────────────────────────────────────────
-- 변경 요약:
-- 1. vo2.ald_ncd_runs    — NCD ALD 공정 (TTIP/TDMAT 두 chemistry, oxidant H2O)
-- 2. vo2.ald_rayvac_runs — Rayvac ALD 공정 (TTIP만, oxidant O3)
-- 3. vo2.samples         — wafer 조각 (ald_source/ald_run_id/ald_chemistry로 ALD 어느 쪽이든 가리킴)
-- 4. vo2.sputter_run_samples — sputter ↔ sample 다대다 매핑
-- 5. vo2.match_pending   — 자동 매칭 실패 격리
-- 6. vo2.sputter_runs    — 컬럼 2개 추가 (sample_label_raw, sample_label_norm) + 옛 sample_id TEXT DROP
--
-- 데이터 흐름:
-- ALD xlsx (NCD/Rayvac) → ald_ncd_runs / ald_rayvac_runs (배치 등록)
--                      ↓
--                   samples (wafer 조각) — ald_source + ald_run_id 로 ALD 가리킴
-- 사람 sputter log → sputter_runs (Sub. 컬럼은 sample_label_raw에)
--                  → sputter_run_samples (다대다)
-- 측정 .dat → samples 매칭 (있으면 measurements, 없으면 match_pending)
--
-- 역추적: 측정 → samples → sputter_run_samples → sputter_runs (sputter 공정)
--                       └→ ald_source / ald_run_id  (ALD 공정 — NCD or Rayvac)

-- ─────── 1. ald_ncd_runs (NCD 전용, oxidant H2O) ─────────────────────────
-- NCD xlsx의 "레시피 및 결과(TTIP)" / "(TDMAT)" 시트의 한 row = 한 batch.
-- batch_no는 시트 내 자체 일련번호. 같은 batch_no가 여러 row일 수 있음 (재공정 케이스 — xlsx 그대로 보존).
-- UNIQUE는 xlsx 위치 (source_file_id, row_number). (chemistry, batch_no)는 검색용 INDEX.
CREATE TABLE IF NOT EXISTS vo2.ald_ncd_runs (
    ald_run_id BIGSERIAL PRIMARY KEY,
    batch_no INTEGER NOT NULL,
    chemistry TEXT NOT NULL,             -- 'TTIP' | 'TDMAT'
    process_date DATE NOT NULL,

    -- 공정 파라미터 (NCD 컬럼명 그대로)
    temp_c REAL,                         -- 'Temp (°C)'
    pre_heat_delay_s REAL,
    stable_time_s REAL,
    pre_heat_temp_c REAL,
    precursor_temp_c REAL,               -- 'TTIP/TDMAT Temp'
    precursor_pulse_s REAL,
    precursor_purge_s REAL,
    h2o_temp_c REAL,
    h2o_pulse_s REAL,
    h2o_purge_s REAL,
    precursor_assist_flow_sccm REAL,
    source_carrier_flow_sccm REAL,
    h2o_carrier_flow_sccm REAL,
    outer_flow_sccm REAL,
    cycles INTEGER,

    -- 누적 (NCD 고유)
    precursor_cum_cycles INTEGER,        -- 'TTIP/TDMAT 소비 사이클'
    h2o_cum_cycles INTEGER,
    chamber_clean_cum_cycles INTEGER,    -- '챔버 클리닝 이후 누적 사이클'

    -- 측정 결과
    gpc_a_per_cycle REAL,
    avg_max_min_pct REAL,

    -- 메타
    source_file_id BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number INTEGER,
    raw_json JSONB NOT NULL,             -- 모든 43 원본 컬럼 보존 (분석 안전망)
    parse_status TEXT,                   -- NULL=clean, 'partial' 등
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_ald_ncd_runs UNIQUE (source_file_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_ald_ncd_runs_date ON vo2.ald_ncd_runs (process_date DESC);
CREATE INDEX IF NOT EXISTS idx_ald_ncd_runs_batch ON vo2.ald_ncd_runs (chemistry, batch_no);

-- ─────── 2. ald_rayvac_runs (Rayvac 전용, oxidant O3) ─────────────────────
-- Rayvac xlsx의 "공정 레시피 & 결과 정리" 시트의 한 row = 한 batch.
-- batch_no는 시트 내 자체 일련번호. NCD와는 별개 시리즈.
-- UNIQUE는 xlsx 위치 (source_file_id, row_number)로 일관성 유지 (NCD 패턴과 동일).
CREATE TABLE IF NOT EXISTS vo2.ald_rayvac_runs (
    ald_run_id BIGSERIAL PRIMARY KEY,
    batch_no INTEGER NOT NULL,
    process_date DATE NOT NULL,

    -- 공정 파라미터 (Rayvac 컬럼명 그대로)
    stable_time_min REAL,
    stage_temp_c REAL,                   -- Rayvac 'Stage Temp (°C)'
    body_temp_c REAL,
    top_temp_c REAL,
    stage_height_mm REAL,
    precursor_line_temp_c REAL,
    reactant_line_temp_c REAL,
    base_pressure_torr REAL,
    throttle_pct REAL,
    ttip_temp_c REAL,
    source_base_sccm REAL,
    ttip_assist_sccm REAL,
    reactant_base_sccm REAL,
    o3_conc REAL,
    o2_flow_sccm REAL,
    ttip_assist_time_s REAL,
    ttip_pulse_s REAL,
    ttip_purge_s REAL,
    o3_pulse_s REAL,                     -- Rayvac은 oxidant가 O3
    o3_purge_s REAL,
    cycles INTEGER,
    plasma_cleaning_flag BOOLEAN,

    -- 측정 결과
    gpc_a_per_cycle REAL,
    max_min_pct REAL,
    virtual_max_min_pct REAL,

    -- 메타
    source_file_id BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number INTEGER,
    raw_json JSONB NOT NULL,             -- 모든 39 원본 컬럼 보존
    parse_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_ald_rayvac_runs UNIQUE (source_file_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_ald_rayvac_runs_date ON vo2.ald_rayvac_runs (process_date DESC);
CREATE INDEX IF NOT EXISTS idx_ald_rayvac_runs_batch ON vo2.ald_rayvac_runs (batch_no);

-- ─────── 3. samples (wafer 조각) ─────────────────────────────────────────
-- ALD에서 나온 sample 또는 기판 reference (Si/M-plane/external).
-- ALD 매칭은 옵션 A (soft reference): ald_source + ald_run_id (FK 없음)
-- → 운영 중 ALD 데이터 늦게 입력된 케이스도 lazy 매칭 가능.
-- → 강한 정합성은 match_pending에서 보강.
CREATE TABLE IF NOT EXISTS vo2.samples (
    sample_id BIGSERIAL PRIMARY KEY,
    sample_label_raw TEXT NOT NULL,      -- 'Ncd#(63)*2' 원본
    sample_label_norm TEXT NOT NULL,     -- 'ALD-63' 등 정규화
    substrate_kind TEXT NOT NULL,        -- 'ald_ncd' | 'ald_rayvac' | 'si' | 'm_plane' | 'external' | 'unknown'

    -- ALD reference (soft, 옵션 A)
    ald_source TEXT,                     -- 'NCD' | 'Rayvac' | NULL
    ald_run_id BIGINT,                   -- 해당 ald_*_runs 테이블의 PK (FK 없음)
    ald_chemistry TEXT,                  -- NCD인 경우 'TTIP'/'TDMAT', Rayvac이면 'TTIP'

    sub_sample_n INTEGER,                -- wafer 쪼갠 조각 번호
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT,

    CONSTRAINT uq_samples_norm UNIQUE (sample_label_norm),
    CONSTRAINT chk_ald_ref CHECK (
        (ald_source IS NULL AND ald_run_id IS NULL) OR
        (ald_source IS NOT NULL AND ald_run_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_samples_ald
    ON vo2.samples (ald_source, ald_run_id) WHERE ald_source IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_samples_substrate ON vo2.samples (substrate_kind);

-- ─────── 4. sputter_run_samples (다대다 매핑) ────────────────────────────
-- 한 sputter run에 여러 sample이 동시에 들어갈 수 있음 ("M-plane, #37,#50").
CREATE TABLE IF NOT EXISTS vo2.sputter_run_samples (
    sputter_run_id TEXT NOT NULL REFERENCES vo2.sputter_runs(sputter_run_id) ON DELETE CASCADE,
    sample_id BIGINT NOT NULL REFERENCES vo2.samples(sample_id) ON DELETE CASCADE,
    position TEXT,
    PRIMARY KEY (sputter_run_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_srs_sample ON vo2.sputter_run_samples (sample_id);

-- ─────── 5. match_pending (자동 매칭 실패 격리) ─────────────────────────
-- parse_errors와 동일 철학. 운영자가 DBeaver에서 수동 해결.
-- source_kind: 'sputter_log_sub' | 'measurement_path' | 'ald_xref' | 'auto_xlsx_match'
CREATE TABLE IF NOT EXISTS vo2.match_pending (
    id BIGSERIAL PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    candidates JSONB,
    reason TEXT NOT NULL,
    raw_data JSONB,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT,
    resolved_to TEXT,
    resolved_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_pending_unresolved
    ON vo2.match_pending (source_kind, detected_at DESC) WHERE NOT resolved;

-- ─────── 6. sputter_runs 컬럼 변경 ──────────────────────────────────────
-- sample_label_raw : 사람 log Sub. 컬럼 원본 (예: 'Ncd#(63)*2')
-- sample_label_norm: 정규화 식별자 (예: 'ALD-63')
-- 옛 sample_id TEXT는 DROP (Phase 4 다대다 매핑으로 대체)
ALTER TABLE vo2.sputter_runs DROP COLUMN IF EXISTS sample_id;
ALTER TABLE vo2.sputter_runs ADD COLUMN IF NOT EXISTS sample_label_raw TEXT;
ALTER TABLE vo2.sputter_runs ADD COLUMN IF NOT EXISTS sample_label_norm TEXT;
CREATE INDEX IF NOT EXISTS idx_sputter_runs_sample_norm
    ON vo2.sputter_runs (sample_label_norm) WHERE sample_label_norm IS NOT NULL;

-- ──────────────────────────────────────────────────────────────────────────
-- vo2_reader / vo2_writer 권한 (5 신규 테이블)
-- ──────────────────────────────────────────────────────────────────────────
GRANT SELECT ON
    vo2.ald_ncd_runs, vo2.ald_rayvac_runs,
    vo2.samples, vo2.sputter_run_samples, vo2.match_pending
    TO vo2_reader;

GRANT SELECT, INSERT, UPDATE ON
    vo2.ald_ncd_runs, vo2.ald_rayvac_runs,
    vo2.samples, vo2.sputter_run_samples, vo2.match_pending
    TO vo2_writer;

GRANT USAGE, SELECT ON SEQUENCE vo2.ald_ncd_runs_ald_run_id_seq TO vo2_writer;
GRANT USAGE, SELECT ON SEQUENCE vo2.ald_rayvac_runs_ald_run_id_seq TO vo2_writer;
GRANT USAGE, SELECT ON SEQUENCE vo2.samples_sample_id_seq TO vo2_writer;
GRANT USAGE, SELECT ON SEQUENCE vo2.match_pending_id_seq TO vo2_writer;
