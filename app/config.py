from datetime import time
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = Field(default="", repr=False)
    postgres_password: SecretStr | None = None

    @model_validator(mode="after")
    def resolve_database_url(self):
        if not self.database_url:
            if self.postgres_password is None or not self.postgres_password.get_secret_value():
                raise ValueError("Set POSTGRES_PASSWORD or DATABASE_URL")
            self.database_url = URL.create(
                "postgresql+asyncpg",
                username="monitor",
                password=self.postgres_password.get_secret_value(),
                host="postgres",
                port=5432,
                database="monitor",
            ).render_as_string(hide_password=False)
        return self


class Settings(DatabaseSettings):
    telegram_bot_token: SecretStr
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    dmsu_enabled: bool = True
    document_enabled: bool = True
    document_browser_headless: bool = False
    dmsu_poll_interval: float = Field(60, ge=30)
    document_poll_interval: float = Field(90, ge=30)
    dmsu_fast_enabled: bool = False
    dmsu_fast_start: time = time(23, 55)
    dmsu_fast_end: time = time(0, 10)
    dmsu_fast_interval: float = Field(30, ge=10)
    jitter: float = Field(0.15, ge=0, le=0.2)
    http_timeout: float = Field(20, gt=0)
    max_retries: int = Field(2, ge=0, le=5)
    backoff_factor: float = Field(2, ge=1)
    global_request_interval: float = Field(1, ge=0.1)
    provider_request_interval: float = Field(2, ge=1)
    circuit_threshold: int = Field(5, ge=1)
    circuit_cooldown: float = Field(300, ge=30)
    admin_failure_threshold: int = Field(3, ge=1)
    admin_cooldown: float = Field(1800, ge=60)
    reappearance_cooldown: float = Field(600, ge=0)
    catalog_ttl: float = Field(21600, ge=60)
    max_concurrent_jobs: int = Field(4, ge=1, le=16)
    metrics_port: int = Field(8080, ge=1, le=65535)
    dashboard_username: str = "admin"
    dashboard_password: SecretStr | None = None
    log_level: str = "INFO"
    tz: str = "Europe/Kyiv"

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_admins(cls, value):
        return (
            [int(v.strip()) for v in value.split(",") if v.strip()]
            if isinstance(value, str)
            else value
        )

    @model_validator(mode="after")
    def providers_enabled(self):
        if not (self.dmsu_enabled or self.document_enabled):
            raise ValueError("Enable at least one provider")
        return self
