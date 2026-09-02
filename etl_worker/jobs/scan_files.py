"""source_files 인덱싱 — 5분 tick의 첫 단계.

각 source 파일의 sha256/mtime/size를 계산해 vo2.source_files 에 UPSERT.

Race-safe: mtime > now - GRACE_SECONDS 인 파일은 외부 PC가 append 중일
가능성 → SourceFileRecord.is_race_unsafe = True 로 표시되고 parser가 skip.

멱등성: (file_path, sha256) UNIQUE — 같은 파일 같은 내용 재스캔 시
INSERT 거부, last_seen_at 만 갱신. 파일이 변경(append)되면 sha256이 달라
새 row 생성 ← parser는 이전 처리 위치(row_count) 이후만 처리.
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from shared.config import get_settings
from shared.db import session_scope_writer

log = logging.getLogger("etl.scan_files")

# Phase 4 Step 12 — 데이터 source 6종.
# 신규 source_type 4종은 아직 파서 없음 → sync_sputter.py에서 무시.
# Phase 4 Step 13~18에서 각 파서 추가.
#
# 마운트 매핑 (docker-compose volumes):
#   /data       ← /volume1/VanaM_Sputter
#   /data_ald   ← /volume1/VanaM_ALD
#   /data_vo2   ← /volume1/VanaM_Measurement/VO2
#
# 측정 .dat (5번)은 폴더 트리 traversal이 필요해 별도 파서 (measurement_dat).
# SOURCE_FILES는 단일 파일 인덱싱만 다루므로 측정은 여기서 제외, Step 17에서 처리.
SOURCE_FILES: list[dict] = [
    # ───── ALD (Phase 4 Step 13) ─────────────────────────────────────
    {
        "source_type": "ald_ncd_xlsx",
        "chamber": None,                          # ALD는 chamber 무관
        "file_path": "/data_ald/측정 Data/NCD/TiO2/TIO2 레시피, 데이터 정리_베이지안_측정용.xlsx",
    },
    {
        "source_type": "ald_rayvac_xlsx",
        "chamber": None,
        "file_path": "/data_ald/측정 Data/Rayvac/TiO2/tio2두께 정리.xlsx",
    },
    # ───── Sputter (Phase 4 Step 15-16) ──────────────────────────────
    {
        "source_type": "sputter_human_xlsx",      # 사람 입력 — 마스터
        "chamber": "CH1",
        "file_path": "/data/Ch1 process log (1).xlsx",
    },
    {
        "source_type": "sputter_auto_xlsx",       # 자동 — 보강
        "chamber": "CH1",
        "file_path": "/data/Process_log/CH1.xlsx",
    },
    # ───── RGA (Phase 4 Step 18) ─────────────────────────────────────
    {
        "source_type": "rga_csv",
        "chamber": "CH1",
        "file_path": "/data/RGA/Ch.1/RGA_spectrums.csv",
    },
]

_CHUNK = 64 * 1024


@dataclass
class SourceFileRecord:
    """한 source 파일의 인덱싱 결과 — parser들이 받아서 처리 여부 결정."""
    id: int
    source_type: str
    chamber: Optional[str]  # ALD 등 chamber 무관 source는 None
    file_path: Path
    file_name: str
    sha256: str
    mtime: datetime
    size: int
    is_new: bool                  # 이번 tick에 새로 INSERT됐는지
    is_race_unsafe: bool          # mtime이 grace 안이면 True
    previous_row_count: int       # 이전 처리 위치 (parser는 이후만 처리)
    metadata: dict = field(default_factory=dict)


def _compute_sha256(p: Path) -> str:
    """파일의 sha256 hex digest. 큰 파일도 chunk read로 메모리 안전."""
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _is_race_unsafe(mtime: datetime, grace_seconds: int) -> bool:
    """파일 mtime이 현재 시각으로부터 grace 안이면 외부 append 가능성."""
    now = datetime.now(timezone.utc)
    age_sec = (now - mtime).total_seconds()
    return age_sec < grace_seconds


_UPSERT_SQL = text("""
    INSERT INTO vo2.source_files (
        source_type, equipment, chamber, file_path, file_name, file_ext,
        file_size, modified_at, sha256, last_seen_at, last_indexed_at,
        parser_status
    )
    VALUES (
        :source_type, :equipment, :chamber, :file_path, :file_name, :file_ext,
        :file_size, :mtime, :sha256, NOW(), NOW(),
        'pending'
    )
    ON CONFLICT (file_path, sha256) DO UPDATE SET
        last_seen_at = NOW(),
        modified_at  = EXCLUDED.modified_at,
        file_size    = EXCLUDED.file_size
    RETURNING id, row_count, metadata, (xmax = 0) AS inserted
""")


_FAST_PATH_LOOKUP_SQL = text("""
SELECT id, sha256, row_count, metadata, modified_at, file_size
FROM vo2.source_files
WHERE file_path = :file_path
ORDER BY id DESC
LIMIT 1
""")


def _scan_one(spec: dict, grace_seconds: int) -> Optional[SourceFileRecord]:
    """한 source 파일을 인덱싱. 파일이 없으면 None.

    Fast-path: DB 의 최신 row 와 mtime + size 가 같으면 sha 재계산 skip.
    """
    p = Path(spec["file_path"])
    if not p.exists():
        log.warning(f"file not found: {p}")
        return None

    stat = p.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    size = stat.st_size
    race_unsafe = _is_race_unsafe(mtime, grace_seconds)

    # Fast-path: DB 최신 row 와 mtime + size 같으면 sha 재계산 skip
    # mtime 은 OS 가 자동 갱신 (xlsx save / csv append 모두 mtime 변경됨)
    # mtime + size 둘 다 같으면 내용 동일 보장
    sha: Optional[str] = None
    try:
        with session_scope_writer() as s:
            existing = s.execute(_FAST_PATH_LOOKUP_SQL, {
                "file_path": str(p),
            }).fetchone()
        if existing is not None:
            db_mtime = existing[4]
            db_size = existing[5]
            # mtime 비교 (tzinfo 정규화)
            if db_mtime is not None and db_mtime.tzinfo is None:
                db_mtime = db_mtime.replace(tzinfo=timezone.utc)
            if db_mtime == mtime and db_size == size:
                sha = existing[1]  # DB 의 sha 재사용
                log.debug(f"fast-path: {p.name} (mtime+size unchanged)")
    except Exception as e:
        log.warning(f"fast-path lookup failed for {p.name}, falling back to sha: {type(e).__name__}: {e}")
        sha = None

    if sha is None:
        sha = _compute_sha256(p)  # 변경 감지된 경우만 sha 재계산

    # ALD 등 chamber 무관 source는 None 가능. 안전 처리.
    chamber = spec.get("chamber")
    if chamber:
        equipment = chamber.lower()
    else:
        # source_type prefix를 equipment로 (ald_ncd_xlsx → 'ald', rga_csv → 'rga')
        equipment = spec["source_type"].split("_")[0]

    with session_scope_writer() as s:
        row = s.execute(_UPSERT_SQL, {
            "source_type": spec["source_type"],
            "equipment":   equipment,
            "chamber":     chamber,
            "file_path":   str(p),
            "file_name":   p.name,
            "file_ext":    p.suffix.lstrip("."),
            "file_size":   size,
            "mtime":       mtime,
            "sha256":      sha,
        }).fetchone()

    if row is None:
        log.error(f"UPSERT returned no row for {p}")
        return None

    return SourceFileRecord(
        id=row[0],
        source_type=spec["source_type"],
        chamber=chamber,
        file_path=p,
        file_name=p.name,
        sha256=sha,
        mtime=mtime,
        size=size,
        is_new=bool(row[3]),
        is_race_unsafe=race_unsafe,
        previous_row_count=row[1] or 0,
        metadata=row[2] or {},
    )


# OES 파일명 정규식 — 2025-10-15 이후 표준 패턴만 매치
# (옛 파일 *.dat / 20251001_#3.csv / ana_*.xlsx / *_복사본.csv 는 자연 skip)
OES_FILENAME_RE = re.compile(r'^OES_Data_(\d{8})_(\d{6})\.csv$')

# 마운트 경로 (docker-compose: /volume1/VanaM_Sputter → /data:ro)
OES_DIR = "/data/OES/CH1"


def _scan_oes_tree(grace_seconds: int) -> list[SourceFileRecord]:
    """OES csv 트리 traversal — Phase 4 Step 25.

    /data/OES/CH1/ 의 OES_Data_YYYYMMDD_HHMMSS.csv 패턴 파일만 인덱싱.
    옛 파일 (자유 명명 .csv/.dat, ana_*.xlsx, 복사본 등) 은 regex 매칭에서 자연 제외.

    각 OES csv = source_files 한 row (sha256 기반).
    sputter run 당 새 파일 생성 패턴이라 SOURCE_FILES (단일 file_path) 방식 부적합.
    measurement_dat 의 트리 traversal 과 유사하지만 measurement 와 달리
    source_files 거침 (audit 추적 필요).

    Returns: SourceFileRecord 리스트 (parse 단계에서 OES 파서가 받음).
    """
    records: list[SourceFileRecord] = []
    base = Path(OES_DIR)

    if not base.exists():
        log.warning(f"OES_DIR not found: {base}")
        return records

    # glob 으로 모든 csv 나열 (subdir 없으니 평탄 traversal)
    csv_paths = sorted(base.glob("*.csv"))

    matched = 0
    skipped = 0
    for csv_path in csv_paths:
        m = OES_FILENAME_RE.match(csv_path.name)
        if not m:
            skipped += 1
            continue
        matched += 1

        # 기존 _scan_one() 의 spec dict 형식으로 변환해서 재사용
        spec = {
            "source_type": "oes_csv",
            "chamber": "CH1",
            "file_path": str(csv_path),
        }
        try:
            rec = _scan_one(spec, grace_seconds)
            if rec is not None:
                records.append(rec)
        except Exception as e:
            log.warning(f"OES scan failed for {csv_path.name}: {type(e).__name__}: {e}")

    log.info(
        f"OES tree scan: {matched} matched (OES_Data_*), "
        f"{skipped} skipped (non-standard names), "
        f"{len(records)} indexed"
    )
    return records


def scan_all() -> list[SourceFileRecord]:
    """SOURCE_FILES 의 모든 파일을 인덱싱.

    파일 없는 항목은 list에서 제외.
    race_unsafe 인 항목은 list에 포함되지만 parser에서 skip할 책임.
    """
    settings = get_settings()
    grace = settings.ETL_GRACE_SECONDS

    records: list[SourceFileRecord] = []
    for spec in SOURCE_FILES:
        rec = _scan_one(spec, grace)
        if rec is None:
            continue
        log.info(
            f"indexed {rec.source_type}/{rec.chamber}: {rec.file_name} "
            f"sha={rec.sha256[:8]} new={rec.is_new} "
            f"race_unsafe={rec.is_race_unsafe} prev_rows={rec.previous_row_count}"
        )
        records.append(rec)

    # Phase 4 Step 25: OES 트리 (sputter run 별 csv)
    oes_records = _scan_oes_tree(grace)
    for rec in oes_records:
        log.info(
            f"indexed {rec.source_type}/{rec.chamber}: {rec.file_name} "
            f"sha={rec.sha256[:8]} new={rec.is_new} "
            f"race_unsafe={rec.is_race_unsafe} prev_rows={rec.previous_row_count}"
        )
    records.extend(oes_records)

    return records
