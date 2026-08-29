from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-only runtime configuration for the v2 service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LITV2_",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = (
        "postgresql+psycopg://literature_app:literature-app-local@127.0.0.1:5433/literature_v2"
    )
    migration_database_url: str = (
        "postgresql+psycopg://literature:literature@127.0.0.1:5433/literature_v2"
    )
    worker_database_url: str = (
        "postgresql+psycopg://literature_worker:literature-worker-local@127.0.0.1:5433/"
        "literature_v2"
    )

    oidc_issuer: str = "http://127.0.0.1:8081/realms/literature-v2"
    oidc_discovery_url: str = (
        "http://127.0.0.1:8081/realms/literature-v2/.well-known/openid-configuration"
    )
    oidc_client_id: str = "literature-v2-web"
    oidc_client_secret: SecretStr = SecretStr("local-development-only")
    oidc_signing_algorithms: str = "RS256"
    oidc_backend_base_url: str = "http://127.0.0.1:8081"
    public_api_base_url: str = "http://127.0.0.1:8020"
    frontend_url: str = "http://127.0.0.1:5174"
    browser_origin_map: str = ""
    session_secret: SecretStr = Field(
        default=SecretStr("local-development-secret-replace-me"),
        min_length=32,
    )
    web_session_ttl_seconds: int = Field(default=60 * 60 * 24 * 14, ge=300)
    oidc_attempt_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    session_cookie_name: str = "litv2_session"
    csrf_cookie_name: str = "litv2_csrf"
    bootstrap_admin_emails: str = ""
    chat_service_token: SecretStr | None = None

    s3_endpoint_url: str = "http://127.0.0.1:9002"
    s3_public_endpoint_url: str = "http://127.0.0.1:9002"
    s3_access_key: str = "literature"
    s3_secret_key: SecretStr = SecretStr("literature-local-secret")
    s3_bucket: str = "literature-v2"
    s3_region: str = "us-east-1"

    crossref_base_url: str = "https://api.crossref.org"
    openalex_base_url: str = "https://api.openalex.org"
    arxiv_api_base_url: str = "https://export.arxiv.org/api"
    arxiv_min_interval_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    metadata_provider_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    scholarly_api_mailto: str | None = None
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    citation_import_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    pdf_import_max_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    asset_upload_max_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    zotero_import_max_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    pdf_extract_max_pages: int = Field(default=2, ge=1, le=10)
    fake_pdf_text_latency_seconds: float = Field(default=60.0, ge=0.0, le=600.0)
    fake_pdf_text_word_count: int = Field(default=500, ge=10, le=10_000)
    embedding_base_url: str = "http://223.109.141.102:6012/v1"
    embedding_api_key: SecretStr | None = None
    embedding_model: str = "bge-m3"
    embedding_dimensions: int = Field(default=1024, ge=1, le=65_536)
    embedding_batch_size: int = Field(default=32, ge=1, le=2048)
    embedding_max_batch_tokens: int = Field(default=10_000, ge=1, le=1_000_000)
    embedding_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    embedding_max_retries: int = Field(default=2, ge=0, le=10)
    document_pdf_text_url: str | None = None
    document_pdf_text_request_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    document_pdf_text_job_timeout_seconds: float = Field(default=3600.0, ge=60.0, le=7200.0)
    document_pdf_text_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    document_pipeline_url: str | None = None
    document_source_concurrency: int = Field(default=1, ge=1, le=1)
    document_pipeline_concurrency: int = Field(default=4, ge=1, le=64)
    document_index_concurrency: int = Field(default=1, ge=1, le=8)
    document_task_lease_seconds: int = Field(default=3900, ge=30, le=7200)
    document_scheduler_poll_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
