# vo2-mcp-server
# FastAPI read-only 도구 + audit log
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    TZ=Asia/Seoul

WORKDIR /app

# psycopg 빌드 의존성 + healthcheck용 curl + tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
        tzdata \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치 (Docker layer cache 활용)
COPY requirements.txt /app/requirements.txt
COPY mcp_server/requirements.txt /app/mcp_server/requirements.txt
RUN pip install -r /app/requirements.txt -r /app/mcp_server/requirements.txt

# 코드 복사
COPY shared/ /app/shared/
COPY mcp_server/ /app/mcp_server/

# 비루트 사용자 (SMB mount 없으므로 compose user override 불필요)
RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

EXPOSE 8000

CMD ["uvicorn", "mcp_server.app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--log-level", "info"]
