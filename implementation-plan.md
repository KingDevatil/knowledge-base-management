# 企业中央知识库 + MCP Gateway — 分模块详细实现计划

> 基于 plan.md v1.3 方案，按 Phase 拆解为可执行的任务级计划。
> 重点细化 Web UI 页面布局与 UE 设计。

---

## 目录

- [Phase 1: 基础环境搭建（Day 1）](#phase-1-基础环境搭建day-1)
- [Phase 2: 基础设施部署（Day 1-2）](#phase-2-基础设施部署day-1-2)
- [Phase 3: MCP Gateway 核心开发（Day 2-5）](#phase-3-mcp-gateway-核心开发day-2-5)
- [Phase 4: 后台管理页面开发（Day 5-7）](#phase-4-后台管理页面开发day-5-7)
- [Phase 5: 部署与集成测试（Day 7-8）](#phase-5-部署与集成测试day-7-8)
- [Phase 6: 上线与运维（Day 8+）](#phase-6-上线与运维day-8)
- [Web UI 页面布局与 UE 详设](#web-ui-页面布局与-ue-详设)
- [模块依赖图与关键路径](#模块依赖图与关键路径)

---

## Phase 1: 基础环境搭建（Day 1）

### 1.1 服务器准备

| 子任务 | 详细步骤 | 验证命令 |
|--------|---------|---------|
| 购买/准备服务器 | 4核8G，100GB SSD，CentOS 9 / Ubuntu 22.04 | `cat /etc/os-release` |
| 配置安全组 | 开放 22(SSH), 80(HTTP), 443(HTTPS), 9000(MinIO控制台) | 云厂商控制台操作 |
| 创建非 root 用户 | `adduser kbadmin && usermod -aG docker kbadmin` | `id kbadmin` |
| 配置 SSH 密钥登录 | 禁用密码登录，仅允许密钥 | `ssh -i key kbadmin@host` |

### 1.2 Docker 环境

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER

# 验证
docker --version        # >= 24.x
docker compose version  # >= 2.x
```

### 1.3 域名与 HTTPS

| 子任务 | 详细步骤 | 产出文件 |
|--------|---------|---------|
| DNS 解析 | A 记录 `kb.company.com` → 服务器公网 IP | - |
| 安装 Certbot | `apt install certbot` | - |
| 申请证书 | `certbot certonly --standalone -d kb.company.com` | `/etc/letsencrypt/live/kb.company.com/` |
| 自动续期 | 配置 cron: `0 3 * * * certbot renew --quiet` | - |

### 1.4 Nginx 反向代理（初版）

```nginx
# /etc/nginx/sites-available/kb-company
types {
    text/event-stream  sse;
}

upstream mcp_gateway {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl http2;
    server_name kb.company.com;

    ssl_certificate /etc/letsencrypt/live/kb.company.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kb.company.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # MCP SSE 端点
    location /sse {
        proxy_pass http://mcp_gateway/sse;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-API-Key $http_x_api_key;
    }

    # 后台管理页面
    location /admin {
        proxy_pass http://mcp_gateway/admin;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 静态资源
    location /static {
        proxy_pass http://mcp_gateway/static;
        expires 7d;
    }
}

server {
    listen 80;
    server_name kb.company.com;
    return 301 https://$server_name$request_uri;
}
```

---

## Phase 2: 基础设施部署（Day 1-2）

### 2.1 Docker Compose 基础服务编排

```yaml
# docker-compose.yml（基础设施部分）
version: "3.9"

services:
  redis:
    image: redis:7-alpine
    restart: always
    volumes:
      - redis_data:/data
    ports:
      - "127.0.0.1:6379:6379"
    command: redis-server --appendonly yes

  chroma:
    image: chromadb/chroma:0.6.4
    restart: always
    volumes:
      - chroma_data:/chroma/chroma
    ports:
      - "127.0.0.1:8001:8000"
    environment:
      - ANONYMIZED_TELEMETRY=false

  minio:
    image: minio/minio:RELEASE.2025-01-20T14-49-07Z
    restart: always
    command: server /data --console-address ":9001"
    volumes:
      - minio_data:/data
    ports:
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9001:9001"
    environment:
      - MINIO_ROOT_USER=minioadmin
      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}

  ollama:
    image: ollama/ollama:0.6.0
    restart: always
    volumes:
      - ollama_data:/root/.ollama
    ports:
      - "127.0.0.1:11434:11434"
    # GPU 支持（如需）
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: 1
    #           capabilities: [gpu]

volumes:
  redis_data:
  chroma_data:
  minio_data:
  ollama_data:
```

### 2.2 各服务初始化验证

| 服务 | 验证命令 | 预期结果 |
|------|---------|---------|
| Redis | `redis-cli ping` | `PONG` |
| Chroma | `curl http://localhost:8001/api/v1/heartbeat` | `{"nanosecond heartbeat": ...}` |
| MinIO | 访问 `http://localhost:9001`，登录控制台 | 能创建 bucket |
| Ollama | `curl http://localhost:11434/api/tags` | 返回模型列表 |

### 2.3 拉取 Embedding 模型

```bash
# 在 ollama 容器内执行
docker compose exec ollama ollama pull bge-m3

# 验证
curl http://localhost:11434/api/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-m3", "prompt": "测试中文 embedding"}'
```

### 2.4 MinIO Bucket 初始化

```bash
# 创建 kb-sources bucket（用于存储原始 Markdown）
docker compose exec minio mc alias set local http://localhost:9000 minioadmin $MINIO_ROOT_PASSWORD
docker compose exec minio mc mb local/kb-sources
# 设置公开读取策略（仅源文件路径需要内部访问，无需对外公开）
```

---

## Phase 3: MCP Gateway 核心开发（Day 2-5）

### 3.1 项目骨架搭建

```
mcp-gateway/
├── pyproject.toml          # Poetry 依赖管理
├── requirements.txt        # 备选 pip 安装
├── src/
│   ├── __init__.py
│   ├── main.py             # FastAPI 应用入口
│   ├── config.py           # 配置管理（Pydantic Settings）
│   ├── server.py           # MCP SSE 服务器初始化
│   ├── auth.py             # API Key 认证中间件
│   ├── admin_auth.py       # 管理员 Session 认证
│   ├── tools.py            # MCP 工具注册与实现
│   ├── lock.py             # Redis 分布式写入锁
│   ├── knowledge_base.py   # Chroma 客户端封装
│   ├── source_store.py     # MinIO 源文件操作（支持目录树）
│   ├── directory_tree.py   # 目录树聚合与维护
│   ├── chunker.py          # Markdown 切片逻辑
│   ├── embedding.py        # Ollama Embedding 调用
│   ├── models.py           # Pydantic 数据模型
│   └── admin/              # 后台管理模块
│       ├── __init__.py
│       ├── routes.py
│       ├── templates/
│       └── static/
└── tests/
```

### 3.2 依赖清单

```toml
[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.115.0"
uvicorn = {extras = ["standard"], version = "^0.34.0"}
python-multipart = "^0.0.20"
jinja2 = "^3.1.0"
httpx = "^0.28.0"
redis = "^5.2.0"
chromadb = "^0.6.0"
bcrypt = "^4.3.0"
python-jose = {extras = ["cryptography"], version = "^3.4.0"}
pydantic = "^2.11.0"
pydantic-settings = "^2.9.0"
minio = "^7.2.0"
python-markdown = "^3.8.0"
```

### 3.3 配置管理（config.py）

```python
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    # 服务
    APP_NAME: str = "Knowledge Base Management"
    DEBUG: bool = False
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Chroma
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8001
    CHROMA_COLLECTION: str = "knowledge_base_management"
    
    # Ollama
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "bge-m3"
    
    # MinIO
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "kb-sources"
    MINIO_SECURE: bool = False
    
    # 认证
    API_KEY_FILE: str = "config/api_keys.json"
    ADMIN_ACCOUNTS_FILE: str = "config/admin_accounts.json"
    SESSION_SECRET: str = "change-me-in-production"
    SESSION_MAX_AGE: int = 86400  # 24小时
    
    # 切片
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    
    # 锁
    WRITE_LOCK_KEY: str = "kb:write_lock"
    WRITE_LOCK_TTL: int = 30
    
    # 限流
    RATE_LIMIT_DEFAULT: int = 30  # 每分钟请求数
    
    class Config:
        env_file = ".env"

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

### 3.4 API Key 认证中间件（auth.py）

**接口定义：**

```python
class APIKeyAuth:
    """API Key 认证与生命周期管理"""
    
    async def authenticate(request: Request) -> APIKeyInfo:
        """
        认证流程：
        1. 从 Header X-API-Key 读取 Key
        2. Redis 查询 Key 元数据（热路径）
        3. 检查 status: revoked/expired/active
        4. 检查 scope 是否匹配请求的操作
        5. 限流检查（Redis incr + expire）
        6. 更新 last_used_at 和 use_count
        """
        pass
    
    async def create_key(
        applicant: str,
        applicant_note: str,
        scope: list[str],        # ["read"] or ["read", "write"]
        duration: str,           # "1d"/"3d"/"7d"/"30d"/"permanent"
        created_by: str          # 管理员用户名
    ) -> str:
        """创建新 Key，返回完整 Key（仅展示一次）"""
        pass
    
    async def revoke_key(key_id: str, revoked_by: str) -> None:
        """吊销 Key，立即生效"""
        pass
    
    async def list_keys(status: str | None = None) -> list[APIKeyInfo]:
        """列出 Key，支持按状态筛选"""
        pass
```

**核心逻辑 — 认证流程：**

```
请求到达
  ├─ Header 无 X-API-Key → 返回 401 Unauthorized
  ├─ Key 格式无效（不以 sk- 开头）→ 返回 401
  └─ Key 有效 → Redis HGETALL api_key:{key_hash}
       ├─ Key 不存在 → 返回 401
       ├─ status == revoked → 返回 403 Key Revoked
       ├─ status == expired 或 expires_at < now → 返回 403 Key Expired
       ├─ scope 不包含所需权限 → 返回 403 Insufficient Scope
       └─ 通过 → 限流检查（Redis: INCR rate_limit:{key} + EXPIRE 60s）
            ├─ 超过 rate_limit → 返回 429 Too Many Requests
            └─ 通过 → 更新 last_used_at, use_count → 继续执行
```

### 3.5 Redis 分布式写入锁（lock.py）

```python
import redis.asyncio as redis
import uuid

class WriteLock:
    def __init__(self, redis_client: redis.Redis, ttl: int = 30):
        self.redis = redis_client
        self.key = "kb:write_lock"
        self.ttl = ttl
        self._lock_id: str | None = None
    
    async def acquire(self) -> bool:
        self._lock_id = str(uuid.uuid4())
        acquired = await self.redis.set(
            self.key, self._lock_id, nx=True, ex=self.ttl
        )
        return bool(acquired)
    
    async def release(self) -> None:
        if not self._lock_id:
            return
        # Lua 脚本保证原子性检查-删除
        lua = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
        """
        await self.redis.eval(lua, 1, self.key, self._lock_id)
        self._lock_id = None
    
    async def __aenter__(self):
        if not await self.acquire():
            raise WriteLockError("获取写入锁失败，请稍后重试")
        return self
    
    async def __aexit__(self, exc_type, exc, tb):
        await self.release()
```

### 3.6 切片逻辑（chunker.py）

```python
def chunk_markdown(content: str, chunk_size: int = 512, overlap: int = 50) -> list[str]:
    """
    Markdown 切片策略：
    1. 按段落（\n\n）优先切分
    2. 单个段落超过 chunk_size，按句子切分
    3. 切片之间保留 overlap 字符，保证语义连续性
    """
    # 实现略，返回 chunk 列表
    pass
```

### 3.7 MCP 工具实现（tools.py）

| 工具 | 输入 | 输出 | 权限 | 关键逻辑 |
|------|------|------|------|---------|
| `search_knowledge` | query, top_k=5, filter_tags=[], filter_path="" | list[{content, title, path, source_path, score}] | read | 调用 Chroma query → 返回切片 + 元数据。支持按目录路径筛选 |
| `add_document` | title, content, path="", tags=[] | doc_id | write | 生成 doc_id → 保存到 `documents/{path}/{doc_id}/source.md` → 切片 → Embedding → 写锁 → Chroma add（含 path 元数据） |
| `update_document` | doc_id, title, content, path="" | success | write | 保存 MinIO（path 变更则移动文件）→ 写锁 → Chroma delete by doc_id → 重新切片 → Chroma add（更新 path 元数据） |
| `delete_document` | doc_id | success | write | 写锁 → Chroma delete by doc_id → MinIO delete source.md |
| `list_documents` | tags=[], path="", limit=20, offset=0 | list[{doc_id, title, path, tags, chunk_count, created_at}] | read | Chroma 按 metadata 聚合 → 支持按 path 前缀筛选 → 返回文档列表 |
| `import_markdown` | title, markdown_content, path="", tags=[] | doc_id | write | 同 add_document，content 直接传入，可指定目标目录 |
| `list_directories` | - | tree[{name, path, children}] | read | 从所有文档的 path 元数据聚合目录树结构 |

### 3.8 知识库封装（knowledge_base.py）

```python
class KnowledgeBase:
    def __init__(self, chroma_client, collection_name: str):
        self.client = chroma_client
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )
    
    async def add_document_chunks(
        self, 
        doc_id: str,
        title: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict
    ) -> None:
        """将切片批量写入 Chroma"""
        ids = [f"{doc_id}#chunk-{i}" for i in range(len(chunks))]
        metadatas = [{
            **metadata,
            "doc_id": doc_id,
            "title": title,
            "chunk_index": i,
            "total_chunks": len(chunks)
        } for i in range(len(chunks))]
        
        self.collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas
        )
    
    async def delete_document(self, doc_id: str) -> int:
        """删除某文档的所有切片，返回删除数量"""
        # Chroma 不支持按 metadata 字段删除，需先查询再删
        results = self.collection.get(where={"doc_id": doc_id})
        if results["ids"]:
            self.collection.delete(ids=results["ids"])
        return len(results["ids"])
    
    async def search(
        self, 
        query_embedding: list[float], 
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        filter_path: str = ""
    ) -> list[SearchResult]:
        """向量检索，支持按目录路径筛选"""
        where_clause = {}
        if filter_tags:
            where_clause["tags"] = {"$contains": filter_tags}
        if filter_path:
            # 路径前缀匹配：path 等于 filter_path 或以 filter_path/ 开头
            where_clause["path"] = {"$eq": filter_path}
        
        where = where_clause if where_clause else None
        
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"]
        )
        return [...]
    
    async def list_documents(
        self,
        tags: list[str] | None = None,
        path: str = "",
        limit: int = 20,
        offset: int = 0
    ) -> list[DocumentInfo]:
        """按条件列出文档，支持目录路径前缀筛选"""
        where_clause = {}
        if tags:
            where_clause["tags"] = {"$contains": tags}
        if path:
            where_clause["path"] = {"$eq": path}
        
        where = where_clause if where_clause else None
        
        # Chroma get 返回所有匹配项，需手动分页
        results = self.collection.get(
            where=where,
            include=["metadatas"]
        )
        
        # 按 doc_id 去重聚合
        docs = {}
        for meta in results["metadatas"]:
            doc_id = meta["doc_id"]
            if doc_id not in docs:
                docs[doc_id] = {
                    "doc_id": doc_id,
                    "title": meta["title"],
                    "path": meta.get("path", ""),
                    "tags": meta.get("tags", []),
                    "chunk_count": 0,
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", "")
                }
            docs[doc_id]["chunk_count"] += 1
        
        doc_list = list(docs.values())
        return doc_list[offset:offset + limit]
```

### 3.9 MinIO 源文件管理（source_store.py）

支持多级目录树存储，路径格式：`documents/{path}/{doc_id}/source.md`

```python
class SourceStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str):
        self.client = Minio(endpoint, access_key, secret_key, secure=False)
        self.bucket = bucket
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)
    
    def _build_path(self, doc_id: str, path: str = "") -> str:
        """构建 MinIO 存储路径"""
        if path:
            # 清理路径：去除前后斜杠，统一为正斜杠
            path = path.strip("/").replace("\\", "/")
            return f"documents/{path}/{doc_id}/source.md"
        return f"documents/{doc_id}/source.md"
    
    def save_source(self, doc_id: str, content: str, path: str = "") -> str:
        """保存原始 Markdown，返回存储路径"""
        object_path = self._build_path(doc_id, path)
        self.client.put_object(
            self.bucket, object_path,
            data=io.BytesIO(content.encode("utf-8")),
            length=len(content.encode("utf-8")),
            content_type="text/markdown; charset=utf-8"
        )
        return object_path
    
    def get_source(self, doc_id: str, path: str = "") -> str:
        """读取原始 Markdown 内容"""
        object_path = self._build_path(doc_id, path)
        obj = self.client.get_object(self.bucket, object_path)
        return obj.read().decode("utf-8")
    
    def get_source_by_full_path(self, source_path: str) -> str:
        """通过完整 source_path 读取内容"""
        obj = self.client.get_object(self.bucket, source_path)
        return obj.read().decode("utf-8")
    
    def delete_source(self, doc_id: str, path: str = "") -> None:
        """删除源文件"""
        object_path = self._build_path(doc_id, path)
        self.client.remove_object(self.bucket, object_path)
    
    def move_source(self, doc_id: str, old_path: str, new_path: str) -> str:
        """移动文档到新的目录路径，返回新路径"""
        old_object_path = self._build_path(doc_id, old_path)
        new_object_path = self._build_path(doc_id, new_path)
        # MinIO 没有原生 move，用 copy + delete
        self.client.copy_object(
            self.bucket, new_object_path,
            CopySource(self.bucket, old_object_path)
        )
        self.client.remove_object(self.bucket, old_object_path)
        return new_object_path
    
    def list_all_documents(self) -> list[dict]:
        """列出所有文档对象，用于构建目录树"""
        objects = self.client.list_objects(self.bucket, prefix="documents/", recursive=True)
        docs = []
        for obj in objects:
            if obj.object_name.endswith("/source.md"):
                # 解析 path 和 doc_id: documents/{path}/{doc_id}/source.md
                parts = obj.object_name.replace("documents/", "").split("/")
                if len(parts) >= 2:
                    doc_id = parts[-2]
                    path = "/".join(parts[:-2]) if len(parts) > 2 else ""
                    docs.append({
                        "doc_id": doc_id,
                        "path": path,
                        "source_path": obj.object_name,
                        "size": obj.size,
                        "last_modified": obj.last_modified
                    })
        return docs
```

### 3.10 目录树管理（directory_tree.py）

```python
class DirectoryTree:
    """从 Chroma metadata 或 MinIO 对象列表聚合目录树"""
    
    @staticmethod
    def build_from_metadata(metadatas: list[dict]) -> dict:
        """
        从文档元数据构建目录树
        返回嵌套字典：{"name": "root", "children": [{"name": "技术", "children": [...]}]}
        """
        tree = {"name": "root", "path": "", "children": {}}
        
        for meta in metadatas:
            path = meta.get("path", "")
            if not path:
                continue
            
            parts = path.split("/")
            current = tree
            current_path = ""
            
            for part in parts:
                current_path = f"{current_path}/{part}".strip("/")
                if part not in current["children"]:
                    current["children"][part] = {
                        "name": part,
                        "path": current_path,
                        "children": {}
                    }
                current = current["children"][part]
        
        # 将 children dict 转为 list 便于模板遍历
        def dict_to_list(node):
            return {
                "name": node["name"],
                "path": node["path"],
                "children": [dict_to_list(child) for child in node["children"].values()]
            }
        
        return dict_to_list(tree)
    
    @staticmethod
    def build_from_minio(source_store: SourceStore) -> dict:
        """从 MinIO 对象列表构建目录树"""
        docs = source_store.list_all_documents()
        # 去重：按 doc_id 取最新版本
        seen = {}
        for doc in docs:
            seen[doc["doc_id"]] = doc
        
        metadatas = [{"path": doc["path"]} for doc in seen.values()]
        return DirectoryTree.build_from_metadata(metadatas)
    
    @staticmethod
    def validate_path(path: str) -> str:
        """验证并规范化路径"""
        if not path:
            return ""
        path = path.strip("/").replace("\\", "/")
        # 禁止 .. 和空目录名
        parts = [p for p in path.split("/") if p and p != ".."]
        return "/".join(parts)
    
    @staticmethod
    def get_breadcrumbs(path: str) -> list[dict]:
        """获取面包屑导航列表"""
        if not path:
            return []
        parts = path.split("/")
        breadcrumbs = []
        accum = ""
        for part in parts:
            accum = f"{accum}/{part}".strip("/")
            breadcrumbs.append({"name": part, "path": accum})
        return breadcrumbs
```

### 3.10 FastAPI 主入口（main.py）

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化连接池
    app.state.redis = redis.from_url(settings.REDIS_URL)
    app.state.chroma = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    app.state.kb = KnowledgeBase(app.state.chroma, settings.CHROMA_COLLECTION)
    app.state.source_store = SourceStore(...)
    app.state.embedder = OllamaEmbedder(settings.OLLAMA_URL, settings.OLLAMA_MODEL)
    app.state.api_key_auth = APIKeyAuth(app.state.redis, settings.API_KEY_FILE)
    yield
    # 关闭
    await app.state.redis.close()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# MCP SSE 路由
app.include_router(mcp_router, prefix="/sse")

# 后台管理路由
app.include_router(admin_router, prefix="/admin")

# 静态文件
app.mount("/static", StaticFiles(directory="src/admin/static"), name="static")
```

---

## Phase 4: 后台管理页面开发（Day 5-7）

### 4.1 技术栈确认

| 层级 | 技术 | 说明 |
|------|------|------|
| 模板引擎 | Jinja2 | 服务端渲染 HTML |
| 交互增强 | HTMX 2.0 (CDN) | 局部刷新、无刷新表单提交 |
| CSS 框架 | TailwindCSS 4.0 (CDN) | 原子化 CSS，零构建 |
| 图标 | Lucide Icons (CDN) | SVG 图标库 |
| Markdown 编辑 | textarea + 实时预览 | 轻量方案，Phase 2 可升级 |
| 表单验证 | HTML5 native + 后端校验 | 简洁可靠 |

### 4.2 路由结构

```python
# admin/routes.py
from fastapi import APIRouter, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页"""

@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    """登录提交"""

@router.get("/logout")
async def logout(request: Request):
    """登出"""

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """仪表盘"""

@router.get("/documents", response_class=HTMLResponse)
async def document_list(request: Request, path: str = "", q: str = "", tag: str = "", page: int = 1):
    """文档列表（目录树 + 文档表格双栏）"""

@router.get("/documents/tree", response_class=HTMLResponse)
async def document_tree_partial(request: Request):
    """目录树 HTMX 局部刷新片段"""

@router.get("/documents/{doc_id}", response_class=HTMLResponse)
async def document_view(request: Request, doc_id: str):
    """文档查看"""

@router.get("/documents/{doc_id}/edit", response_class=HTMLResponse)
async def document_edit_page(request: Request, doc_id: str):
    """文档编辑页"""

@router.post("/documents/{doc_id}/edit")
async def document_edit_submit(
    request: Request, 
    doc_id: str, 
    title: str = Form(...), 
    content: str = Form(...), 
    path: str = Form(""),
    tags: str = Form(...)
):
    """文档编辑提交"""

@router.post("/documents/{doc_id}/delete")
async def document_delete(request: Request, doc_id: str):
    """文档删除"""

@router.get("/documents/upload", response_class=HTMLResponse)
async def upload_page(request: Request, path: str = ""):
    """上传页面（可预填目标目录）"""

@router.post("/documents/upload")
async def upload_submit(
    request: Request, 
    file: UploadFile, 
    title: str = Form(...), 
    path: str = Form(""),
    tags: str = Form("")
):
    """上传提交（支持保持目录结构）"""

@router.get("/api-keys", response_class=HTMLResponse)
async def api_key_list(request: Request, status: str = "active"):
    """API Key 列表"""

@router.get("/api-keys/create", response_class=HTMLResponse)
async def api_key_create_page(request: Request):
    """创建 API Key 页"""

@router.post("/api-keys/create")
async def api_key_create_submit(...) -> dict:
    """创建 API Key，返回完整 Key（仅一次）"""

@router.post("/api-keys/{key_id}/revoke")
async def api_key_revoke(request: Request, key_id: str):
    """吊销 API Key"""

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """系统设置"""
```

---

## Web UI 页面布局与 UE 详设

### 页面 1: 登录页 `/admin/login`

**布局结构：**

```
┌─────────────────────────────────────────┐
│              [公司 Logo]                 │
│                                         │
│          知识库管理后台                  │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │  用户名                          │   │
│  │  [________________________]     │   │
│  │                                 │   │
│  │  密码                            │   │
│  │  [________________________]     │   │
│  │  [显示密码 👁]                    │   │
│  │                                 │   │
│  │  [      登  录      ]           │   │
│  └─────────────────────────────────┘   │
│                                         │
│         © 2026 Company                  │
└─────────────────────────────────────────┘
```

**UE 设计：**
- 居中卡片布局，最大宽度 400px
- 输入框聚焦时边框变蓝色（Tailwind `focus:ring-2 focus:ring-blue-500`）
- 密码可点击眼睛图标切换显示/隐藏
- 登录失败：表单上方显示红色提示，不清空输入
- 登录成功：重定向到 `/admin/dashboard`
- Session 过期后自动跳转回登录页，URL 带 `?next=` 参数

---

### 页面 2: 仪表盘 `/admin/dashboard`

**布局结构：**

```
┌─────────────────────────────────────────────────────────────┐
│ [Logo] 知识库管理后台          [👤 admin ▼] [登出]          │
├─────────────────────────────────────────────────────────────┤
│ [📊 仪表盘] [📄 文档管理] [🔑 API Key] [⚙️ 设置]            │
├──────────┬──────────────────────────────────────────────────┤
│          │  今日概览                                          │
│          │  ┌──────────┬──────────┬──────────┬──────────┐    │
│          │  │ 📄 文档   │ 🔍 检索   │ 🔑 活跃   │ ⚠️ 即将   │    │
│          │  │   128    │  1,234   │   15     │   过期 2  │    │
│          │  │ +3 今日  │ +56 今日 │          │          │    │
│          │  └──────────┴──────────┴──────────┴──────────┘    │
│          │                                                   │
│          │  最近活动                                          │
│          │  ┌─────────────────────────────────────────────┐  │
│          │  │ 时间          用户      操作        对象      │  │
│          │  │ 10:23      sk-abc   检索      技术文档     │  │
│          │  │ 09:45      admin    新增      API 设计规范  │  │
│          │  │ 09:12      sk-def   写入      产品需求文档  │  │
│          │  │ ...                                         │  │
│          │  └─────────────────────────────────────────────┘  │
│          │                                                   │
│          │  文档增长趋势（近7天）                              │
│          │  [柱状图 / 简单 SVG 图表]                           │
│          │                                                   │
│          │  系统健康状态                                      │
│          │  🟢 Chroma  │  🟢 Redis  │  🟢 Ollama  │  🟢 MinIO │
│          └───────────────────────────────────────────────────┘
```

**UE 设计：**
- 侧边栏固定，主内容区可滚动
- 统计卡片 hover 有轻微阴影提升（`hover:shadow-md transition`）
- 最近活动表格每行 hover 高亮
- 系统健康状态用彩色圆点表示，每 30 秒自动刷新（HTMX `hx-trigger="every 30s"`）
- 即将过期 API Key 数字红色高亮，点击跳转 API Key 管理页

---

### 页面 3: 文档管理列表 `/admin/documents`

**布局结构（目录树 + 文档列表双栏）：**

```
┌─────────────────────────────────────────────────────────────────────┐
│ [Logo] ... [📄 文档管理] ...                                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  文档管理                                  [+ 新建文档] [⇅ 上传]    │
│                                                                     │
│  📁 全部文档 > 技术 > API                  [🔍 搜索当前目录...]    │
│                                                                     │
│  ┌──────────────┬─────────────────────────────────────────────────┐ │
│  │ 📁 目录树     │  当前目录：技术/API                              │ │
│  │              │  ┌───────────────────────────────────────────┐   │ │
│  │ ▼ 📁 技术     │  │ ☐ │ 标题         │ 标签    │ 切片 │ 更新   │ 操作 │   │ │
│  │   ▼ 📁 API   │  ├───┼──────────────┼─────────┼──────┼────────┼──────┤   │ │
│  │   │   📄 文档1 │  │   │ API 设计规范  │ 技术,API│ 12   │ 2小时前│ 👁✏️🗑│   │ │
│  │   │   📄 文档2 │  │   │ 接口认证流程  │ 技术    │ 8    │ 1天前  │ 👁✏️🗑│   │ │
│  │   ▶ 📁 架构  │  └───────────────────────────────────────────┘   │ │
│  │   ▶ 📁 前端  │                                                    │ │
│  │ ▶ 📁 产品    │  [< 上一页]  第 1 / 2 页  [下一页 >]              │ │
│  │ ▶ 📁 行政    │                                                    │ │
│  │ ▶ 📁 销售    │                                                    │ │
│  │              │                                                    │ │
│  │ [+ 新建目录] │                                                    │ │
│  └──────────────┴─────────────────────────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- **目录树交互**：
  - 左侧目录树支持展开/折叠（点击 ▶/▼ 图标）
  - 点击目录名：右侧表格刷新为该目录下的文档（HTMX `hx-get` 局部刷新）
  - 当前选中目录高亮（蓝色背景）
  - 目录树底部「+ 新建目录」按钮，点击后弹出输入框创建新目录
  - 空目录（无文档）在树中显示但灰色，点击后右侧显示「该目录暂无文档」
- **面包屑导航**：顶部显示当前路径 `📁 全部文档 > 技术 > API`，点击上级目录快速跳转
- 搜索框：默认搜索当前目录下的文档，支持勾选「搜索全部目录」
- 标签筛选：下拉多选，常用标签快速 chips
- 表格行操作：
  - 👁 查看：跳转到查看页
  - ✏️ 编辑：跳转到编辑页
  - 🗑 删除：点击后弹出确认对话框（`hx-confirm="确定删除该文档吗？"`），确认后无刷新删除行
- 新建文档：跳转到编辑页（doc_id 为空，预填当前目录到 path 字段）
- 上传：跳转到上传页（预填当前目录到 path 字段）

---

### 页面 4: 文档查看 `/admin/documents/{doc_id}`

**布局结构：**

```
┌─────────────────────────────────────────────────────────────┐
│ ... [📄 文档管理 > 技术 > API > API 设计规范] ...           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ API 设计规范                              [✏️ 编辑] [⬇️ 下载]││
│  │                                                         ││
│  │ 路径: 📁 技术 > API                                     ││
│  │ 标签: [技术] [API]                              12 切片 ││
│  │ 来源: documents/技术/API/doc-xxx/source.md   更新: 2小时前││
│  │                                                         ││
│  ├─────────────────────────────────────────────────────────┤│
│  │                                                         ││
│  │ # API 设计规范                                          ││
│  │                                                         ││
│  │ ## 1. 接口认证                                          ││
│  │ 所有接口必须使用 API Key 进行认证...                      ││
│  │                                                         ││
│  │ ## 2. 错误码定义                                        ││
│  │ ...                                                     ││
│  │                                                         ││
│  │ （Markdown 渲染后的 HTML，带语法高亮）                    ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- 面包屑导航：`文档管理 > 文档标题`
- Markdown 内容使用 `python-markdown` 渲染为 HTML，带代码高亮（highlight.js CDN）
- 编辑按钮：跳转到编辑页
- 下载按钮：直接下载 `.md` 文件（`Content-Disposition: attachment`）
- 页面底部可展开「原始 Markdown」切换查看源码

---

### 页面 5: 文档编辑 `/admin/documents/{doc_id}/edit`

**布局结构（新建时 doc_id 为 new）：**

```
┌─────────────────────────────────────────────────────────────┐
│ ... [📄 文档管理 > ✏️ 编辑文档] ...                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 编辑文档                             [💾 保存] [取消]    ││
│  │                                                         ││
│  │ 标题 *                                                  ││
│  │ [API 设计规范_______________________________________]   ││
│  │                                                         ││
│  │ 所在目录 *                                               ││
│  │ [📁 技术 > API                    ▼]  [修改目录]        ││
│  │                                                         ││
│  │ 标签（逗号分隔）                                         ││
│  │ [技术, API__________________________________________]   ││
│  │                                                         ││
│  │ ┌──────────────────────────┬──────────────────────────┐ ││
│  │ │ Markdown 源码            │  实时预览                │ ││
│  │ │                          │                          │ ││
│  │ │ # API 设计规范           │  # API 设计规范          │ ││
│  │ │                          │                          │ ││
│  │ │ ## 1. 接口认证           │  ## 1. 接口认证          │ ││
│  │ │ ...                      │  ...                     │ ││
│  │ │                          │                          │ ││
│  │ │ [____________________]   │  [渲染后的 HTML________]  │ ││
│  │ │                          │                          │ ││
│  │ └──────────────────────────┴──────────────────────────┘ ││
│  │                                                         ││
│  │ ⚠️ 保存后将重新切片并更新知识库索引，可能需要几秒        ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- 左右分栏：左侧 Markdown 编辑区（textarea，等宽字体），右侧实时预览（HTMX 每 500ms 发送内容到后端渲染，或纯前端 JS 渲染）
- 标题、所在目录为必填项，验证失败时边框变红并显示提示
- **目录选择**：
  - 默认显示当前所在目录（如 `技术/API`）
  - 点击「修改目录」弹出目录树选择对话框（模态框）
  - 对话框内展示完整目录树，点击目录选中，支持新建目录
  - 选中后更新下拉框显示
- 保存按钮：
  - 点击后显示「保存中...」加载状态
  - HTMX POST 提交，成功后显示绿色 toast「保存成功，正在重新索引...」
  - 如果写入锁被占用，显示黄色提示「其他操作正在进行，请等待...」并轮询
  - 如果 path 变更（移动目录），MinIO 执行 copy + delete 移动源文件
- 取消按钮：返回文档列表（保留目录筛选状态）
- 新建文档时：
  - 左侧为空，右侧显示提示「开始输入 Markdown...」
  - 目录默认选中当前浏览的目录（URL `?path=` 参数带入）

---

### 页面 6: Markdown 上传 `/admin/documents/upload`

**布局结构：**

```
┌─────────────────────────────────────────────────────────────┐
│ ... [📄 文档管理 > ⇅ 上传文档] ...                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 上传 Markdown 文件                                       ││
│  │                                                         ││
│  │ ┌─────────────────────────────────────────────────────┐ ││
│  │ │                                                     │ ││
│  │ │         📁                                        │ ││
│  │ │                                                     │ ││
│  │ │    拖拽文件到此处，或点击选择                      │ ││
│  │ │                                                     │ ││
│  │ │    支持 .md 文件，最大 10MB                         │ ││
│  │ │                                                     │ ││
│  │ └─────────────────────────────────────────────────────┘ ││
│  │                                                         ││
│  │ 标题 *                                                  ││
│  │ [________________________________________________]      ││
│  │ （自动从文件名推断，可修改）                            ││
│  │                                                         ││
│  │ 目标目录 *                                               ││
│  │ [📁 技术 > API                    ▼]  [修改目录]        ││
│  │ （支持保持文件目录结构上传）                             ││
│  │                                                         ││
│  │ 标签（逗号分隔）                                         ││
│  │ [________________________________________________]      ││
│  │                                                         ││
│  │ [☑️ 保持原始目录结构] （从压缩包或文件夹上传时生效）      ││
│  │                                                         ││
│  │ [    开始导入    ]                                      ││
│  │                                                         ││
│  │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  导入中... 65%        ││
│  │                                                         ││
│  │ ✅ 导入成功！文档 ID: doc-xxx-xxx                       ││
│  │    [查看文档]  [继续上传]                               ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- 拖拽区域：支持拖拽上传，hover 时边框变蓝、背景变浅色
- **批量上传**：支持一次拖拽多个 `.md` 文件或包含 `.md` 文件的文件夹
- **目录结构保持**：勾选后，上传的文件会按照原始相对目录结构存储（如拖拽 `docs/技术/API/规范.md`，则 path 自动设置为 `技术/API`）
- 选择文件后：
  - 单文件：自动读取文件名填入标题（去除 `.md` 后缀），内容预填到隐藏字段
  - 多文件：显示文件列表，每个文件可单独编辑标题和目录
- 目录选择：
  - 默认选中当前浏览的目录（URL `?path=` 参数带入）
  - 点击「修改目录」弹出目录树选择对话框
  - 支持在对话框中「新建目录」
- 导入按钮：
  - 点击后禁用，显示进度条动画（多文件时显示总体进度和当前文件名）
  - 后端同步处理，前端显示 spinner，完成后返回结果
- 导入成功：显示成功消息（如「成功导入 3 个文档」）+ 文档链接列表
- 导入失败：显示错误详情（文件过大、格式错误、Chroma 写入失败等），成功的文档仍保留

---

### 页面 7: API Key 管理 `/admin/api-keys`

**布局结构：**

```
┌─────────────────────────────────────────────────────────────┐
│ ... [🔑 API Key 管理] ...                                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  API Key 管理                          [+ 新建 API Key]    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ [🔵 活跃 15]  [⚪ 过期 3]  [🔴 已吊销 2]  [全部 20] │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌────────────────────────────────────────────────────────┐│
│  │ Key前缀 │ 申请人   │ 权限   │ 有效期    │ 调用次数 │ 状态 │ 操作 ││
│  ├─────────┼─────────┼────────┼───────────┼──────────┼──────┤│
│  │ sk-abc..│ 张三     │ 读写   │ 7天       │ 42       │ 🔵   │ 吊销 ││
│  │ sk-def..│ 李四     │ 只读   │ 3天       │ 128      │ ⚠️   │ 吊销 ││
│  │ sk-ghi..│ 王五     │ 读写   │ 已过期    │ 256      │ ⚪   │ -    ││
│  │ ...     │         │        │           │          │      │      ││
│  └─────────┴─────────┴────────┴───────────┴──────────┴──────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- 状态标签页：点击切换，HTMX 局部刷新表格
- 即将过期（<24h）：行背景黄色高亮，状态显示 ⚠️
- 吊销按钮：
  - 活跃 Key 显示「吊销」按钮，红色
  - 点击后 `hx-confirm="确定吊销该 Key 吗？申请人将立即失去访问权限。"`
  - 确认后无刷新移除行或更新状态
- 新建 Key：跳转到创建页

---

### 页面 8: 新建 API Key `/admin/api-keys/create`

**布局结构：**

```
┌─────────────────────────────────────────────────────────────┐
│ ... [🔑 API Key 管理 > 新建] ...                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 新建 API Key                                            ││
│  │                                                         ││
│  │ 申请人 *                                                ││
│  │ [________________________________________________]      ││
│  │                                                         ││
│  │ 申请备注 / 用途                                         ││
│  │ [________________________________________________]      ││
│  │                                                         ││
│  │ 权限 *                                                  ││
│  │ (○) 只读 — 仅允许检索知识库                            ││
│  │ (●) 读写 — 允许检索、添加、更新、删除文档              ││
│  │                                                         ││
│  │ 有效期 *                                                ││
│  │ [1天 ▼]  1天 / 3天 / 7天 / 30天 / 长期有效             ││
│  │                                                         ││
│  │ [    生  成    ]                                        ││
│  │                                                         ││
│  ├─────────────────────────────────────────────────────────┤│
│  │ ⚠️ 请立即复制以下 Key，页面关闭后将无法再次查看！       ││
│  │                                                         ││
│  │ ┌─────────────────────────────────────────────────────┐ ││
│  │ │ sk-live-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx            │ ││
│  │ │              [📋 一键复制]                           │ ││
│  │ └─────────────────────────────────────────────────────┘ ││
│  │                                                         ││
│  │ [完成，返回列表]                                        ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- 表单验证：申请人必填、权限必选、有效期必选
- 生成按钮：后端创建 Key，返回页面展示完整 Key（HTMX 提交后替换表单区域为结果展示）
- Key 展示框：
  - 大字体、等宽字体显示
  - 一键复制按钮（JS `navigator.clipboard.writeText`）
  - 点击复制后按钮变为「已复制 ✅」2 秒后恢复
- 警告提示：醒目的黄色/红色提示框，强调「仅展示一次」
- 完成按钮：跳转回列表页

---

### 页面 9: 系统设置 `/admin/settings`

**布局结构：**

```
┌─────────────────────────────────────────────────────────────┐
│ ... [⚙️ 系统设置] ...                                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 系统设置                                                ││
│  │                                                         ││
│  │ ┌─ Embedding 配置 ────────────────────────────────────┐ ││
│  │ │ 模型: [bge-m3 ▼]                                    │ ││
│  │ │ Ollama 地址: [http://localhost:11434____________]   │ ││
│  │ └─────────────────────────────────────────────────────┘ ││
│  │                                                         ││
│  │ ┌─ 切片配置 ──────────────────────────────────────────┐ ││
│  │ │ 切片大小: [512____] 字符                            │ ││
│  │ │ 重叠长度: [50_____] 字符                            │ ││
│  │ └─────────────────────────────────────────────────────┘ ││
│  │                                                         ││
│  │ ┌─ 写入锁配置 ────────────────────────────────────────┐ ││
│  │ │ 锁超时时间: [30____] 秒                             │ ││
│  │ └─────────────────────────────────────────────────────┘ ││
│  │                                                         ││
│  │ ┌─ 限流配置 ──────────────────────────────────────────┐ ││
│  │ │ 默认限流: [30____] 请求/分钟                        │ ││
│  │ └─────────────────────────────────────────────────────┘ ││
│  │                                                         ││
│  │ [保存设置]                                              ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 危险操作                                                ││
│  │                                                         ││
│  │ [清空知识库]  — 删除所有文档和切片，不可恢复           ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**UE 设计：**
- 分组卡片展示，每组带边框和标题
- 保存后显示绿色 toast「设置已保存」
- 危险操作区域：红色边框，按钮红色，点击后二次确认对话框

---

### 公共组件：Toast 通知系统

```html
<!-- 嵌入 base.html，HTMX 触发 -->
<div id="toast-container" class="fixed top-4 right-4 z-50 space-y-2">
  <!-- 成功 -->
  <div class="bg-green-50 border border-green-200 text-green-800 px-4 py-3 rounded-lg shadow-lg flex items-center gap-2"
       hx-swap-oob="true" id="toast-success">
    <svg class="w-5 h-5 text-green-500">...</svg>
    <span>操作成功</span>
  </div>
  
  <!-- 错误 -->
  <div class="bg-red-50 border border-red-200 text-red-800 px-4 py-3 rounded-lg shadow-lg"
       hx-swap-oob="true" id="toast-error">
    <span>操作失败: ...</span>
  </div>
  
  <!-- 警告 -->
  <div class="bg-yellow-50 border border-yellow-200 text-yellow-800 px-4 py-3 rounded-lg shadow-lg"
       hx-swap-oob="true" id="toast-warning">
    <span>写入锁被占用，请稍后...</span>
  </div>
</div>
```

---

### 公共组件：确认对话框

```html
<!-- HTMX 原生支持 hx-confirm -->
<button hx-post="/admin/documents/{id}/delete"
        hx-confirm="确定删除文档「API 设计规范」吗？此操作不可恢复。"
        hx-target="closest tr"
        hx-swap="outerHTML"
        class="text-red-600 hover:text-red-800">
  删除
</button>
```

---

### HTMX 交互模式汇总

| 场景 | HTMX 属性 | 说明 |
|------|----------|------|
| 搜索实时过滤 | `hx-get hx-trigger="keyup changed delay:300ms"` | 输入 300ms 后自动搜索 |
| 无刷新删除行 | `hx-post hx-target="closest tr" hx-swap="delete"` | 删除表格行不刷新页面 |
| 标签页切换 | `hx-get hx-target="#content-area" hx-push-url="true"` | 局部刷新内容区，更新 URL |
| 表单提交 | `hx-post hx-target="#form-result" hx-swap="innerHTML"` | 表单结果替换指定区域 |
| 加载状态 | `hx-indicator="#spinner"` | 请求时显示 loading 动画 |
| 轮询状态 | `hx-get hx-trigger="every 2s"` | 每 2 秒轮询状态更新 |

---

## Phase 5: 部署与集成测试（Day 7-8）

### 5.1 Docker Compose 完整编排

```yaml
# docker-compose.yml（完整版，新增 mcp-gateway 服务）
version: "3.9"

services:
  # ... redis, chroma, minio, ollama 同上 ...

  mcp-gateway:
    build: ./mcp-gateway
    restart: always
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./config:/app/config:ro
      - ./mcp-gateway/src/admin/templates:/app/src/admin/templates:ro
      - ./mcp-gateway/src/admin/static:/app/src/admin/static:ro
    environment:
      - REDIS_URL=redis://redis:6379/0
      - CHROMA_HOST=chroma
      - CHROMA_PORT=8000
      - OLLAMA_URL=http://ollama:11434
      - MINIO_ENDPOINT=minio:9000
      - MINIO_ACCESS_KEY=minioadmin
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD}
      - SESSION_SECRET=${SESSION_SECRET}
      - DEBUG=false
    depends_on:
      - redis
      - chroma
      - minio
      - ollama
```

### 5.2 测试用例矩阵

| 测试模块 | 测试项 | 预期结果 | 验证方式 |
|----------|--------|---------|---------|
| **认证** | 无 API Key | 401 | curl |
| | 无效 API Key | 401 | curl |
| | 过期 API Key | 403 "expired" | curl + 修改 expires_at |
| | 吊销 API Key | 403 "revoked" | curl + 后台吊销 |
| | 只读 Key 调写入 | 403 "insufficient scope" | curl |
| | 管理员错误密码 | 登录失败 | 浏览器 |
| | Session 过期 | 重定向登录 | 浏览器 |
| **检索** | 正常搜索 | 返回 top_k 结果 | curl |
| | 按标签筛选 | 只返回匹配标签 | curl |
| | 并发 20 请求 | 全部 < 2s | wrk/vegeta |
| **写入** | 正常添加文档（指定 path） | doc_id 返回，存储在正确目录 | curl + MinIO 验证路径 |
| | 添加文档到多级目录 | `documents/技术/API/前端/{doc_id}/source.md` | MinIO 验证 |
| | 并发写入（2客户端） | 一个成功，一个 423 | 并行 curl |
| | 写入锁超时（模拟崩溃） | 30s 后自动释放 | 手动删除进程 |
| | 更新文档（重新切片） | 旧切片删除，新切片写入 | 后台查 Chroma |
| | 更新文档（变更 path） | MinIO 源文件移动到新目录 | MinIO + Chroma 验证 |
| | 删除文档 | MinIO + Chroma 同时清理 | 后台验证 |
| **目录树** | 添加带 path 的文档 | 目录树自动聚合出新节点 | 浏览器刷新目录树 |
| | 移动文档目录 | 源文件路径变更，目录树更新 | 浏览器 + MinIO |
| | 删除目录下最后文档 | 该目录从树中消失 | 浏览器验证 |
| | 按目录筛选文档 | 只返回该目录及子目录文档 | curl / 浏览器 |
| | 检索时指定 filter_path | 只返回该目录下匹配结果 | curl |
| | 目录树大规模测试 | 1000 个文档分布在 50 个目录 | 树加载 < 1s |
| **导入** | 上传 1MB md 到指定目录 | 成功，存储在正确目录 | 浏览器 + curl |
| | 批量上传保持目录结构 | 多个文件按原始目录存储 | MinIO 验证 |
| | 上传 15MB md | 413 Payload Too Large | 浏览器 |
| | 上传非 md | 400 Bad Request | 浏览器 |
| **后台** | 新建文档（选择目录） | 文档出现在对应目录 | 浏览器 |
| | 编辑文档（变更目录） | 文档移动到新目录 | 浏览器 + 目录树刷新 |
| | 删除文档 | 列表消失，Chroma 清理，目录树更新 | 浏览器 |
| | 新建空目录 | 目录出现在树中 | 浏览器 |
| | 创建 API Key | 页面展示完整 Key | 浏览器 |
| | 吊销 API Key | 状态变 revoked | 浏览器 + curl |
| | 上传 Markdown | 成功导入，列表出现 | 浏览器 |
| **集成** | 端到端（Agent → MCP → 检索） | Agent 能获取知识 | Cursor 配置测试 |

### 5.3 性能测试基准

```bash
# 检索压力测试
wrk -t4 -c20 -d60s \
  -H "X-API-Key: sk-test" \
  "https://kb.company.com/sse/tools/search_knowledge?query=test"

# 目标: 20 并发下，p99 < 2s，错误率 < 1%

# 写入锁测试（并行 5 个写入）
for i in {1..5}; do
  curl -X POST -H "X-API-Key: sk-write" \
    -d '{"title":"test","content":"xxx"}' \
    https://kb.company.com/sse/tools/add_document &
done
wait
# 预期: 1 个 200，4 个 423（或等待重试后成功）
```

---

## Phase 6: 上线与运维（Day 8+）

### 6.1 Nginx 安全加固

```nginx
# 追加到 nginx.conf

# 限流
limit_req_zone $binary_remote_addr zone=mcp:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=admin:10m rate=5r/s;

location /sse {
    limit_req zone=mcp burst=20 nodelay;
    # ... 原有配置
}

location /admin {
    limit_req zone=admin burst=10 nodelay;
    # 可选 IP 白名单
    # allow 192.168.1.0/24;
    # deny all;
    # ... 原有配置
}

# 安全响应头
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

### 6.2 备份脚本

```bash
#!/bin/bash
# backup.sh — 每日凌晨 3 点执行

DATE=$(date +%Y%m%d)
BACKUP_DIR="/backup/kb-$DATE"
mkdir -p $BACKUP_DIR

# 备份 Chroma
tar czf $BACKUP_DIR/chroma.tar.gz /var/lib/docker/volumes/knowledge-base-management_chroma_data/

# 备份 MinIO
tar czf $BACKUP_DIR/minio.tar.gz /var/lib/docker/volumes/knowledge-base-management_minio_data/

# 备份配置
cp /opt/knowledge-base-management/config/*.json $BACKUP_DIR/

# 上传到远程（可选）
# rclone sync $BACKUP_DIR remote:backups/

# 保留最近 30 天
find /backup -name "kb-*" -mtime +30 -delete
```

### 6.3 监控检查清单

| 检查项 | 命令/方式 | 告警阈值 |
|--------|----------|---------|
| 服务存活 | `curl /health` | 连续 3 次失败 |
| 磁盘空间 | `df -h` | > 80% |
| 内存使用 | `free -m` | > 90% |
| Redis 内存 | `INFO memory` | > 500MB |
| Chroma 查询延迟 | 日志埋点 | p99 > 3s |
| API Key 即将过期 | 后台扫描 | < 24h |
| 写入锁长期占用 | Redis TTL 监控 | > 60s |

---

## 模块依赖图与关键路径

```
Day 1          Day 2          Day 3          Day 4          Day 5
──────────────────────────────────────────────────────────────────
Phase 1        Phase 2        Phase 3.1-3.4  Phase 3.5-3.8
├─服务器         ├─Chroma       ├─项目骨架       ├─MCP工具
├─Docker         ├─Ollama       ├─API Key认证   ├─锁+KB封装
├─域名证书       ├─MinIO        ├─分布式锁      ├─SSE适配
├─Nginx          ├─Redis        ├─Embedding     └─集成测试
└─Docker Compose └─模型拉取

Day 5          Day 6          Day 7          Day 8
──────────────────────────────────────────────────
Phase 4.1-4.3  Phase 4.4-4.6  Phase 5        Phase 6
├─Admin认证      ├─API Key页面   ├─Compose编排   ├─Nginx加固
├─仪表盘         ├─设置页面      ├─并发测试      ├─备份脚本
├─文档管理列表    └─HTMX集成      ├─端到端测试    ├─监控告警
└─文档查看/编辑                  └─接入文档      └─上线
```

**关键路径（决定总工期）：**
服务器准备 → Docker 环境 → Chroma/Ollama 就绪 → API Key 认证 → MCP 工具实现 → 文档管理页面 → 集成测试 → 上线

---

## 任务级工时估算

| Phase | 任务数 | 人天 | 说明 |
|-------|--------|------|------|
| Phase 1 | 4 | 0.5 | 环境准备，可并行 |
| Phase 2 | 5 | 0.5 | 容器启动，模型下载耗时 |
| Phase 3 | 8 | 2.5 | 核心开发，占主要工时 |
| Phase 4 | 6 | 2.0 | 页面开发，UE 细节打磨 |
| Phase 5 | 5 | 1.0 | 测试+修复 |
| Phase 6 | 4 | 0.5+ | 上线后持续 |
| **合计** | **32** | **7-8 人天** | 含联调 buffer |

---

*文档版本：v1.0*
*基于 plan.md v1.3 拆解*
*预计阅读后可直接进入编码阶段*
