import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_DIR_NAME = "config"
DATA_DIR_NAMES = ("sources", "minio", "chroma", "graph")
BACKUP_POLICY_FILE = "backup_policy.json"
BACKUP_INDEX_FILE = "backup_index.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_policy() -> dict[str, Any]:
    return {
        "enabled": False,
        "config_enabled": True,
        "config_interval_hours": 24,
        "data_enabled": False,
        "data_interval_hours": 168,
        "full_enabled": False,
        "full_interval_hours": 168,
        "include_secrets": True,
        "retention_count": 7,
        "last_config_backup_at": "",
        "last_data_backup_at": "",
        "last_full_backup_at": "",
    }


@dataclass
class BackupTask:
    task_id: str
    kind: str
    status: str = "pending"
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str = ""
    finished_at: str = ""
    backup_id: str = ""
    filename: str = ""
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "backup_id": self.backup_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "error": self.error,
        }


@dataclass
class RestoreTask:
    task_id: str
    backup_id: str
    status: str = "pending"
    mode: str = "merge"
    conflict_policy: str = "skip"
    restore_config: bool = True
    restore_data: bool = True
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str = ""
    finished_at: str = ""
    stage: str = "pending"
    total: int = 0
    processed: int = 0
    current: str = ""
    error: str = ""
    pre_restore_backup_id: str = ""
    requires_restart: bool = False
    requires_reindex: bool = False

    def to_dict(self) -> dict[str, Any]:
        progress = int((self.processed / self.total) * 100) if self.total else 0
        return {
            "task_id": self.task_id,
            "backup_id": self.backup_id,
            "status": self.status,
            "mode": self.mode,
            "conflict_policy": self.conflict_policy,
            "restore_config": self.restore_config,
            "restore_data": self.restore_data,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage": self.stage,
            "total": self.total,
            "processed": self.processed,
            "current": self.current,
            "progress": min(progress, 100),
            "error": self.error,
            "pre_restore_backup_id": self.pre_restore_backup_id,
            "requires_restart": self.requires_restart,
            "requires_reindex": self.requires_reindex,
        }


@dataclass
class ReindexTask:
    task_id: str
    status: str = "pending"
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str = ""
    finished_at: str = ""
    stage: str = "pending"
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    current: str = ""
    error: str = ""
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        progress = int((self.processed / self.total) * 100) if self.total else 0
        return {
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage": self.stage,
            "total": self.total,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "current": self.current,
            "progress": min(progress, 100),
            "error": self.error,
            "failures": self.failures[-20:],
        }


class BackupManager:
    def __init__(
        self,
        kbdata_dir: str,
        app_version: str = "",
        backup_dir: str | None = None,
        redis_client: Any | None = None,
    ):
        base = Path(kbdata_dir or "kbdata").resolve()
        self.kbdata_dir = base
        self.backup_dir = Path(backup_dir).resolve() if backup_dir else base / "backups"
        self.config_dir = base / CONFIG_DIR_NAME
        self.app_version = app_version
        self.redis = redis_client
        self.tasks: dict[str, BackupTask] = {}
        self.restore_tasks: dict[str, RestoreTask] = {}
        self.reindex_tasks: dict[str, ReindexTask] = {}
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    @property
    def policy_path(self) -> Path:
        return self.config_dir / BACKUP_POLICY_FILE

    @property
    def index_path(self) -> Path:
        return self.backup_dir / BACKUP_INDEX_FILE

    def load_policy(self) -> dict[str, Any]:
        policy = default_policy()
        if self.policy_path.exists():
            try:
                raw = json.loads(self.policy_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    policy.update(raw)
            except (OSError, json.JSONDecodeError):
                pass
        return policy

    def save_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        policy = self.load_policy()
        for key in default_policy():
            if key in data:
                policy[key] = data[key]
        for key in ("config_interval_hours", "data_interval_hours", "full_interval_hours", "retention_count"):
            try:
                policy[key] = max(1, int(policy[key]))
            except (TypeError, ValueError):
                policy[key] = default_policy()[key]
        for key in ("enabled", "config_enabled", "data_enabled", "full_enabled", "include_secrets"):
            policy[key] = bool(policy[key])
        self.policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        return policy

    def list_backups(self) -> list[dict[str, Any]]:
        index = self._load_index()
        entries = [item for item in index if self._backup_path(item.get("filename", "")).exists()]
        entries.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return entries

    def get_backup_path(self, backup_id: str) -> Path:
        for item in self.list_backups():
            if item.get("backup_id") == backup_id:
                path = self._backup_path(item.get("filename", ""))
                if path.exists():
                    return path
        raise FileNotFoundError("Backup not found")

    def delete_backup(self, backup_id: str) -> bool:
        index = self._load_index()
        kept = []
        deleted = False
        for item in index:
            if item.get("backup_id") == backup_id:
                path = self._backup_path(item.get("filename", ""))
                if path.exists():
                    path.unlink()
                deleted = True
            else:
                kept.append(item)
        if deleted:
            self._save_index(kept)
        return deleted

    def start_backup(self, kind: str, include_secrets: bool = True) -> BackupTask:
        kind = self._normalize_kind(kind)
        task = BackupTask(task_id=f"backup-{uuid.uuid4().hex}", kind=kind)
        self.tasks[task.task_id] = task
        asyncio.create_task(self._run_task(task, include_secrets))
        return task

    def start_restore(
        self,
        backup_id: str,
        restore_config: bool = True,
        restore_data: bool = True,
        data_mode: str = "merge",
        conflict_policy: str = "skip",
        create_pre_restore_backup: bool = True,
    ) -> RestoreTask:
        if not restore_config and not restore_data:
            raise ValueError("Select config or data to restore")
        data_mode = self._normalize_data_mode(data_mode)
        conflict_policy = self._normalize_conflict_policy(conflict_policy)
        task = RestoreTask(
            task_id=f"restore-{uuid.uuid4().hex}",
            backup_id=backup_id,
            mode=data_mode,
            conflict_policy=conflict_policy,
            restore_config=restore_config,
            restore_data=restore_data,
        )
        self.restore_tasks[task.task_id] = task
        asyncio.create_task(self._run_restore_task(task, create_pre_restore_backup))
        return task

    def start_reindex(self, tools: Any) -> ReindexTask:
        task = ReindexTask(task_id=f"reindex-{uuid.uuid4().hex}")
        self.reindex_tasks[task.task_id] = task
        asyncio.create_task(self._run_reindex_task(task, tools))
        return task

    async def run_backup_now(self, kind: str, include_secrets: bool = True) -> BackupTask:
        task = BackupTask(task_id=f"backup-{uuid.uuid4().hex}", kind=self._normalize_kind(kind))
        self.tasks[task.task_id] = task
        await self._run_task(task, include_secrets)
        return task

    async def run_restore_now(
        self,
        backup_id: str,
        restore_config: bool = True,
        restore_data: bool = True,
        data_mode: str = "merge",
        conflict_policy: str = "skip",
        create_pre_restore_backup: bool = True,
    ) -> RestoreTask:
        task = RestoreTask(
            task_id=f"restore-{uuid.uuid4().hex}",
            backup_id=backup_id,
            mode=self._normalize_data_mode(data_mode),
            conflict_policy=self._normalize_conflict_policy(conflict_policy),
            restore_config=restore_config,
            restore_data=restore_data,
        )
        self.restore_tasks[task.task_id] = task
        await self._run_restore_task(task, create_pre_restore_backup)
        return task

    async def run_due_backups(self) -> list[BackupTask]:
        policy = self.load_policy()
        if not policy.get("enabled"):
            return []
        created: list[BackupTask] = []
        for kind in ("config", "data", "full"):
            if not policy.get(f"{kind}_enabled"):
                continue
            if self._is_due(policy.get(f"last_{kind}_backup_at", ""), int(policy.get(f"{kind}_interval_hours", 24))):
                task = self.start_backup(kind, bool(policy.get("include_secrets", True)))
                created.append(task)
                policy[f"last_{kind}_backup_at"] = utc_now_iso()
        if created:
            self.save_policy(policy)
        return created

    def prune_backups(self, retention_count: int | None = None) -> None:
        keep = max(1, int(retention_count or self.load_policy().get("retention_count", 7)))
        entries = self.list_backups()
        for item in entries[keep:]:
            self.delete_backup(item.get("backup_id", ""))

    async def _run_task(self, task: BackupTask, include_secrets: bool) -> None:
        task.status = "running"
        task.started_at = utc_now_iso()
        try:
            runtime_config = await self._collect_runtime_config(include_secrets)
            result = await asyncio.to_thread(self._create_backup_file, task.kind, include_secrets, runtime_config)
            task.status = "completed"
            task.backup_id = result["backup_id"]
            task.filename = result["filename"]
            task.size_bytes = result["size_bytes"]
            self.prune_backups()
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
        finally:
            task.finished_at = utc_now_iso()

    async def _run_restore_task(self, task: RestoreTask, create_pre_restore_backup: bool) -> None:
        task.status = "running"
        task.started_at = utc_now_iso()
        try:
            task.stage = "validate"
            backup_path = self.get_backup_path(task.backup_id)
            manifest, checksums = await asyncio.to_thread(self._validate_backup_archive, backup_path)
            kind = manifest.get("kind")
            if task.restore_config and kind not in {"config", "full"}:
                raise ValueError("Selected backup does not contain config data")
            if task.restore_data and kind not in {"data", "full"}:
                raise ValueError("Selected backup does not contain knowledge base data")

            if create_pre_restore_backup:
                task.stage = "pre_restore_backup"
                pre_kind = "full" if task.restore_config and task.restore_data else ("config" if task.restore_config else "data")
                pre_task = await self.run_backup_now(pre_kind, include_secrets=True)
                if pre_task.status != "completed":
                    raise RuntimeError(pre_task.error or "Pre-restore backup failed")
                task.pre_restore_backup_id = pre_task.backup_id

            task.stage = "restore"
            await asyncio.to_thread(self._restore_from_archive, backup_path, task, checksums)
            if task.restore_config:
                await self._restore_runtime_config(backup_path)
                task.requires_restart = True
            task.status = "completed"
            task.stage = "completed"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
        finally:
            task.finished_at = utc_now_iso()

    async def _run_reindex_task(self, task: ReindexTask, tools: Any) -> None:
        task.status = "running"
        task.started_at = utc_now_iso()
        try:
            task.stage = "scan"
            docs = await tools.kb._doc_index_all()
            task.total = len(docs)
            task.stage = "reindex"
            for doc in docs:
                doc_id = doc.get("doc_id", "")
                title = doc.get("title", doc_id)
                task.current = title or doc_id
                if not doc_id:
                    task.processed += 1
                    task.failed += 1
                    continue
                try:
                    await tools.reindex_document(doc_id)
                    task.succeeded += 1
                except Exception as exc:
                    task.failed += 1
                    task.failures.append({"doc_id": doc_id, "title": title, "error": str(exc)})
                finally:
                    task.processed += 1
            task.status = "completed" if task.failed == 0 else "failed"
            task.stage = "completed"
            if task.failed:
                task.error = f"{task.failed} documents failed to reindex"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
        finally:
            task.finished_at = utc_now_iso()

    async def _collect_runtime_config(self, include_secrets: bool) -> dict[str, Any]:
        if self.redis is None:
            return {}
        config: dict[str, Any] = {}
        for key in ("kb:config:graph:semantic_threshold", "kb:config:ddns", "kb:config:ddns:services"):
            try:
                value = await self.redis.get(key)
            except Exception:
                continue
            if value is not None:
                config[key] = self._redact_runtime_value(key, value, include_secrets)
        return config

    def _redact_runtime_value(self, key: str, value: Any, include_secrets: bool) -> Any:
        if include_secrets:
            return value
        if key in {"kb:config:ddns", "kb:config:ddns:services"}:
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return "<redacted>"
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict) and item.get("api_token"):
                    item["api_token"] = "<redacted>"
            return items if isinstance(parsed, list) else items[0]
        return value

    def _create_backup_file(self, kind: str, include_secrets: bool, runtime_config: dict[str, Any] | None = None) -> dict[str, Any]:
        backup_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        filename = f"kb-backup-{backup_id}-{kind}.zip"
        path = self.backup_dir / filename
        manifest = {
            "backup_id": backup_id,
            "kind": kind,
            "created_at": utc_now_iso(),
            "app_version": self.app_version,
            "include_secrets": include_secrets,
            "kbdata_dir": str(self.kbdata_dir),
        }
        checksums: dict[str, str] = {}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            included = self._write_backup_contents(zf, kind, include_secrets, checksums, runtime_config or {})
            manifest["included"] = included
            self._write_json(zf, "manifest.json", manifest)
            self._write_json(zf, "checksums.json", checksums)
        stat = path.stat()
        entry = {
            "backup_id": backup_id,
            "kind": kind,
            "filename": filename,
            "created_at": manifest["created_at"],
            "size_bytes": stat.st_size,
            "include_secrets": include_secrets,
        }
        index = [entry] + [item for item in self._load_index() if item.get("backup_id") != backup_id]
        self._save_index(index)
        return entry

    def _validate_backup_archive(self, path: Path) -> tuple[dict[str, Any], dict[str, str]]:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                raise ValueError("Backup manifest.json is missing")
            for name in names:
                self._safe_archive_name(name)
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            checksums = {}
            if "checksums.json" in names:
                checksums = json.loads(zf.read("checksums.json").decode("utf-8"))
            if checksums:
                for name, expected in checksums.items():
                    if name not in names:
                        raise ValueError(f"Backup file missing: {name}")
                    digest = hashlib.sha256(zf.read(name)).hexdigest()
                    if digest != expected:
                        raise ValueError(f"Backup checksum mismatch: {name}")
        return manifest, checksums

    def _restore_from_archive(self, path: Path, task: RestoreTask, checksums: dict[str, str]) -> None:
        with zipfile.ZipFile(path) as zf:
            members = [name for name in zf.namelist() if name not in {"manifest.json", "checksums.json"}]
            selected = []
            if task.restore_config:
                selected.extend([name for name in members if name.startswith("config/")])
            if task.restore_data:
                selected.extend([name for name in members if name.startswith("data/")])
            task.total = len(selected)

            if task.restore_config:
                task.stage = "restore_config"
                self._restore_config(zf, selected, task)
            if task.restore_data:
                task.stage = "restore_data"
                self._restore_data(zf, selected, task)

    def _restore_config(self, zf: zipfile.ZipFile, members: list[str], task: RestoreTask) -> None:
        config_members = [name for name in members if name.startswith("config/") and not name.endswith("/")]
        if not config_members:
            raise ValueError("Backup does not contain config files")
        for child in self.config_dir.iterdir() if self.config_dir.exists() else []:
            if child.name == BACKUP_POLICY_FILE:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        for name in config_members:
            if name == "config/runtime-config.json":
                task.processed += 1
                continue
            task.current = name
            rel = Path(name).relative_to("config")
            target = self.config_dir / rel
            self._write_member(zf, name, target)
            task.processed += 1

    def _restore_data(self, zf: zipfile.ZipFile, members: list[str], task: RestoreTask) -> None:
        data_members = [name for name in members if name.startswith("data/") and not name.endswith("/")]
        if not data_members:
            raise ValueError("Backup does not contain data files")
        if task.mode == "overwrite":
            for data_dir in DATA_DIR_NAMES:
                target = self.kbdata_dir / data_dir
                if target.exists():
                    shutil.rmtree(target)
        for name in data_members:
            task.current = name
            rel = Path(name).relative_to("data")
            top = rel.parts[0] if rel.parts else ""
            if task.mode == "merge" and top == "chroma":
                task.requires_reindex = True
                task.processed += 1
                continue
            target = self.kbdata_dir / rel
            if task.mode == "merge":
                target = self._resolve_merge_target(target, task.conflict_policy)
                if target is None:
                    task.processed += 1
                    continue
                if top in {"sources", "minio"}:
                    task.requires_reindex = True
            self._write_member(zf, name, target)
            task.processed += 1

    async def _restore_runtime_config(self, path: Path) -> None:
        if self.redis is None:
            return
        try:
            with zipfile.ZipFile(path) as zf:
                if "config/runtime-config.json" not in zf.namelist():
                    return
                runtime_config = json.loads(zf.read("config/runtime-config.json").decode("utf-8"))
        except Exception:
            return
        if not isinstance(runtime_config, dict):
            return
        for key, value in runtime_config.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            try:
                await self.redis.set(key, value)
            except Exception:
                continue

    def _write_member(self, zf: zipfile.ZipFile, name: str, target: Path) -> None:
        self._safe_archive_name(name)
        target = target.resolve()
        if self.kbdata_dir not in target.parents and target != self.kbdata_dir:
            raise ValueError("Backup member escapes data directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(name) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    def _resolve_merge_target(self, target: Path, conflict_policy: str) -> Path | None:
        if not target.exists():
            return target
        if conflict_policy == "skip":
            return None
        if conflict_policy == "overwrite":
            return target
        stem = target.stem
        suffix = target.suffix
        parent = target.parent
        for index in range(1, 10000):
            candidate = parent / f"{stem}.restored-{index}{suffix}"
            if not candidate.exists():
                return candidate
        raise ValueError(f"Cannot find available filename for {target.name}")

    def _safe_archive_name(self, name: str) -> None:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe backup member path: {name}")

    def _write_backup_contents(
        self,
        zf: zipfile.ZipFile,
        kind: str,
        include_secrets: bool,
        checksums: dict[str, str],
        runtime_config: dict[str, Any],
    ) -> list[str]:
        included: list[str] = []
        if kind in {"config", "full"}:
            included.append("config")
            self._add_path(zf, self.config_dir, "config", checksums, include_secrets)
            if runtime_config:
                self._write_json(zf, "config/runtime-config.json", runtime_config)
        if kind in {"data", "full"}:
            for name in DATA_DIR_NAMES:
                path = self.kbdata_dir / name
                if path.exists():
                    included.append(name)
                    self._add_path(zf, path, f"data/{name}", checksums, include_secrets)
        return included

    def _add_path(
        self,
        zf: zipfile.ZipFile,
        source: Path,
        arc_prefix: str,
        checksums: dict[str, str],
        include_secrets: bool,
    ) -> None:
        if source.is_file():
            self._add_file(zf, source, arc_prefix, checksums, include_secrets)
            return
        for file_path in source.rglob("*"):
            if not file_path.is_file():
                continue
            if self._should_skip(file_path, include_secrets):
                continue
            arcname = f"{arc_prefix}/{file_path.relative_to(source).as_posix()}"
            self._add_file(zf, file_path, arcname, checksums, include_secrets)

    def _add_file(
        self,
        zf: zipfile.ZipFile,
        file_path: Path,
        arcname: str,
        checksums: dict[str, str],
        include_secrets: bool,
    ) -> None:
        if self._should_skip(file_path, include_secrets):
            return
        zf.write(file_path, arcname)
        checksums[arcname] = self._sha256(file_path)

    def _should_skip(self, file_path: Path, include_secrets: bool) -> bool:
        try:
            if self.backup_dir in file_path.resolve().parents:
                return True
        except OSError:
            return True
        if include_secrets:
            return False
        return file_path.name in {"admin_accounts.json", "api_keys.json", "service.env", BACKUP_POLICY_FILE}

    def _load_index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_index(self, entries: list[dict[str, Any]]) -> None:
        self.index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def _backup_path(self, filename: str) -> Path:
        path = (self.backup_dir / filename).resolve()
        if self.backup_dir not in path.parents and path != self.backup_dir:
            raise ValueError("Invalid backup filename")
        return path

    def _normalize_kind(self, kind: str) -> str:
        kind = str(kind or "").strip().lower()
        if kind not in {"config", "data", "full"}:
            raise ValueError("Backup kind must be config, data, or full")
        return kind

    def _normalize_data_mode(self, mode: str) -> str:
        mode = str(mode or "merge").strip().lower()
        if mode not in {"overwrite", "merge"}:
            raise ValueError("Data restore mode must be overwrite or merge")
        return mode

    def _normalize_conflict_policy(self, policy: str) -> str:
        policy = str(policy or "skip").strip().lower()
        if policy not in {"skip", "overwrite", "rename"}:
            raise ValueError("Conflict policy must be skip, overwrite, or rename")
        return policy

    def _is_due(self, last_at: str, interval_hours: int) -> bool:
        if not last_at:
            return True
        try:
            last = datetime.fromisoformat(str(last_at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return True
        return (time.time() - last) >= interval_hours * 3600

    def _sha256(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_json(self, zf: zipfile.ZipFile, arcname: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        zf.writestr(arcname, payload)


async def backup_scheduler_loop(manager: BackupManager, stop_event: asyncio.Event, interval_seconds: int = 60) -> None:
    while not stop_event.is_set():
        try:
            await manager.run_due_backups()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
