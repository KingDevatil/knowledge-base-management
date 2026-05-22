# 企业中央知识库 + MCP Gateway 实施方案

## 1. 项目概述

### 1.1 目标
在公司公网服务器部署一套中央知识库系统，支持 RAG 检索，通过自建 MCP（Model Context Protocol）Gateway 暴露给员工的云端 AI Agent。所有员工共用同一个知识库，通过 API Key 令牌进行访问控制，支持并发写入排队锁，配套 Web 管理后台供管理员维护知识库内容。

### 1.2 核心需求
| 需求项 | 说明 |
|--------|------|
| 中央知识库 | 单库共享，不隔离数据，全公司共用 |
| RAG 检索 | 支持向量检索 + 关键词检索混合 |
| MCP 协议 | 云端 Agent 通过 SSE/HTTP 方式访问 |
| 令牌验权 | API Key 认证，支持读写权限分级 |
| 文档写入 | 支持员工向知识库添加/更新文档 |
| **写入排队锁** | 多用户同时写入时排队，避免并发冲突 |
| **管理员账号** | 独立的管理员认证体系，区别于普通 API Key |
| **后台管理页面** | Web UI 查看/编辑/删除知识库文档，管理 API Key |
| **API Key 生命周期** | 管理员后台分发，支持 1/3/7/30 天或长期有效期，过期自动作废，可手动吊销 |
| **Markdown 上传导入** | Web UI 和 MCP 均支持 .md 文件上传，自动导入知识库 |
| **源文件管理** | 知识库源文件统一以 Markdown 形式保存在 MinIO，切片数据与源文件分离 |
| 可维护性 | Docker 化部署，单服务器即可运行 |

### 1.3 非功能性需求
- 并发：支持 20-50 人同时检索，写入排队不丢数据
- 延迟：检索响应 < 2s，写入排队等待 < 5s
- 可用性：99% 以上在线时间
- 安全：HTTPS 传输，API Key 不落地日志，管理后台独立认证

---

## 2. 架构设计

### 2.1 总体架构

```
                                  员工端
    ┌─────────────────┐     ┌─────────────────┐
    │   员工A Agent    │     │   员工B Agent    │
    │  (Cursor/Claude) │     │  (Cursor/Claude) │
    └────────┬────────┘     └────────┬────────┘
             │                       │
             │   MCP SSE + X-API-Key  │
             ▼                       ▼
    ┌─────────────────────────────────────────────────────────┐
    │                   公司公网服务器                          │
    │  ┌──────────────────────────────────────────────────┐   │
    │  │           Nginx (反向代理 + HTTPS + 限流)          │   │
    │  │         /sse  →  MCP Gateway                     │   │
    │  │         /admin →  后台管理页面                   │   │
    │  └──────────────────────┬───────────────────────────┘   │
    │                         │                                │
    │  ┌──────────────────────┴───────────────────────────┐   │
    │  │           MCP Gateway (FastAPI + mcp SDK)         │   │
    │  │  - API Key 认证                                    │   │
    │  │  - 权限校验 (read / write)                         │   │
    │  │  - 工具路由 (search / add / update / delete)       │   │
    │  │  - 写入 Redis 分布式锁                             │   │
    │  └──────────────────────┬───────────────────────────┘   │
    │                         │                                │
    │  ┌──────────────────────┴───────────────────────────┐   │
    │  │           后台管理 (FastAPI + Jinja2 + HTMX)       │   │
    │  │  - 管理员 Session 认证                              │   │
    │  │  - 文档查看/编辑/删除                               │   │
    │  │  - API Key 管理                                     │   │
    │  │  - 系统统计面板                                     │   │
    │  └──────────────────────┬───────────────────────────┘   │
    │                         │                                │
    │  ┌──────────────────────┴───────────────────────────┐   │
    │  │              中央知识库引擎                        │   │
    │  │  Chroma (向量数据库) — 单 collection，全公司共享   │   │
    │  └──────────────────────┬───────────────────────────┘   │
    │                         │                                │
    │  ┌──────────────────────┴───────────────────────────┐   │
    │  │              基础设施层                            │   │
    │  │  Redis (锁 + 缓存 + 限流)                          │   │
    │  │  Ollama + bge-m3 (Embedding)                       │   │
    │  │  MinIO (对象存储)                                  │   │
    │  └──────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────┘
```

### 2.2 数据流

#### 三层数据架构

```
原始层（MinIO）          处理层（Gateway）           索引层（Chroma）
┌─────────────┐         ┌─────────────┐            ┌─────────────┐
│  source.md  │ ──────→ │  文本提取   │ ─────────→ │  chunk-0    │
│  (完整文档)  │         │  切片chunk  │            │  chunk-1    │
│             │         │  Embedding │            │  chunk-2    │
└─────────────┘         └─────────────┘            └─────────────┘
     ↑                                               ↓
     └────────── 编辑/更新时重新切片 ──────────────────┘
```

1. **检索流**：Agent → MCP Gateway → Chroma 向量检索 → 返回文档片段 + 关联的源文件路径（无锁，高并发）
2. **写入流**：Agent/MCP/Web → Markdown 内容 + `path` 目录路径 → 保存为 `documents/{path}/{doc_id}/source.md` 到 MinIO → 切片 → Embedding → 获取 Redis 写锁 → Chroma 写入（含 `path` 元数据）→ 释放锁
3. **管理流**：管理员登录 Web 后台 → 选择目标目录 → 上传/编辑 Markdown → 保存源文件到 MinIO → 重新切片 → 替换 Chroma 中的旧切片 → 完成
4. **删除流**：删除文档 → 从 MinIO 删除 `source.md` → 获取写锁 → 从 Chroma 删除所有关联切片 → 释放锁

---

## 3. 技术选型

### 3.1 组件选型

| 组件 | 选型 | 说明 |
|------|------|------|
| **MCP Gateway** | FastAPI + `mcp` SDK | 自建 SSE 服务器，完全可控认证和工具 |
| **向量数据库** | Chroma | 轻量、零配置、单 collection 共享 |
| **Embedding** | Ollama + bge-m3 | 本地运行，中文效果优秀 |
| **对象存储** | MinIO | 文档原文存储 |
| **缓存/锁/限流** | Redis | 分布式写锁 + API Key 缓存 + 限频 |
| **后台页面** | FastAPI + Jinja2 + HTMX + TailwindCSS | 纯服务端渲染，无构建步骤 |
| **反向代理** | Nginx | SSL 终止、路由分发、限流 |
| **部署** | Docker Compose | 单机一键部署 |

### 3.2 后台页面技术说明

选用 **Jinja2 + HTMX + TailwindCSS(CDN)** 而非 React/Vue：
- 零构建：不需要 npm、webpack、前端构建流程
- 开发快：Python 后端直接渲染 HTML，一套代码搞定
- 维护简单：不需要前端技术栈，后端团队可独立维护
- 体验不差：HTMX 让服务端渲染页面拥有局部刷新、表单无刷新提交等交互

> Phase 2 如需更复杂交互（如富文本编辑器、实时协作），可迁移到 React 前端。

---

## 4. 详细设计

### 4.1 MCP Gateway 设计

#### 4.1.1 暴露的工具集

| 工具名 | 描述 | 参数 | 权限 |
|--------|------|------|------|
| `search_knowledge` | 向量检索知识库 | `query`, `top_k=5`, `filter_tags=[]` | read |
| `add_document` | 添加新文档 | `title`, `content`, `path=""`, `tags=[]` | write |
| `update_document` | 更新已有文档 | `doc_id`, `title`, `content` | write |
| `delete_document` | 删除文档 | `doc_id` | write |
| `list_documents` | 列出文档（分页） | `tags=[]`, `limit=20`, `offset=0` | read |
| `import_markdown` | 导入 Markdown 文件 | `title`, `markdown_content`, `path=""`, `tags=[]` | write |

#### 4.1.2 API Key 生命周期管理

API Key 不再硬编码在配置文件中，而是通过管理后台动态创建、分发、过期和吊销。

##### 数据结构

```json
{
  "sk-abc123def456": {
    "key_prefix": "sk-abc123",
    "applicant": "张三",
    "applicant_note": "前端组，需要查询技术文档",
    "role": "user",
    "scope": ["read"],
    "rate_limit": 30,
    "status": "active",
    "duration": "7d",
    "created_at": "2026-05-21T10:00:00",
    "expires_at": "2026-05-28T10:00:00",
    "revoked_at": null,
    "revoked_by": null,
    "created_by": "admin",
    "last_used_at": "2026-05-21T15:30:00",
    "use_count": 42
  }
}
```

| 字段 | 说明 |
|------|------|
| `applicant` | 申请人姓名（必填） |
| `applicant_note` | 申请备注（用途说明） |
| `duration` | 有效期：`1d` / `3d` / `7d` / `30d` / `permanent` |
| `status` | `active` / `expired` / `revoked` |
| `expires_at` | 过期时间，根据 duration 自动计算 |
| `revoked_at` / `revoked_by` | 手动吊销记录 |
| `created_by` | 哪个管理员分发的 |
| `last_used_at` / `use_count` | 调用统计 |

##### 创建与分发流程

```
管理员登录后台
    ↓
进入 "API Key 管理 → 新建"
    ↓
填写：申请人、备注、权限(read/write)、有效期(1/3/7/30/长期)
    ↓
点击生成 → 系统创建 Key，计算 expires_at
    ↓
页面展示完整 Key（仅一次，不存储明文）
    ↓
管理员复制 Key 分发给申请人
    ↓
Key 存入 Redis（带过期时间 TTL）+ 持久化到 JSON
```

##### 认证流程（含过期检查）

```
Agent 请求 → Header: X-API-Key: sk-xxx
                ↓
          Gateway 从 Redis 查询 Key 信息
                ↓
          检查 status
                ↓
          ├─ revoked → 返回 403 Key 已吊销
          ├─ expired → 返回 403 Key 已过期
          └─ active  → 继续校验 scope + rate_limit
                ↓
          通过 → 执行工具
```

##### 过期处理策略

- **主动检查**：每次请求时实时检查 `expires_at`
- **Redis TTL**：Key 存入 Redis 时同时设置 TTL = 剩余有效期，Redis 自动清理过期 Key
- **定时任务**：每小时扫描持久化文件，将过期 Key 的 status 标记为 `expired`
- **提前提醒**：可选，Key 过期前 24 小时在管理后台标红提醒

##### 吊销机制

- 管理员在后台点击"吊销"，Key 的 status 立即变为 `revoked`
- 已连接的 Agent 下次请求时立即被拒绝
- 吊销记录永久保留（`revoked_at` + `revoked_by`），便于审计

### 4.2 写入排队锁（并发控制）

Chroma 不支持事务和并发写入保护，必须在应用层实现锁。

#### 4.2.1 Redis 分布式锁设计

```python
import redis
import uuid

WRITE_LOCK_KEY = "kb:write_lock"
WRITE_LOCK_TTL = 30  # 锁最大持有时间 30 秒

async def acquire_write_lock(redis_client) -> str | None:
    """获取写入锁，返回 lock_id 或 None"""
    lock_id = str(uuid.uuid4())
    acquired = await redis_client.set(
        WRITE_LOCK_KEY, lock_id, nx=True, ex=WRITE_LOCK_TTL
    )
    return lock_id if acquired else None

async def release_write_lock(redis_client, lock_id: str):
    """释放写入锁（Lua 脚本保证原子性）"""
    lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
    """
    await redis_client.eval(lua_script, 1, WRITE_LOCK_KEY, lock_id)
```

#### 4.2.2 写入流程（带锁）

```
写入请求到达
    ↓
校验 API Key + write 权限
    ↓
生成 Embedding（无锁，可并发）← 耗时操作在锁外
    ↓
尝试获取 Redis 写锁
    ↓
┌─ 获取成功 ─┐    ┌─ 获取失败 ─┐
│ 执行 Chroma │    │ 返回 423   │
│ add/update  │    │ Locked     │
│ /delete     │    │ 客户端重试 │
└──────┬──────┘    └────────────┘
       ↓
释放写锁
       ↓
返回结果
```

**锁的粒度**：全局单锁（`kb:write_lock`），因为 Chroma 单 collection 不支持细粒度锁。如未来替换为 PostgreSQL + pgvector，可升级为行级锁。

**锁超时处理**：
- 锁 TTL 30 秒，防止进程崩溃导致死锁
- 正常写入操作应在 1-3 秒内完成，远小于 TTL
- 后台管理页面的编辑操作同样走此锁

### 4.3 双层认证体系

| 认证层 | 用途 | 认证方式 | 适用端 |
|--------|------|---------|--------|
| **API Key** | AI Agent 调用 MCP 接口 | Header: `X-API-Key` | Cursor、Claude Desktop、Kimi Code |
| **Session** | 管理员登录 Web 后台 | Cookie Session + 用户名密码 | 浏览器访问管理后台 |

#### 4.3.1 管理员账号配置

独立配置文件 `admin_accounts.json`：

```json
{
  "admin": {
    "username": "admin",
    "password_hash": "$2b$12$...",
    "role": "super_admin",
    "created_at": "2026-05-21"
  }
}
```

- 密码使用 bcrypt 哈希存储
- 支持多管理员账号
- Session 有效期 24 小时，可配置

### 4.4 后台管理页面设计

#### 4.4.1 页面结构

| 路由 | 页面 | 功能 |
|------|------|------|
| `/admin/login` | 登录页 | 用户名/密码登录 |
| `/admin/dashboard` | 仪表盘 | 文档总量、今日调用量、活跃用户数 |
| `/admin/documents` | 文档管理 | 目录树 + 文档列表、搜索、查看、编辑、删除 |
| `/admin/documents/{id}/edit` | 编辑文档 | 表单编辑标题/内容/路径/标签 |
| `/admin/documents/upload` | 上传导入 | 支持保持目录结构上传 .md 文件 |
| `/admin/api-keys` | API Key 管理 | 创建、删除、修改权限、查看调用统计 |
| `/admin/settings` | 系统设置 | Embedding 模型配置、切片参数、锁超时 |

#### 4.4.2 核心功能说明

**文档管理**（目录树 + 文档列表双栏布局）：
- **目录树侧边栏**：左侧展示多级目录树结构，支持展开/折叠、点击切换目录、当前目录高亮。目录从所有文档的 `path` 字段聚合生成
- **列表页**：右侧表格展示当前目录下的文档，显示标题、路径、标签、切片数、源文件大小、创建时间，支持按标签筛选、关键词搜索
- **面包屑导航**：顶部显示当前目录路径 `技术 > API`，点击可快速跳转上级目录
- **新建目录**：在目录树右键菜单或顶部按钮新建子目录（仅逻辑目录，空目录不占用存储）
- **上传 Markdown**：拖拽或选择 .md 文件上传，支持选择目标目录，自动读取内容、保存源文件到 MinIO、切片、导入 Chroma。支持批量上传保持目录结构
- **查看页**：从 MinIO 读取原始 Markdown 内容展示，显示完整路径，支持复制全文、下载 .md 文件
- **编辑页**：Markdown 文本编辑器，可修改标题、内容、所在路径（移动目录）、标签，保存后覆盖 MinIO 源文件 → 重新切片 → 替换 Chroma 旧切片（全走写入锁保护）
- **删除**：确认弹窗，删除 MinIO 源文件 + Chroma 所有关联切片。删除后若目录为空，目录自动从树中移除

**API Key 管理**：
- **新建 Key**：表单填写申请人、备注、权限(scope)、有效期(1天/3天/7天/30天/长期)，生成后页面仅展示一次完整 Key
- **Key 列表**：表格展示所有 Key，按状态(active/expired/revoked)分标签页，支持搜索申请人
- **吊销**：一键吊销活跃 Key，立即生效，记录吊销人和时间
- **过期提醒**：即将过期（< 24h）的 Key 标红高亮
- **统计**：每个 Key 的调用次数、最后调用时间、创建人、过期倒计时

**仪表盘**：
- 文档总量、今日新增量
- MCP 接口今日调用总量、QPS 趋势
- 活跃 API Key 数量
- 最近写入操作日志

### 4.5 知识库数据模型

#### 4.5.1 MinIO 源文件存储（支持目录树）

原始 Markdown 文件保存在 MinIO 中，支持**多级目录树**组织：

```
bucket: kb-sources
path:   documents/{path}/{doc_id}/source.md
```

示例：
```
documents/
├── 技术/
│   ├── API/
│   │   ├── doc-uuid-1/
│   │   │   └── source.md      # API 设计规范
│   │   └── doc-uuid-2/
│   │       └── source.md      # 接口认证流程
│   └── 架构/
│       └── doc-uuid-3/
│           └── source.md      # 微服务拆分方案
├── 产品/
│   └── doc-uuid-4/
│       └── source.md          # 产品需求文档
└── 行政/
    └── doc-uuid-5/
        └── source.md          # 员工手册
```

- `{path}` 为文档所在目录路径，如 `技术/API`，支持多级嵌套
- 每个文档一个独立的 `{doc_id}/source.md` 目录，保证 doc_id 全局唯一
- `source.md` 为完整原始 Markdown 内容，不修改、不切片
- 空目录不会在 MinIO 中创建对象，目录结构通过文档的 `path` 字段隐式维护
- 支持未来扩展：同一 `doc_id` 目录可存 `source.pdf`、`attachments/` 等

#### 4.5.2 Chroma 切片存储

Chroma collection 中存储切片后的数据：

```python
{
    "ids": ["doc-uuid-1#chunk-0", "doc-uuid-1#chunk-1"],
    "documents": ["第一段切片文本...", "第二段切片文本..."],
    "metadatas": [{
        "doc_id": "doc-uuid-1",           # 关联的文档 ID
        "title": "文档标题",
        "path": "技术/API",               # 文档所在目录路径（支持多级）
        "tags": ["技术", "API"],
        "source_path": "documents/技术/API/doc-uuid-1/source.md",  # MinIO 完整路径
        "source_format": "markdown",
        "chunk_index": 0,                 # 第几个切片
        "total_chunks": 5,                # 该文档总共切片数
        "created_at": "2026-05-21T10:00:00",
        "updated_at": "2026-05-21T10:00:00",
        "created_by": "张三",
        "updated_by": "管理员"
    }, {
        "doc_id": "doc-uuid-1",
        "title": "文档标题",
        "path": "技术/API",
        "tags": ["技术", "API"],
        "source_path": "documents/技术/API/doc-uuid-1/source.md",
        "source_format": "markdown",
        "chunk_index": 1,
        "total_chunks": 5,
        ...
    }],
    "embeddings": [[0.1, ...], [0.2, ...]]   # bge-m3，1024 维
}
```

**关键设计**：
- `doc_id` 将同一文档的所有切片关联在一起
- `source_path` 指向 MinIO 中的原始文件，检索结果可回溯完整内容
- 更新文档时：先覆盖 MinIO 的 `source.md`，再删除旧切片，插入新切片
- 删除文档时：删除 MinIO 的 `source.md` + 删除 Chroma 中所有同 `doc_id` 的切片

### 4.6 文档导入与 Embedding 流水线

#### 4.6.1 从 Markdown 导入（MCP / Web 通用）

```
Markdown 内容到达（MCP import_markdown 或 Web 上传），携带 path 参数
    ↓
生成 doc_id（UUID）
    ↓
保存原始内容到 MinIO：documents/{path}/{doc_id}/source.md
    ↓
文本清洗（去除多余空白、特殊字符）
    ↓
切片（chunk_size=512, overlap=50）
    ↓
Ollama bge-m3 生成 Embedding（HTTP 调用，可并发）
    ↓
获取 Redis 写锁
    ↓
写入 Chroma：多个切片，共享同一个 doc_id
    ↓
释放写锁
    ↓
返回 doc_id
```

#### 4.6.2 从 Web 上传文件

```
管理员拖拽上传 .md 文件
    ↓
后端接收文件，读取内容
    ↓
走「从 Markdown 导入」流水线（同上）
    ↓
返回导入成功，跳转到文档列表
```

#### 4.6.3 更新文档（编辑后重新切片）

```
编辑请求到达（修改了 source.md 内容或 path）
    ↓
若 path 变更：删除旧路径的 source.md，保存到新路径
    ↓
保存新内容到 MinIO：documents/{new_path}/{doc_id}/source.md
    ↓
获取 Redis 写锁
    ↓
从 Chroma 删除该 doc_id 的所有旧切片
    ↓
对新内容重新切片 + Embedding
    ↓
写入 Chroma：新切片
    ↓
释放写锁
    ↓
返回成功
```

> **为什么更新要删除旧切片再插入，而不是原地修改？** Chroma 不支持 update 操作的切片数量变化（比如原文变长导致切片数增加），最可靠的方式是全量替换。

---

## 5. 实施路线图

### Phase 1：基础环境搭建（Day 1-2）

| 任务 | 内容 | 产出 |
|------|------|------|
| 1.1 | 准备公网服务器（4C8G，100GB SSD） | 服务器就绪 |
| 1.2 | 安装 Docker + Docker Compose | `docker compose` 可用 |
| 1.3 | 配置域名 + HTTPS 证书（Let's Encrypt） | `kb.company.com` 可访问 |
| 1.4 | 配置 Nginx 反向代理（分 `/sse` 和 `/admin` 路由） | `nginx.conf` 就绪 |

### Phase 2：基础设施部署（Day 2-3）

| 任务 | 内容 | 产出 |
|------|------|------|
| 2.1 | 部署 Chroma 容器 | Chroma `:8001` |
| 2.2 | 部署 Ollama + 拉取 bge-m3 | Embedding 服务就绪 |
| 2.3 | 部署 MinIO 容器 | MinIO `:9000` |
| 2.4 | 部署 Redis 容器 | Redis `:6379` |
| 2.5 | 初始化 Chroma collection + 测试数据 | 验证检索正常 |

### Phase 3：MCP Gateway 开发（Day 3-5）

| 任务 | 内容 | 产出 |
|------|------|------|
| 3.1 | 初始化 FastAPI 项目 + mcp SDK | 项目骨架 |
| 3.2 | 实现 API Key 认证中间件 | `auth.py` |
| 3.3 | 实现 5 个 MCP 工具 | `tools.py` |
| 3.4 | 实现 Redis 写入分布式锁 | `lock.py` |
| 3.5 | 集成 Chroma 客户端 | `knowledge_base.py` |
| 3.6 | 集成 Ollama Embedding | `embedding.py` |
| 3.7 | SSE 协议适配 + 错误处理 | `server.py` |
| 3.8 | 日志 + 监控埋点 | `logger.py` |

### Phase 4：后台管理页面开发（Day 5-7）

| 任务 | 内容 | 产出 |
|------|------|------|
| 4.1 | FastAPI Session 认证（管理员登录） | `admin_auth.py` |
| 4.2 | 仪表盘页面 | `dashboard.html` |
| 4.3 | 文档管理（列表/搜索/查看/编辑/删除） | `documents.html` |
| 4.4 | API Key 管理页面 | `api_keys.html` |
| 4.5 | 系统设置页面 | `settings.html` |
| 4.6 | HTMX 交互集成（无刷新表单提交） | 前端交互完成 |

### Phase 5：部署与集成测试（Day 7-8）

| 任务 | 内容 | 产出 |
|------|------|------|
| 5.1 | 编写 `docker-compose.yml`（全服务编排） | 一键启动 |
| 5.2 | 配置环境变量 + 密钥管理 | `.env` 文件 |
| 5.3 | 并发写入测试（多客户端同时写入） | 锁机制验证 |
| 5.4 | 端到端测试（检索/写入/权限/后台） | 测试报告 |
| 5.5 | 员工端接入文档 | `员工接入指南.md` |

### Phase 6：上线与运维（Day 8+）

| 任务 | 内容 | 产出 |
|------|------|------|
| 6.1 | Nginx 限流配置 + 防刷 | 安全加固 |
| 6.2 | 日志收集 + 异常告警 | 监控就绪 |
| 6.3 | 数据备份脚本（Chroma + MinIO） | 定时备份 |
| 6.4 | API Key 管理流程文档 | 运维手册 |

---

## 6. 项目目录结构

```
knowledge-base-management/
├── docker-compose.yml              # 全服务一键部署
├── .env.example                    # 环境变量模板
├── nginx/
│   └── nginx.conf                  # 反向代理（/sse + /admin 路由）
├── config/
│   ├── api_keys.json               # API Key 白名单
│   └── admin_accounts.json         # 管理员账号（bcrypt 哈希）
├── mcp-gateway/                    # 主服务
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── src/
│   │   ├── main.py                 # FastAPI 入口（MCP + Admin 路由）
│   │   ├── server.py               # MCP SSE 服务器
│   │   ├── auth.py                 # API Key 认证
│   │   ├── admin_auth.py           # 管理员 Session 认证
│   │   ├── tools.py                # MCP 工具定义
│   │   ├── lock.py                 # Redis 分布式写入锁
│   │   ├── knowledge_base.py       # Chroma 客户端（切片索引）
│   │   ├── source_store.py         # MinIO 源文件管理（支持目录树）
│   │   ├── directory_tree.py       # 目录树聚合与维护
│   │   ├── chunker.py              # Markdown 文本切片逻辑
│   │   ├── embedding.py            # Ollama Embedding
│   │   ├── config.py               # 配置管理
│   │   └── admin/                  # 后台管理模块
│   │       ├── routes.py           # 管理后台路由
│   │       ├── templates/          # Jinja2 模板
│   │       │   ├── base.html
│   │       │   ├── login.html
│   │       │   ├── dashboard.html
│   │       │   ├── documents.html          # 目录树 + 文档列表双栏
│   │       │   ├── document_view.html      # 查看原始 Markdown
│   │       │   ├── document_edit.html      # 编辑 Markdown（含路径选择）
│   │       │   ├── upload.html             # 上传 Markdown 文件（含目录选择）
│   │       │   ├── api_keys.html
│   │       │   └── settings.html
│   │       └── static/             # 静态资源（HTMX + Tailwind）
│   │           └── htmx.min.js
│   └── tests/
│       ├── test_tools.py
│       ├── test_lock.py
│       ├── test_admin.py
│       └── test_directory_tree.py
├── chroma/                         # Chroma 数据卷挂载点
└── minio/                          # MinIO 数据卷挂载点
```

---

## 7. 安全配置清单

| 检查项 | 措施 | 优先级 |
|--------|------|--------|
| HTTPS | Nginx 强制 SSL，HSTS 头 | P0 |
| API Key 传输 | 仅通过 HTTPS Header | P0 |
| API Key 存储 | 不记录日志，配置文件 600 权限 | P0 |
| 管理员密码 | bcrypt 哈希，禁止明文存储 | P0 |
| Session 安全 | HttpOnly + Secure Cookie，24h 过期 | P0 |
| 写入锁超时 | 锁 TTL 30 秒，防止死锁 | P0 |
| 限流 | Nginx `limit_req` + Gateway 令牌桶 | P1 |
| 后台访问控制 | `/admin` 路由可配 IP 白名单 | P1 |
| 日志脱敏 | 过滤 API Key 和密码 | P1 |
| 备份加密 | Chroma/MinIO 备份加密存储 | P2 |

---

## 8. 员工接入指南（概要）

### Cursor / Windsurf 配置
```json
{
  "mcpServers": {
    "knowledge-base-management": {
      "url": "https://kb.company.com/sse",
      "headers": {
        "X-API-Key": "sk-user-xxx"
      }
    }
  }
}
```

### Claude Desktop / Kimi Code 配置
```json
{
  "mcpServers": {
    "knowledge-base-management": {
      "command": "npx",
      "args": ["-y", "mcp-proxy", "https://kb.company.com/sse?api_key=sk-user-xxx"]
    }
  }
}
```

### 管理后台访问
浏览器访问 `https://kb.company.com/admin`，使用管理员账号登录。

---

## 9. 风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|---------|
| 写入锁竞争 | 多用户同时写入排队等待 | Redis 锁 + 客户端重试，锁持有时间 < 3s |
| 锁未释放（进程崩溃） | 死锁，所有写入阻塞 | 锁 TTL 30 秒自动释放 |
| 服务器单点故障 | 知识库不可用 | 定期快照备份 |
| API Key 泄露 | 未授权访问 | 支持 Key 轮换，后台可一键吊销；过期自动失效 |
| API Key 过期未续 | 员工无法使用 | 管理后台过期提醒，提前通知管理员续期 |
| Embedding 模型故障 | 新文档无法索引 | Ollama 自动重启，后台告警 |
| 文档量爆炸 | 检索性能下降 | Chroma 支持百万级，超限迁移 pgvector |
| 员工客户端不支持 MCP | 无法接入 | 提前确认版本，提供 REST API 备选方案 |
| MinIO 源文件丢失 | 无法查看/编辑原始文档 | Chroma 切片仍可检索，但需从备份恢复源文件 |
| 切片与源文件不一致 | 检索结果和原始内容对不上 | 更新/编辑时强制全量替换切片，不走增量更新 |
| 大文件上传失败 | Markdown 导入中断 | Nginx 调整 `client_max_body_size`，后台限制单文件 10MB |

---

## 10. 预算估算

| 项目 | 月成本 | 说明 |
|------|--------|------|
| 云服务器（4C8G） | ￥200-400 | 阿里云/腾讯云轻量应用服务器 |
| 域名 + HTTPS | ￥50-100/年 | Let's Encrypt 免费 |
| 总成本 | ￥200-400/月 | 无额外软件授权费用 |

---

## 11. 交付物清单

- [ ] `docker-compose.yml` — 全服务一键部署
- [ ] MCP Gateway 完整源代码（含写入锁）
- [ ] 后台管理页面完整源码（Jinja2 + HTMX）
- [ ] `nginx.conf` — 反向代理配置（含路由分发）
- [ ] `.env.example` — 环境变量模板
- [ ] `api_keys.json` — API Key 配置模板
- [ ] `admin_accounts.json` — 管理员账号配置模板
- [ ] `员工接入指南.md` — 各客户端配置教程
- [ ] `运维手册.md` — 备份/监控/告警/锁故障排查
- [ ] 测试用例集 — 并发写入/锁/后台/端到端

---

*方案版本：v1.3*
*更新日期：2026-05-21*
*新增内容：写入排队锁、管理员账号、后台管理页面、API Key 生命周期管理、Markdown 上传导入、源文件与切片分离存储*
*预计工期：9-10 个工作日*
