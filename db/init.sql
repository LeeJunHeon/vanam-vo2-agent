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
-- Phase 4 Step 11: ALD/Sample/측정 데이터 모델 도입
-- ──────────────────────────────────────────────────────────────────────────
-- 변경 요약:
--   1. vo2.ald_runs           — ALD 공정 (TIO2 레시피 xlsx 1 row = 1 batch)
--   2. vo2.samples            — wafer 조각 (ald_runs 또는 기판 reference)
--   3. vo2.sputter_run_samples— sputter ↔ sample 다대다 매핑
--   4. vo2.match_pending      — 자동 매칭 실패 격리 (parse_errors와 동일 철학)
--   5. vo2.sputter_runs       — 컬럼 2개 추가 (sample_label_raw, sample_label_norm)
--
-- 데이터 흐름:
--   ALD xlsx       → ald_runs
--   사람 sputter log → sputter_runs (Sub. 컬럼은 sample_label_raw에)
--                  → samples (Sub. 파싱 결과)
--                  → sputter_run_samples (다대다)
--   측정 .dat       → samples (있으면 매칭, 없으면 match_pending)

-- ─────── 1. ald_runs ─────────────────────────────────────────────────────
-- ALD xlsx의 "레시피 및 결과(TTIP)" / "(TDMAT)" 시트의 한 row = 한 batch.
-- batch_no는 사람이 운영 중 부여한 일련번호 (18부터 시작, 시트 내 unique).
-- 같은 batch_no가 TTIP/TDMAT 두 시트에 모두 등장하면 source 컬럼으로 구분.
CREATE TABLE IF NOT EXISTS vo2.ald_runs (
    ald_run_id          BIGSERIAL PRIMARY KEY,
    batch_no            INTEGER NOT NULL,
    process_date        DATE NOT NULL,
    source              TEXT NOT NULL,            -- 'TTIP' or 'TDMAT'
    recipe_name         TEXT,
    temp_c              REAL,                     -- Temp (°C)
    cycles              INTEGER,                  -- Cycle (#)
    ttip_pulse_s        REAL,                     -- TTIP/TDMAT Pulse time
    ttip_purge_s        REAL,
    h2o_pulse_s         REAL,
    h2o_purge_s         REAL,
    gpc_a_per_cycle     REAL,                     -- GPC (A/cycle)
    avg_max_min_pct     REAL,                     -- AVG Max-min(%)
    cum_cycles          INTEGER,                  -- 챔버 클리닝 이후 누적
    source_file_id      BIGINT REFERENCES vo2.source_files(id) ON DELETE SET NULL,
    row_number          INTEGER,
    raw_json            JSONB NOT NULL,
    parse_status        TEXT,                     -- NULL=clean, 'partial' 등
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ald_runs_batch UNIQUE (source, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_ald_runs_date ON vo2.ald_runs (process_date DESC);
CREATE INDEX IF NOT EXISTS idx_ald_runs_batch ON vo2.ald_runs (batch_no);

-- ─────── 2. samples ──────────────────────────────────────────────────────
-- wafer 조각 한 개 = 한 sample.
-- ALD에서 나온 sample (ald_run_id NOT NULL) 또는 기판 reference (NULL).
-- sample_label_norm은 정규화 식별자 (예: 'ALD-63', 'ALD-63-S2', 'SI-bare', 'MPLANE').
-- raw에는 사용자 원본 ('Ncd#(63)*2' 등) 보존.
CREATE TABLE IF NOT EXISTS vo2.samples (
    sample_id           BIGSERIAL PRIMARY KEY,
    sample_label_raw    TEXT NOT NULL,            -- 'Ncd#(63)*2' 원본
    sample_label_norm   TEXT NOT NULL,            -- 'ALD-63' 등 정규화
    substrate_kind      TEXT NOT NULL,            -- 'ald_ncd' | 'si' | 'm_plane' | 'external' | 'unknown'
    ald_run_id          BIGINT REFERENCES vo2.ald_runs(ald_run_id) ON DELETE SET NULL,
    sub_sample_n        INTEGER,                  -- wafer 쪼갠 조각 번호 (있으면)
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes               TEXT,
    CONSTRAINT uq_samples_norm UNIQUE (sample_label_norm)
);
CREATE INDEX IF NOT EXISTS idx_samples_ald ON vo2.samples (ald_run_id);
CREATE INDEX IF NOT EXISTS idx_samples_substrate ON vo2.samples (substrate_kind);

-- ─────── 3. sputter_run_samples (다대다 매핑) ─────────────────────────────
-- 한 sputter run에 여러 sample이 동시에 들어갈 수 있음 ("M-plane, #37,#50").
-- sputter_runs와 samples 사이 매핑 테이블.
CREATE TABLE IF NOT EXISTS vo2.sputter_run_samples (
    sputter_run_id      TEXT NOT NULL REFERENCES vo2.sputter_runs(sputter_run_id) ON DELETE CASCADE,
    sample_id           BIGINT NOT NULL REFERENCES vo2.samples(sample_id) ON DELETE CASCADE,
    position            TEXT,                     -- 'main', 'co-loaded', etc (선택)
    PRIMARY KEY (sputter_run_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_srs_sample ON vo2.sputter_run_samples (sample_id);

-- ─────── 4. match_pending (격리) ──────────────────────────────────────────
-- 자동 매칭 실패한 row 격리. 운영자가 DBeaver에서 수동 해결.
-- source_kind: 'sputter_log_sub' (사람 log Sub. 파싱 실패),
--              'measurement_path' (측정 파일 경로 매칭 실패),
--              'ald_xref' (사람 log Sub.가 ald_runs에 없는 batch_no 참조),
--              'auto_xlsx_match' (CH1.xlsx 보강 시 사람 log row 매칭 실패)
CREATE TABLE IF NOT EXISTS vo2.match_pending (
    id                  BIGSERIAL PRIMARY KEY,
    source_kind         TEXT NOT NULL,
    source_pk           TEXT NOT NULL,            -- 원본 row 식별자 (자유 텍스트)
    candidates          JSONB,                    -- 매칭 후보 [{sample_id: 1, score: 0.7}, ...]
    reason              TEXT NOT NULL,
    raw_data            JSONB,                    -- 디버깅용 원본 row
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved            BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,
    resolved_to         TEXT,                     -- 운영자가 지정한 sample_label_norm 등
    resolved_note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_pending_unresolved
    ON vo2.match_pending (source_kind, detected_at DESC) WHERE NOT resolved;

-- ─────── 5. sputter_runs 컬럼 추가 ────────────────────────────────────────
-- sample_label_raw : 사람 log Sub. 컬럼 원본 (예: 'Ncd#(63)*2')
-- sample_label_norm: 정규화 식별자 (예: 'ALD-63'). NULL이면 매칭 안 된 row.
-- 두 컬럼 모두 NULL 허용 — 기존 1034 row는 NULL로 시작, 추후 backfill.
ALTER TABLE vo2.sputter_runs ADD COLUMN IF NOT EXISTS sample_label_raw  TEXT;
ALTER TABLE vo2.sputter_runs ADD COLUMN IF NOT EXISTS sample_label_norm TEXT;
CREATE INDEX IF NOT EXISTS idx_sputter_runs_sample_norm
    ON vo2.sputter_runs (sample_label_norm) WHERE sample_label_norm IS NOT NULL;

-- ──────────────────────────────────────────────────────────────────────────
-- vo2_reader / vo2_writer 권한 부여 (새 테이블)
-- ──────────────────────────────────────────────────────────────────────────
GRANT SELECT ON vo2.ald_runs, vo2.samples, vo2.sputter_run_samples, vo2.match_pending TO vo2_reader;
GRANT SELECT, INSERT, UPDATE ON vo2.ald_runs, vo2.samples, vo2.sputter_run_samples, vo2.match_pending TO vo2_writer;
GRANT USAGE, SELECT ON SEQUENCE vo2.ald_runs_ald_run_id_seq TO vo2_writer;
GRANT USAGE, SELECT ON SEQUENCE vo2.samples_sample_id_seq TO vo2_writer;
GRANT USAGE, SELECT ON SEQUENCE vo2.match_pending_id_seq TO vo2_writer;
