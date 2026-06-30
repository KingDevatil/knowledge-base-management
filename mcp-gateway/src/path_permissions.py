from typing import Any


def normalize_path(path: str) -> str:
    return str(path or "").strip().strip("/").replace("\\", "/")


def parse_allowed_paths(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [normalize_path(item) for item in raw]
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [normalize_path(item) for item in parsed]
        except json.JSONDecodeError:
            pass
        return [normalize_path(item) for item in raw.split(",") if normalize_path(item)]
    return []


def has_path_access(api_key_info: Any, path: str) -> bool:
    mode = getattr(api_key_info, "path_mode", "all") or "all"
    if mode == "all":
        return True
    target = normalize_path(path)
    allowed_paths = [normalize_path(item) for item in getattr(api_key_info, "allowed_paths", []) or []]
    if not allowed_paths:
        return False
    for allowed in allowed_paths:
        if allowed == "":
            return True
        if target == allowed or target.startswith(allowed + "/"):
            return True
    return False


def filter_docs_by_path_access(api_key_info: Any, docs: list[dict]) -> list[dict]:
    return [doc for doc in docs if has_path_access(api_key_info, str(doc.get("path", "")))]
