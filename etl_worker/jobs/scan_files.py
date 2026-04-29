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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from shared.config import get_settings
from shared.db import session_scope_writer

log = logging.getLogger("etl.scan_files")

# Phase 1b 한정 — CH1 두 파일만. Phase 2부터 OES, ALD recipe, anneal, measurement 추가.
SOURCE_FILES: list[dict] = [
    {
        "source_type": "sputter_csv",
        "chamber":     "CH1",
        "file_path":   "/data/Sputter/Calib/Database/Ch1_log.csv",
    },
    {
        "source_type": "sputter_xlsx",
        "chamber":     "CH1",
        "file_path":   "/data/Process_log/CH1.xlsx",
    },
]

_CHUNK = 64 * 1024


@dataclass
class SourceFileRecord:
    """한 source 파일의 인덱싱 결과 — parser들이 받아서 처리 여부 결정."""
    id: int
    source_type: str
    chamber: str
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


def _scan_one(spec: dict, grace_seconds: int) -> Optional[SourceFileRecord]:
    """한 source 파일을 인덱싱. 파일이 없으면 None."""
    p = Path(spec["file_path"])
    if not p.exists():
        log.warning(f"file not found: {p}")
        return None

    stat = p.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    size = stat.st_size
    sha = _compute_sha256(p)
    race_unsafe = _is_race_unsafe(mtime, grace_seconds)

    with session_scope_writer() as s:
        row = s.execute(_UPSERT_SQL, {
            "source_type": spec["source_type"],
            "equipment":   spec["chamber"].lower(),
            "chamber":     spec["chamber"],
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
        chamber=spec["chamber"],
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
    return records
