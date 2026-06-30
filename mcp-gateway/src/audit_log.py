import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLogger:
    def __init__(self, kbdata_dir: str):
        self.path = Path(kbdata_dir or "kbdata").resolve() / "config" / "audit_logs.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        action: str,
        actor_type: str = "system",
        actor: str = "system",
        target_type: str = "",
        target_id: str = "",
        path: str = "",
        success: bool = True,
        detail: dict[str, Any] | None = None,
        ip: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        entry = {
            "id": f"audit-{uuid.uuid4().hex}",
            "timestamp": utc_now(),
            "actor_type": actor_type,
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "path": path,
            "success": success,
            "detail": detail or {},
            "ip": ip,
            "user_agent": user_agent,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def list_logs(
        self,
        action: str = "",
        actor: str = "",
        target_type: str = "",
        success: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if action and item.get("action") != action:
                        continue
                    if actor and actor not in item.get("actor", ""):
                        continue
                    if target_type and item.get("target_type") != target_type:
                        continue
                    if success is not None and bool(item.get("success")) is not success:
                        continue
                    rows.append(item)
        rows.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return {"logs": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}


def actor_from_api_key(api_key_info: Any | None) -> tuple[str, str]:
    if api_key_info is None:
        return "system", "system"
    return "api_key", getattr(api_key_info, "key_prefix", "") or getattr(api_key_info, "applicant", "api_key")
