from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CHATV2_",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    database_url: str = (
        "postgresql+psycopg://chat_app:chat-app-local@127.0.0.1:5434/chat_v2"
    )
    migration_database_url: str = "postgresql+psycopg://chat:chat@127.0.0.1:5434/chat_v2"
    worker_database_url: str = (
        "postgresql+psycopg://chat_worker:chat-worker-local@127.0.0.1:5434/chat_v2"
    )
    allow_development_actor_header: bool = True
    provider: Literal["fake", "openai"] = "fake"
    model: str = "fake-chat-model"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_organization: str | None = None
    openai_project: str | None = None
    openai_timeout_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    literature_api_base_url: str = "http://127.0.0.1:8020/api/v2"
    literature_api_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    literature_service_token: str | None = None
    literature_retrieval_mode: Literal["BM25", "VECTOR", "HYBRID"] = "HYBRID"
    literature_retrieval_top_k: int = Field(default=20, ge=1, le=100)
    literature_chunk_top_k_per_document: int = Field(default=5, ge=1, le=20)
    literature_doi_document_max_chars: int = Field(default=20_000, ge=1_000, le=100_000)
    fake_response_prefix: str = "Fake response"
    fake_stream_delay_seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    worker_poll_seconds: float = Field(default=0.25, ge=0.05, le=60.0)
    sse_poll_seconds: float = Field(default=0.25, ge=0.05, le=10.0)
    sse_heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    turn_lease_seconds: int = Field(default=120, ge=15, le=7200)
    worker_max_concurrency: int = Field(default=32, ge=1, le=1000)
    principal_max_concurrency: int = Field(default=3, ge=1, le=100)
    max_tool_calls_ceiling: int = Field(default=100, ge=0, le=1000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
