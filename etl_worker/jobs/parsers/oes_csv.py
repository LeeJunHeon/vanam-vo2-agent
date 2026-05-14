"""OES csv 파서 — Phase 4 Step 25.

VANAM sputter chamber 분광기 csv → vo2.oes_runs.

파일 구조 (검증됨):
- 1025 컬럼: Time (HH:MM:SS) + 1014 valid wavelength + 10 dark pixel (col name '0.0' ~ '0.0.9')
- 약 1.5초/sample, 한 run 당 ~1000 timestep
- 한 csv = 한 sputter run

파이프라인 (9 단계):
1. csv 읽기, 파일명에서 started_at 추출
2. sputter_runs_auto_main 매칭 (±30min 윈도우, 가장 가까운 row)
   - 매칭 실패 → skip (다음 tick 재시도)
3. Plasma ON 필터 (total intensity midpoint)
4. Baseline 제거 (channel 별 percentile-5)
5. Peak 검출 (prominence + distance, 시간 평균 spectrum)
6. Integration band (peak ± 2 channel 합)
7. MinMax scaling per peak
8. PCA (n_components=3, svd_full, whiten=False) + statistics + T²/SPE
9. vo2.oes_runs INSERT

가드 (rga_csv.py 와 동일):
- race_unsafe → skip
- all_processed → skip (sha 같은 파일 재처리 안 함)

신규 source_type: 'oes_csv'. SourceFileRecord 받음.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Any

import numpy as np
import pandas as pd
from scipy.signal import find_peaks
from sklearn.decomposition import PCA
from sklearn.preprocessing import MinMaxScaler
from sqlalchemy import text

from shared.db import session_scope_writer
from etl_worker.jobs.scan_files import SourceFileRecord

log = logging.getLogger("etl.parsers.oes_csv")

# ───── 파이프라인 상수 ─────
PIPELINE_VERSION = "v1.0"

PLASMA_ON_METHOD = "total_intensity_midpoint"
BASELINE_METHOD = "percentile_5_per_channel"
PEAK_PROMINENCE = 10.0
PEAK_DISTANCE = 2
INTEGRATION_BAND = 2

SCALING_METHOD = "minmax_per_peak"

PCA_N_COMPONENTS = 3
PCA_SVD_SOLVER = "full"
PCA_WHITEN = False
PCA_VALID_THRESHOLD = 0.95

MATCH_WINDOW_MINUTES = 30
MATCH_METHOD = "nearest_within_window_30min"

# 파일명 패턴: OES_Data_YYYYMMDD_HHMMSS.csv
_FILENAME_RE = re.compile(r'^OES_Data_(\d{8})_(\d{6})\.csv$')

# ───── INSERT SQL ─────
_INSERT_SQL = text("""
INSERT INTO vo2.oes_runs (
    source_file_id, file_name, file_path, sha256,
    started_at, ended_at, sample_interval_sec, duration_sec,
    sputter_auto_main_id, match_delta_sec, match_method,
    n_timesteps_total, n_timesteps_on,
    n_wavelengths_total, n_wavelengths_valid,
    intensity_min, intensity_max, intensity_median,
    plasma_on_threshold, plasma_on_method,
    baseline_method, baseline_min, baseline_max, baseline_median,
    peak_prominence, peak_distance, n_peaks_detected,
    peak_wavelengths, peak_intensities, integration_band,
    scaling_method,
    pca_n_components, pca_svd_solver, pca_whiten,
    pca_explained_variance_ratio, pca_cum_variance, pca_valid,
    pc1_mean, pc1_std, pc1_min, pc1_max,
    pc2_mean, pc2_std, pc2_min, pc2_max,
    pc3_mean, pc3_std, pc3_min, pc3_max,
    t2_peak, t2_median, spe_peak, spe_median,
    pipeline_version, raw_json, parse_status, parse_error
) VALUES (
    :source_file_id, :file_name, :file_path, :sha256,
    :started_at, :ended_at, :sample_interval_sec, :duration_sec,
    :sputter_auto_main_id, :match_delta_sec, :match_method,
    :n_timesteps_total, :n_timesteps_on,
    :n_wavelengths_total, :n_wavelengths_valid,
    :intensity_min, :intensity_max, :intensity_median,
    :plasma_on_threshold, :plasma_on_method,
    :baseline_method, :baseline_min, :baseline_max, :baseline_median,
    :peak_prominence, :peak_distance, :n_peaks_detected,
    CAST(:peak_wavelengths AS REAL[]),
    CAST(:peak_intensities AS REAL[]),
    :integration_band,
    :scaling_method,
    :pca_n_components, :pca_svd_solver, :pca_whiten,
    CAST(:pca_explained_variance_ratio AS REAL[]),
    :pca_cum_variance, :pca_valid,
    :pc1_mean, :pc1_std, :pc1_min, :pc1_max,
    :pc2_mean, :pc2_std, :pc2_min, :pc2_max,
    :pc3_mean, :pc3_std, :pc3_min, :pc3_max,
    :t2_peak, :t2_median, :spe_peak, :spe_median,
    :pipeline_version, CAST(:raw_json AS JSONB), :parse_status, :parse_error
)
ON CONFLICT (file_path, sha256) DO NOTHING
""")

_UPDATE_SOURCE_METADATA_SQL = text("""
UPDATE vo2.source_files
SET metadata = CAST(:metadata AS JSONB),
    row_count = :row_count,
    parser_status = :parser_status,
    parser_error = :parser_error,
    last_indexed_at = NOW()
WHERE id = :id
""")


def _parse_filename_datetime(file_name: str) -> Optional[datetime]:
    """파일명 'OES_Data_YYYYMMDD_HHMMSS.csv' → datetime."""
    m = _FILENAME_RE.match(file_name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _find_sputter_auto_match(started_at: datetime) -> tuple[Optional[int], Optional[float]]:
    """sputter_runs_auto_main 에서 매칭되는 row 찾기.

    조건: process_datetime < started_at AND 차이 ≤ MATCH_WINDOW_MINUTES
    가장 가까운 row 선택.

    Returns:
        (sputter_auto_main_id, delta_seconds) or (None, None)
    """
    sql = text("""
        SELECT id, EXTRACT(EPOCH FROM (:started_at::timestamp - process_datetime)) AS delta_sec
        FROM vo2.sputter_runs_auto_main
        WHERE process_datetime < :started_at::timestamp
          AND process_datetime >= :started_at::timestamp - (:window_seconds * INTERVAL '1 second')
        ORDER BY ABS(EXTRACT(EPOCH FROM (:started_at::timestamp - process_datetime)))
        LIMIT 1
    """)
    with session_scope_writer() as s:
        result = s.execute(sql, {
            "started_at": started_at,
            "window_seconds": MATCH_WINDOW_MINUTES * 60,
        }).fetchone()

    if result is None:
        return None, None
    return int(result[0]), float(result[1])


def _update_source_metadata(
    record_id: int, metadata: dict, row_count: int,
    parser_status: str, parser_error: Optional[str],
) -> None:
    """source_files 의 metadata/parser_status 갱신."""
    with session_scope_writer() as s:
        s.execute(_UPDATE_SOURCE_METADATA_SQL, {
            "id": record_id,
            "metadata": json.dumps(metadata, ensure_ascii=False),
            "row_count": row_count,
            "parser_status": parser_status,
            "parser_error": parser_error[:1000] if parser_error else None,
        })


def parse_oes_csv(record: SourceFileRecord) -> dict:
    """OES csv 한 파일 처리. rga_csv 와 같은 인터페이스.

    Returns:
        {"status": "ok"|"error"|"skipped", "inserted": N, "reason": ...}
    """
    # 가드 1: race_unsafe
    if record.is_race_unsafe:
        log.info(f"skip {record.file_name} (race_unsafe)")
        return {"status": "skipped", "reason": "race_unsafe", "inserted": 0}

    # 가드 2: already processed (sha 같으면 skip)
    if record.metadata and record.metadata.get("all_processed"):
        log.info(f"skip {record.file_name} (sha already processed)")
        return {"status": "skipped", "reason": "already_processed", "inserted": 0}

    # Step 1: 파일명에서 started_at
    started_at = _parse_filename_datetime(record.file_name)
    if started_at is None:
        msg = f"filename pattern mismatch: {record.file_name}"
        log.warning(msg)
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": "filename_pattern_mismatch", "inserted": 0}

    # Step 2: sputter_auto 매칭
    sputter_id, delta_sec = _find_sputter_auto_match(started_at)
    if sputter_id is None:
        # skip — 다음 tick 재시도. source_files metadata 는 갱신하지 않음 (sha 같으면 또 시도되도록)
        log.info(
            f"OES {record.file_name}: sputter_auto match 없음 "
            f"(start={started_at}). 다음 tick 재시도."
        )
        return {"status": "skipped", "reason": "no_sputter_match", "inserted": 0}

    log.info(
        f"OES {record.file_name}: matched sputter_auto id={sputter_id} "
        f"(delta=+{delta_sec/60:.1f}min)"
    )

    # Step 3: csv 읽기
    try:
        df = pd.read_csv(record.file_path)
    except Exception as e:
        msg = f"csv_read: {type(e).__name__}: {e}"
        log.exception(f"OES csv read failed: {record.file_name}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    # Time + valid wavelengths (dark pixel '0.0', '0.0.1' ... 제외)
    wl_cols = [
        c for c in df.columns[1:]
        if c != '0.0' and not c.startswith('0.0.')
    ]
    n_wl_total = len(df.columns) - 1
    n_wl_valid = len(wl_cols)
    n_timesteps_total = len(df)

    # 기본 sanity check
    if n_wl_valid < 100:
        msg = f"too_few_valid_wavelengths: {n_wl_valid}"
        log.warning(f"OES {record.file_name}: {msg}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    if n_timesteps_total < 10:
        msg = f"too_few_timesteps: {n_timesteps_total}"
        log.warning(f"OES {record.file_name}: {msg}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    try:
        wl_values = np.array([float(c) for c in wl_cols])
    except ValueError as e:
        msg = f"wavelength_parse_failed: {e}"
        log.warning(f"OES {record.file_name}: {msg}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    X_full = df[wl_cols].values.astype(np.float64)

    # sample_interval 계산 (Time 컬럼 HH:MM:SS)
    sample_interval_sec = 1.5
    try:
        t_first_str = str(df.iloc[0, 0]).strip()
        t_last_str = str(df.iloc[-1, 0]).strip()
        t_first = datetime.strptime(t_first_str, "%H:%M:%S")
        t_last = datetime.strptime(t_last_str, "%H:%M:%S")
        delta = (t_last - t_first).total_seconds()
        if delta < 0:
            delta += 86400  # 자정 넘김
        sample_interval_sec = delta / max(1, n_timesteps_total - 1)
    except Exception:
        log.warning(f"OES {record.file_name}: sample_interval 추정 실패, 1.5s 사용")

    duration_sec = sample_interval_sec * (n_timesteps_total - 1)
    ended_at = started_at + timedelta(seconds=duration_sec)

    intensity_min = float(X_full.min())
    intensity_max = float(X_full.max())
    intensity_median = float(np.median(X_full))

    # Step 3-2: Plasma ON 필터
    total = X_full.sum(axis=1)
    plasma_on_threshold = float(total.min() + (total.max() - total.min()) * 0.5)
    on_mask = total > plasma_on_threshold
    X_on = X_full[on_mask]
    n_timesteps_on = int(on_mask.sum())

    if n_timesteps_on < 10:
        msg = f"too_few_plasma_on_samples: {n_timesteps_on}"
        log.warning(f"OES {record.file_name}: {msg}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    # Step 4: Baseline 제거
    baseline = np.percentile(X_on, 5, axis=0)
    X_bc = X_on - baseline

    # Step 5: Peak 검출
    mean_spec = X_bc.mean(axis=0)
    peaks, _ = find_peaks(
        mean_spec,
        prominence=PEAK_PROMINENCE,
        distance=PEAK_DISTANCE,
    )
    n_peaks = len(peaks)

    if n_peaks < PCA_N_COMPONENTS:
        msg = f"too_few_peaks: {n_peaks} < {PCA_N_COMPONENTS}"
        log.warning(f"OES {record.file_name}: {msg}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    peak_wl_list = [float(x) for x in wl_values[peaks]]
    peak_int_list = [float(x) for x in mean_spec[peaks]]

    # Step 6: Integration band
    peak_features = np.zeros((X_bc.shape[0], n_peaks))
    for i, p in enumerate(peaks):
        lo = max(0, p - INTEGRATION_BAND)
        hi = min(X_bc.shape[1], p + INTEGRATION_BAND + 1)
        peak_features[:, i] = X_bc[:, lo:hi].sum(axis=1)

    # Step 7: MinMax scaling per peak
    scaler = MinMaxScaler()
    peak_scaled = scaler.fit_transform(peak_features)

    # Step 8: PCA + T² + SPE
    try:
        pca = PCA(
            n_components=PCA_N_COMPONENTS,
            svd_solver=PCA_SVD_SOLVER,
            whiten=PCA_WHITEN,
        )
        scores = pca.fit_transform(peak_scaled)
    except Exception as e:
        msg = f"pca_failed: {type(e).__name__}: {e}"
        log.exception(f"OES {record.file_name}: PCA failed")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    evr = [float(x) for x in pca.explained_variance_ratio_]
    cum_variance = float(sum(evr))
    pca_valid = cum_variance >= PCA_VALID_THRESHOLD

    # per-component statistics
    pc_stats = {}
    for i in range(PCA_N_COMPONENTS):
        s = scores[:, i]
        pc_stats[f"pc{i+1}_mean"] = float(s.mean())
        pc_stats[f"pc{i+1}_std"] = float(s.std())
        pc_stats[f"pc{i+1}_min"] = float(s.min())
        pc_stats[f"pc{i+1}_max"] = float(s.max())

    # Hotelling T² = sum( score_i^2 / variance_i )
    explained_var = pca.explained_variance_
    # 0 으로 나눔 방지
    explained_var_safe = np.where(explained_var > 1e-12, explained_var, 1e-12)
    t2 = (scores ** 2 / explained_var_safe).sum(axis=1)
    # SPE = ||X - X_reconstructed||²
    X_recon = pca.inverse_transform(scores)
    spe = ((peak_scaled - X_recon) ** 2).sum(axis=1)

    t2_peak = float(t2.max())
    t2_median = float(np.median(t2))
    spe_peak = float(spe.max())
    spe_median = float(np.median(spe))

    # raw_json: 디버깅용 부가 정보
    raw_json = {
        "peak_indices_in_wl_array": [int(p) for p in peaks],
        "pca_components_shape": list(pca.components_.shape),
        "started_at_raw": str(df.iloc[0, 0]) if n_timesteps_total > 0 else None,
        "ended_at_raw": str(df.iloc[-1, 0]) if n_timesteps_total > 0 else None,
    }

    # Step 9: INSERT
    payload = {
        "source_file_id": record.id,
        "file_name": record.file_name,
        "file_path": str(record.file_path),
        "sha256": record.sha256,
        "started_at": started_at,
        "ended_at": ended_at,
        "sample_interval_sec": sample_interval_sec,
        "duration_sec": duration_sec,
        "sputter_auto_main_id": sputter_id,
        "match_delta_sec": delta_sec,
        "match_method": MATCH_METHOD,
        "n_timesteps_total": n_timesteps_total,
        "n_timesteps_on": n_timesteps_on,
        "n_wavelengths_total": n_wl_total,
        "n_wavelengths_valid": n_wl_valid,
        "intensity_min": intensity_min,
        "intensity_max": intensity_max,
        "intensity_median": intensity_median,
        "plasma_on_threshold": plasma_on_threshold,
        "plasma_on_method": PLASMA_ON_METHOD,
        "baseline_method": BASELINE_METHOD,
        "baseline_min": float(baseline.min()),
        "baseline_max": float(baseline.max()),
        "baseline_median": float(np.median(baseline)),
        "peak_prominence": PEAK_PROMINENCE,
        "peak_distance": PEAK_DISTANCE,
        "n_peaks_detected": n_peaks,
        "peak_wavelengths": peak_wl_list,
        "peak_intensities": peak_int_list,
        "integration_band": INTEGRATION_BAND,
        "scaling_method": SCALING_METHOD,
        "pca_n_components": PCA_N_COMPONENTS,
        "pca_svd_solver": PCA_SVD_SOLVER,
        "pca_whiten": PCA_WHITEN,
        "pca_explained_variance_ratio": evr,
        "pca_cum_variance": cum_variance,
        "pca_valid": pca_valid,
        **pc_stats,
        "t2_peak": t2_peak,
        "t2_median": t2_median,
        "spe_peak": spe_peak,
        "spe_median": spe_median,
        "pipeline_version": PIPELINE_VERSION,
        "raw_json": json.dumps(raw_json, default=str, ensure_ascii=False),
        "parse_status": "ok",
        "parse_error": None,
    }

    inserted = 0
    try:
        with session_scope_writer() as s:
            result = s.execute(_INSERT_SQL, payload)
            inserted = result.rowcount or 0
    except Exception as e:
        msg = f"insert_failed: {type(e).__name__}: {e}"
        log.exception(f"OES INSERT failed for {record.file_name}")
        _update_source_metadata(record.id, {}, 0, "error", msg)
        return {"status": "error", "reason": msg, "inserted": 0}

    # source_files metadata 갱신 (all_processed=True → 다음 tick skip)
    new_metadata = {
        "all_processed": True,
        "inserted": inserted,
        "n_peaks": n_peaks,
        "pca_cum_variance": cum_variance,
        "pca_valid": pca_valid,
        "sputter_auto_main_id": sputter_id,
        "pipeline_version": PIPELINE_VERSION,
    }
    _update_source_metadata(record.id, new_metadata, inserted, "ok", None)

    log.info(
        f"OES {record.file_name}: +{inserted} row "
        f"(n_peaks={n_peaks}, cum_var={cum_variance:.4f}, valid={pca_valid}, "
        f"t2_peak={t2_peak:.1f}, spe_peak={spe_peak:.4f}, "
        f"sputter_auto_id={sputter_id})"
    )

    return {
        "status": "ok",
        "inserted": inserted,
        "n_peaks": n_peaks,
        "pca_cum_variance": cum_variance,
        "pca_valid": pca_valid,
        "sputter_auto_main_id": sputter_id,
    }
