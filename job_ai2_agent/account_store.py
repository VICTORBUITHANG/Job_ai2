from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_ai2_agent.config import Settings


def load_account_profile(settings: Settings, email: str) -> dict[str, Any] | None:
    path = _account_path(settings, email)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_account_profile(settings: Settings, email: str) -> dict[str, Any]:
    existing = load_account_profile(settings, email)
    if existing is not None:
        return existing
    now = _now()
    profile = {
        "email": email.strip().lower(),
        "created_at": now,
        "updated_at": now,
        "completed_sections": False,
        "fields": {},
    }
    _write_account_profile(settings, email, profile)
    return profile


def save_account_fields(settings: Settings, email: str, fields: dict[str, str]) -> dict[str, Any]:
    profile = ensure_account_profile(settings, email)
    cleaned = {
        key: value.strip()
        for key, value in fields.items()
        if isinstance(value, str) and value.strip()
    }
    profile["fields"] = cleaned
    profile["completed_sections"] = True
    profile["updated_at"] = _now()
    _write_account_profile(settings, email, profile)
    return profile


def account_fields(profile: dict[str, Any] | None) -> dict[str, str]:
    if not profile:
        return {}
    fields = profile.get("fields", {})
    if not isinstance(fields, dict):
        return {}
    return {str(key): str(value) for key, value in fields.items() if str(value).strip()}


def _write_account_profile(settings: Settings, email: str, profile: dict[str, Any]) -> None:
    settings.account_dir.mkdir(parents=True, exist_ok=True)
    _account_path(settings, email).write_text(json.dumps(profile, indent=2), encoding="utf-8")


def _account_path(settings: Settings, email: str) -> Path:
    normalized = email.strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return settings.account_dir / f"{digest}.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()
