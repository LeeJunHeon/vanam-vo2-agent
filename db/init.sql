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
