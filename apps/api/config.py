from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_env: Literal["development", "test"] = "development"
    data_source: Literal["mock"] = "mock"
    database_url: str = (
        "postgresql+psycopg://expense@localhost:55432/expense_m4a_demo"
    )
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]
    llm_provider: Literal["mock", "deepseek", "qwen", "openai"] = "mock"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = Field(default="deepseek-flash", min_length=1, max_length=128)
    deepseek_timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    deepseek_max_tokens: int = Field(default=1024, ge=128, le=4096)
    embedding_model: str = "BAAI/bge-m3"
    policy_retriever: Literal["inmemory", "pgvector"] = "inmemory"
    embedding_provider: Literal["sentence_transformers", "deterministic", "remote"] = (
        "sentence_transformers"
    )
    embedding_dimension: int = 1024
    embedding_device: str = "cpu"
    embedding_base_url: str = ""
    embedding_api_key: SecretStr | None = None
    embedding_timeout: float = Field(default=15.0, gt=0, le=120, allow_inf_nan=False)
    embedding_max_attempts: int = Field(default=3, ge=1, le=5)
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    mock_store_path: str = ".m1-runtime/mock.sqlite3"
    ocr_binary: str = "tesseract"
    ocr_languages: str = "eng"
    ocr_tessdata: str | None = None
    p4a_enabled: bool = False
    p4b_enabled: bool = False
    p4b_advisory_enabled: bool = False
    p4b_advisory_protocol: Literal[
        "p4b.advisory.1", "p4b.advisory.2", "p4b.advisory.3", "p4b.advisory.4"
    ] = "p4b.advisory.1"
    p4b_live_llm: bool = False
    p4b_live_budget_path: str | None = None
    p4b_live_max_requests: int = Field(default=20, ge=1, le=30)
    p4b_max_tokens: int = Field(default=4096, ge=1024, le=4096)
    enterprise_enabled: bool = False
    enterprise_session_seconds: int = Field(default=28800, ge=300, le=86400)
    enterprise_cookie_secure: bool = True

    policy_demo_enabled: bool = False
    policy_demo_token: SecretStr | None = None
    policy_draft_embedding: Literal["disabled", "deterministic", "remote"] = "disabled"
    policy_remote_enabled: bool = False
    policy_remote_max_requests: int = Field(default=0, ge=0, le=30)
    policy_remote_budget_path: str = "output/ui-p3a/remote-http-budget.sqlite3"
