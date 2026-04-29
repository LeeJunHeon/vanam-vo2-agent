# vo2-etl-worker
# 5분마다 SMB 파일 → vo2 schema 적재

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    TZ=Asia/Seoul

WORKDIR /app

# psycopg, pandas 빌드 의존성 + tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치 (Docker layer cache 활용)
COPY requirements.txt /app/requirements.txt
COPY etl_worker/requirements.txt /app/etl_worker/requirements.txt
RUN pip install -r /app/requirements.txt -r /app/etl_worker/requirements.txt

# 코드 복사
COPY shared/ /app/shared/
COPY etl_worker/ /app/etl_worker/

# state 디렉토리 (volume mount 될 위치, 빈 디렉토리로 생성)
RUN mkdir -p /app/state

# 비루트 사용자로 실행 (보안)
RUN useradd -m -u 1000 etluser && chown -R etluser:etluser /app
USER etluser

CMD ["python", "-m", "etl_worker.main"]
