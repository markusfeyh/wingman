"""Settings from .env plus the YAML files in config/."""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    wingman_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # "anthropic" or "openai"; empty = Anthropic if its key is set, else OpenAI if its key is set
    wingman_llm: str = ""
    brightdata_api_key: str = ""
    brightdata_unlocker_zone: str = "web_unlocker1"
    brightdata_serp_zone: str = "serp_api1"
    deepgram_api_key: str = ""
    github_token: str = ""

    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    graph_name: str = "wingman"

    # Opus 5.5 everywhere by default. WINGMAN_MODEL_LIVE=claude-haiku-5-5 gives faster live cards.
    wingman_model_live: str = "claude-opus-5-5"
    wingman_model_recap: str = "claude-opus-5-5"
    wingman_openai_model_live: str = "gpt-5.5"
    wingman_openai_model_recap: str = "gpt-5.5"
    # Business Search (people lookup) is early access; set false if the account isn't enabled, to skip the 403
    brightdata_business_search: bool = True

    wingman_event: str = "github-universe-2026"
    wingman_timezone: str = "America/Los_Angeles"
    data_dir: Path = ROOT / "data"

    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = "mailto:wingman@example.com"

    # Demo only: a fixtures directory (see demo/README.md). When set, Bright Data and Claude responses are
    # replayed from it instead of calling the APIs, with simulated latency divided by the speed factor.
    wingman_fixtures: str = ""
    wingman_fixture_speed: float = 1.0
    # Demo only: Listen plays this scripted conversation (wingman/demo.py) instead of using the mic
    wingman_demo_script: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def _me_path() -> Path:
    return Path(os.environ.get("WINGMAN_ME_FILE") or ROOT / "config" / "me.yaml")


def me_config() -> dict:
    return _load_yaml(_me_path())


def save_me_config(data: dict) -> None:
    _me_path().write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def event_config(event_id: str | None = None) -> dict:
    return _load_yaml(ROOT / "config" / "events" / f"{event_id or settings().wingman_event}.yaml")
