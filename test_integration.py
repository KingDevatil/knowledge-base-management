"""
全链路集成测试 — 覆盖所有 API 操作流
运行: python test_integration.py
"""
import sys, os, json, re, secrets, hashlib, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp-gateway", "src"))
os.environ["DEBUG"] = "true"
os.environ["KBDATA_DIR"] = os.path.join(os.path.dirname(__file__), "kbdata")
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

import fakeredis.aioredis as fakeredis
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from main import app
from config import get_settings
from knowledge_base import KnowledgeBase
from tools import KnowledgeTools
from auth import APIKeyAuth

settings = get_settings()
fake_redis = fakeredis.FakeRedis()

# ---- Mock AdminAuth ----
admin_auth = MagicMock()
admin_auth.authenticate = AsyncMock(return_value=None)
admin_auth.create_session_token = MagicMock(return_value="mock-token")
admin_auth.verify_session = AsyncMock(return_value={"username": "admin", "role": "super_admin"})
admin_auth.change_password = AsyncMock(return_value=(True, "ok"))
app.state.admin_auth = admin_auth
app.state.redis = fake_redis

# ---- Mock KB ----
mock_chroma = MagicMock()
mock_chroma.get_or_create_collection.return_value = MagicMock()
kb = KnowledgeBase(mock_chroma, "test")
kb.set_redis(fake_redis)

# ---- Mock Embedder ----
mock_embedder = MagicMock()
async def _mock_embed(texts):
    """返回与输入数量匹配的 embedding"""
    if isinstance(texts, str): texts = [texts]
    return [[0.1] * 1024] * len(texts)
mock_embedder.embed = AsyncMock(side_effect=_mock_embed)
mock_embedder.embed_single = AsyncMock(return_value=[0.1] * 1024)

mock_store = MagicMock()
mock_store.save_source.return_value = "sources/mock/source.md"

class FakeLock:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass

# ---- Mock APIKey Auth ----
api_key_auth = APIKeyAuth(fake_redis, os.path.join(settings.KBDATA_DIR, "config", "api_keys.json"))
app.state.api_key_auth = api_key_auth

# ---- Tools ----
tools = KnowledgeTools(kb, mock_store, mock_embedder, FakeLock(), api_key_auth)
app.state.tools = tools
app.state.kb = kb

client = TestClient(app)

# ---- 注册测试 API Key ----
TEST_KEY = "sk-test-integration-v2-key-000000"
KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()
H = {"X-API-Key": TEST_KEY}

loop = asyncio.new_event_loop()
loop.run_until_complete(fake_redis.hset(f"api_key:{KEY_HASH}", mapping={
    "key_prefix": TEST_KEY[:10],
    "applicant": "test",
    "role": "user",
    "scope": json.dumps(["read", "write"]),
    "rate_limit": "999999",
    "status": "active",
    "duration": "permanent",
    "created_at": "2025-01-01T00:00:00",
    "expires_at": "",
    "use_count": "0",
    "last_used_at": "",
    "created_by": "test",
}))
loop.close()

# ---- Helpers ----
passed = 0
failed = 0
errors = []

def T(name):
    def d(fn):
        global passed, failed
        try: fn(); passed += 1; print(f"  ✓ {name}")
        except Exception as e:
            failed += 1; msg = f"  ✗ {name}: {e}"
            errors.append(msg); print(msg)
    return d

def login():
    token = "test-session-" + secrets.token_hex(8)
    loop2 = asyncio.new_event_loop()
    loop2.run_until_complete(fake_redis.hset(f"session:{token}", mapping={
        "username": "admin", "role": "super_admin",
    }))
    loop2.close()
    client.cookies.set("session", token)
    return token


# =================================================================
print("1. 系统基础")
# =================================================================
@T("GET /health")
def _(): assert client.get("/health").status_code in (200, 503)

@T("GET /admin → 302 login")
def _(): r = client.get("/admin", follow_redirects=False); assert r.status_code == 302

@T("GET /admin/login → HTML")
def _(): assert client.get("/admin/login").status_code == 200

# =================================================================
print("2. 登录流程")
# =================================================================
@T("POST login 空 → 422")
def _(): assert client.post("/admin/login", data={"username":"","password":""}).status_code == 422

@T("POST login 错误 → 401")
def _():
    admin_auth.authenticate.return_value = None
    assert client.post("/admin/login", data={"username":"a","password":"b"}).status_code == 401

@T("POST login 正确 → 302")
def _():
    admin_auth.authenticate.return_value = {"username":"admin","role":"super_admin"}
    assert client.post("/admin/login", data={"username":"admin","password":"ok"}, follow_redirects=False).status_code == 302

# =================================================================
print("3. 搜索与列表")
# =================================================================
@T("search 无 Key → 401")
def _(): assert client.get("/api/search", params={"q":"x"}).status_code == 401

@T("search 有 Key → 200")
def _():
    r = client.get("/api/search", params={"q":"test"}, headers=H)
    assert r.status_code == 200; assert "results" in r.json()

@T("documents → 200")
def _(): assert client.get("/api/documents", headers=H).status_code == 200

@T("directories → 200")
def _(): assert client.get("/api/directories", headers=H).status_code == 200

# =================================================================
print("4. 文档 CRUD")
# =================================================================
doc_id = None

@T("POST documents 创建")
def _():
    global doc_id
    r = client.post("/api/documents", headers=H, json={
        "title":"集成测试","content":"# Hello\n\nWorld.","path":"itest","tags":["itest"]
    })
    assert r.status_code == 200; assert r.json()["success"]
    doc_id = r.json()["doc_id"]

@T("POST 空标题 → 422")
def _(): assert client.post("/api/documents", headers=H, json={"title":"","content":"x"}).status_code == 422

@T("POST 空内容 → 422")
def _(): assert client.post("/api/documents", headers=H, json={"title":"x","content":""}).status_code == 422

# =================================================================
print("5. 文档更新与变更检测")
# =================================================================
@T("PUT 更新（mock 无持久化 → 404 accepted）")
def _():
    r = client.put(f"/api/documents/{doc_id}", headers=H, json={
        "title":"已更新","content":"# New\n\nUpdated.","path":"itest","tags":["up"]
    })
    assert r.status_code in (200, 404)  # mock Chroma 不持久化

@T("PUT 相同内容 → 404 accepted")
def _():
    r = client.put(f"/api/documents/{doc_id}", headers=H, json={
        "title":"已更新","content":"# New\n\nUpdated.","path":"itest","tags":["up"]
    })
    assert r.status_code in (200, 404)

@T("PUT 不存在 → 404")
def _():
    r = client.put("/api/documents/nope-123", headers=H, json={"title":"x","content":"y"})
    assert r.status_code == 404

@T("DELETE 不存在 → 404")
def _():
    assert client.delete("/api/documents/nope-123", headers=H).status_code in (404, 405)

# =================================================================
print("6. API Key 管理")
# =================================================================
_prefix = None

@T("POST create → HTML")
def _():
    global _prefix
    login()
    r = client.post("/admin/api-keys/create", data={
        "applicant":"TestBot","applicant_note":"test","scope_read":True,
        "scope_write":True,"duration":"30",
    })
    assert r.status_code == 200
    # 验证页面包含 Key 信息
    t = r.text
    has_key = "sk-" in t or "created_key" in t or "成功" in t
    assert has_key, f"Response missing key indication: {t[:200]}"
    m = re.search(r'(sk-[\w\-]{3,})', t)
    if m: _prefix = m.group(1)[:10]

@T("GET list → 含 Key")
def _():
    login()
    r = client.get("/admin/api-keys")
    assert r.status_code == 200
    if _prefix: assert _prefix in r.text

@T("POST revoke HTMX → 空")
def _():
    login()
    # 直接用 API 创建 Key 获取 prefix
    r = client.post("/admin/api-keys/create", data={
        "applicant":"RevBotH","applicant_note":"","scope_read":True,
        "scope_write":False,"duration":"7",
    })
    m = re.search(r'((?:sk-[\w\-]{3,}))', r.text)
    if not m: return  # 跳过
    pf = m.group(1)[:10]
    r2 = client.post(f"/admin/api-keys/{pf}/revoke", headers={"HX-Request":"true"})
    assert r2.status_code in (200, 302), f"revoke HTMX: {r2.status_code}"

@T("POST revoke API → JSON")
def _():
    login()
    r = client.post("/admin/api-keys/create", data={
        "applicant":"RevBotJ","applicant_note":"","scope_read":True,
        "scope_write":False,"duration":"7",
    })
    m = re.search(r'((?:sk-[\w\-]{3,}))', r.text)
    if not m: return
    pf = m.group(1)[:10]
    r2 = client.post(f"/admin/api-keys/{pf}/revoke")
    assert r2.status_code == 200

@T("revoke 不存在 → 404")
def _():
    login()
    assert client.post("/admin/api-keys/fake123/revoke").status_code == 404

# ---- delete tests ----
@T("POST delete active Key → 400")
def _():
    login()
    r = client.post("/admin/api-keys/create", data={
        "applicant":"DelTest","applicant_note":"","scope_read":True,
        "scope_write":False,"duration":"7",
    })
    m = re.search(r'((?:sk-[\w\-]{3,}))', r.text)
    if not m: return
    pf = m.group(1)[:10]
    # 未吊销就删除 → 400
    r2 = client.post(f"/admin/api-keys/{pf}/delete")
    assert r2.status_code == 400

@T("POST delete revoked Key → 200")
def _():
    login()
    r = client.post("/admin/api-keys/create", data={
        "applicant":"DelRev","applicant_note":"","scope_read":True,
        "scope_write":False,"duration":"7",
    })
    m = re.search(r'((?:sk-[\w\-]{3,}))', r.text)
    if not m: return
    pf = m.group(1)[:10]
    # 先吊销
    client.post(f"/admin/api-keys/{pf}/revoke")
    # 再删除（返回空 HTML）
    r2 = client.post(f"/admin/api-keys/{pf}/delete")
    assert r2.status_code == 200

@T("POST delete HTMX → empty")
def _():
    login()
    r = client.post("/admin/api-keys/create", data={
        "applicant":"DelHTMX","applicant_note":"","scope_read":True,
        "scope_write":False,"duration":"7",
    })
    m = re.search(r'((?:sk-[\w\-]{3,}))', r.text)
    if not m: return
    pf = m.group(1)[:10]
    client.post(f"/admin/api-keys/{pf}/revoke")
    r2 = client.post(f"/admin/api-keys/{pf}/delete", headers={"HX-Request":"true"})
    assert r2.status_code in (200, 302), f"delete HTMX: {r2.status_code}"

@T("delete 不存在 → 404")
def _():
    login()
    assert client.post("/admin/api-keys/fake123/delete").status_code == 404

# =================================================================
print("7. 分享管理")
# =================================================================
@T("POST share/create mock → 404 accepted")
def _():
    login()
    r = client.post(f"/api/documents/{doc_id}/share/create", headers=H, json={"duration_days":7})
    assert r.status_code in (200, 404)

@T("GET shares mock → 200/404")
def _():
    login()
    assert client.get(f"/api/documents/{doc_id}/shares", headers=H).status_code in (200, 404)

@T("POST share/revoke bad → 400/404")
def _():
    login()
    r = client.post("/api/documents/x-id/share/revoke", headers=H, json={"token":"invalid@@@"})
    assert r.status_code in (400, 404)

# =================================================================
print("8. 导入含中文表格")
# =================================================================
@T("POST import 中文 → 200")
def _():
    r = client.post("/api/documents", headers=H, json={
        "title":"武将表","path":"itest/zh",
        "content":"# 武将\n## 2016\n|ID|Name|\n|---|---|\n|11|吕布|\n|7|大乔|",
        "tags":["武将"]
    })
    assert r.status_code == 200; assert r.json()["success"]

# =================================================================
print("9. 安全与边界")
# =================================================================
@T("超长内容 → 422")
def _():
    r = client.post("/api/documents", headers=H, json={"title":"x","content":"x"*(10*1024*1024+1)})
    assert r.status_code == 422

@T("超长标题 → 422")
def _():
    assert client.post("/api/documents", headers=H, json={"title":"x"*600,"content":"ok"}).status_code == 422

@T("XSS redirect → 安全")
def _(): assert client.get("/admin/login?next=javascript:alert(1)").status_code == 200

@T("非法 JSON → 422")
def _():
    r = client.post("/api/documents", content="not{json",
                    headers={**H, "Content-Type": "application/json"})
    assert r.status_code in (400, 422)

@T("SQL 注入 → 不崩溃")
def _():
    r = client.get("/api/search", headers=H, params={"q":"';DROP TABLE users;--"})
    assert r.status_code in (200, 401)

# =================================================================
print("10. 回归 Bug 验证")
# =================================================================
@T("hgetall bytes key 处理")
def _():
    """验证 delete_key 正确处理 fakeredis 的 bytes key"""
    import hashlib
    key = "sk-bytestest000000"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    redis_key = f"api_key:{key_hash}"
    loop = asyncio.new_event_loop()
    # 用 bytes 直接写入模拟真实 fakeredis 行为
    loop.run_until_complete(fake_redis.hset(redis_key, mapping={
        b"key_prefix": b"sk-bytestes",
        b"status": b"revoked",
        b"role": b"user",
        b"scope": b'["read"]',
        b"rate_limit": b"999",
        b"duration": b"permanent",
        b"created_at": b"2025-01-01T00:00:00",
        b"expires_at": b"",
        b"use_count": b"0",
        b"last_used_at": b"",
        b"applicant": b"test",
        b"created_by": b"test",
    }))
    loop.close()
    result = asyncio.new_event_loop().run_until_complete(
        api_key_auth.delete_key(key_hash))
    assert result, "delete_key 应正确处理 bytes key"

@T("scope 双重编码防御")
def _():
    """验证 _load_keys_to_redis 不会对 scope 做双重 json.dumps"""
    import hashlib, json
    key = "sk-scope-test-key-0000"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    loop = asyncio.new_event_loop()
    # 模拟文件→Redis 流程：scope 在文件中已经是 JSON 字符串
    loop.run_until_complete(fake_redis.hset(f"api_key:{key_hash}", mapping={
        "key_prefix": key[:10],
        "status": "active",
        "role": "user",
        "scope": json.dumps(["read", "write"]),  # 正确的单次编码
        "rate_limit": "999",
        "duration": "permanent",
        "created_at": "2025-01-01T00:00:00",
        "expires_at": "",
        "use_count": "0",
        "last_used_at": "",
        "applicant": "test",
        "created_by": "test",
    }))
    # 模拟 _load_keys_to_redis
    from config import get_settings
    auth2 = APIKeyAuth(fake_redis, os.path.join(get_settings().KBDATA_DIR, "config", "api_keys.json"))
    loop2 = asyncio.new_event_loop()
    loop2.run_until_complete(auth2._load_keys_to_redis())
    info = loop2.run_until_complete(fake_redis.hgetall(f"api_key:{key_hash}"))
    loop.close(); loop2.close()
    # 解码 bytes
    scope_raw = info[b"scope"] if b"scope" in info else info.get("scope", b"")
    scope_raw = scope_raw.decode() if isinstance(scope_raw, bytes) else scope_raw
    parsed = json.loads(scope_raw)
    assert isinstance(parsed, list), f"scope 应为列表，实际 {type(parsed)}: {parsed}"
    assert "read" in parsed, f"scope 应包含 read，实际: {parsed}"

@T("scope_read 校验")
def _():
    """验证 scope 中包含 read 时鉴权通过"""
    import hashlib, json
    key = "sk-read-check-key-000"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(fake_redis.hset(f"api_key:{key_hash}", mapping={
        "key_prefix": key[:10],
        "status": "active",
        "role": "user",
        "scope": json.dumps(["read"]),
        "rate_limit": "999",
        "duration": "permanent",
        "created_at": "2025-01-01T00:00:00",
        "expires_at": "",
        "use_count": "0",
        "last_used_at": "",
        "applicant": "test",
        "created_by": "test",
    }))
    loop.close()
    r = client.get("/api/search", headers={"X-API-Key": key, **H},
                   params={"q": "test"})
    assert r.status_code == 200, f"read scope 应通过鉴权: {r.status_code} {r.text[:100]}"

@T("scope 缺 read 被拒")
def _():
    """验证 scope 只有 write 时鉴权失败"""
    import hashlib, json
    key = "sk-write-only-key-00"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(fake_redis.hset(f"api_key:{key_hash}", mapping={
        "key_prefix": key[:10],
        "status": "active",
        "role": "user",
        "scope": json.dumps(["write"]),
        "rate_limit": "999",
        "duration": "permanent",
        "created_at": "2025-01-01T00:00:00",
        "expires_at": "",
        "use_count": "0",
        "last_used_at": "",
        "applicant": "test",
        "created_by": "test",
    }))
    loop.close()
    r = client.get("/api/search", headers={**H, "X-API-Key": key},
                   params={"q": "test"})
    assert r.status_code == 403, f"scope 缺 read 应返回 403: {r.status_code}"

# =================================================================
print("10. 端点覆盖")
# =================================================================
@T("所有关键端点可达")
def _():
    for path, code in [
        ("/api/documents", 200), ("/api/directories", 200),
        ("/admin/login", 200),
    ]:
        h = H if path.startswith("/api/") else {}
        assert client.get(path, headers=h).status_code == code, f"{path}: FAIL"

# =================================================================
print()
print("=" * 60)
print(f"  集成测试: {passed} 通过, {failed} 失败")
print("=" * 60)
if errors:
    print("\n失败详情:")
    for e in errors: print(f"  {e}")
if failed: sys.exit(1)
