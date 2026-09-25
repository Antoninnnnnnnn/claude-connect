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
    # nothing should reach them from off-box. Override with YT_HOST.
    host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("YT_HOST", "HOST"))
    port: int = Field(default=8095, validation_alias=AliasChoices("YT_PORT", "PORT"))

    yt_proxy: str | None = Field(default=None, validation_alias=AliasChoices("YT_PROXY"))
    yt_proxies: str | None = Field(default=None, validation_alias=AliasChoices("YT_PROXIES"))
    decodo_proxy: str | None = Field(default=None, validation_alias=AliasChoices("DECODO_PROXY"))
    dataimpulse_proxy: str | None = Field(default=None, validation_alias=AliasChoices("DATAIMPULSE_PROXY"))
    evomi_proxy: str | None = Field(default=None, validation_alias=AliasChoices("EVOMI_PROXY"))

    # YouTube refuses transcripts to most datacenter IPs, so requests go through the proxy
    # pool when one is configured. YT_DIRECT_FIRST tries the server's own IP before spending
    # residential bandwidth; YT_ALLOW_DIRECT_FALLBACK tries it after the pool gave up.
    yt_direct_first: bool = Field(default=False, validation_alias=AliasChoices("YT_DIRECT_FIRST"))
    yt_allow_direct_fallback: bool = Field(default=False, validation_alias=AliasChoices("YT_ALLOW_DIRECT_FALLBACK"))

    # The library sets no timeout at all: a tarpitted proxy would hang a worker forever.
    # yt_timeout bounds each HTTP request, yt_deadline the whole call across retries:
    # keep it under the nginx proxy_read_timeout (90s).
    yt_timeout: float = Field(default=15.0, validation_alias=AliasChoices("YT_TIMEOUT"))
    yt_deadline: float = Field(default=75.0, validation_alias=AliasChoices("YT_DEADLINE"))
    yt_max_retries: int = Field(default=4, validation_alias=AliasChoices("YT_MAX_RETRIES"))
    yt_min_interval: float = Field(default=0.5, validation_alias=AliasChoices("YT_MIN_INTERVAL"))

    # Tried in order when the caller passes no `lang`.
    yt_default_languages: str = Field(default="fr,en", validation_alias=AliasChoices("YT_DEFAULT_LANGUAGES"))
    # ~5k tokens: enough for a 20-minute video, the agent pages through longer ones.
    yt_default_max_chars: int = Field(default=20000, validation_alias=AliasChoices("YT_DEFAULT_MAX_CHARS"))
    # Skip the ~300 KB watch page and ask the player API for three fields only (app/light.py).
    # Falls back to the full library path by itself if YouTube rejects the request shape.
    yt_light_mode: bool = Field(default=True, validation_alias=AliasChoices("YT_LIGHT_MODE"))
    # Title and channel come with the light path; otherwise from oEmbed, one small extra request.
    yt_fetch_title: bool = Field(default=True, validation_alias=AliasChoices("YT_FETCH_TITLE"))

    # A transcript never changes once published, and every miss downloads the full watch
    # page through a billed proxy: cache long. Entries can weigh hundreds of KB, keep few.
    yt_cache_ttl: float = Field(default=21600.0, validation_alias=AliasChoices("YT_CACHE_TTL"))
    yt_cache_max_entries: int = Field(default=64, validation_alias=AliasChoices("YT_CACHE_MAX_ENTRIES"))

    # Search, channel, playlist and video metadata use the innertube WEB client (app/innertube.py).
    # YouTube accepts older client versions for a long while; if these endpoints start
    # answering `upstream_rejected`, bump this to the clientVersion youtube.com sends today.
    yt_web_client_version: str = Field(default="2.20250925.01.00", validation_alias=AliasChoices("YT_WEB_CLIENT_VERSION"))
    # Interface language: YouTube ranks results for it and shows titles translated into it
    # when a translation exists (creator-provided or automatic), so a French user wants fr:
    # hl=en turns French videos' titles into English. gl sets the ranking region.
    yt_hl: str = Field(default="fr", validation_alias=AliasChoices("YT_HL"))
    yt_gl: str = Field(default="FR", validation_alias=AliasChoices("YT_GL"))
    # Result pages go stale quickly but get re-read on every `next` that cuts a page:
    # a short TTL, and a cache of their own so they never evict transcripts.
    yt_browse_cache_ttl: float = Field(default=600.0, validation_alias=AliasChoices("YT_BROWSE_CACHE_TTL"))
    yt_browse_cache_max_entries: int = Field(default=128, validation_alias=AliasChoices("YT_BROWSE_CACHE_MAX_ENTRIES"))
    # Upstream pages one call may fetch to reach `limit` (~20 results per search page, ~30 per channel page).
    yt_max_pages: int = Field(default=5, validation_alias=AliasChoices("YT_MAX_PAGES"))

    @field_validator("yt_proxy", "yt_proxies", "decodo_proxy", "dataimpulse_proxy", "evomi_proxy", mode="before")
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None

    def proxy_urls(self) -> list[str]:
        values: list[str] = []
        if self.yt_proxies:
            for part in self.yt_proxies.replace("\n", ",").replace(";", ",").split(","):
                clean = part.strip()
                if clean and clean not in values:
                    values.append(clean)
        for value in [self.yt_proxy, self.decodo_proxy, self.dataimpulse_proxy, self.evomi_proxy]:
            if value and value not in values:
                values.append(value)
        return values

    def default_languages(self) -> list[str]:
        values = [part.strip() for part in self.yt_default_languages.split(",") if part.strip()]
        return values or ["en"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
