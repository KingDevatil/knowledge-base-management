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
ENV_PATH = Path(os.environ.get("SERVICE_ENV_FILE") or PROJECT_ROOT / ".env")
PROFILES_KEY = "SERVICE_ENV_PROFILES_JSON"
ACTIVE_PROFILE_KEY = "ACTIVE_SERVICE_ENV_PROFILE_ID"
REVERSE_PROXY_CONFIGS_KEY = "REVERSE_PROXY_CONFIGS_JSON"
ACTIVE_REVERSE_PROXY_CONFIG_KEY = "ACTIVE_REVERSE_PROXY_CONFIG_ID"
REVERSE_PROXY_ENABLED_KEY = "REVERSE_PROXY_ENABLED"

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

DEFAULT_REVERSE_PROXY_CONFIG = {
    "id": "",
    "name": "",
    "enabled": True,
    "proxy_type": "caddy",
    "domain": "",
    "upstream_host": "127.0.0.1",
    "upstream_port": "8000",
    "ssl_cert_file": "",
    "ssl_key_file": "",
    "force_https": True,
    "config_text": "",
}

REVERSE_PROXY_RUNTIME_KEYS = {
    REVERSE_PROXY_ENABLED_KEY,
    "REVERSE_PROXY_DOMAIN",
    "REVERSE_PROXY_UPSTREAM_HOST",
    "REVERSE_PROXY_UPSTREAM_PORT",
    "REVERSE_PROXY_SSL_CERT_FILE",
    "REVERSE_PROXY_SSL_KEY_FILE",
    "REVERSE_PROXY_FORCE_HTTPS",
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
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def is_docker_deployment() -> bool:
    return os.environ.get("RUNNING_IN_DOCKER", "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_env(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


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
        raise ValueError("部署模式无效，请选择内网模式、外网模式或内外网混合模式。")
    profile["deployment_mode"] = mode
    for key in ("external_domain", "internal_domain", "cors_origins", "ssl_cert_file", "ssl_key_file"):
        profile[key] = str(profile.get(key) or "").strip()
    if mode in {"external", "hybrid"} and not profile["external_domain"]:
        raise ValueError("外网模式或内外网混合模式必须填写外网域名，例如 kb.example.com。")
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
        raise ValueError("部署模式配置不存在或已被删除。")
    values = {ACTIVE_PROFILE_KEY: profile_id, **profile_to_env(profile)}
    write_env_values(values)
    return profile


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否", ""}


def generate_reverse_proxy_config(config: dict[str, Any]) -> str:
    domain = config["domain"]
    upstream = f"{config['upstream_host']}:{config['upstream_port']}"
    proxy_type = config["proxy_type"]
    cert = config.get("ssl_cert_file", "")
    key = config.get("ssl_key_file", "")
    if proxy_type == "caddy":
        tls_line = f"\n    tls {cert} {key}" if cert and key else ""
        return f"{domain} {{{tls_line}\n    reverse_proxy {upstream}\n}}\n"
    if proxy_type == "nginx":
        listen = "443 ssl http2" if cert and key else "80"
        ssl_lines = f"\n    ssl_certificate {cert};\n    ssl_certificate_key {key};" if cert and key else ""
        return (
            "server {\n"
            f"    listen {listen};\n"
            f"    server_name {domain};{ssl_lines}\n\n"
            "    location /mcp {\n"
            f"        proxy_pass http://{upstream};\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Connection \"\";\n"
            "        proxy_buffering off;\n"
            "        proxy_cache off;\n"
            "        proxy_read_timeout 3600s;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_set_header X-API-Key $http_x_api_key;\n"
            "    }\n\n"
            "    location /sse {\n"
            f"        proxy_pass http://{upstream};\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Connection \"\";\n"
            "        proxy_buffering off;\n"
            "        proxy_cache off;\n"
            "        proxy_read_timeout 3600s;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_set_header X-API-Key $http_x_api_key;\n"
            "    }\n\n"
            "    location / {\n"
            f"        proxy_pass http://{upstream};\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection \"upgrade\";\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_read_timeout 3600s;\n"
            "    }\n"
            "}\n"
        )
    raise ValueError("反向代理类型无效，请选择 Caddy 或 Nginx。")


def normalize_reverse_proxy_config(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    config = {**DEFAULT_REVERSE_PROXY_CONFIG, **(existing or {})}
    config.update({k: data.get(k, config[k]) for k in DEFAULT_REVERSE_PROXY_CONFIG})
    config["id"] = str(data.get("id") or config.get("id") or uuid.uuid4())
    config["name"] = str(config.get("name") or "未命名反向代理").strip()
    config["enabled"] = _truthy(config.get("enabled"))
    config["proxy_type"] = str(config.get("proxy_type") or "caddy").strip().lower()
    if config["proxy_type"] not in {"caddy", "nginx"}:
        raise ValueError("反向代理类型无效，请选择 Caddy 或 Nginx。")
    for key in ("domain", "upstream_host", "upstream_port", "ssl_cert_file", "ssl_key_file", "config_text"):
        config[key] = str(config.get(key) or "").strip()
    if not config["domain"]:
        raise ValueError("反向代理必须填写外网域名，例如 kb.example.com。")
    if not config["upstream_host"]:
        raise ValueError("反向代理必须填写后端服务地址，例如 127.0.0.1。")
    if not config["upstream_port"].isdigit():
        raise ValueError("反向代理后端端口必须是数字，例如 8000。")
    port = int(config["upstream_port"])
    if port < 1 or port > 65535:
        raise ValueError("反向代理后端端口必须在 1 到 65535 之间。")
    config["force_https"] = _truthy(config.get("force_https"))
    if not config["config_text"]:
        config["config_text"] = generate_reverse_proxy_config(config)
    return config


def _runtime_upstream_host(config: dict[str, Any]) -> str:
    host = str(config.get("upstream_host") or "127.0.0.1").strip()
    if is_docker_deployment() and host in {"127.0.0.1", "localhost"}:
        return "mcp-gateway"
    return host


def reverse_proxy_runtime_env(config: dict[str, Any] | None, enabled: bool) -> dict[str, Any]:
    values: dict[str, Any] = {REVERSE_PROXY_ENABLED_KEY: "true" if enabled else "false"}
    if not enabled or not config:
        for key in REVERSE_PROXY_RUNTIME_KEYS - {REVERSE_PROXY_ENABLED_KEY}:
            values[key] = ""
        return values
    values.update({
        "REVERSE_PROXY_DOMAIN": config.get("domain", ""),
        "REVERSE_PROXY_UPSTREAM_HOST": _runtime_upstream_host(config),
        "REVERSE_PROXY_UPSTREAM_PORT": config.get("upstream_port", "8000"),
        "REVERSE_PROXY_SSL_CERT_FILE": config.get("ssl_cert_file", ""),
        "REVERSE_PROXY_SSL_KEY_FILE": config.get("ssl_key_file", ""),
        "REVERSE_PROXY_FORCE_HTTPS": "true" if _truthy(config.get("force_https")) else "false",
    })
    return values


def list_reverse_proxy_configs() -> tuple[list[dict[str, Any]], str]:
    env = read_env()
    active_id = env.get(ACTIVE_REVERSE_PROXY_CONFIG_KEY, "")
    raw = env.get(REVERSE_PROXY_CONFIGS_KEY, "")
    configs: list[dict[str, Any]] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                configs = [normalize_reverse_proxy_config(item) for item in parsed if isinstance(item, dict)]
        except Exception:
            configs = []
    return configs, active_id


def get_reverse_proxy_service_state() -> dict[str, Any]:
    env = read_env()
    configs, active_id = list_reverse_proxy_configs()
    active_config = next((item for item in configs if item["id"] == active_id), None)
    enabled = _bool_env(env.get(REVERSE_PROXY_ENABLED_KEY), default=True)
    return {
        "enabled": enabled,
        "active_id": active_id,
        "active_config": active_config,
        "runtime_host": _runtime_upstream_host(active_config) if active_config else "",
        "runtime_port": active_config.get("upstream_port", "") if active_config else "",
        "docker_deployment": is_docker_deployment(),
    }


def save_reverse_proxy_config(data: dict[str, Any]) -> dict[str, Any]:
    configs, active_id = list_reverse_proxy_configs()
    config_id = str(data.get("id") or "")
    existing = next((item for item in configs if item["id"] == config_id), None)
    config = normalize_reverse_proxy_config(data, existing)
    if existing:
        configs = [config if item["id"] == config["id"] else item for item in configs]
    else:
        configs.append(config)
    values = {REVERSE_PROXY_CONFIGS_KEY: json.dumps(configs, ensure_ascii=False)}
    if not active_id:
        values[ACTIVE_REVERSE_PROXY_CONFIG_KEY] = config["id"]
    elif active_id == config["id"] and get_reverse_proxy_service_state()["enabled"]:
        values.update(reverse_proxy_runtime_env(config, True))
    write_env_values(values)
    return config


def activate_reverse_proxy_config(config_id: str) -> dict[str, Any]:
    configs, _ = list_reverse_proxy_configs()
    config = next((item for item in configs if item["id"] == config_id), None)
    if not config:
        raise ValueError("反向代理配置不存在或已被删除。")
    values = {ACTIVE_REVERSE_PROXY_CONFIG_KEY: config_id}
    if get_reverse_proxy_service_state()["enabled"]:
        values.update(reverse_proxy_runtime_env(config, True))
    write_env_values(values)
    return config


def set_reverse_proxy_service_enabled(enabled: bool, config_id: str | None = None) -> dict[str, Any]:
    configs, active_id = list_reverse_proxy_configs()
    target_id = config_id or active_id
    config = next((item for item in configs if item["id"] == target_id), None)
    if enabled and not config:
        raise ValueError("Please create and activate a reverse proxy config before enabling the service.")
    values = {ACTIVE_REVERSE_PROXY_CONFIG_KEY: target_id or ""}
    values.update(reverse_proxy_runtime_env(config, enabled))
    write_env_values(values)
    return get_reverse_proxy_service_state()


def apply_reverse_proxy_config(data: dict[str, Any]) -> dict[str, Any]:
    config = save_reverse_proxy_config({**data, "enabled": True})
    activate_reverse_proxy_config(config["id"])
    state = set_reverse_proxy_service_enabled(True, config["id"])
    return {"config": config, "state": state}


def delete_reverse_proxy_config(config_id: str) -> bool:
    configs, active_id = list_reverse_proxy_configs()
    kept = [item for item in configs if item["id"] != config_id]
    if len(kept) == len(configs):
        return False
    values = {REVERSE_PROXY_CONFIGS_KEY: json.dumps(kept, ensure_ascii=False)}
    if active_id == config_id:
        next_config = kept[0] if kept else None
        values[ACTIVE_REVERSE_PROXY_CONFIG_KEY] = next_config["id"] if next_config else ""
        values.update(reverse_proxy_runtime_env(next_config, bool(next_config) and get_reverse_proxy_service_state()["enabled"]))
    write_env_values(values)
    return True


async def restart_current_service(delay_seconds: float = 1.0) -> None:
    if is_docker_deployment():
        raise RuntimeError("Docker 部署下不能从容器内部直接重启服务，请在宿主机执行 docker compose restart mcp-gateway。")
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
