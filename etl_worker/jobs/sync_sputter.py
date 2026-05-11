"""ETL orchestrator — scan_files → parser → etl_runs 기록.

처리 순서 (5분 tick마다):
1. etl_runs INSERT (job_name='sync_sputter', status='running')
2. scan_all() → SourceFileRecord list (sha256, mtime, race-safe)
3. source_type별 파서 호출
4. etl_runs UPDATE (status, files/rows 통계, parser_results를 metadata JSONB에)

전략: ETL은 xlsx → DB 단순 복사. 해석/매칭은 agent (MCP).

source_type 매핑:
- sputter_human_xlsx → parse_sputter_human → sputter_runs_human
- sputter_auto_xlsx  → parse_sputter_auto  → sputter_runs_auto_main + sputter_runs_auto_plasma
- ald_ncd_xlsx       → parse_ald_ncd       → ald_ncd_runs
- ald_rayvac_xlsx    → parse_ald_rayvac    → ald_rayvac_runs
- rga_csv            → parse_rga_csv       → rga_runs

별도 트리 traversal (source_files 안 거침):
- VO2 측정 .dat 트리 → parse_measurements_tree → measurements
"""

import json
import logging
import traceback

from sqlalchemy import text

from shared.db import session_scope_writer
from etl_worker.jobs.scan_files import scan_all
from etl_worker.jobs.parsers.ald_ncd import parse_ald_ncd
from etl_worker.jobs.parsers.ald_rayvac import parse_ald_rayvac
from etl_worker.jobs.parsers.sputter_human import parse_sputter_human
from etl_worker.jobs.parsers.sputter_auto import parse_sputter_auto
from etl_worker.jobs.parsers.measurement_dat import parse_measurements_tree
from etl_worker.jobs.parsers.rga_csv import parse_rga_csv

log = logging.getLogger("etl.sync_sputter")

_INSERT_ETL_RUN_SQL = text("""
    INSERT INTO vo2.etl_runs (job_name, status, started_at)
    VALUES (:job_name, 'running', NOW())
    RETURNING id
""")

_UPDATE_ETL_RUN_SQL = text("""
    UPDATE vo2.etl_runs
    SET finished_at = NOW(),
        status = :status,
        files_seen = :files_seen,
        files_processed = :files_processed,
        rows_inserted = :rows_inserted,
        rows_updated = :rows_updated,
        error = :error,
        metadata = CAST(:metadata AS JSONB)
    WHERE id = :id
""")


def sync_all() -> dict:
    """5분 tick의 메인 함수. scan + parse + 통계 기록."""
    log.info("=== sync_sputter tick start ===")

    with session_scope_writer() as s:
        run_id = s.execute(_INSERT_ETL_RUN_SQL, {
            "job_name": "sync_sputter",
        }).scalar_one()

    log.info(f"etl_run started, id={run_id}")

    files_seen = 0
    files_processed = 0
    rows_inserted = 0
    error_msg = None
    parser_results = []
    status = "ok"

    try:
        records = scan_all()
        files_seen = len(records)
        log.info(f"scan_all returned {files_seen} records")

        # source_type별 분기
        sputter_human_records = [r for r in records if r.source_type == "sputter_human_xlsx"]
        sputter_auto_records  = [r for r in records if r.source_type == "sputter_auto_xlsx"]
        ald_ncd_records       = [r for r in records if r.source_type == "ald_ncd_xlsx"]
        ald_rayvac_records    = [r for r in records if r.source_type == "ald_rayvac_xlsx"]
        rga_csv_records       = [r for r in records if r.source_type == "rga_csv"]

        # 사람 sputter log (Phase 4 Step 15)
        for rec in sputter_human_records:
            result = parse_sputter_human(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += result.get("inserted", 0)

        # 자동 CH1 (Phase 4 Step 16) — 두 시트
        for rec in sputter_auto_records:
            result = parse_sputter_auto(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += (
                    result.get("main_inserted", 0)
                    + result.get("plasma_inserted", 0)
                )

        # ALD NCD (Phase 4 Step 13)
        for rec in ald_ncd_records:
            result = parse_ald_ncd(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += (
                    result.get("ttip_inserted", 0)
                    + result.get("tdmat_inserted", 0)
                )

        # ALD Rayvac (Phase 4 Step 14)
        for rec in ald_rayvac_records:
            result = parse_ald_rayvac(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += result.get("inserted", 0)

        # RGA CSV (Phase 4 Step 18)
        for rec in rga_csv_records:
            result = parse_rga_csv(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += result.get("inserted", 0)

        # 측정 .dat 트리 traversal (Phase 4 Step 17 + Step 17 fix)
        # source_files 인덱싱 안 거치고 자체 traversal — file_path UNIQUE로 멱등성
        # files_with_error도 카운트 (격리 INSERT 성공한 row — DB에 존재함)
        meas_result = parse_measurements_tree()
        parser_results.append({"file": "measurements_tree", **meas_result})
        if meas_result["status"] == "ok":
            meas_total = (
                meas_result.get("files_inserted", 0)
                + meas_result.get("files_with_error", 0)
            )
            files_processed += meas_total
            rows_inserted += meas_total

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log.error(f"sync_all failed: {e}", exc_info=True)
        status = "error"

    with session_scope_writer() as s:
        s.execute(_UPDATE_ETL_RUN_SQL, {
            "id": run_id,
            "status": status,
            "files_seen": files_seen,
            "files_processed": files_processed,
            "rows_inserted": rows_inserted,
            "rows_updated": 0,
            "error": error_msg[:1000] if error_msg else None,
            "metadata": json.dumps({"parsers": parser_results}, ensure_ascii=False),
        })

    log.info(
        f"=== sync_sputter tick done: status={status}, "
        f"files {files_processed}/{files_seen}, +{rows_inserted} rows ==="
    )
    return {
        "status": status,
        "files_seen": files_seen,
        "files_processed": files_processed,
        "rows_inserted": rows_inserted,
    }
