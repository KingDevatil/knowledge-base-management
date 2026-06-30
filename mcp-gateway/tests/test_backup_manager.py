import asyncio
import os
import sys
import zipfile
from types import SimpleNamespace

os.environ["DEBUG"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from backup_manager import BackupManager
from admin import routes_admin_misc


class FakeRequest:
    def __init__(self, manager, body=None):
        self.app = SimpleNamespace(state=SimpleNamespace(backup_manager=manager))
        self._body = body or {}

    async def json(self):
        return self._body


class FakeRedis:
    def __init__(self, values):
        self.values = values

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_config_backup_includes_manifest_and_config_files(tmp_path):
    kbdata = tmp_path / "kbdata"
    config = kbdata / "config"
    config.mkdir(parents=True)
    (config / "service.env").write_text("APP_NAME=test\n", encoding="utf-8")
    (config / "admin_accounts.json").write_text("{}", encoding="utf-8")

    manager = BackupManager(str(kbdata), app_version="test")
    task = await manager.run_backup_now("config", include_secrets=True)

    assert task.status == "completed"
    backup_path = manager.get_backup_path(task.backup_id)
    with zipfile.ZipFile(backup_path) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "checksums.json" in names
    assert "config/service.env" in names
    assert "config/admin_accounts.json" in names


@pytest.mark.asyncio
async def test_backup_can_exclude_secret_config_files(tmp_path):
    kbdata = tmp_path / "kbdata"
    config = kbdata / "config"
    config.mkdir(parents=True)
    (config / "service.env").write_text("SECRET=value\n", encoding="utf-8")
    (config / "api_keys.json").write_text("{}", encoding="utf-8")
    (config / "public.json").write_text("{}", encoding="utf-8")

    manager = BackupManager(str(kbdata))
    task = await manager.run_backup_now("config", include_secrets=False)

    with zipfile.ZipFile(manager.get_backup_path(task.backup_id)) as zf:
        names = set(zf.namelist())
    assert "config/public.json" in names
    assert "config/service.env" not in names
    assert "config/api_keys.json" not in names


@pytest.mark.asyncio
async def test_config_backup_exports_runtime_config_with_redaction(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    redis = FakeRedis({
        "kb:config:graph:semantic_threshold": "0.45",
        "kb:config:ddns:services": '[{"domain":"example.com","api_token":"secret"}]',
    })
    manager = BackupManager(str(kbdata), redis_client=redis)

    task = await manager.run_backup_now("config", include_secrets=False)

    with zipfile.ZipFile(manager.get_backup_path(task.backup_id)) as zf:
        runtime_config = zf.read("config/runtime-config.json").decode("utf-8")
    assert "0.45" in runtime_config
    assert "secret" not in runtime_config
    assert "<redacted>" in runtime_config


@pytest.mark.asyncio
async def test_full_backup_includes_data_directories(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    (kbdata / "sources").mkdir()
    (kbdata / "sources" / "doc.md").write_text("# Doc", encoding="utf-8")
    (kbdata / "graph").mkdir()
    (kbdata / "graph" / "graph.html").write_text("<html></html>", encoding="utf-8")

    manager = BackupManager(str(kbdata))
    task = await manager.run_backup_now("full")

    with zipfile.ZipFile(manager.get_backup_path(task.backup_id)) as zf:
        names = set(zf.namelist())
    assert "data/sources/doc.md" in names
    assert "data/graph/graph.html" in names


@pytest.mark.asyncio
async def test_restore_config_always_overwrites_current_config(tmp_path):
    kbdata = tmp_path / "kbdata"
    config = kbdata / "config"
    config.mkdir(parents=True)
    (config / "service.env").write_text("APP_NAME=old\n", encoding="utf-8")
    manager = BackupManager(str(kbdata))
    backup_task = await manager.run_backup_now("config")
    (config / "service.env").write_text("APP_NAME=new\n", encoding="utf-8")

    restore_task = await manager.run_restore_now(
        backup_task.backup_id,
        restore_config=True,
        restore_data=False,
        create_pre_restore_backup=False,
    )

    assert restore_task.status == "completed"
    assert (config / "service.env").read_text(encoding="utf-8") == "APP_NAME=old\n"
    assert restore_task.requires_restart is True


@pytest.mark.asyncio
async def test_data_only_backup_restore_can_merge_without_config(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    sources = kbdata / "sources"
    sources.mkdir()
    (sources / "doc.md").write_text("old", encoding="utf-8")
    manager = BackupManager(str(kbdata))
    backup_task = await manager.run_backup_now("data")
    (sources / "other.md").write_text("current", encoding="utf-8")
    (sources / "doc.md").write_text("changed", encoding="utf-8")

    restore_task = await manager.run_restore_now(
        backup_task.backup_id,
        restore_config=False,
        restore_data=True,
        data_mode="merge",
        conflict_policy="rename",
        create_pre_restore_backup=False,
    )

    assert restore_task.status == "completed"
    assert (sources / "other.md").exists()
    assert (sources / "doc.restored-1.md").read_text(encoding="utf-8") == "old"
    assert restore_task.requires_reindex is True


@pytest.mark.asyncio
async def test_merge_restore_skips_chroma_and_requires_reindex(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    chroma = kbdata / "chroma"
    chroma.mkdir()
    (chroma / "index.bin").write_text("backup-index", encoding="utf-8")
    manager = BackupManager(str(kbdata))
    backup_task = await manager.run_backup_now("data")
    (chroma / "index.bin").write_text("current-index", encoding="utf-8")

    restore_task = await manager.run_restore_now(
        backup_task.backup_id,
        restore_config=False,
        restore_data=True,
        data_mode="merge",
        create_pre_restore_backup=False,
    )

    assert restore_task.status == "completed"
    assert (chroma / "index.bin").read_text(encoding="utf-8") == "current-index"
    assert restore_task.requires_reindex is True


def test_policy_save_normalizes_values(tmp_path):
    manager = BackupManager(str(tmp_path / "kbdata"))

    policy = manager.save_policy({
        "enabled": True,
        "config_interval_hours": "0",
        "retention_count": "3",
    })

    assert policy["enabled"] is True
    assert policy["config_interval_hours"] == 1
    assert policy["retention_count"] == 3
    assert manager.load_policy()["enabled"] is True


@pytest.mark.asyncio
async def test_backup_api_can_create_task_and_list(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    (kbdata / "config" / "service.env").write_text("APP_NAME=test\n", encoding="utf-8")
    manager = BackupManager(str(kbdata))
    request = FakeRequest(manager)

    response = await routes_admin_misc.api_backups_create(
        request,
        routes_admin_misc.BackupCreateRequest(kind="config", include_secrets=True),
        {"username": "admin"},
    )
    payload = json_response(response)
    assert payload["kind"] == "config"

    task = manager.tasks[payload["task_id"]]
    while task.status in {"pending", "running"}:
        await asyncio.sleep(0.01)

    response = await routes_admin_misc.api_backups_list(request, {"username": "admin"})
    payload = json_response(response)
    assert len(payload["backups"]) == 1
    assert payload["tasks"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_restore_api_starts_restore_task(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    (kbdata / "config" / "service.env").write_text("APP_NAME=test\n", encoding="utf-8")
    manager = BackupManager(str(kbdata))
    backup_task = await manager.run_backup_now("config")
    request = FakeRequest(manager)

    response = await routes_admin_misc.api_backup_restore(
        request,
        backup_task.backup_id,
        routes_admin_misc.BackupRestoreRequest(
            restore_config=True,
            restore_data=False,
            create_pre_restore_backup=False,
        ),
        {"username": "admin"},
    )
    payload = json_response(response)
    assert payload["backup_id"] == backup_task.backup_id
    assert payload["restore_config"] is True


@pytest.mark.asyncio
async def test_reindex_task_tracks_progress(tmp_path):
    class FakeKB:
        async def _doc_index_all(self):
            return [{"doc_id": "a", "title": "A"}, {"doc_id": "b", "title": "B"}]

    class FakeTools:
        def __init__(self):
            self.kb = FakeKB()

        async def reindex_document(self, doc_id):
            if doc_id == "b":
                raise RuntimeError("boom")
            return {"success": True}

    manager = BackupManager(str(tmp_path / "kbdata"))
    task = manager.start_reindex(FakeTools())
    while task.status in {"pending", "running"}:
        await asyncio.sleep(0.01)

    assert task.processed == 2
    assert task.succeeded == 1
    assert task.failed == 1
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_backup_policy_api_saves_policy(tmp_path):
    manager = BackupManager(str(tmp_path / "kbdata"))
    request = FakeRequest(manager, {"enabled": True, "retention_count": 2})

    response = await routes_admin_misc.api_backup_policy_save(request, {"username": "admin"})
    payload = json_response(response)

    assert payload["enabled"] is True
    assert payload["retention_count"] == 2


@pytest.mark.asyncio
async def test_prune_backups_keeps_latest_entries(tmp_path):
    kbdata = tmp_path / "kbdata"
    (kbdata / "config").mkdir(parents=True)
    (kbdata / "config" / "public.json").write_text("{}", encoding="utf-8")
    manager = BackupManager(str(kbdata))

    for _ in range(3):
        await manager.run_backup_now("config")

    manager.prune_backups(retention_count=2)

    assert len(manager.list_backups()) == 2


def json_response(response):
    import json

    return json.loads(response.body.decode("utf-8"))
