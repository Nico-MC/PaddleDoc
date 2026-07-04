from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'PaddleDoc API'
    database_url: str = ''
    postgres_host: str = ''
    postgres_port: int = 5432
    postgres_db: str = ''
    postgres_user: str = ''
    postgres_password: str = ''
    redis_url: str = 'redis://redis:6379/0'
    cors_origins: list[str] = ['http://localhost:3000']
    max_upload_bytes: int = 100 * 1024 * 1024
    rate_limit_per_minute: int = 60
    uploads_dir: Path = Path('backend/storage/uploads')
    results_dir: Path = Path('backend/storage/results')
    paddle_default_profile: str = 'ppocrv6_tiny'
    paddle_timeout_seconds: int = 300
    worker_concurrency: int = 1
    openai_api_base_url: str = ''
    openai_api_bearer_token: str = ''


def _build_database_url(settings: Settings) -> str:
    if settings.database_url:
        return settings.database_url

    if settings.postgres_host and settings.postgres_db and settings.postgres_user:
        user = quote_plus(settings.postgres_user)
        password = quote_plus(settings.postgres_password)
        db = quote_plus(settings.postgres_db)
        if settings.postgres_password:
            auth = f'{user}:{password}'
        else:
            auth = user
        return f'postgresql+psycopg://{auth}@{settings.postgres_host}:{settings.postgres_port}/{db}'

    return 'sqlite:///./PaddleDoc.db'


settings = Settings()
settings.database_url = _build_database_url(settings)
