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
    host: str = Field(default="0.0.0.0", validation_alias=AliasChoices("CENTRALE_HOST", "HOST"))
    port: int = Field(default=8094, validation_alias=AliasChoices("CENTRALE_PORT", "PORT"))

    centrale_proxy: str | None = Field(default=None, validation_alias=AliasChoices("CENTRALE_PROXY"))
    centrale_proxies: str | None = Field(default=None, validation_alias=AliasChoices("CENTRALE_PROXIES"))
    lbc_proxy: str | None = Field(default=None, validation_alias=AliasChoices("LBC_PROXY"))
    lbc_proxies: str | None = Field(default=None, validation_alias=AliasChoices("LBC_PROXIES"))
    decodo_proxy: str | None = Field(default=None, validation_alias=AliasChoices("DECODO_PROXY"))
    dataimpulse_proxy: str | None = Field(default=None, validation_alias=AliasChoices("DATAIMPULSE_PROXY"))
    evomi_proxy: str | None = Field(default=None, validation_alias=AliasChoices("EVOMI_PROXY"))
    vinted_proxy: str | None = Field(default=None, validation_alias=AliasChoices("VINTED_PROXY"))

    centrale_upstream_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CENTRALE_UPSTREAM_API_KEY"),
    )
    centrale_client_source: str = Field(
        default="lc:recherche:front",
        validation_alias=AliasChoices("CENTRALE_CLIENT_SOURCE"),
    )

    centrale_timeout: float = Field(default=45.0, validation_alias=AliasChoices("CENTRALE_TIMEOUT"))
    centrale_min_interval: float = Field(default=1.5, validation_alias=AliasChoices("CENTRALE_MIN_INTERVAL"))
    centrale_max_retries: int = Field(default=3, validation_alias=AliasChoices("CENTRALE_MAX_RETRIES"))
    centrale_impersonates: str = Field(
        # DataDome blocks desktop TLS/HTTP2 fingerprints since 2026-08; mobile ones pass.
        default="chrome_android,chrome131_android",
        validation_alias=AliasChoices("CENTRALE_IMPERSONATES"),
    )
    centrale_rotate_proxy_per_request: bool = Field(
        default=True,
        validation_alias=AliasChoices("CENTRALE_ROTATE_PROXY_PER_REQUEST"),
    )
    centrale_max_pages_per_search: int = Field(
        default=3,
        validation_alias=AliasChoices("CENTRALE_MAX_PAGES_PER_SEARCH"),
    )
    centrale_cache_ttl: float = Field(default=20.0, validation_alias=AliasChoices("CENTRALE_CACHE_TTL"))
    centrale_cache_max_entries: int = Field(default=256, validation_alias=AliasChoices("CENTRALE_CACHE_MAX_ENTRIES"))
    centrale_listing_cache_ttl: float = Field(
        default=3600.0,
        validation_alias=AliasChoices("CENTRALE_LISTING_CACHE_TTL"),
    )
    centrale_listing_cache_max_entries: int = Field(
        default=128,
        validation_alias=AliasChoices("CENTRALE_LISTING_CACHE_MAX_ENTRIES"),
    )
    centrale_listing_max_workers: int = Field(
        default=4,
        validation_alias=AliasChoices("CENTRALE_LISTING_MAX_WORKERS"),
    )
    centrale_upstream_api_key_fallbacks: str = Field(
        default="",
        validation_alias=AliasChoices("CENTRALE_UPSTREAM_API_KEY_FALLBACKS"),
    )
    centrale_cookie_file: str = Field(
        default=str(PROJECT_ROOT / "data" / "cookies.json"),
        validation_alias=AliasChoices("CENTRALE_COOKIE_FILE"),
    )
    centrale_primary_strategy: str = Field(
        default="auto",
        validation_alias=AliasChoices("CENTRALE_PRIMARY_STRATEGY"),
    )
    centrale_metadata_cache_ttl: float = Field(
        default=300.0,
        validation_alias=AliasChoices("CENTRALE_METADATA_CACHE_TTL"),
    )
    centrale_www_use_proxy: bool = Field(
        default=True,
        validation_alias=AliasChoices("CENTRALE_WWW_USE_PROXY"),
    )
    centrale_warmup_on_start: bool = Field(
        default=False,
        validation_alias=AliasChoices("CENTRALE_WARMUP_ON_START"),
    )

    # Browser-backed DataDome clearance for www.lacentrale.fr HTML (see www_session.py).
    centrale_browser_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CENTRALE_BROWSER_ENABLED"),
    )
    centrale_browser_python: str = Field(
        default=str(WORKSPACE_ROOT / ".venv-browser" / "bin" / "python"),
        validation_alias=AliasChoices("CENTRALE_BROWSER_PYTHON"),
    )
    centrale_browser_xvfb: bool = Field(
        # The device check only auto-clears headed, so a headless host needs a virtual display.
        default=True,
        validation_alias=AliasChoices("CENTRALE_BROWSER_XVFB"),
    )
    centrale_browser_proxy: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CENTRALE_BROWSER_PROXY"),
    )
    centrale_browser_mint_attempts: int = Field(
        default=4,
        validation_alias=AliasChoices("CENTRALE_BROWSER_MINT_ATTEMPTS"),
    )
    centrale_browser_mint_timeout: float = Field(
        default=420.0,
        validation_alias=AliasChoices("CENTRALE_BROWSER_MINT_TIMEOUT"),
    )
    centrale_browser_mint_cooldown: float = Field(
        default=300.0,
        validation_alias=AliasChoices("CENTRALE_BROWSER_MINT_COOLDOWN"),
    )
    centrale_datadome_token_file: str = Field(
        default=str(PROJECT_ROOT / "data" / "datadome_token.json"),
        validation_alias=AliasChoices("CENTRALE_DATADOME_TOKEN_FILE"),
    )
    centrale_datadome_token_max_age: float = Field(
        default=3600.0,
        validation_alias=AliasChoices("CENTRALE_DATADOME_TOKEN_MAX_AGE"),
    )
    centrale_www_min_interval: float = Field(
        # Clearance survives 5s spacing indefinitely but burns at ~1 req/s.
        default=5.0,
        validation_alias=AliasChoices("CENTRALE_WWW_MIN_INTERVAL"),
    )
    centrale_www_impersonate: str = Field(
        default="chrome",
        validation_alias=AliasChoices("CENTRALE_WWW_IMPERSONATE"),
    )
    centrale_www_fetch_use_proxy: bool = Field(
        # Clearance cookies are IP-portable, so HTML fetches skip the metered proxy.
        default=False,
        validation_alias=AliasChoices("CENTRALE_WWW_FETCH_USE_PROXY"),
    )

    def max_fetchable_limit(self) -> int:
        return 24 * max(1, self.centrale_max_pages_per_search)

    @field_validator("centrale_primary_strategy")
    @classmethod
    def validate_primary_strategy(cls, value: str) -> str:
        normalized = (value or "auto").strip().lower()
        if normalized not in {"auto", "json", "ssr"}:
            raise ValueError("centrale_primary_strategy must be auto, json, or ssr")
        return normalized

    @field_validator(
        "centrale_proxy",
        "centrale_proxies",
        "lbc_proxy",
        "lbc_proxies",
        "decodo_proxy",
        "dataimpulse_proxy",
        "evomi_proxy",
        "vinted_proxy",
        "centrale_browser_proxy",
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
        for raw in [self.centrale_proxies, self.lbc_proxies]:
            if raw:
                for part in raw.replace("\n", ",").replace(";", ",").split(","):
                    clean = part.strip()
                    if clean:
                        values.append(clean)
        for value in [
            self.centrale_proxy,
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
        values = [part.strip() for part in self.centrale_impersonates.split(",") if part.strip()]
        return values or ["chrome"]

    def upstream_api_key_fallbacks(self) -> list[str]:
        if not self.centrale_upstream_api_key_fallbacks:
            return []
        return [
            part.strip()
            for part in self.centrale_upstream_api_key_fallbacks.replace("\n", ",").split(",")
            if part.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
