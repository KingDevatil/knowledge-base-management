"""DDNS service management and update loop."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from logger import get_logger

DDNS_SERVICES_KEY = "kb:config:ddns:services"
DDNS_LEGACY_KEY = "kb:config:ddns"

DDNS_PROVIDERS = {"cloudflare", "dnspod", "aliyun", "tencentcloud", "custom"}
DDNS_PROVIDER_LABELS = {
    "cloudflare": "Cloudflare",
    "dnspod": "DNSPod",
    "aliyun": "Aliyun DNS",
    "tencentcloud": "Tencent Cloud DNSPod",
    "custom": "Custom API",
}
DDNS_RECORD_TYPES = {"A", "AAAA", "CNAME"}

DEFAULT_SERVICE = {
    "id": "",
    "enabled": False,
    "provider": "cloudflare",
    "domain": "",
    "record_name": "",
    "record_type": "A",
    "ttl": 600,
    "update_interval_minutes": 5,
    "endpoint": "",
    "access_key": "",
    "api_token": "",
    "ipv4_enabled": True,
    "ipv4_mode": "auto",
    "ipv4_address": "",
    "ipv6_enabled": False,
    "ipv6_mode": "auto",
    "ipv6_address": "",
    "status": "not_configured",
    "last_ip": "",
    "last_update_at": "",
    "last_checked_at": "",
    "last_error": "",
}

logger = get_logger()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_service(service: dict[str, Any]) -> dict[str, Any]:
    item = {k: v for k, v in service.items() if k != "api_token"}
    item["has_token"] = bool(service.get("api_token"))
    item["provider_label"] = DDNS_PROVIDER_LABELS.get(item.get("provider"), item.get("provider", ""))
    return item


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def normalize_service(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    service = {**DEFAULT_SERVICE, **existing}
    service["id"] = str(data.get("id") or service.get("id") or uuid.uuid4())
    service["enabled"] = _bool(data.get("enabled", service.get("enabled", False)))

    provider = str(data.get("provider", service.get("provider", "cloudflare"))).strip().lower()
    if provider not in DDNS_PROVIDERS:
        raise ValueError("Unsupported DDNS provider")
    service["provider"] = provider

    record_type = str(data.get("record_type", service.get("record_type", "A"))).strip().upper()
    if record_type not in DDNS_RECORD_TYPES:
        raise ValueError("Unsupported record type")
    service["record_type"] = record_type

    for key in ("domain", "record_name", "endpoint", "access_key", "ipv4_address", "ipv6_address"):
        service[key] = str(data.get(key, service.get(key, ""))).strip()

    service["ipv4_enabled"] = _bool(data.get("ipv4_enabled", service.get("ipv4_enabled", True)))
    service["ipv6_enabled"] = _bool(data.get("ipv6_enabled", service.get("ipv6_enabled", False)))

    token = str(data.get("api_token", "")).strip()
    if token:
        service["api_token"] = token
    elif _bool(data.get("clear_api_token")):
        service["api_token"] = ""

    for key, default, min_value, max_value in (
        ("ttl", 600, 60, 86400),
        ("update_interval_minutes", 5, 1, 1440),
    ):
        try:
            value = int(data.get(key, service.get(key, default)))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        service[key] = max(min_value, min(max_value, value))

    for key in ("ipv4_mode", "ipv6_mode"):
        mode = str(data.get(key, service.get(key, "auto"))).strip().lower()
        if mode not in {"auto", "custom", "disabled"}:
            raise ValueError(f"{key} is invalid")
        service[key] = mode

    if not service["domain"]:
        raise ValueError("Domain is required")
    if not service["record_name"]:
        service["record_name"] = "@"
    if not service["ipv4_enabled"] and not service["ipv6_enabled"]:
        raise ValueError("IPv4 or IPv6 DDNS must be enabled")

    return service


async def _load_raw_services(redis) -> list[dict[str, Any]]:
    raw = await redis.get(DDNS_SERVICES_KEY)
    if raw:
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return [dict(item) for item in items if isinstance(item, dict)]
        except Exception:
            logger.warning("Failed to parse DDNS services JSON")

    legacy = await redis.hgetall(DDNS_LEGACY_KEY)
    if legacy:
        migrated = normalize_service({**legacy, "id": str(uuid.uuid4())})
        await _save_raw_services(redis, [migrated])
        return [migrated]
    return []


async def _save_raw_services(redis, services: list[dict[str, Any]]) -> None:
    await redis.set(DDNS_SERVICES_KEY, json.dumps(services, ensure_ascii=True))


async def list_services(redis) -> list[dict[str, Any]]:
    return await _load_raw_services(redis)


async def save_service(redis, data: dict[str, Any]) -> dict[str, Any]:
    services = await _load_raw_services(redis)
    service_id = str(data.get("id") or "")
    existing = next((item for item in services if item.get("id") == service_id), None)
    service = normalize_service(data, existing)
    if existing:
        services = [service if item.get("id") == service["id"] else item for item in services]
    else:
        services.append(service)
    await _save_raw_services(redis, services)
    return service


async def delete_service(redis, service_id: str) -> bool:
    services = await _load_raw_services(redis)
    kept = [item for item in services if item.get("id") != service_id]
    if len(kept) == len(services):
        return False
    await _save_raw_services(redis, kept)
    return True


async def update_service_state(redis, service_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    services = await _load_raw_services(redis)
    found = None
    for item in services:
        if item.get("id") == service_id:
            item.update(patch)
            found = item
            break
    if found:
        await _save_raw_services(redis, services)
    return found


async def get_public_ip(version: int) -> str:
    url = "https://api.ipify.org?format=json" if version == 4 else "https://api64.ipify.org?format=json"
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return str(resp.json().get("ip", "")).strip()


async def resolve_target_value(service: dict[str, Any], record_type: str) -> str:
    if record_type == "CNAME":
        value = service.get("ipv4_address") or service.get("ipv6_address")
        if not value:
            raise ValueError("CNAME target is required")
        return str(value).strip()

    if record_type == "AAAA":
        mode = service.get("ipv6_mode", "auto")
        custom_value = service.get("ipv6_address", "")
        if not service.get("ipv6_enabled"):
            raise ValueError("IPv6 DDNS is disabled")
        ip_version = 6
    else:
        mode = service.get("ipv4_mode", "auto")
        custom_value = service.get("ipv4_address", "")
        if not service.get("ipv4_enabled"):
            raise ValueError("IPv4 DDNS is disabled")
        ip_version = 4

    if mode == "disabled":
        raise ValueError(f"{record_type} is disabled")
    if mode == "custom":
        if not custom_value:
            raise ValueError(f"{record_type} custom address is required")
        return str(custom_value).strip()
    return await get_public_ip(ip_version)


def full_record_name(service: dict[str, Any]) -> str:
    record = str(service.get("record_name") or "@").strip()
    domain = str(service.get("domain") or "").strip()
    if record == "@":
        return domain
    if record.endswith(f".{domain}") or record == domain:
        return record
    return f"{record}.{domain}"


def _record_name_for_provider(service: dict[str, Any]) -> str:
    record = str(service.get("record_name") or "@").strip()
    return "@" if record == service.get("domain") else record


async def _http_json(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict[str, Any]:
    resp = await client.request(method, url, **kwargs)
    resp.raise_for_status()
    if not resp.content:
        return {}
    return resp.json()


async def update_cloudflare(service: dict[str, Any], record_type: str, value: str) -> dict[str, Any]:
    token = service.get("api_token")
    if not token:
        raise ValueError("Cloudflare API Token is required")

    domain = service["domain"]
    name = full_record_name(service)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        zones_data = await _http_json(
            client,
            "GET",
            "https://api.cloudflare.com/client/v4/zones",
            headers=headers,
            params={"name": domain, "status": "active"},
        )
        zones = zones_data.get("result", [])
        if not zones:
            raise ValueError("Cloudflare zone not found or token has no permission")
        zone_id = zones[0]["id"]

        records_data = await _http_json(
            client,
            "GET",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers=headers,
            params={"type": record_type, "name": name},
        )
        records = records_data.get("result", [])
        payload = {
            "type": record_type,
            "name": name,
            "content": value,
            "ttl": int(service.get("ttl", 600)),
            "proxied": False,
        }
        if records:
            result = await _http_json(
                client,
                "PUT",
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{records[0]['id']}",
                headers=headers,
                json=payload,
            )
        else:
            result = await _http_json(
                client,
                "POST",
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
                headers=headers,
                json=payload,
            )
        if not result.get("success", False):
            raise ValueError(str(result.get("errors") or "Cloudflare update failed"))
    return {"record": name, "record_type": record_type, "value": value}


async def probe_cloudflare(service: dict[str, Any]) -> None:
    token = service.get("api_token")
    if not token:
        raise ValueError("Cloudflare API Token is required")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        data = await _http_json(
            client,
            "GET",
            "https://api.cloudflare.com/client/v4/zones",
            headers=headers,
            params={"name": service["domain"], "status": "active"},
        )
    if not data.get("result"):
        raise ValueError("Cloudflare zone not found or token has no permission")


async def update_dnspod(service: dict[str, Any], record_type: str, value: str) -> dict[str, Any]:
    # DNSPod legacy OpenAPI uses login_token="<id>,<token>".
    token_id = service.get("access_key")
    token = service.get("api_token")
    if not token_id or not token:
        raise ValueError("DNSPod token ID and token are required")

    domain = service["domain"]
    sub_domain = _record_name_for_provider(service)
    data = {
        "login_token": f"{token_id},{token}",
        "format": "json",
        "domain": domain,
        "sub_domain": sub_domain,
        "record_type": record_type,
    }
    headers = {"User-Agent": "knowledge-base-management-ddns/1.0"}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        list_resp = await _http_json(
            client,
            "POST",
            "https://api.dnspod.com/Record.List",
            headers=headers,
            data=data,
        )
        records = list_resp.get("records") or []
        record = next((item for item in records if item.get("type") == record_type and item.get("name") == sub_domain), None)
        common = {
            "login_token": f"{token_id},{token}",
            "format": "json",
            "domain": domain,
            "sub_domain": sub_domain,
            "record_type": record_type,
            "record_line": "default",
            "value": value,
            "ttl": str(int(service.get("ttl", 600))),
        }
        if record:
            endpoint = "https://api.dnspod.com/Record.Modify"
            common["record_id"] = record["id"]
        else:
            endpoint = "https://api.dnspod.com/Record.Create"
        result = await _http_json(client, "POST", endpoint, headers=headers, data=common)
        if str(result.get("status", {}).get("code")) != "1":
            raise ValueError(result.get("status", {}).get("message") or "DNSPod update failed")
    return {"record": full_record_name(service), "record_type": record_type, "value": value}


async def probe_dnspod(service: dict[str, Any]) -> None:
    token_id = service.get("access_key")
    token = service.get("api_token")
    if not token_id or not token:
        raise ValueError("DNSPod token ID and token are required")
    headers = {"User-Agent": "knowledge-base-management-ddns/1.0"}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        result = await _http_json(
            client,
            "POST",
            "https://api.dnspod.com/Record.List",
            headers=headers,
            data={
                "login_token": f"{token_id},{token}",
                "format": "json",
                "domain": service["domain"],
                "sub_domain": _record_name_for_provider(service),
            },
        )
    if str(result.get("status", {}).get("code")) not in {"1", "10"}:
        raise ValueError(result.get("status", {}).get("message") or "DNSPod credential check failed")


def _aliyun_percent_encode(value: Any) -> str:
    return quote(str(value), safe="~")


def _aliyun_sign(params: dict[str, Any], secret: str) -> str:
    sorted_query = "&".join(f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(params[k])}" for k in sorted(params))
    string_to_sign = f"GET&%2F&{_aliyun_percent_encode(sorted_query)}"
    digest = hmac.new(f"{secret}&".encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def _aliyun_request(service: dict[str, Any], action: str, params: dict[str, Any]) -> dict[str, Any]:
    access_key = service.get("access_key")
    secret = service.get("api_token")
    if not access_key or not secret:
        raise ValueError("Aliyun AccessKey ID and Secret are required")
    common = {
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Action": action,
    }
    all_params = {**common, **params}
    all_params["Signature"] = _aliyun_sign(all_params, secret)
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        return await _http_json(client, "GET", "https://alidns.aliyuncs.com/", params=all_params)


async def update_aliyun(service: dict[str, Any], record_type: str, value: str) -> dict[str, Any]:
    rr = _record_name_for_provider(service)
    list_data = await _aliyun_request(service, "DescribeDomainRecords", {
        "DomainName": service["domain"],
        "RRKeyWord": rr,
        "Type": record_type,
    })
    records = list_data.get("DomainRecords", {}).get("Record", [])
    record = next((item for item in records if item.get("RR") == rr and item.get("Type") == record_type), None)
    if record:
        await _aliyun_request(service, "UpdateDomainRecord", {
            "RecordId": record["RecordId"],
            "RR": rr,
            "Type": record_type,
            "Value": value,
            "TTL": int(service.get("ttl", 600)),
        })
    else:
        await _aliyun_request(service, "AddDomainRecord", {
            "DomainName": service["domain"],
            "RR": rr,
            "Type": record_type,
            "Value": value,
            "TTL": int(service.get("ttl", 600)),
        })
    return {"record": full_record_name(service), "record_type": record_type, "value": value}


async def probe_aliyun(service: dict[str, Any]) -> None:
    await _aliyun_request(service, "DescribeDomainRecords", {
        "DomainName": service["domain"],
        "RRKeyWord": _record_name_for_provider(service),
    })


def _tc3_sign(secret_key: str, date: str, service: str, string_to_sign: str) -> str:
    secret_date = hmac.new(("TC3" + secret_key).encode(), date.encode(), hashlib.sha256).digest()
    secret_service = hmac.new(secret_date, service.encode(), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
    return hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()


async def tencent_request(service: dict[str, Any], action: str, payload: dict[str, Any]) -> dict[str, Any]:
    secret_id = service.get("access_key")
    secret_key = service.get("api_token")
    if not secret_id or not secret_key:
        raise ValueError("Tencent Cloud SecretId and SecretKey are required")

    host = "dnspod.tencentcloudapi.com"
    endpoint = f"https://{host}"
    tc_service = "dnspod"
    version = "2021-03-23"
    timestamp = int(time.time())
    date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    hashed_request_payload = hashlib.sha256(body.encode()).hexdigest()
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        canonical_headers,
        signed_headers,
        hashed_request_payload,
    ])
    credential_scope = f"{date}/{tc_service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = _tc3_sign(secret_key, date, tc_service, string_to_sign)
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
    }
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        data = await _http_json(client, "POST", endpoint, headers=headers, content=body.encode())
    response = data.get("Response", {})
    if "Error" in response:
        err = response["Error"]
        raise ValueError(f"{err.get('Code')}: {err.get('Message')}")
    return response


async def update_tencentcloud(service: dict[str, Any], record_type: str, value: str) -> dict[str, Any]:
    sub_domain = _record_name_for_provider(service)
    list_data = await tencent_request(service, "DescribeRecordList", {
        "Domain": service["domain"],
        "Subdomain": sub_domain,
        "RecordType": record_type,
    })
    records = list_data.get("RecordList", [])
    record = next((item for item in records if item.get("Type") == record_type and item.get("Name") == sub_domain), None)
    payload = {
        "Domain": service["domain"],
        "SubDomain": sub_domain,
        "RecordType": record_type,
        "RecordLine": "default",
        "Value": value,
        "TTL": int(service.get("ttl", 600)),
    }
    if record:
        payload["RecordId"] = int(record["RecordId"])
        await tencent_request(service, "ModifyRecord", payload)
    else:
        await tencent_request(service, "CreateRecord", payload)
    return {"record": full_record_name(service), "record_type": record_type, "value": value}


async def probe_tencentcloud(service: dict[str, Any]) -> None:
    await tencent_request(service, "DescribeRecordList", {
        "Domain": service["domain"],
        "Subdomain": _record_name_for_provider(service),
    })


async def update_custom(service: dict[str, Any], record_type: str, value: str) -> dict[str, Any]:
    endpoint = service.get("endpoint")
    if not endpoint:
        raise ValueError("Custom API endpoint is required")
    payload = {
        "domain": service["domain"],
        "record_name": service["record_name"],
        "record_type": record_type,
        "value": value,
        "ttl": int(service.get("ttl", 600)),
        "access_key": service.get("access_key", ""),
        "api_token": service.get("api_token", ""),
    }
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        resp = await client.post(endpoint, json=payload)
        resp.raise_for_status()
    return {"record": full_record_name(service), "record_type": record_type, "value": value}


async def update_one_record(service: dict[str, Any], record_type: str, value: str) -> dict[str, Any]:
    provider = service.get("provider")
    if provider == "cloudflare":
        return await update_cloudflare(service, record_type, value)
    if provider == "dnspod":
        return await update_dnspod(service, record_type, value)
    if provider == "aliyun":
        return await update_aliyun(service, record_type, value)
    if provider == "tencentcloud":
        return await update_tencentcloud(service, record_type, value)
    if provider == "custom":
        return await update_custom(service, record_type, value)
    raise ValueError(f"Unsupported DDNS provider: {provider}")


async def probe_provider(service: dict[str, Any]) -> None:
    provider = service.get("provider")
    if provider == "cloudflare":
        await probe_cloudflare(service)
    elif provider == "dnspod":
        await probe_dnspod(service)
    elif provider == "aliyun":
        await probe_aliyun(service)
    elif provider == "tencentcloud":
        await probe_tencentcloud(service)
    elif provider == "custom":
        if not service.get("endpoint"):
            raise ValueError("Custom API endpoint is required")
    else:
        raise ValueError(f"Unsupported DDNS provider: {provider}")


async def update_service(redis, service: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    updates = []
    if service.get("ipv4_enabled"):
        value = await resolve_target_value(service, "A")
        updates.append(await update_one_record(service, "A", value))
    if service.get("ipv6_enabled"):
        value = await resolve_target_value(service, "AAAA")
        updates.append(await update_one_record(service, "AAAA", value))
    if not updates:
        raise ValueError("No IP version is enabled for DDNS")

    last_ip = " / ".join(item["value"] for item in updates)
    patch = {
        "status": "updated",
        "last_ip": last_ip,
        "last_update_at": utc_now(),
        "last_checked_at": utc_now(),
        "last_error": "",
    }
    if persist:
        await update_service_state(redis, service["id"], patch)
    return {"success": True, "updates": updates, **patch}


async def test_service(redis, service: dict[str, Any]) -> dict[str, Any]:
    try:
        values = []
        if service.get("ipv4_enabled"):
            values.append(await resolve_target_value(service, "A"))
        if service.get("ipv6_enabled"):
            values.append(await resolve_target_value(service, "AAAA"))
        if not values:
            raise ValueError("No IP version is enabled for DDNS")
        if service.get("provider") in {"cloudflare", "dnspod", "aliyun", "tencentcloud"}:
            if not service.get("access_key") and service.get("provider") in {"dnspod", "aliyun", "tencentcloud"}:
                raise ValueError("Access Key / account is required")
            if not service.get("api_token"):
                raise ValueError("API token / secret is required")
        if service.get("provider") == "custom" and not service.get("endpoint"):
            raise ValueError("Custom API endpoint is required")
        await probe_provider(service)
        patch = {
            "status": "reachable",
            "last_ip": " / ".join(values),
            "last_checked_at": utc_now(),
            "last_error": "",
        }
        await update_service_state(redis, service["id"], patch)
        return {"success": True, "status": "reachable", "message": "Configuration looks valid", "ip": patch["last_ip"]}
    except Exception as exc:
        await update_service_state(redis, service["id"], {
            "status": "error",
            "last_checked_at": utc_now(),
            "last_error": str(exc),
        })
        return {"success": False, "status": "error", "message": str(exc)}


async def update_due_services(redis) -> None:
    services = await _load_raw_services(redis)
    now = datetime.now(timezone.utc)
    for service in services:
        if not service.get("enabled"):
            continue
        interval = int(service.get("update_interval_minutes") or 5)
        last_raw = service.get("last_update_at") or service.get("last_checked_at")
        due = True
        if last_raw:
            try:
                last = datetime.fromisoformat(str(last_raw))
                due = (now - last).total_seconds() >= interval * 60
            except Exception:
                due = True
        if not due:
            continue
        try:
            await update_service(redis, service)
        except Exception as exc:
            await update_service_state(redis, service["id"], {
                "status": "error",
                "last_checked_at": utc_now(),
                "last_error": str(exc),
            })


async def ddns_update_loop(redis, stop_event: asyncio.Event) -> None:
    logger.info("DDNS update loop started")
    while not stop_event.is_set():
        try:
            await update_due_services(redis)
        except Exception as exc:
            logger.warning(f"DDNS update loop failed: {exc}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
    logger.info("DDNS update loop stopped")
