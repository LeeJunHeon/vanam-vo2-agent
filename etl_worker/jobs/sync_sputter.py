"""ETL orchestrator — scan_files → parser → etl_runs 기록.

처리 순서 (매일 05:00 KST tick — Step 28-pre-1 변경):
1. etl_runs INSERT (job_name='sync_sputter', status='running')
2. scan_all() → SourceFileRecord list (sha256, mtime, race-safe)
3. source_type별 파서 호출
4. parse_errors housekeeping (옛 sha stale row auto-resolved)
5. etl_runs UPDATE (status, files/rows 통계, parser_results를 metadata JSONB에)

전략: ETL은 xlsx → DB 단순 복사. 해석/매칭은 agent (MCP).

source_type 매핑:
- sputter_human_xlsx → parse_sputter_human → sputter_runs_human
- sputter_auto_xlsx  → parse_sputter_auto  → sputter_runs_auto_main + sputter_runs_auto_plasma
- ald_ncd_xlsx       → parse_ald_ncd       → ald_ncd_runs
- ald_rayvac_xlsx    → parse_ald_rayvac    → ald_rayvac_runs
- rga_csv            → parse_rga_csv       → rga_runs
- oes_csv            → parse_oes_csv       → oes_runs (sputter_auto 후 처리, 매칭 의존)

별도 트리 traversal (source_files 안 거침):
- VO2 측정 .dat 트리 → parse_measurements_tree → measurements
- dataset.dat 트리   → parse_datasets_tree → measurement_summary
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
from etl_worker.jobs.parsers.dataset_dat import parse_datasets_tree
from etl_worker.jobs.parsers.rga_csv import parse_rga_csv
from etl_worker.jobs.parsers.oes_csv import parse_oes_csv

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


_REMATCH_OES_SQL = text("""
UPDATE vo2.oes_runs o
SET
    sputter_auto_main_id = matched.matched_id,
    match_delta_sec = matched.delta_sec,
    match_method = 'nearest_within_window_30min'
FROM (
    SELECT
        o2.id AS oes_id,
        s.id AS matched_id,
        EXTRACT(EPOCH FROM (o2.started_at - s.process_datetime)) AS delta_sec,
        ROW_NUMBER() OVER (
            PARTITION BY o2.id
            ORDER BY ABS(EXTRACT(EPOCH FROM (o2.started_at - s.process_datetime)))
        ) AS rn
    FROM vo2.oes_runs o2
    JOIN vo2.sputter_runs_auto_main s
        ON s.process_datetime < o2.started_at
        AND s.process_datetime >= o2.started_at - INTERVAL '30 minutes'
    WHERE o2.sputter_auto_main_id IS NULL
) matched
WHERE o.id = matched.oes_id
    AND matched.rn = 1
""")


# Step 27 동기화 철학을 parse_errors 까지 확장:
# 현재 latest sha 아닌 source_file 의 unresolved 격리 row 는 자동 resolved 마크.
# (Step 27 의 DELETE+INSERT 는 ald_*_runs / sputter_runs_* / rga_runs 만 동기화,
#  parse_errors 는 누적만 됨 → housekeeping 으로 일관성 확보)
_HOUSEKEEPING_PARSE_ERRORS_SQL = text("""
    UPDATE vo2.parse_errors
       SET resolved = true,
           resolved_at = NOW(),
           resolved_note = 'auto: superseded by newer sha (housekeeping)'
     WHERE source_file_id NOT IN (
        SELECT DISTINCT ON (source_type, file_path) id
          FROM vo2.source_files
         WHERE source_type IN ('ald_ncd_xlsx', 'ald_rayvac_xlsx')
         ORDER BY source_type, file_path, last_indexed_at DESC
     )
     AND NOT resolved
""")


def _rematch_oes_to_sputter_auto() -> int:
    """OES rows with NULL sputter_auto_main_id 를 재매칭.

    Step 27 (DELETE+INSERT 동기화) 부작용 hotfix.

    oes_runs.sputter_auto_main_id FK 가 ON DELETE SET NULL 이라
    Step 27 이 sputter_runs_auto_main 을 통째 DELETE 할 때마다
    oes_runs.sputter_auto_main_id 가 자동 NULL 처리된다.

    OES 파서는 sha skip 가드 때문에 NULL 복원 안 됨 → 이 함수가 매 tick 자동 복구.

    sputter_auto 처리 후, OES 처리 전에 호출.

    Returns:
        복원된 row 수
    """
    try:
        with session_scope_writer() as s:
            result = s.execute(_REMATCH_OES_SQL)
            n = result.rowcount or 0
        if n > 0:
            log.info(f"OES sputter_auto FK 재매칭: {n} row 복원")
        return n
    except Exception as e:
        log.warning(f"OES sputter_auto FK 재매칭 실패 (무시): {type(e).__name__}: {e}")
        return 0


def _housekeeping_parse_errors() -> int:
    """옛 sha 의 unresolved parse_errors 를 자동 resolved 마크.

    Step 27 의 DELETE+INSERT 동기화 철학이 ald_*_runs / sputter_runs_* / rga_runs
    에는 적용됐지만 parse_errors 는 누적만 되어 옛 sha 의 격리 row 가 잔재.

    매 tick 끝에 호출 — 현재 NAS 의 latest sha 아닌 source_file 에 매달린
    parse_errors 는 정의상 '옛 xlsx 의 흔적'이라 자동 resolved.

    운영자 SELECT WHERE NOT resolved 결과는 항상 '현재 xlsx 의 진짜 문제 row'
    만 보이게 됨.

    Returns:
        resolved 마크된 row 수
    """
    try:
        with session_scope_writer() as s:
            result = s.execute(_HOUSEKEEPING_PARSE_ERRORS_SQL)
            n = result.rowcount or 0
            if n > 0:
                log.info(f"parse_errors housekeeping: {n} stale row auto-resolved")
            return n
    except Exception as e:
        log.warning(
            f"parse_errors housekeeping 실패 (무시): {type(e).__name__}: {e}"
        )
        return 0


def sync_all() -> dict:
    """매일 05:00 KST tick 의 메인 함수. scan + parse + housekeeping + 통계 기록.

    Step 28-pre-1 이후: 하루 1회 실행 (이전엔 5분 cron).
    """
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

        # Step 27 부작용 hotfix — sputter_auto DELETE+INSERT 후 OES NULL FK 재매칭
        # 이유: oes_runs.sputter_auto_main_id FK 가 ON DELETE SET NULL 이라
        # sputter_auto 가 통째 DELETE 될 때마다 OES FK 가 자동 NULL 처리됨.
        # OES 파서는 sha skip 가드라 재매칭 안 함 → 매 tick 명시적 복구 필요.
        rematch_n = _rematch_oes_to_sputter_auto()
        parser_results.append({"file": "oes_rematch", "status": "ok", "restored": rematch_n})

        # OES CSV (Phase 4 Step 25) — sputter_auto 후 처리 (매칭 의존)
        oes_csv_records = [r for r in records if r.source_type == "oes_csv"]
        for rec in oes_csv_records:
            result = parse_oes_csv(rec)
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

        # Phase 4 Step 26 — dataset.dat 트리 (계산 결과 summary)
        # measurement_dat 와 같은 ROOT 별도 traversal — file_path UNIQUE 멱등성
        dataset_result = parse_datasets_tree()
        parser_results.append({"file": "datasets_tree", **dataset_result})
        if dataset_result["status"] == "ok":
            files_processed += dataset_result.get("files_inserted", 0)
            rows_inserted += dataset_result.get("samples_inserted", 0)
            rows_inserted += dataset_result.get("samples_with_error", 0)

        # Step 28-pre-3: parse_errors 자동 housekeeping (Step 27 동기화 철학 확장)
        # 현재 latest sha 가 아닌 source_file 에 매달린 unresolved row 를 auto-resolved.
        housekeeping_n = _housekeeping_parse_errors()
        parser_results.append({
            "file": "parse_errors_housekeeping",
            "status": "ok",
            "resolved": housekeeping_n,
        })

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
