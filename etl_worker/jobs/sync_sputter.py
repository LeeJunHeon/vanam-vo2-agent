"""ETL orchestrator — scan_files → parser → etl_runs 기록.

처리 순서 (5분 tick마다):
1. etl_runs INSERT (job_name='sync_sputter', status='running')
2. scan_all() → SourceFileRecord list (sha256, mtime, race-safe)
3. xlsx records 먼저 처리 (xlsx 매칭 lookup이 csv보다 먼저 들어가야 함)
4. csv records 나중 처리 (xlsx에 없는 row만 INSERT)
5. etl_runs UPDATE (status, files/rows 통계, parser_results를 metadata JSONB에)
"""
import json
import logging
import traceback

from sqlalchemy import text

from shared.db import session_scope_writer
from etl_worker.jobs.scan_files import scan_all
from etl_worker.jobs.parsers.sputter_xlsx import parse_xlsx
from etl_worker.jobs.parsers.sputter_csv import parse_csv
from etl_worker.jobs.parsers.ald_ncd import parse_ald_ncd
from etl_worker.jobs.parsers.ald_rayvac import parse_ald_rayvac

log = logging.getLogger("etl.sync_sputter")


_INSERT_ETL_RUN_SQL = text("""
    INSERT INTO vo2.etl_runs (job_name, status, started_at)
    VALUES (:job_name, 'running', NOW())
    RETURNING id
""")


_UPDATE_ETL_RUN_SQL = text("""
    UPDATE vo2.etl_runs
    SET finished_at     = NOW(),
        status          = :status,
        files_seen      = :files_seen,
        files_processed = :files_processed,
        rows_inserted   = :rows_inserted,
        rows_updated    = :rows_updated,
        error           = :error,
        metadata        = CAST(:metadata AS JSONB)
    WHERE id = :id
""")


def sync_all() -> dict:
    """5분 tick의 메인 함수. scan + parse + 통계 기록.

    Returns: {"status": "ok"|"error", "files_seen": N, "rows_inserted": M, ...}
    """
    log.info("=== sync_sputter tick start ===")

    # 1. etl_run 시작 기록
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

        # xlsx 먼저 (매칭 lookup 보장)
        xlsx_records = [r for r in records if r.source_type == "sputter_xlsx"]
        csv_records = [r for r in records if r.source_type == "sputter_csv"]
        ald_ncd_records = [r for r in records if r.source_type == "ald_ncd_xlsx"]
        ald_rayvac_records = [r for r in records if r.source_type == "ald_rayvac_xlsx"]

        for rec in xlsx_records:
            result = parse_xlsx(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += result.get("main_inserted", 0) + result.get("cleaning_inserted", 0)

        for rec in csv_records:
            result = parse_csv(rec)
            parser_results.append({"file": rec.file_name, **result})
            if result["status"] == "ok":
                files_processed += 1
                rows_inserted += result.get("inserted", 0)

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

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log.error(f"sync_all failed: {e}", exc_info=True)
        status = "error"

    # 2. etl_run 완료 기록
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
