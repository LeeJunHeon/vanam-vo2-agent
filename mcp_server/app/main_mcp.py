"""vo2-mcp-server MCP 프로토콜 어댑터.

Phase 4 Step 20: agentic SQL 3개 도구를 MCP tool로 등록.
FastMCP가 streamable HTTP transport ASGI app을 만들고, main.py가 /mcp/ 에 mount.

ChatGPT Custom Connector, Claude Desktop 등 외부 MCP 클라이언트가 이 endpoint 사용.

stateless_http=True: Mcp-Session-Id 추적 없음. curl 검증 단순화 + ChatGPT 호환.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import TransportSecuritySettings
from mcp.types import ToolAnnotations

from mcp_server.app.tools import describe_schema as _describe_tool
from mcp_server.app.tools import get_timeseries as _ts_tool
from mcp_server.app.tools import run_sql as _sql_tool
from shared.logging_config import setup_logging

log = setup_logging("mcp_server.main_mcp")


mcp = FastMCP(
    "vo2-mcp-server",
    stateless_http=True,
    streamable_http_path="/",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "vo2-mcp.vanam.synology.me",
            "vo2-mcp.vanam.synology.me:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "https://vo2-mcp.vanam.synology.me",
            "https://vo2-mcp.vanam.synology.me:*",
        ],
    ),
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Describe VO2 schema",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def describe_schema(table: str | None = None) -> dict[str, Any]:
    """VO2 공정 DB의 schema/도메인 지식/예시 row를 한 번에 반환.

    분석을 시작할 때 처음 호출하면 DB 전체 그림을 잡을 수 있다.
    vo2 스키마(공정 데이터) + equipment 스키마(장비 유지보수) 두 곳 모두 지원.

    Args:
        table: 조회할 테이블. bare name이면 스키마 자동 추론 (예: 'measurements', 'equipment_logs').
            'vo2.X' / 'equipment.X' 형태로 명시 가능.
            None이면 전체 18개 테이블 요약 + 도메인 overview + sample 매핑 가이드
            + 자주 쓰는 쿼리 예시.

    사용 가능 테이블:
        [vo2 스키마 — 공정 데이터]
        source_files, etl_runs, mcp_audit_logs, parse_errors,
        ald_ncd_runs, ald_rayvac_runs,
        sputter_runs_human, sputter_runs_auto_main, sputter_runs_auto_plasma,
        measurements, rga_runs

        [equipment 스키마 — 장비 유지보수 (read-only)]
        equipments, equipment_logs, equipment_log_entries,
        equipment_photos, equipment_entry_photos,
        cleaning_type_options, vent_reason_options

    Returns:
        전체 모드: {schemas, database, total_tables, domain_overview,
                    sample_mapping_guide, common_queries, tables, relationships}
        상세 모드: {table, purpose, domain_notes, row_count, unique_constraint,
                    foreign_keys, key_columns, columns, sample_rows}
    """
    return _describe_tool.run(table=table)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Run read-only SQL on vo2 schema",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def run_sql(sql: str, max_rows: int = 100) -> dict[str, Any]:
    """vo2 schema에 read-only SELECT 실행.

    안전 장치 (PostgreSQL vo2_reader 권한 + 추가 검증):
    - SELECT 또는 WITH...SELECT만 허용
    - INSERT/UPDATE/DELETE/DROP/ALTER/COPY/CALL 차단
    - 세미콜론 multi-statement 차단
    - 자동 LIMIT wrap (max_rows, 기본 100, 최대 1000)
    - 10초 statement_timeout
    - 배열 컬럼 (temperature_c, resistance_ohm, intensity)은 길이/preview만 반환
    - 큰 JSONB는 keys + size만 반환

    배열/시계열 raw가 필요하면 get_timeseries 도구 사용.

    Args:
        sql: SELECT 또는 WITH...SELECT 문. vo2.* 테이블 대상.
        max_rows: 1~1000, 기본 100.

    Returns:
        {columns: [...], rows: [...], row_count: N, truncated: bool,
         duration_ms: N, sql_executed: str, note?: str, error?: str}
    """
    return _sql_tool.run(sql=sql, max_rows=max_rows)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get timeseries array for one row",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_timeseries(
    table: Literal["measurements", "rga_runs"],
    row_id: int,
) -> dict[str, Any]:
    """measurements/rga_runs 한 row의 시계열 배열 raw + 메타데이터 + 통계.

    한 번에 한 row만 (토큰 폭발 방지).

    table='measurements':
        한 .dat 파일 = 한 측정 = 한 row.
        temperature_c (°C) + resistance_ohm (Ohm) 배열 같은 인덱스끼리 짝.
        평균 500점. VO2 전이온도 분석은 dR/dT 변곡점 (60~70°C).

    table='rga_runs':
        한 측정 시점의 Mass 1~65 partial pressure.
        intensity[i] = Mass (i+1)의 값.
        notable_masses에 주요 mass (H2O, N2, O2, Ar 등) 미리 highlight.

    Args:
        table: 'measurements' 또는 'rga_runs'
        row_id: 해당 테이블의 id 컬럼 값. run_sql로 id 미리 조회 가능.

    Returns:
        {table, row_id, metadata, data: {배열들}, summary: {통계}, info, notable_masses?}
    """
    return _ts_tool.run(table=table, row_id=row_id)


# Streamable HTTP transport ASGI app — main.py에서 mount.
mcp_app = mcp.streamable_http_app()