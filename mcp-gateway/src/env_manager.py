"""Manage service deployment profiles stored in the project .env file."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
PROFILES_KEY = "SERVICE_ENV_PROFILES_JSON"
ACTIVE_PROFILE_KEY = "ACTIVE_SERVICE_ENV_PROFILE_ID"

ENV_PROFILE_KEYS = {
    "DEPLOYMENT_MODE",
    "EXTERNAL_DOMAIN",
    "INTERNAL_DOMAIN",
    "CORS_ORIGINS",
    "SSL_CERT_FILE",
    "SSL_KEY_FILE",
}

DEFAULT_PROFILE = {
    "id": "",
    "name": "",
    "deployment_mode": "internal",
    "external_domain": "",
    "internal_domain": "localhost",
    "cors_origins": "*",
    "ssl_cert_file": "",
    "ssl_key_file": "",
}


def _decode_env_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
        try:
            return json.loads(raw) if raw[0] == '"' else raw[1:-1]
        except Exception:
            return raw[1:-1]
    return raw


def _encode_env_value(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text or any(ch.isspace() for ch in text) or any(ch in text for ch in ['"', "'", "#", "=", "{", "}", "[", "]"]):
        return json.dumps(text, ensure_ascii=False)
    return text


def read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = _decode_env_value(value)
    return result


def write_env_values(values: dict[str, Any]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen = set()
    output = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in values:
                output.append(f"{key}={_encode_env_value(values[key])}")
                seen.add(key)
                continue
        output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={_encode_env_value(value)}")
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def list_profiles() -> tuple[list[dict[str, Any]], str]:
    env = read_env()
    active_id = env.get(ACTIVE_PROFILE_KEY, "")
    raw = env.get(PROFILES_KEY, "")
    profiles: list[dict[str, Any]] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                profiles = [normalize_profile(item) for item in parsed if isinstance(item, dict)]
        except Exception:
            profiles = []
    return profiles, active_id


def normalize_profile(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = {**DEFAULT_PROFILE, **(existing or {})}
    profile.update({k: data.get(k, profile[k]) for k in DEFAULT_PROFILE})
    profile["id"] = str(data.get("id") or profile.get("id") or uuid.uuid4())
    profile["name"] = str(profile.get("name") or "未命名环境").strip()
    mode = str(profile.get("deployment_mode") or "internal").strip().lower()
    if mode not in {"internal", "external", "hybrid"}:
        raise ValueError("deployment_mode must be internal, external, or hybrid")
    profile["deployment_mode"] = mode
    for key in ("external_domain", "internal_domain", "cors_origins", "ssl_cert_file", "ssl_key_file"):
        profile[key] = str(profile.get(key) or "").strip()
    if mode in {"external", "hybrid"} and not profile["external_domain"]:
        raise ValueError("external_domain is required for external or hybrid mode")
    return profile


def save_profile(data: dict[str, Any]) -> dict[str, Any]:
    profiles, active_id = list_profiles()
    profile_id = str(data.get("id") or "")
    existing = next((item for item in profiles if item["id"] == profile_id), None)
    profile = normalize_profile(data, existing)
    if existing:
        profiles = [profile if item["id"] == profile["id"] else item for item in profiles]
    else:
        profiles.append(profile)
    values = {PROFILES_KEY: json.dumps(profiles, ensure_ascii=False)}
    if not active_id:
        values[ACTIVE_PROFILE_KEY] = profile["id"]
        values.update(profile_to_env(profile))
    write_env_values(values)
    return profile


def delete_profile(profile_id: str) -> bool:
    profiles, active_id = list_profiles()
    kept = [item for item in profiles if item["id"] != profile_id]
    if len(kept) == len(profiles):
        return False
    values = {PROFILES_KEY: json.dumps(kept, ensure_ascii=False)}
    if active_id == profile_id:
        values[ACTIVE_PROFILE_KEY] = kept[0]["id"] if kept else ""
        if kept:
            values.update(profile_to_env(kept[0]))
    write_env_values(values)
    return True


def profile_to_env(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "DEPLOYMENT_MODE": profile["deployment_mode"],
        "EXTERNAL_DOMAIN": profile["external_domain"],
        "INTERNAL_DOMAIN": profile["internal_domain"],
        "CORS_ORIGINS": profile["cors_origins"] or "*",
        "SSL_CERT_FILE": profile["ssl_cert_file"],
        "SSL_KEY_FILE": profile["ssl_key_file"],
    }


def activate_profile(profile_id: str) -> dict[str, Any]:
    profiles, _ = list_profiles()
    profile = next((item for item in profiles if item["id"] == profile_id), None)
    if not profile:
        raise ValueError("profile not found")
    values = {ACTIVE_PROFILE_KEY: profile_id, **profile_to_env(profile)}
    write_env_values(values)
    return profile


async def restart_current_service(delay_seconds: float = 1.0) -> None:
    await asyncio.sleep(delay_seconds)
    command = (
        "Start-Sleep -Seconds 2; "
        "$env:PYTHONPATH='mcp-gateway/src'; "
        "python -m uvicorn main:app --host 0.0.0.0 --port 8000"
    )
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
    )
    os._exit(0)
