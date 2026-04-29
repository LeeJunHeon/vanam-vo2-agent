"""환경 설정 — pydantic-settings로 .env 자동 로드.

각 컨테이너는 docker-compose의 env_file: .env 또는
환경변수 직접 주입 방식으로 받는다. 본 모듈은 그것을 type-safe로 노출한다.

비밀번호는 SecretStr로 감싸 logger에 실수로 출력해도 ********로 가려진다.
실제 평문 필요한 곳(DB URL 만들 때 등)은 .get_secret_value() 호출.
"""
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DB 호스트 / 포트 / DB 이름
    POSTGRES_HOST: str = "inventory-web-postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "inventory"

    # vo2 user 비밀번호 (SecretStr)
    VO2_ADMIN_PASSWORD: SecretStr
    VO2_WRITER_PASSWORD: SecretStr
    VO2_READER_PASSWORD: SecretStr

    # 완성된 SQLAlchemy 연결 문자열 (이미 비밀번호 박힘)
    DATABASE_URL_WRITER: SecretStr
    DATABASE_URL_READER: SecretStr
    DATABASE_URL_ADMIN: SecretStr

    # MCP server 인증 토큰 (32자 이상 hex)
    INTERNAL_API_TOKEN: SecretStr

    # 로그 / CORS / Phase
    LOG_LEVEL: str = "INFO"
    ALLOWED_ORIGINS: str = "https://vanam.synology.me"
    PHASE: int = 1

    # ETL 동작 파라미터
    ETL_GRACE_SECONDS: int = 60
    ETL_INTERVAL_SECONDS: int = 300
    ETL_STATE_DIR: str = "/app/state"

    # 시간대
    TZ: str = "Asia/Seoul"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings 싱글톤. 한 번 로드 후 재사용."""
    return Settings()  # type: ignore[call-arg]
