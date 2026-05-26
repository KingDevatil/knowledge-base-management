"""
MCP Streamable HTTP TDD Test
验证 /mcp 端点处理标准 MCP initialize 请求
"""
import sys, os, asyncio, json, hashlib
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ['DEBUG'] = 'true'
os.environ['KBDATA_DIR'] = os.path.join(_ROOT, 'kbdata')
os.environ['REDIS_URL'] = 'redis://localhost:6379/0'
os.environ['SESSION_SECRET'] = 'a' * 32

import fakeredis.aioredis as fr
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, AsyncMock
from main import app
from auth import APIKeyAuth
from tools import KnowledgeTools
from knowledge_base import KnowledgeBase

# Setup mocks
f = fr.FakeRedis()
app.state.redis = f
app.state.admin_auth = MagicMock()
app.state.admin_auth.verify_session = AsyncMock(return_value={'username': 'admin', 'role': 'super_admin'})

auth = APIKeyAuth(f, 'D:/kimicode/knowledge-base-management/kbdata/config/api_keys.json')
app.state.api_key_auth = auth

kb = KnowledgeBase(MagicMock(), 'mcp-test')
kb.set_redis(f)
mt = MagicMock()
mt.embed = AsyncMock(side_effect=lambda texts: [[0.1] * 1024] * (len(texts) if isinstance(texts, list) else 1))
mt.embed_single = AsyncMock(return_value=[0.1] * 1024)
ms = MagicMock()
ms.save_source.return_value = 'test-source.md'

class FakeLock:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass

app.state.tools = KnowledgeTools(kb, ms, mt, FakeLock(), auth)
app.state.kb = kb

# Create test API key
TEST_KEY = 'sk-mcp-tdd-test-key-00000'
key_hash = hashlib.sha256(TEST_KEY.encode()).hexdigest()
loop = asyncio.new_event_loop()
loop.run_until_complete(f.hset(f'api_key:{key_hash}', mapping={
    'key_prefix': TEST_KEY[:10],
    'applicant': 'tdd-test',
    'applicant_note': '',
    'role': 'user',
    'scope': '["read"]',
    'rate_limit': '999',
    'status': 'active',
    'duration': 'permanent',
    'created_at': '2025-01-01T00:00:00',
    'expires_at': '',
    'use_count': '0',
    'last_used_at': '',
    'created_by': 'tdd',
}))
loop.run_until_complete(app.state.api_key_auth._load_keys_to_redis())
loop.close()

client = TestClient(app)
H = {'X-API-Key': TEST_KEY, 'Content-Type': 'application/json'}

# MCP initialize message
INIT_MSG = json.dumps({
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "tdd-test", "version": "1.0"}
    },
    "id": 1
})

# ── Tests ──
failed = 0

print("TDD: MCP Streamable HTTP")
# Test 1: POST /mcp with valid key returns non-error
r = client.post("/mcp", content=INIT_MSG, headers=H)
if r.status_code == 200:
    print("  PASS: POST /mcp → 200")
else:
    print(f"  FAIL: POST /mcp → {r.status_code} {r.text[:200]}")
    failed += 1

# Test 2: POST /mcp without key returns 401  
r2 = client.post("/mcp", content=INIT_MSG,
                 headers={"Content-Type": "application/json"})
if r2.status_code == 401:
    print("  PASS: no key → 401")
else:
    print(f"  FAIL: no key → {r2.status_code}")
    failed += 1

# Test 3: GET /mcp with valid key returns non-error
r3 = client.get("/mcp", headers={'X-API-Key': TEST_KEY})
if r3.status_code not in (500,):
    print(f"  PASS: GET /mcp → {r3.status_code}")
else:
    print(f"  FAIL: GET /mcp → {r3.status_code} {r3.text[:200]}")
    failed += 1

if failed:
    print(f"\n{failed} FAILED")
    sys.exit(1)
else:
    print("\nALL PASSED")
