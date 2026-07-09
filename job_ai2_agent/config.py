# File: /Users/victorbui/AI/Job_ai2/job_ai2_agent/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTERPRETER = Path("/Users/victorbui/venvs/ai312/bin/python")


@dataclass(frozen=True, slots=True)
class Settings:
    app_host: str
    app_port: int
    openai_api_key: str
    openai_model: str
    local_llm_provider: str
    local_llm_base_url: str
    local_llm_model: str
    local_llm_timeout_seconds: int
    browser_headless: bool
    browser_hold_seconds: int
    upload_dir: Path
    review_dir: Path
    screenshot_dir: Path
    account_dir: Path


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings(
        app_host=os.getenv("APP_HOST", "127.0.0.1"),
        app_port=int(os.getenv("APP_PORT", "8022")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        local_llm_provider=os.getenv("LOCAL_LLM_PROVIDER", "").strip().lower(),
        local_llm_base_url=os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434"),
        local_llm_model=os.getenv("LOCAL_LLM_MODEL", ""),
        local_llm_timeout_seconds=int(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "60")),
        browser_headless=_env_bool("BROWSER_HEADLESS", default=False),
        browser_hold_seconds=int(os.getenv("BROWSER_HOLD_SECONDS", "900")),
        upload_dir=PROJECT_ROOT / "artifacts" / "uploads",
        review_dir=PROJECT_ROOT / "artifacts" / "reviews",
        screenshot_dir=PROJECT_ROOT / "artifacts" / "screenshots",
        account_dir=PROJECT_ROOT / "artifacts" / "accounts",
    )


def ensure_artifact_dirs(settings: Settings) -> None:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.review_dir.mkdir(parents=True, exist_ok=True)
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
    settings.account_dir.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
