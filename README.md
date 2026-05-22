# 企业中央知识库 + MCP Gateway

基于 FastAPI + Chroma + Ollama 构建的企业级中央知识库系统，通过 MCP (Model Context Protocol) Gateway 向 AI Agent 暴露检索与写入能力，配套 Web 管理后台供管理员维护知识库内容。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **中央知识库** | 单库共享，全公司共用，支持向量检索 |
| **MCP 协议** | 通过 SSE/HTTP 方式向 Cursor、Claude、Kimi Code 等 Agent 暴露工具 |
| **写入排队锁** | Redis 分布式锁保护 Chroma 并发写入，避免数据冲突 |
| **API Key 生命周期** | 支持 1/3/7/30 天或长期有效期，过期自动作废，可手动吊销 |
| **源文件管理** | Markdown 源文件保存在 MinIO，切片数据与源文件分离 |
| **后台管理** | Jinja2 + HTMX + TailwindCSS，零构建步骤，支持文档 CRUD、API Key 管理 |
| **双层认证** | API Key（Agent 调用）+ 管理员 Session（Web 后台） |
| **Docker 化部署** | 单服务器 `docker compose up -d` 一键启动 |

---

## 架构

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

---

## 快速开始

### 环境要求

- Docker + Docker Compose
- 服务器内存：建议 8GB+（Ollama  embedding 模型需要）
- 磁盘：根据知识库规模，建议 50GB+

### 1. 克隆并进入项目

```bash
git clone <your-repo-url>
cd knowledge-base-management
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，修改以下关键配置：
# - MINIO_SECRET_KEY: MinIO 管理员密码
# - SESSION_SECRET: 至少 32 位随机字符串
# - OLLAMA_MODEL: 默认 bge-m3（中文效果优秀）
```

### 3. 启动服务

```bash
docker compose up -d
```

首次启动会拉取镜像并初始化服务，大约需要 2-3 分钟。

### 4. 初始化管理员账号

默认管理员账号已内置在 `config/admin_accounts.json`：

- **用户名**: `admin`
- **密码**: `admin123`

> ⚠️ 生产环境务必修改密码！使用以下命令生成新密码哈希：
> ```bash
> docker compose exec mcp-gateway python -c "import bcrypt; print(bcrypt.hashpw('你的新密码'.encode(), bcrypt.gensalt()).decode())"
> ```

### 5. 访问服务

| 端点 | 地址 | 说明 |
|------|------|------|
| 后台管理 | `http://服务器IP:8000/admin` | 管理员登录入口 |
| 健康检查 | `http://服务器IP:8000/health` | 服务状态 |
| 运行指标 | `http://服务器IP:8000/metrics` | 运行时长、文档数等 |
| MCP SSE | `http://服务器IP:8000/sse` | Agent 连接端点 |
| REST API | `http://服务器IP:8000/api/*` | 直接调用 API |

---

## 配置 Nginx + HTTPS（生产环境）

将 `nginx/nginx.conf` 复制到服务器，修改 `server_name` 和 SSL 证书路径：

```bash
# 安装证书（以 Let's Encrypt 为例）
sudo certbot --nginx -d kb.yourcompany.com

# 复制配置
sudo cp nginx/nginx.conf /etc/nginx/conf.d/kb.conf
sudo nginx -t && sudo systemctl reload nginx
```

---

## MCP 工具说明

AI Agent 通过 MCP 协议可调用以下工具：

| 工具名 | 描述 | 权限 |
|--------|------|------|
| `search_knowledge` | 向量检索知识库 | read |
| `add_document` | 添加新文档 | write |
| `update_document` | 更新已有文档 | write |
| `delete_document` | 删除文档 | write |
| `list_documents` | 列出文档（分页） | read |
| `import_markdown` | 导入 Markdown 内容 | write |
| `list_directories` | 列出目录树结构 | read |

### Cursor 配置示例

在 Cursor Settings → MCP 中添加：

```json
{
  "mcpServers": {
    "knowledge-base-management": {
      "url": "https://kb.yourcompany.com/sse",
      "headers": {
        "X-API-Key": "sk-your-api-key-here"
      }
    }
  }
}
```

---

## API Key 管理

1. 管理员登录后台 `/admin`
2. 进入 "API Key 管理 → 新建"
3. 填写申请人、备注、权限(read/write)、有效期
4. 生成后页面展示完整 Key（仅一次，务必复制保存）
5. 将 Key 分发给员工，员工配置到各自 Agent 中

### Key 有效期

| 选项 | 说明 |
|------|------|
| `1d` | 1 天后过期 |
| `3d` | 3 天后过期 |
| `7d` | 7 天后过期 |
| `30d` | 30 天后过期 |
| `permanent` | 长期有效 |

---

## 数据流

### 检索流（高并发，无锁）

```
Agent → MCP Gateway → Embedding (Ollama) → Chroma 向量检索 → 返回结果
```

### 写入流（分布式锁保护）

```
Agent/Web → Markdown 内容 → MinIO 保存源文件 → 切片 → Embedding → 获取 Redis 写锁 → Chroma 写入 → 释放锁
```

---

## 目录结构

```
.
├── docker-compose.yml          # Docker 编排
├── .env.example                # 环境变量模板
├── nginx/
│   └── nginx.conf              # Nginx 反向代理配置
├── config/
│   ├── api_keys.json           # API Key 持久化存储
│   └── admin_accounts.json     # 管理员账号
├── mcp-gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── main.py             # FastAPI 入口
│       ├── server.py           # MCP 服务器
│       ├── tools.py            # MCP 工具实现
│       ├── knowledge_base.py   # Chroma 封装
│       ├── source_store.py     # MinIO 封装
│       ├── embedding.py        # Ollama Embedding
│       ├── chunker.py          # Markdown 切片
│       ├── auth.py             # API Key 认证
│       ├── admin_auth.py       # 管理员认证
│       ├── lock.py             # Redis 分布式锁
│       ├── directory_tree.py   # 目录树工具
│       ├── config.py           # 配置管理
│       ├── models.py           # Pydantic 模型
│       └── admin/              # 后台管理
│           ├── routes.py
│           └── templates/
│               ├── base.html
│               ├── login.html
│               ├── dashboard.html
│               ├── documents.html
│               ├── document_view.html
│               ├── document_edit.html
│               ├── upload.html
│               ├── api_keys.html
│               ├── api_key_create.html
│               └── settings.html
```

---

## 监控与维护

### 健康检查

```bash
curl http://localhost:8000/health
```

返回示例：
```json
{
  "status": "ok",
  "timestamp": "2026-05-21T12:00:00+00:00",
  "services": {
    "redis": "ok",
    "chroma": "ok",
    "ollama": "ok",
    "minio": "ok"
  }
}
```

### 运行指标

```bash
curl http://localhost:8000/metrics
```

返回示例：
```json
{
  "app": "Knowledge Base Management",
  "version": "1.0.0",
  "uptime_seconds": 3600.5,
  "uptime_human": "0d 1h 0m",
  "documents_total": 152,
  "api_keys_total": 12,
  "timestamp": "2026-05-21T13:00:00+00:00"
}
```

### 查看日志

```bash
# 所有服务
docker compose logs -f

# 仅 Gateway
docker compose logs -f mcp-gateway

# 仅 Ollama
docker compose logs -f ollama
```

### 备份

```bash
# 备份 Chroma 数据
docker compose exec chroma tar czf /tmp/chroma-backup.tar.gz /chroma/chroma
docker compose cp chroma:/tmp/chroma-backup.tar.gz ./backups/

# 备份 MinIO 数据
docker compose exec minio mc mirror /data ./backups/minio

# 备份 Redis
docker compose exec redis redis-cli BGSAVE
```

---

## 常见问题

### Q: Ollama 首次启动模型下载很慢？

A: 首次启动时会自动下载 `bge-m3` 模型（约 1GB），取决于网络速度。可以预先下载：

```bash
docker compose exec ollama ollama pull bge-m3
```

### Q: 写入时返回 423 Locked？

A: 表示有其他写入操作正在进行，Chroma 不支持并发写入。Agent 端会自动重试，或稍后手动重试。

### Q: 如何重置知识库？

A: **⚠️ 危险操作，会清空所有数据！**

```bash
docker compose down -v
docker compose up -d
```

### Q: 如何升级版本？

```bash
docker compose pull
docker compose up -d
```

---

## 技术栈

| 组件 | 选型 | 版本 |
|------|------|------|
| Gateway | FastAPI + mcp SDK | 0.115+ / 1.6.0+ |
| 向量数据库 | Chroma | 0.6.4 |
| Embedding | Ollama + bge-m3 | 0.6.0 |
| 对象存储 | MinIO | 2025-01 |
| 缓存/锁 | Redis | 7-alpine |
| 后台 UI | Jinja2 + HTMX + TailwindCSS(CDN) | - |
| 反向代理 | Nginx | - |
| 部署 | Docker Compose | 3.9+ |

---

## License

MIT
