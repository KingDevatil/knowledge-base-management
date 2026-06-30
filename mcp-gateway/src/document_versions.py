import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentVersionStore:
    def __init__(self, kbdata_dir: str):
        self.base_dir = Path(kbdata_dir or "kbdata").resolve() / "versions"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_version(
        self,
        doc_id: str,
        title: str,
        content: str,
        path: str = "",
        tags: list[str] | None = None,
        created_by: str = "system",
        reason: str = "manual",
    ) -> dict[str, Any]:
        version_id = f"v-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        data = {
            "version_id": version_id,
            "doc_id": doc_id,
            "title": title,
            "content": content,
            "path": path,
            "tags": tags or [],
            "created_at": utc_now(),
            "created_by": created_by,
            "reason": reason,
        }
        folder = self.base_dir / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{version_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def list_versions(self, doc_id: str) -> list[dict[str, Any]]:
        folder = self.base_dir / doc_id
        if not folder.exists():
            return []
        versions = []
        for file_path in folder.glob("*.json"):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            versions.append({k: v for k, v in data.items() if k != "content"})
        versions.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return versions

    def get_version(self, doc_id: str, version_id: str) -> dict[str, Any]:
        path = self.base_dir / doc_id / f"{version_id}.json"
        if not path.exists():
            raise FileNotFoundError("Document version not found")
        return json.loads(path.read_text(encoding="utf-8"))
