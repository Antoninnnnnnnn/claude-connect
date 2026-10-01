from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(WORKSPACE_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(default="", validation_alias=AliasChoices("API_KEY"))
    # Bind loopback only: these services sit behind the local reverse proxy,
    # nothing should reach them from off-box. Override with GH_HOST.
    host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("GH_HOST", "HOST"))
    port: int = Field(default=8096, validation_alias=AliasChoices("GH_PORT", "PORT"))

    # Personal access token every request is made with. Empty: anonymous, public repos only
    # (60 API calls/hour), which is enough to smoke-test the git relay.
    github_token: str = Field(default="", validation_alias=AliasChoices("GITHUB_TOKEN"))
    github_api_url: str = Field(default="https://api.github.com", validation_alias=AliasChoices("GH_API_URL"))
    github_git_url: str = Field(default="https://github.com", validation_alias=AliasChoices("GH_GIT_URL"))

    # JSON API calls. The git relay has no read timeout: GitHub can think for minutes
    # before the first pack byte of a big clone.
    gh_timeout: float = Field(default=30.0, validation_alias=AliasChoices("GH_TIMEOUT"))
    # Directory listings and recursive trees are capped so one call stays a readable size.
    gh_max_tree_entries: int = Field(default=2000, validation_alias=AliasChoices("GH_MAX_TREE_ENTRIES"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
