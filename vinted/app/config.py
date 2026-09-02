from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices
from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(default="", validation_alias=AliasChoices("API_KEY"))
    # Bind loopback only: these services sit behind the local reverse proxy,
    # nothing should reach them from off-box. Override with VINTED_HOST.
    host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("VINTED_HOST", "HOST"))
    port: int = Field(default=8091, validation_alias=AliasChoices("VINTED_PORT", "PORT"))
    default_domain: str = Field(default="fr", validation_alias=AliasChoices("VINTED_DEFAULT_DOMAIN", "DEFAULT_DOMAIN"))
    vinted_proxy: str | None = Field(default=None, validation_alias=AliasChoices("VINTED_PROXY"))
    vinted_timeout: float = Field(default=20.0, validation_alias=AliasChoices("VINTED_TIMEOUT"))
    vinted_min_interval: float = Field(default=1.2, validation_alias=AliasChoices("VINTED_MIN_INTERVAL"))
    vinted_max_retries: int = Field(default=3, validation_alias=AliasChoices("VINTED_MAX_RETRIES"))
    cookie_file: Path = Field(default=Path("data/cookies.json"), validation_alias=AliasChoices("VINTED_COOKIE_FILE", "COOKIE_FILE"))

    @field_validator("vinted_proxy", mode="before")
    @classmethod
    def blank_proxy_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
