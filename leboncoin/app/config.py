from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(WORKSPACE_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(default="", validation_alias=AliasChoices("API_KEY"))
    # Bind loopback only: these services sit behind the local reverse proxy,
    # nothing should reach them from off-box. Override with LBC_HOST.
    host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("LBC_HOST", "HOST"))
    port: int = Field(default=8092, validation_alias=AliasChoices("LBC_PORT", "PORT"))

    lbc_proxy: str | None = Field(default=None, validation_alias=AliasChoices("LBC_PROXY"))
    lbc_proxies: str | None = Field(default=None, validation_alias=AliasChoices("LBC_PROXIES"))
    decodo_proxy: str | None = Field(default=None, validation_alias=AliasChoices("DECODO_PROXY"))
    dataimpulse_proxy: str | None = Field(default=None, validation_alias=AliasChoices("DATAIMPULSE_PROXY"))
    evomi_proxy: str | None = Field(default=None, validation_alias=AliasChoices("EVOMI_PROXY"))
    vinted_proxy: str | None = Field(default=None, validation_alias=AliasChoices("VINTED_PROXY"))

    # Short on purpose: a tarpitted residential exit IP trickles at a few KB/s, so it is
    # cheaper to abandon it and reconnect (new exit IP) than to wait it out.
    lbc_timeout: float = Field(default=12.0, validation_alias=AliasChoices("LBC_TIMEOUT"))
    lbc_min_interval: float = Field(default=1.5, validation_alias=AliasChoices("LBC_MIN_INTERVAL"))
    lbc_max_retries: int = Field(default=6, validation_alias=AliasChoices("LBC_MAX_RETRIES"))
    # The mobile JSON API expects a mobile-app client: desktop TLS fingerprints get 403'd.
    lbc_impersonates: str = Field(default="safari_ios,chrome_android,firefox", validation_alias=AliasChoices("LBC_IMPERSONATES"))
    lbc_rotate_proxy_per_request: bool = Field(default=True, validation_alias=AliasChoices("LBC_ROTATE_PROXY_PER_REQUEST"))
    lbc_max_pages_per_search: int = Field(default=3, validation_alias=AliasChoices("LBC_MAX_PAGES_PER_SEARCH"))
    # Requests always go through the proxy pool; set this to true to let a failing pool fall
    # back on the server's own IP instead of erroring out.
    lbc_allow_direct_fallback: bool = Field(default=False, validation_alias=AliasChoices("LBC_ALLOW_DIRECT_FALLBACK"))
    # A response slower than this retires the session: its exit IP is throttled.
    lbc_slow_session_seconds: float = Field(default=4.0, validation_alias=AliasChoices("LBC_SLOW_SESSION_SECONDS"))
    lbc_session_ttl: float = Field(default=600.0, validation_alias=AliasChoices("LBC_SESSION_TTL"))
    lbc_cache_ttl: float = Field(default=20.0, validation_alias=AliasChoices("LBC_CACHE_TTL"))
    lbc_cache_max_entries: int = Field(default=256, validation_alias=AliasChoices("LBC_CACHE_MAX_ENTRIES"))

    @field_validator(
        "lbc_proxy",
        "lbc_proxies",
        "decodo_proxy",
        "dataimpulse_proxy",
        "evomi_proxy",
        "vinted_proxy",
        mode="before",
    )
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None

    def proxy_urls(self) -> list[str]:
        values: list[str] = []
        if self.lbc_proxies:
            for part in self.lbc_proxies.replace("\n", ",").replace(";", ",").split(","):
                clean = part.strip()
                if clean:
                    values.append(clean)
        for value in [
            self.lbc_proxy,
            self.decodo_proxy,
            self.dataimpulse_proxy,
            self.evomi_proxy,
            self.vinted_proxy,
        ]:
            if value and value not in values:
                values.append(value)
        return values

    def impersonates(self) -> list[str]:
        values = [part.strip() for part in self.lbc_impersonates.split(",") if part.strip()]
        return values or ["chrome"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
