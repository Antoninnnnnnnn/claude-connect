from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(WORKSPACE_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(default="", validation_alias=AliasChoices("API_KEY"))
    # Bind loopback only: these services sit behind the local reverse proxy,
    # nothing should reach them from off-box. Override with ED_HOST.
    host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("ED_HOST", "HOST"))
    port: int = Field(default=8093, validation_alias=AliasChoices("ED_PORT", "PORT"))

    ed_username: str = Field(default="", validation_alias=AliasChoices("ED_USERNAME"))
    ed_password: str = Field(default="", validation_alias=AliasChoices("ED_PASSWORD"))

    qcm_file: Path = Field(
        default=PROJECT_ROOT / "qcm.json",
        validation_alias=AliasChoices("ED_QCM_FILE"),
    )

    # A failed login is never retried immediately: EcoleDirecte locks accounts after a
    # handful of bad attempts, and every API call would otherwise trigger a fresh try.
    ed_login_backoff_base: float = Field(default=30.0, validation_alias=AliasChoices("ED_LOGIN_BACKOFF_BASE"))
    ed_login_backoff_max: float = Field(default=1800.0, validation_alias=AliasChoices("ED_LOGIN_BACKOFF_MAX"))
    ed_login_timeout: float = Field(default=20.0, validation_alias=AliasChoices("ED_LOGIN_TIMEOUT"))
    # Credentials rejected outright (not a network blip) stop the retry loop entirely
    # until the process restarts or the MFA answer changes.
    ed_max_credential_failures: int = Field(
        default=3,
        validation_alias=AliasChoices("ED_MAX_CREDENTIAL_FAILURES"),
    )

    @field_validator("ed_username", "ed_password", mode="before")
    @classmethod
    def strip_value(cls, value: str | None) -> str:
        return str(value or "").strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
